# pragma pylint: disable=missing-docstring, invalid-name, too-many-instance-attributes
"""
SupertrendSD — SuperTrend + Supply/Demand multi-timeframe strategy.

Konsep (dari analisa chart TradingView):
  • SuperTrend 4H  → filter arah (bull → hanya LONG, bear → hanya SHORT)
  • SuperTrend 15m → trigger entry saat flip searah dengan 4H
  • Volume spike   → konfirmasi reversal (replika marker "R" volume-tinggi)
  • Supply/Demand  → swing high/low terdekat sebagai konteks S/R
"""
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
from pandas import DataFrame

import talib.abstract as ta
from freqtrade.strategy import (
    IStrategy, informative, DecimalParameter, IntParameter,
)
from freqtrade.persistence import Trade


def supertrend(dataframe: DataFrame, period: int = 10, multiplier: float = 3.0):
    """
    Return (st_line, direction).
    direction: +1 = bullish (garis hijau di bawah harga),
               -1 = bearish (garis merah di atas harga).
    """
    df  = dataframe
    atr = ta.ATR(df, timeperiod=period)
    hl2 = (df["high"] + df["low"]) / 2

    upper = np.asarray(hl2 + multiplier * atr, dtype="float64").copy()
    lower = np.asarray(hl2 - multiplier * atr, dtype="float64").copy()
    close = np.asarray(df["close"], dtype="float64")
    n     = len(df)

    in_up = np.ones(n, dtype=bool)
    for i in range(1, n):
        if close[i] > upper[i - 1]:
            in_up[i] = True
        elif close[i] < lower[i - 1]:
            in_up[i] = False
        else:
            in_up[i] = in_up[i - 1]
            # Band hanya boleh mengetat searah tren (trailing)
            if in_up[i] and lower[i] < lower[i - 1]:
                lower[i] = lower[i - 1]
            if (not in_up[i]) and upper[i] > upper[i - 1]:
                upper[i] = upper[i - 1]

    st_line   = np.where(in_up, lower, upper)
    direction = np.where(in_up, 1, -1)
    return (
        pd.Series(st_line,   index=df.index),
        pd.Series(direction, index=df.index),
    )


class SupertrendSD(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "15m"
    can_short = True

    # ROI & stoploss (dasar — bisa di-hyperopt nanti)
    minimal_roi = {
        "0":   0.05,
        "30":  0.03,
        "60":  0.02,
        "120": 0.0,
    }
    stoploss = -0.08

    trailing_stop = True
    trailing_stop_positive = 0.02
    trailing_stop_positive_offset = 0.03
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    startup_candle_count = 200

    # ── Hyperparameters ───────────────────────────────────────
    st_period    = IntParameter(7, 20, default=10, space="buy")
    st_mult      = DecimalParameter(1.5, 4.0, default=3.0, decimals=1, space="buy")
    vol_factor   = DecimalParameter(1.0, 2.5, default=1.3, decimals=1, space="buy")
    leverage_num = IntParameter(1, 10, default=5, space="buy")

    # ── Higher timeframe: SuperTrend 4H (filter arah) ─────────
    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        _, st_dir = supertrend(dataframe, self.st_period.value, self.st_mult.value)
        dataframe["st_dir"] = st_dir
        return dataframe

    # ── Base timeframe: 15m ───────────────────────────────────
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        st_line, st_dir = supertrend(dataframe, self.st_period.value, self.st_mult.value)
        dataframe["st_line"] = st_line
        dataframe["st_dir"]  = st_dir
        # Flip = titik reversal
        dataframe["st_flip_up"]   = (st_dir == 1)  & (st_dir.shift(1) == -1)
        dataframe["st_flip_down"] = (st_dir == -1) & (st_dir.shift(1) == 1)

        # Volume spike (konfirmasi "R")
        dataframe["vol_ma"]    = dataframe["volume"].rolling(20).mean()
        dataframe["vol_spike"] = dataframe["volume"] > (self.vol_factor.value * dataframe["vol_ma"])

        # Supply/Demand konteks — swing high/low 20 candle
        dataframe["demand"] = dataframe["low"].rolling(20).min()
        dataframe["supply"] = dataframe["high"].rolling(20).max()

        # Filter tren dasar
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"]    = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    # ── Entry ─────────────────────────────────────────────────
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_cond = (
            dataframe["st_flip_up"]                 # 15m flip bullish
            & (dataframe["st_dir_4h"] == 1)         # 4H tren bullish
            & dataframe["vol_spike"]                # volume konfirmasi
            & (dataframe["rsi"] < 70)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[long_cond, ["enter_long", "enter_tag"]] = (1, "st_flip_long")

        short_cond = (
            dataframe["st_flip_down"]               # 15m flip bearish
            & (dataframe["st_dir_4h"] == -1)        # 4H tren bearish
            & dataframe["vol_spike"]                # volume konfirmasi
            & (dataframe["rsi"] > 30)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[short_cond, ["enter_short", "enter_tag"]] = (1, "st_flip_short")
        return dataframe

    # ── Exit ──────────────────────────────────────────────────
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Keluar saat SuperTrend 15m flip berlawanan
        dataframe.loc[dataframe["st_flip_down"], "exit_long"]  = 1
        dataframe.loc[dataframe["st_flip_up"],   "exit_short"] = 1
        return dataframe

    # ── Leverage ──────────────────────────────────────────────
    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: Optional[str],
                 side: str, **kwargs) -> float:
        return float(min(self.leverage_num.value, max_leverage))
