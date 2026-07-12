"""
Web Dashboard Server — Crypto Signal Bot
Jalankan: python3 web_server.py
Buka:     http://localhost:8000
"""

import asyncio
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

import config
from binance_trade import BinanceTrader
from crypto_signal import get_mtf_signal, get_klines, run_backtest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

executor = ThreadPoolExecutor(max_workers=10)
FUTURES_URL = "https://fapi.binance.com"

# State
_start_time:     float             = time.time()
_last_signals:   list              = []
_last_scan_time: Optional[datetime] = None
_is_scanning:    bool               = False
_log_buffer:     list              = []
_active_orders:  list              = []   # orders placed this session


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m     = s // 60
    if d:   return f"{d}d {h}h {m}m"
    if h:   return f"{h}h {m}m"
    return f"{m}m"


def _get_uptime() -> dict:
    bot_up = _fmt_uptime(time.time() - _start_time)
    sys_up = "N/A"
    try:
        with open("/proc/uptime") as f:
            sys_up = _fmt_uptime(float(f.read().split()[0]))
    except Exception:
        pass
    return {"bot": bot_up, "sys": sys_up}


# ---------------------------------------------------------------
# WebSocket manager
# ---------------------------------------------------------------
class _Manager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        msg  = json.dumps(data, default=str)
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send_to(self, ws: WebSocket, data: dict):
        try:
            await ws.send_text(json.dumps(data, default=str))
        except Exception:
            self.disconnect(ws)


manager = _Manager()


# ---------------------------------------------------------------
# Scanner helpers (blocking, run in thread pool)
# ---------------------------------------------------------------
def _get_top_symbols() -> list:
    r = requests.get(f"{FUTURES_URL}/fapi/v1/ticker/24hr", timeout=15)
    r.raise_for_status()
    tickers = [t for t in r.json()
               if t["symbol"].endswith("USDT") and "_" not in t["symbol"]]
    tickers.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in tickers[:config.SCAN_LIMIT]]


def _scan_one(symbol) -> Optional[dict]:
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
    except Exception:
        return None


def _next_scan_epoch() -> float:
    iv     = config.INTERVAL_ENTRY
    units  = {"m": 60, "h": 3600}
    period = int(iv[:-1]) * units[iv[-1]]
    return (math.floor(time.time() / period) + 1) * period + 5


# ---------------------------------------------------------------
# Async scan orchestrator
# ---------------------------------------------------------------
async def _add_log(level: str, message: str):
    entry = {
        "type":      "log",
        "level":     level,
        "message":   message,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
    }
    _log_buffer.append(entry)
    if len(_log_buffer) > 100:
        _log_buffer.pop(0)
    await manager.broadcast(entry)


