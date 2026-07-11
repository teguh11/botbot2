"""
Binance Futures Testnet Bot — Multi-Symbol Scanner
====================================================
Scan top-volume USDT pairs setiap 15m candle close.
Analisis: 15m entry + 4H trend confirmation.
Output: kartu sinyal detail (entry, SL, TP, reasoning).
Eksekusi otomatis jika AUTO_TRADE=true di .env.

python3 futures_bot.py
"""

import time
import math
import hmac
import hashlib
import logging
import requests
from typing import Optional
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config
from crypto_signal import get_mtf_signal

TESTNET_URL = "https://testnet.binancefuture.com"
FUTURES_URL = "https://fapi.binance.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------
# API helper (Testnet — untuk order)
# ---------------------------------------------------------------
def _sign(params: dict) -> str:
    return hmac.new(
        config.API_SECRET.encode(),
        urlencode(params).encode(),
        hashlib.sha256,
    ).hexdigest()


def api(method: str, path: str, params: dict = None, signed: bool = False):
    params  = dict(params or {})
    headers = {"X-MBX-APIKEY": config.API_KEY}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = _sign(params)
    url = TESTNET_URL + path
    kwargs = dict(headers=headers, timeout=10)
    if method == "get":
        r = requests.get(url, params=params, **kwargs)
    elif method == "post":
        r = requests.post(url, data=params, **kwargs)
    elif method == "delete":
        r = requests.delete(url, params=params, **kwargs)
    else:
        raise ValueError(method)
    if not r.ok:
        log.error("API %s %s → %s | %s", method.upper(), path, r.status_code, r.text[:200])
        r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------
# Akun & posisi
# ---------------------------------------------------------------
def set_leverage(symbol):
    try:
        api("post", "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": config.LEVERAGE}, signed=True)
    except Exception as e:
        log.warning("set_leverage %s: %s", symbol, e)


def get_balance() -> float:
    data = api("get", "/fapi/v2/account", signed=True)
    for a in data.get("assets", []):
        if a["asset"] == "USDT":
            return float(a["availableBalance"])
    return 0.0


