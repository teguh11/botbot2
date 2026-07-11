"""
Binance Crypto Signal Analyzer — Multi-Timeframe Edition
=========================================================
Timeframe entry (15m) dikonfirmasi dengan timeframe trend (4h).
SL/TP dihitung berbasis ATR — lebih adaptif dari persentase tetap.

pip install requests pandas numpy
"""

import requests
import pandas as pd

BASE_URL    = "https://api.binance.com"
FUTURES_URL = "https://fapi.binance.com"


# ---------------------------------------------------------------
# Data
# ---------------------------------------------------------------
def get_klines(symbol="BTCUSDT", interval="1h", limit=300, futures=False):
    url    = f"{FUTURES_URL}/fapi/v1/klines" if futures else f"{BASE_URL}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    if not r.ok and not futures:
        # fallback ke futures endpoint untuk simbol yang tidak ada di spot
        r = requests.get(f"{FUTURES_URL}/fapi/v1/klines", params=params, timeout=10)
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def get_funding_rate(symbol="BTCUSDT"):
    try:
        r = requests.get(
            f"{FUTURES_URL}/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 1}, timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return float(data[0]["fundingRate"]) if data else None
    except Exception:
        return None


# ---------------------------------------------------------------
# Indikator
# ---------------------------------------------------------------
def _rsi(series, period=14):
    delta    = series.diff()
    avg_gain = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd(series, fast=12, slow=26, signal=9):
    line   = series.ewm(span=fast, adjust=False).mean() - series.ewm(span=slow, adjust=False).mean()
    signal = line.ewm(span=signal, adjust=False).mean()
    return line, signal


def _ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def _bb(series, period=20, mult=2):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    return mid + mult * std, mid, mid - mult * std


def _atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ---------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------
def _compute(symbol, interval, limit=300, futures=False):
    df    = get_klines(symbol, interval, limit, futures=futures)
    close = df["close"]

    df["rsi"]                = _rsi(close)
    df["macd"], df["msig"]   = _macd(close)
    df["ema50"]              = _ema(close, 50)
    df["ema200"]             = _ema(close, 200)
    df["bb_u"], _, df["bb_l"] = _bb(close)
    df["vol_ma"]             = df["volume"].rolling(20).mean()
    df["atr"]                = _atr(df)

    last = df.iloc[-1]
    prev = df.iloc[-2]
    signals, score = [], 0

    # RSI
    if last["rsi"] < 30:
        signals.append(f"RSI {last['rsi']:.1f} → OVERSOLD (potensi rebound)"); score += 2
    elif last["rsi"] > 70:
        signals.append(f"RSI {last['rsi']:.1f} → OVERBOUGHT (potensi koreksi)"); score -= 2
    else:
        signals.append(f"RSI {last['rsi']:.1f} → netral")

    # MACD
    bullish_cross = prev["macd"] < prev["msig"] and last["macd"] > last["msig"]
    bearish_cross = prev["macd"] > prev["msig"] and last["macd"] < last["msig"]
    if bullish_cross:
        signals.append("MACD → BULLISH crossover ✓"); score += 2
    elif bearish_cross:
        signals.append("MACD → BEARISH crossover ✓"); score -= 2
    elif last["macd"] > last["msig"]:
        signals.append("MACD → di atas signal (bullish momentum)"); score += 1
    else:
        signals.append("MACD → di bawah signal (bearish momentum)"); score -= 1

    # EMA trend
    if last["ema50"] > last["ema200"]:
        signals.append("EMA50 > EMA200 → uptrend"); score += 1
    else:
        signals.append("EMA50 < EMA200 → downtrend"); score -= 1

    # Bollinger
    if last["close"] <= last["bb_l"]:
        signals.append("Harga di lower BB → area oversold"); score += 1
    elif last["close"] >= last["bb_u"]:
        signals.append("Harga di upper BB → area overbought"); score -= 1
    else:
        signals.append("Harga di dalam BB → normal")

    # Volume
    if last["volume"] > 1.5 * last["vol_ma"]:
        signals.append(f"Volume {last['volume']/last['vol_ma']:.1f}x rata-rata → konfirmasi kuat")
        score += 1 if score > 0 else (-1 if score < 0 else 0)
    else:
        signals.append("Volume normal/rendah → sinyal kurang terkonfirmasi")

    # Funding rate
    fr = get_funding_rate(symbol)
    if fr is not None:
        pct = fr * 100
        if fr > 0.0005:
            signals.append(f"Funding {pct:.4f}% → long crowded, rawan flush"); score -= 1
        elif fr < -0.0005:
            signals.append(f"Funding {pct:.4f}% → short crowded, rawan squeeze"); score += 1
        else:
            signals.append(f"Funding {pct:.4f}% → netral")

    return {
        "score":     score,
        "signals":   signals,
        "price":     float(last["close"]),
        "open_time": last["open_time"],
        "atr":       float(last["atr"]),
        "rsi":       float(last["rsi"]),
        "ema_trend": "BULL" if last["ema50"] > last["ema200"] else "BEAR",
    }


# ---------------------------------------------------------------
# Public: multi-timeframe signal (dipakai bot)
# ---------------------------------------------------------------
def get_mtf_signal(
    symbol,
    interval_entry="15m",
    interval_trend="4h",
    sl_atr_mult=1.5,
    tp1_rr=1.5,
    tp2_rr=3.0,
    min_score=3,
):
    """
    Analisis dua timeframe.
    4H → bias tren, 15m → timing entry.
    Return dict sinyal lengkap, atau None jika tidak ada setup.
    """
    trend  = _compute(symbol, interval_trend, limit=250, futures=True)
    entry  = _compute(symbol, interval_entry, limit=300, futures=True)

    s4h  = trend["score"]
    s15m = entry["score"]

    # Konfirmasi: 15m harus searah dengan bias 4H
    direction = None
    if s15m >= min_score and s4h >= 0:
        direction = "LONG"
    elif s15m <= -min_score and s4h <= 0:
        direction = "SHORT"

    if direction is None:
        return None

    price   = entry["price"]
    atr_val = entry["atr"]
    sl_dist = sl_atr_mult * atr_val

    if direction == "LONG":
        sl  = price - sl_dist
        tp1 = price + sl_dist * tp1_rr
        tp2 = price + sl_dist * tp2_rr
    else:
        sl  = price + sl_dist
        tp1 = price - sl_dist * tp1_rr
        tp2 = price - sl_dist * tp2_rr

    combined = abs(s15m) + abs(s4h)
    confidence = "TINGGI" if combined >= 8 else ("SEDANG" if combined >= 5 else "RENDAH")

    return {
        "symbol":      symbol,
        "direction":   direction,
        "price":       price,
        "sl":          sl,
        "tp1":         tp1,
        "tp2":         tp2,
        "sl_pct":      (sl  - price) / price * 100,
        "tp1_pct":     (tp1 - price) / price * 100,
        "tp2_pct":     (tp2 - price) / price * 100,
        "rr1":         tp1_rr,
        "rr2":         tp2_rr,
        "score_15m":   s15m,
        "score_4h":    s4h,
        "signals_15m": entry["signals"],
        "signals_4h":  trend["signals"],
        "confidence":  confidence,
        "atr":         atr_val,
        "ema_trend_4h": trend["ema_trend"],
    }


# ---------------------------------------------------------------
# Public: single-timeframe score (backward-compat untuk futures_bot)
# ---------------------------------------------------------------
def get_score(symbol="BTCUSDT", interval="1h"):
    r = _compute(symbol, interval)
    return r["score"], r["signals"], r["price"]


# ---------------------------------------------------------------
# CLI standalone
# ---------------------------------------------------------------
def analyze(symbol="BTCUSDT", interval="1h"):
    r = _compute(symbol, interval)
    score, price = r["score"], r["price"]

    if score >= 3:    verdict = "🟢 BUY (bullish kuat)"
    elif score >= 1:  verdict = "🟢 Lean bullish"
    elif score <= -3: verdict = "🔴 SELL (bearish kuat)"
    elif score <= -1: verdict = "🔴 Lean bearish"
    else:             verdict = "⚪ NETRAL"

    print("=" * 60)
    print(f"  {symbol}  |  {interval}  |  harga: {price:,.4f}  |  ATR: {r['atr']:.4f}")
    print(f"  candle: {r['open_time']}  |  EMA trend: {r['ema_trend']}")
    print("=" * 60)
    for s in r["signals"]:
        print(f"  • {s}")
    print("-" * 60)
    print(f"  SKOR: {score:+d}   →   {verdict}")
    print("=" * 60)


if __name__ == "__main__":
    import sys
    sym = sys.argv[1].upper() if len(sys.argv) > 1 else "BTCUSDT"
    tf  = sys.argv[2]         if len(sys.argv) > 2 else "1h"
    analyze(sym, tf)