async def run_scan():
    global _last_signals, _last_scan_time, _is_scanning
    if _is_scanning:
        return
    _is_scanning = True
    try:
        loop    = asyncio.get_event_loop()
        symbols = await loop.run_in_executor(executor, _get_top_symbols)

        await manager.broadcast({"type": "scanning", "count": len(symbols)})
        await _add_log("INFO", "Scanning {} symbols...".format(len(symbols)))

        tasks   = [loop.run_in_executor(executor, _scan_one, s) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals = sorted(
            [r for r in results if isinstance(r, dict)],
            key=lambda x: abs(x["score_15m"]) + abs(x["score_4h"]),
            reverse=True,
        )
        _last_signals   = signals
        _last_scan_time = datetime.now()

        await manager.broadcast({
            "type":       "signals",
            "timestamp":  _last_scan_time.isoformat(),
            "scan_count": len(symbols),
            "next_scan":  _next_scan_epoch(),
            "signals":    signals,
        })
        await _add_log(
            "INFO",
            "Scan selesai — {} sinyal dari {} coin".format(len(signals), len(symbols))
        )
        if config.AUTO_TRADE and signals:
            await _execute_orders(signals)
    except Exception as e:
        await _add_log("ERROR", str(e))
    finally:
        _is_scanning = False


async def _execute_orders(signals: list):
    """Place orders for TINGGI signals if AUTO_TRADE is enabled."""
    global _active_orders
    loop   = asyncio.get_running_loop()
    trader = BinanceTrader(config.API_KEY, config.API_SECRET, testnet=config.TESTNET)
    try:
        balance = await loop.run_in_executor(executor, trader.get_balance)
        margin  = round(balance * config.CAPITAL_PCT, 4)
        await _add_log("INFO",
            "Balance: {:.2f} USDT | Margin/trade: {:.2f} USDT ({:.1f}%)".format(
                balance, margin, config.CAPITAL_PCT * 100
            )
        )
        # Fetch open symbols once to avoid duplicate positions
        open_syms = await loop.run_in_executor(executor, trader.open_symbols)
    except Exception as e:
        await _add_log("ERROR", "Trader init: {}".format(e))
        return

    already = {o["symbol"] for o in _active_orders if o["status"] == "OPEN"}

    for sig in signals:
        if sig.get("confidence") != "TINGGI":
            continue
        symbol = sig["symbol"]
        if symbol in open_syms or symbol in already:
            continue

        def _place(s=sig, m=margin):
            return trader.place_signal_order(
                symbol=s["symbol"], direction=s["direction"],
                entry=s["price"], sl=s["sl"], tp1=s["tp1"], tp2=s["tp2"],
                usdt_margin=m, leverage=config.LEVERAGE,
            )

        try:
            order = await loop.run_in_executor(executor, _place)
            _active_orders.append(order)
            if len(_active_orders) > 500:
                _active_orders = _active_orders[-500:]
            await manager.broadcast({"type": "order_placed", "order": order})
            await _add_log(
                "INFO",
                "Order ✓ {} {} @ {} | margin={} USDT | id={}".format(
                    symbol, sig["direction"], sig["price"],
                    margin, order["order_id"]
                )
            )
        except Exception as e:
            await _add_log("WARN", "Order skip {}: {}".format(symbol, e))


async def _pnl_updater():
    """Broadcast live balance + PnL every 5 s to all connected clients."""
    trader = BinanceTrader(config.API_KEY, config.API_SECRET, testnet=config.TESTNET)
    loop   = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(5)
        if not manager.active:
            continue
        try:
            snap = await loop.run_in_executor(executor, trader.get_account_snapshot)
            await manager.broadcast({
                "type":      "positions_update",
                "positions": snap["positions"],
                "balance":   snap["balance"],
            })
        except Exception as e:
            log.debug("pnl_updater: %r", e)


async def _auto_scanner():
    while True:
        wait = max(_next_scan_epoch() - time.time(), 5)
        await asyncio.sleep(wait)
        await run_scan()


async def _binance_ws_updater():
    """Connect to Binance Futures WebSocket and forward kline updates to clients."""
    BINANCE_WS = "wss://fstream.binance.com/stream?streams={streams}"
    while True:
        if not _last_signals:
            await asyncio.sleep(5)
            continue

        symbols = [s["symbol"] for s in _last_signals[:20]]
        iv      = config.INTERVAL_ENTRY
        streams = "/".join(f"{sym.lower()}@kline_{iv}" for sym in symbols)
        url     = BINANCE_WS.format(streams=streams)

        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as bws:
                log.info("Binance WS connected: %d streams (%s)", len(symbols), iv)
                async for raw in bws:
                    msg  = json.loads(raw)
                    data = msg.get("data", {})
                    if data.get("e") != "kline":
                        continue
                    k = data["k"]
                    await manager.broadcast({
                        "type":   "candle_update",
                        "symbol": data["s"],
                        "candle": {
                            "time":  k["t"] // 1000,
                            "open":  float(k["o"]),
                            "high":  float(k["h"]),
                            "low":   float(k["l"]),
                            "close": float(k["c"]),
                        },
                    })
                    # Reconnect when scan produces a new symbol set
                    if [s["symbol"] for s in _last_signals[:20]] != symbols:
                        break
        except Exception as e:
            log.warning("Binance WS: %r — reconnect in 5s", e)
            await asyncio.sleep(5)


async def _heartbeat():
    while True:
        await asyncio.sleep(30)
        await manager.broadcast({"type": "heartbeat", "uptime": _get_uptime()})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    asyncio.create_task(_auto_scanner())
    asyncio.create_task(_heartbeat())
    asyncio.create_task(_binance_ws_updater())
    asyncio.create_task(_pnl_updater())
    log.info("Dashboard siap di http://localhost:8000")
    yield


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------
# Routes
# ---------------------------------------------------------------
@app.get("/api/orders")
async def api_orders():
    return _active_orders


@app.get("/api/positions")
async def api_positions():
    loop   = asyncio.get_running_loop()
    trader = BinanceTrader(config.API_KEY, config.API_SECRET, testnet=config.TESTNET)
    return await loop.run_in_executor(executor, trader.get_open_positions)


@app.get("/api/balance")
async def api_balance():
    loop   = asyncio.get_running_loop()
    trader = BinanceTrader(config.API_KEY, config.API_SECRET, testnet=config.TESTNET)
    bal    = await loop.run_in_executor(executor, trader.get_balance)
    return {"balance": bal, "testnet": config.TESTNET, "capital_pct": config.CAPITAL_PCT}


@app.get("/api/backtest/{symbol}")
async def api_backtest(symbol: str, days: int = 30, interval: str = "15m"):
    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        executor,
        lambda: run_backtest(
            symbol.upper(), interval=interval, days=days,
            min_score=config.MIN_SCORE,
            sl_atr_mult=config.SL_ATR_MULT,
            tp_rr=config.TP1_RR,
        ),
    )
    return result