def get_position(symbol) -> Optional[dict]:
    data = api("get", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
    for p in data:
        if p["symbol"] == symbol:
            amt = float(p["positionAmt"])
            if amt != 0:
                return {
                    "side":  "LONG" if amt > 0 else "SHORT",
                    "qty":   abs(amt),
                    "entry": float(p["entryPrice"]),
                    "pnl":   float(p["unRealizedProfit"]),
                }
    return None


def get_symbol_precision(symbol) -> dict:
    data = api("get", "/fapi/v1/exchangeInfo")
    for s in data["symbols"]:
        if s["symbol"] == symbol:
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    step = f["stepSize"]
                    prec = len(step.rstrip("0").split(".")[-1]) if "." in step else 0
                    return {"step": float(step), "precision": prec}
    return {"step": 0.001, "precision": 3}


def calc_qty(price: float, prec_info: dict) -> float:
    raw  = (config.MARGIN_USDT * config.LEVERAGE) / price
    qty  = math.floor(raw / prec_info["step"]) * prec_info["step"]
    return round(qty, prec_info["precision"])


# ---------------------------------------------------------------
# Order eksekusi
# ---------------------------------------------------------------
def place_market(symbol, side, qty):
    return api("post", "/fapi/v1/order", {
        "symbol": symbol, "side": side, "type": "MARKET", "quantity": qty,
    }, signed=True)


def place_sl_tp(symbol, direction, entry):
    order_side = "SELL" if direction == "LONG" else "BUY"
    sl_price   = entry * (1 - config.SL_ATR_MULT * 0.01) if direction == "LONG" \
                 else entry * (1 + config.SL_ATR_MULT * 0.01)

    # Gunakan harga dari sinyal (lebih akurat)
    # SL & TP sudah dihitung di sinyal, di sini pakai stopPrice langsung
    for order_type, price_key, label in [
        ("STOP_MARKET",        "sl",  "SL"),
        ("TAKE_PROFIT_MARKET", "tp1", "TP1"),
    ]:
        try:
            api("post", "/fapi/v1/order", {
                "symbol": symbol, "side": order_side,
                "type": order_type, "stopPrice": round(sl_price, 2),
                "closePosition": "true",
            }, signed=True)
        except Exception as e:
            log.error("Gagal pasang %s %s: %s", label, symbol, e)


def open_position(sig: dict):
    symbol = sig["symbol"]
    set_leverage(symbol)
    prec = get_symbol_precision(symbol)
    qty  = calc_qty(sig["price"], prec)
    if qty <= 0:
        log.warning("Qty 0 untuk %s — skip", symbol); return

    side = "BUY" if sig["direction"] == "LONG" else "SELL"
    log.info("Eksekusi %s %s  qty=%.4f", sig["direction"], symbol, qty)
    order = place_market(symbol, side, qty)
    entry = float(order.get("avgPrice") or sig["price"]) or sig["price"]

    # Pasang SL dengan STOP_MARKET
    order_side = "SELL" if sig["direction"] == "LONG" else "BUY"
    sl_price   = round(sig["sl"], 4)
    tp1_price  = round(sig["tp1"], 4)
    for otype, sp, label in [
        ("STOP_MARKET",        sl_price,  "SL"),
        ("TAKE_PROFIT_MARKET", tp1_price, "TP1"),
    ]:
        try:
            api("post", "/fapi/v1/order", {
                "symbol": symbol, "side": order_side,
                "type": otype, "stopPrice": sp, "closePosition": "true",
            }, signed=True)
            log.info("  %s dipasang @ %.4f", label, sp)
        except Exception as e:
            log.error("  Gagal %s: %s", label, e)


def close_position(symbol, pos):
    side = "SELL" if pos["side"] == "LONG" else "BUY"
    try:
        api("delete", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)
    except Exception:
        pass
    place_market(symbol, side, pos["qty"])
    log.info("Posisi %s %s ditutup  PnL: %.2f USDT", pos["side"], symbol, pos["pnl"])


# ---------------------------------------------------------------
# Scanner: ambil semua USDT perpetual, sort by volume
# ---------------------------------------------------------------
def get_top_symbols() -> list:
    r = requests.get(f"{FUTURES_URL}/fapi/v1/ticker/24hr", timeout=15)
    r.raise_for_status()
    tickers = [
        t for t in r.json()
        if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
    ]
    tickers.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    symbols = [t["symbol"] for t in tickers[: config.SCAN_LIMIT]]
    log.info("Scan %d simbol (top %d by volume)", len(symbols), config.SCAN_LIMIT)
    return symbols


def scan_symbol(symbol) -> Optional[dict]:
    try:
        return get_mtf_signal(
            symbol,
            interval_entry=config.INTERVAL_ENTRY,
            interval_trend=config.INTERVAL_TREND,
            sl_atr_mult=config.SL_ATR_MULT,
            tp1_rr=config.TP1_RR,
            tp2_rr=config.TP2_RR,
            min_score=config.MIN_SCORE,
        )
    except Exception as e:
        log.debug("Skip %s: %s", symbol, e)
        return None


def scan_all(symbols: list) -> list:
    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(scan_symbol, s): s for s in symbols}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)
    return sorted(results, key=lambda x: abs(x["score_15m"]) + abs(x["score_4h"]), reverse=True)


