"""
Custom dashboard di atas Freqtrade REST API.

Freqtrade tetap jadi mesin utuh (mutusin + eksekusi). Dashboard ini cuma
lapisan tampilan: poll REST API freqtrade tiap beberapa detik, lalu push
snapshot ke browser lewat WebSocket sendiri. Kredensial freqtrade disimpan
di server (nggak bocor ke browser).

Env:
  FT_API_URL   default http://127.0.0.1:8080
  FT_USERNAME  default botbot2
  FT_PASSWORD  default $FREQTRADE__API_SERVER__PASSWORD
  DASH_PORT    default 8100
"""
import asyncio
import json
import logging
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dashboard")

FT_API_URL  = os.getenv("FT_API_URL", "http://127.0.0.1:8080").rstrip("/")
FT_USERNAME = os.getenv("FT_USERNAME", "botbot2")
FT_PASSWORD = os.getenv("FT_PASSWORD", os.getenv("FREQTRADE__API_SERVER__PASSWORD", ""))
POLL_SECS   = float(os.getenv("DASH_POLL_SECS", "3"))

_auth  = httpx.BasicAuth(FT_USERNAME, FT_PASSWORD)
_last_snapshot: dict = {"connected": False}

# Mode switching (nfi = sabar/proven, active = agresif)
MODE_FILE = Path(__file__).parent.parent / "user_data" / "active_mode"
MODES = {
    "nfi":    {"label": "NFI (Sabar)",   "strategy": "NFIX7Verbose"},
    "active": {"label": "Active (Agresif)", "strategy": "SupertrendSD"},
}


def read_mode() -> str:
    try:
        m = MODE_FILE.read_text().strip()
        return m if m in MODES else "nfi"
    except Exception:
        return "nfi"


# ── Browser WebSocket manager ─────────────────────────────────
class Manager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        msg  = json.dumps(data, default=str)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = Manager()


# ── Freqtrade REST helpers ────────────────────────────────────
async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None):
    r = await client.get(f"{FT_API_URL}/api/v1/{path}", params=params, auth=_auth, timeout=8)
    r.raise_for_status()
    return r.json()


async def build_snapshot(client: httpx.AsyncClient) -> dict:
    """Fetch everything the dashboard needs in one concurrent batch."""
    status, profit, count, balance, whitelist, cfg, logs = await asyncio.gather(
        _get(client, "status"),
        _get(client, "profit"),
        _get(client, "count"),
        _get(client, "balance"),
        _get(client, "whitelist"),
        _get(client, "show_config"),
        _get(client, "logs", {"limit": 30}),
        return_exceptions=True,
    )

    def ok(x):
        return x if not isinstance(x, Exception) else None

    status, profit, count = ok(status) or [], ok(profit) or {}, ok(count) or {}
    balance, whitelist    = ok(balance) or {}, ok(whitelist) or {}
    cfg, logs             = ok(cfg) or {}, ok(logs) or {}

    trades = []
    for t in status:
        trades.append({
            "pair":       t.get("pair"),
            "is_short":   t.get("is_short", False),
            "leverage":   t.get("leverage", 1),
            "open_rate":  t.get("open_rate"),
            "current_rate": t.get("current_rate"),
            "amount":     t.get("amount"),
            "stake":      t.get("stake_amount"),
            "profit_pct": (t.get("profit_ratio") or 0) * 100,
            "profit_abs": t.get("profit_abs") or 0,
            "entries":    t.get("nr_of_successful_entries", t.get("number_of_entries", 1)),
            "enter_tag":  t.get("enter_tag") or "",
            "open_date":  t.get("open_date"),
        })

    return {
        "connected": True,
        "state":     cfg.get("state", "unknown"),
        "dry_run":   cfg.get("dry_run", True),
        "mode":      read_mode(),
        "strategy":  cfg.get("strategy"),
        "trading_mode": cfg.get("trading_mode"),
        "timeframe": cfg.get("timeframe"),
        "balance":   round(balance.get("total", 0) or 0, 2),
        "open_count": count.get("current", 0),
        "max_open":  count.get("max", 0),
        "whitelist": whitelist.get("whitelist", []),
        "profit": {
            "total_abs":   round(profit.get("profit_all_coin", 0) or 0, 4),
            "total_pct":   round(profit.get("profit_all_percent", 0) or 0, 2),
            "closed_abs":  round(profit.get("profit_closed_coin", 0) or 0, 4),
            "trade_count": profit.get("trade_count", 0),
            "closed":      profit.get("closed_trade_count", 0),
            "wins":        profit.get("winning_trades", 0),
            "losses":      profit.get("losing_trades", 0),
        },
        "trades": trades,
        "logs": [
            {"time": row[0], "level": row[3], "msg": row[4]}
            for row in logs.get("logs", []) if len(row) >= 5
        ],
    }


async def poller():
    global _last_snapshot
    async with httpx.AsyncClient() as client:
        while True:
            try:
                snap = await build_snapshot(client)
            except Exception as e:
                snap = {"connected": False, "error": str(e)}
            _last_snapshot = snap
            await manager.broadcast(snap)
            await asyncio.sleep(POLL_SECS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    asyncio.create_task(poller())
    log.info("Dashboard → freqtrade %s (user=%s)", FT_API_URL, FT_USERNAME)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return HTMLResponse(Path(__file__).parent.joinpath("static/index.html").read_text(encoding="utf-8"))


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ft_connected": _last_snapshot.get("connected", False)}


@app.get("/api/mode")
async def get_mode():
    return {"mode": read_mode(), "modes": MODES}


@app.post("/api/mode/{mode}")
async def set_mode(mode: str):
    if mode not in MODES:
        return {"ok": False, "error": f"mode tidak valid: {mode}"}
    try:
        MODE_FILE.write_text(mode + "\n")
    except Exception as e:
        return {"ok": False, "error": f"gagal tulis mode: {e}"}
    # restart freqtrade agar strategy baru aktif
    try:
        r = subprocess.run(
            ["sudo", "systemctl", "restart", "freqtrade"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return {"ok": True, "mode": mode, "restarted": False,
                    "note": "mode tersimpan; restart gagal (jalankan manual): " + (r.stderr or "").strip()[:200]}
    except Exception as e:
        return {"ok": True, "mode": mode, "restarted": False,
                "note": f"mode tersimpan; restart tidak bisa dari sini ({e})"}
    log.info("Mode switched → %s (freqtrade restarting)", mode)
    return {"ok": True, "mode": mode, "restarted": True,
            "strategy": MODES[mode]["strategy"]}


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await manager.connect(sock)
    await sock.send_text(json.dumps(_last_snapshot, default=str))
    try:
        while True:
            await sock.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(sock)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("DASH_PORT", "8100")), reload=False)