@app.get("/api/candles/{symbol}")
async def api_candles(symbol: str, interval: str = "15m", limit: int = 120):
    loop = asyncio.get_running_loop()
    df   = await loop.run_in_executor(executor, lambda: get_klines(symbol.upper(), interval, limit))
    return [
        {
            "time":  int(row["open_time"].timestamp()),
            "open":  float(row["open"]),
            "high":  float(row["high"]),
            "low":   float(row["low"]),
            "close": float(row["close"]),
        }
        for _, row in df.iterrows()
    ]


@app.get("/")
async def index():
    return HTMLResponse(Path("static/index.html").read_text(encoding="utf-8"))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    loop = asyncio.get_running_loop()

    await manager.send_to(ws, {
        "type": "init",
        "config": {
            "interval_entry": config.INTERVAL_ENTRY,
            "interval_trend": config.INTERVAL_TREND,
            "leverage":       config.LEVERAGE,
            "margin_usdt":    config.MARGIN_USDT,
            "auto_trade":     config.AUTO_TRADE,
            "testnet":        config.TESTNET,
            "capital_pct":    config.CAPITAL_PCT,
            "scan_limit":     config.SCAN_LIMIT,
            "min_score":      config.MIN_SCORE,
        },
        "signals":   _last_signals,
        "orders":    _active_orders,
        "last_scan": _last_scan_time.isoformat() if _last_scan_time else None,
        "next_scan": _next_scan_epoch(),
        "uptime":    _get_uptime(),
        "logs":      _log_buffer[-30:],
    })

    # Push initial balance + positions as soon as client connects
    async def _push_snap():
        try:
            trader = BinanceTrader(config.API_KEY, config.API_SECRET, testnet=config.TESTNET)
            snap   = await loop.run_in_executor(executor, trader.get_account_snapshot)
            await manager.send_to(ws, {
                "type":      "positions_update",
                "positions": snap["positions"],
                "balance":   snap["balance"],
            })
        except Exception:
            pass
    asyncio.create_task(_push_snap())

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            t   = msg.get("type")

            if t == "scan_now":
                asyncio.create_task(run_scan())

            elif t == "get_candles":
                sym = msg.get("symbol", "BTCUSDT").upper()
                iv  = msg.get("interval", config.INTERVAL_ENTRY)
                lim = int(msg.get("limit", 120))

                async def _candles(s=sym, i=iv, n=lim):
                    try:
                        df = await loop.run_in_executor(executor, lambda: get_klines(s, i, n))
                        candles = [
                            {"time": int(r["open_time"].timestamp()), "open": float(r["open"]),
                             "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"])}
                            for _, r in df.iterrows()
                        ]
                        await manager.send_to(ws, {"type": "candles", "symbol": s, "candles": candles})
                    except Exception as e:
                        await manager.send_to(ws, {"type": "candles", "symbol": s, "candles": [], "error": str(e)})
                asyncio.create_task(_candles())

            elif t == "get_backtest":
                sym = msg.get("symbol", "BTCUSDT").upper()

                async def _bt(s=sym):
                    try:
                        result = await loop.run_in_executor(executor, lambda: run_backtest(
                            s, interval=config.INTERVAL_ENTRY, days=30,
                            min_score=config.MIN_SCORE,
                            sl_atr_mult=config.SL_ATR_MULT,
                            tp_rr=config.TP1_RR,
                        ))
                        await manager.send_to(ws, {"type": "backtest", "symbol": s, "data": result})
                    except Exception as e:
                        await manager.send_to(ws, {"type": "backtest", "symbol": s, "data": None, "error": str(e)})
                asyncio.create_task(_bt())

            elif t == "get_balance":
                async def _bal():
                    try:
                        trader = BinanceTrader(config.API_KEY, config.API_SECRET, testnet=config.TESTNET)
                        snap   = await loop.run_in_executor(executor, trader.get_account_snapshot)
                        await manager.send_to(ws, {
                            "type":      "positions_update",
                            "positions": snap["positions"],
                            "balance":   snap["balance"],
                        })
                    except Exception:
                        pass
                asyncio.create_task(_bal())

    except WebSocketDisconnect:
        manager.disconnect(ws)


# ---------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_server:app", host="0.0.0.0", port=8000, reload=False)