# ---------------------------------------------------------------
# Kartu sinyal — output ke terminal
# ---------------------------------------------------------------
def print_signal_card(sig: dict):
    d   = sig["direction"]
    sym = sig["symbol"]
    emoji = "🟢" if d == "LONG" else "🔴"
    conf_color = {"TINGGI": "★★★", "SEDANG": "★★☆", "RENDAH": "★☆☆"}[sig["confidence"]]
    W = 58

    def row(left, right=""):
        content = f"  {left:<30}{right}"
        return f"║{content:<{W}}║"

    sep = f"╠{'═' * W}╣"

    lines = [
        f"╔{'═' * W}╗",
        f"║  {emoji} {d}  —  {sym:<20}[{sig['confidence']} {conf_color}]{'':<2}║",
        sep,
        row(f"Entry    :  {sig['price']:>14,.4f}"),
        row(f"Stop Loss:  {sig['sl']:>14,.4f}", f"  ({sig['sl_pct']:+.2f}%)"),
        row(f"TP 1     :  {sig['tp1']:>14,.4f}", f"  ({sig['tp1_pct']:+.2f}%)  R:R 1:{sig['rr1']}"),
        row(f"TP 2     :  {sig['tp2']:>14,.4f}", f"  ({sig['tp2_pct']:+.2f}%)  R:R 1:{sig['rr2']}"),
        row(f"ATR 15m  :  {sig['atr']:>14,.4f}"),
        sep,
    ]
    s4h_label = f"4H Trend  (skor {sig['score_4h']:+d})  EMA: {sig['ema_trend_4h']}"
    lines.append(f"║  {s4h_label:<{W-2}}║")
    for s in sig["signals_4h"][:3]:
        lines.append(f"║    • {s:<{W-6}}║")

    s15m_label = f"15m Entry  (skor {sig['score_15m']:+d})"
    lines += [
        f"║  {s15m_label:<{W-2}}║",
    ]
    for s in sig["signals_15m"][:4]:
        lines.append(f"║    • {s:<{W-6}}║")

    lines += [
        sep,
        f"║  {'Leverage: ' + str(config.LEVERAGE) + 'x   Margin/trade: ' + str(config.MARGIN_USDT) + ' USDT':<{W-2}}║",
        f"║  {'AUTO_TRADE: ' + ('ON ✓' if config.AUTO_TRADE else 'OFF (sinyal saja)'):<{W-2}}║",
        f"╚{'═' * W}╝",
    ]

    print("\n".join(lines))


# ---------------------------------------------------------------
# Timing
# ---------------------------------------------------------------
def seconds_to_candle_close() -> float:
    units  = {"m": 60, "h": 3600}
    iv     = config.INTERVAL_ENTRY
    period = int(iv[:-1]) * units[iv[-1]]
    now    = time.time()
    return (math.floor(now / period) + 1) * period + 5 - now


# ---------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------
def run():
    log.info("=" * 60)
    log.info("  FUTURES BOT TESTNET  —  MULTI-SYMBOL SCANNER")
    log.info("  Entry: %s  |  Trend: %s  |  Scan top %d coins",
             config.INTERVAL_ENTRY, config.INTERVAL_TREND, config.SCAN_LIMIT)
    log.info("  Leverage: %dx  Margin: %s USDT  AUTO_TRADE: %s",
             config.LEVERAGE, config.MARGIN_USDT, config.AUTO_TRADE)
    log.info("=" * 60)

    while True:
        try:
            wait = seconds_to_candle_close()
            log.info("Menunggu candle %s close dalam %.1f menit...",
                     config.INTERVAL_ENTRY, wait / 60)
            time.sleep(wait)

            ts      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            symbols = get_top_symbols()

            log.info("[%s] Memulai scan %d simbol...", ts, len(symbols))
            signals = scan_all(symbols)

            if not signals:
                log.info("Tidak ada sinyal valid saat ini.")
            else:
                log.info("Ditemukan %d sinyal:", len(signals))
                print()
                for sig in signals:
                    print_signal_card(sig)
                    print()

            # Auto-trade: hanya eksekusi sinyal terbaik (skor tertinggi)
            if config.AUTO_TRADE and signals:
                best = signals[0]
                pos  = get_position(best["symbol"])

                if pos:
                    should_close = (
                        (pos["side"] == "LONG"  and best["score_15m"] <= -(config.MIN_SCORE - 1)) or
                        (pos["side"] == "SHORT" and best["score_15m"] >=  (config.MIN_SCORE - 1))
                    )
                    if should_close:
                        close_position(best["symbol"], pos)
                        pos = None
                    else:
                        log.info("Pertahankan posisi %s %s (PnL %.2f USDT)",
                                 pos["side"], best["symbol"], pos["pnl"])

                if not pos:
                    open_position(best)

        except KeyboardInterrupt:
            log.info("Bot dihentikan (Ctrl+C).")
            break
        except Exception as e:
            log.error("Error: %s", e, exc_info=True)
            log.info("Retry dalam 60 detik...")
            time.sleep(60)


if __name__ == "__main__":
    run()
