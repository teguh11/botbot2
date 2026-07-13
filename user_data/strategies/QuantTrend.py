"""
QuantTrend — Managed-Futures / CTA style (time-series momentum + volatility targeting).

Meniru pendekatan hedge fund trend-following (AHL, Winton, Man Group):
  1. Time-series momentum : long tren naik, short tren turun (per aset).
  2. Trend filter         : harga vs EMA panjang + ADX (hindari sideways).
  3. Volatility targeting : ukuran posisi ∝ target_vol / realized_vol —
                            aset volatil dapat posisi kecil, aset kalem
                            posisi besar → kontribusi risiko tiap posisi setara.
                            (INI elemen inti gaya hedge fund.)
  4. Risk exit            : ATR chandelier trailing stop + exit saat tren flip.

Timeframe 4H: trend-following lebih bersih di TF tinggi, lebih sedikit
trade & fee (fewer quality trades — pelajaran dari eksperimen sebelumnya).

⚠️ BELUM di-backtest di environment ini (Binance keblok saat dibuat).
   Backtest dulu sebelum dipercaya. Semua dry-run.
"""
from datetime import datetime
from typing import Optional

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from freqtrade.persistence import Trade


class QuantTrend(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "4h"
    can_short = True

    # Biarkan tren jalan (no fixed ROI); exit lewat trend-flip + ATR stop.
    minimal_roi = {"0": 100.0}
    stoploss = -0.25              # backstop lebar; stop asli via custom_stoploss
    use_custom_stoploss = True
    trailing_stop = False
    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 260

    # ── Hyperparameters ───────────────────────────────────────
    mom_lookback  = IntParameter(20, 60, default=30, space="buy")
    ema_slow      = IntParameter(120, 240, default=200, space="buy")
    adx_min       = IntParameter(15, 35, default=20, space="buy")
    atr_period    = IntParameter(10, 30, default=14, space="buy")
    atr_stop_mult = DecimalParameter(2.0, 5.0, default=3.0, decimals=1, space="sell")
    target_vol    = DecimalParameter(0.20, 0.80, default=0.40, decimals=2, space="buy")
    lev_used      = IntParameter(1, 5, default=1, space="buy")

    _BARS_PER_YEAR = 6 * 365  # 4h → 6 bar/hari

    # ── Protections (kurangi whipsaw & over-trading) ──────────
    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 4},
            {"method": "StoplossGuard", "lookback_period_candles": 24,
             "trade_limit": 3, "stop_duration_candles": 12, "only_per_pair": True},
        ]

    # ── Indicators ────────────────────────────────────────────
    def populate_indicators(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["ema_slow"] = ta.EMA(df, timeperiod=int(self.ema_slow.value))
        df["mom"]      = df["close"] / df["close"].shift(int(self.mom_lookback.value)) - 1.0
        df["atr"]      = ta.ATR(df, timeperiod=int(self.atr_period.value))
        df["adx"]      = ta.ADX(df, timeperiod=14)
        logret = np.log(df["close"] / df["close"].shift(1))
        df["realized_vol"] = logret.rolling(30).std() * np.sqrt(self._BARS_PER_YEAR)

        adx = int(self.adx_min.value)
        df["trend_up"] = (df["close"] > df["ema_slow"]) & (df["mom"] > 0) & (df["adx"] > adx)
        df["trend_dn"] = (df["close"] < df["ema_slow"]) & (df["mom"] < 0) & (df["adx"] > adx)
        return df

    # ── Entry: hanya saat tren BARU muncul (crossover), sekali ──
    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        prev_up = df["trend_up"].shift(1).fillna(False)
        prev_dn = df["trend_dn"].shift(1).fillna(False)
        new_up  = df["trend_up"] & (~prev_up) & (df["volume"] > 0)
        new_dn  = df["trend_dn"] & (~prev_dn) & (df["volume"] > 0)
        df.loc[new_up, ["enter_long",  "enter_tag"]] = (1, "tsmom_long")
        df.loc[new_dn, ["enter_short", "enter_tag"]] = (1, "tsmom_short")
        return df

    # ── Exit: HANYA saat tren balik penuh ke arah lawan ───────
    #    (exit utama tetap via ATR chandelier trailing stop)
    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df.loc[df["trend_dn"], "exit_long"]  = 1
        df.loc[df["trend_up"], "exit_short"] = 1
        return df

    # ── Volatility targeting (posisi ∝ target_vol / realized_vol) ──
    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: Optional[float], max_stake: float,
                            leverage: float, entry_tag: Optional[str], side: str, **kwargs) -> float:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or len(df) == 0:
            return proposed_stake
        rv = float(df["realized_vol"].iloc[-1])
        if not rv or np.isnan(rv) or rv <= 0:
            return proposed_stake
        scale = float(self.target_vol.value) / rv
        scale = max(0.25, min(scale, 2.0))          # batasi 0.25x–2x
        stake = proposed_stake * scale
        if min_stake:
            stake = max(stake, min_stake)
        return min(stake, max_stake)

    # ── ATR chandelier trailing stop ──────────────────────────
    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> Optional[float]:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or len(df) == 0:
            return None
        atr = float(df["atr"].iloc[-1])
        if not atr or np.isnan(atr) or current_rate <= 0:
            return None
        stop_dist = self.atr_stop_mult.value * atr / current_rate
        return -abs(stop_dist)                       # trailing: freqtrade hanya mengetat

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: Optional[str],
                 side: str, **kwargs) -> float:
        return float(min(self.lev_used.value, max_leverage))
