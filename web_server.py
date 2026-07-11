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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

import config
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
    except Exception as e:
        await _add_log("ERROR", str(e))
    finally:
        _is_scanning = False


async def _auto_scanner():
    while True:
        wait = max(_next_scan_epoch() - time.time(), 5)
        await asyncio.sleep(wait)
        await run_scan()


def _fetch_latest_candle(symbol: str) -> Optional[dict]:
    try:
        df  = get_klines(symbol, config.INTERVAL_ENTRY, 2)
        row = df.iloc[-1]
        return {
            "time":  int(row["open_time"].timestamp()),
            "open":  float(row["open"]),
            "high":  float(row["high"]),
            "low":   float(row["low"]),
            "close": float(row["close"]),
        }
    except Exception:
        return None


async def _candle_updater():
    while True:
        await asyncio.sleep(5)
        if not _last_signals or not manager.active:
            continue
        loop    = asyncio.get_running_loop()
        symbols = [s["symbol"] for s in _last_signals[:20]]
        tasks   = [loop.run_in_executor(executor, _fetch_latest_candle, sym) for sym in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, candle in zip(symbols, results):
            if isinstance(candle, dict):
                await manager.broadcast({"type": "candle_update", "symbol": sym, "candle": candle})


async def _heartbeat():
    while True:
        await asyncio.sleep(30)
        await manager.broadcast({"type": "heartbeat", "uptime": _get_uptime()})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    asyncio.create_task(_auto_scanner())
    asyncio.create_task(_heartbeat())
    asyncio.create_task(_candle_updater())
    log.info("Dashboard siap di http://localhost:8000")
    yield


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------
# Routes
# ---------------------------------------------------------------
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
    await manager.send_to(ws, {
        "type": "init",
        "config": {
            "interval_entry": config.INTERVAL_ENTRY,
            "interval_trend": config.INTERVAL_TREND,
            "leverage":       config.LEVERAGE,
            "margin_usdt":    config.MARGIN_USDT,
            "auto_trade":     config.AUTO_TRADE,
            "scan_limit":     config.SCAN_LIMIT,
            "min_score":      config.MIN_SCORE,
        },
        "signals":   _last_signals,
        "last_scan": _last_scan_time.isoformat() if _last_scan_time else None,
        "next_scan": _next_scan_epoch(),
        "uptime":    _get_uptime(),
        "logs":      _log_buffer[-30:],
    })
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "scan_now":
                asyncio.create_task(run_scan())
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ---------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_server:app", host="0.0.0.0", port=8000, reload=False)
