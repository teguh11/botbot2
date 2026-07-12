"""
Binance Futures order execution.
Handles signed requests, order placement (entry + SL + TP), and position queries.
"""
import hashlib
import hmac
import math
import time
import urllib.parse
from typing import Optional

import requests

TESTNET_URL = "https://testnet.binancefuture.com"
LIVE_URL    = "https://fapi.binance.com"

_symbol_cache: dict = {}  # shared across instances, cleared only on restart


def _step_precision(step: str) -> int:
    """'0.001' → 3, '1' → 0, '0.10' → 1"""
    s = step.rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


class BinanceTrader:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.base       = TESTNET_URL if testnet else LIVE_URL

    # ── Signing & HTTP ────────────────────────────────────────
    def _sign(self, params: dict) -> str:
        qs = urllib.parse.urlencode(params)
        return hmac.new(
            self.api_secret.encode(),
            qs.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _hdrs(self) -> dict:
        return {"X-MBX-APIKEY": self.api_key}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        p = dict(params or {})
        p["timestamp"]  = int(time.time() * 1000)
        p["recvWindow"] = 5000
        p["signature"]  = self._sign(p)
        r = requests.get(f"{self.base}{path}", params=p, headers=self._hdrs(), timeout=10)
        if not r.ok:
            raise RuntimeError(f"GET {path} {r.status_code}: {r.text[:300]}")
        return r.json()

    def _post(self, path: str, params: dict) -> dict:
        p = dict(params)
        p["timestamp"]  = int(time.time() * 1000)
        p["recvWindow"] = 5000
        p["signature"]  = self._sign(p)
        r = requests.post(f"{self.base}{path}", params=p, headers=self._hdrs(), timeout=10)
        if not r.ok:
            raise RuntimeError(f"POST {path} {r.status_code}: {r.text[:300]}")
        return r.json()

    # ── Exchange info & precision ─────────────────────────────
    def _load_info(self):
        if _symbol_cache:
            return
        r = requests.get(f"{self.base}/fapi/v1/exchangeInfo", timeout=15)
        r.raise_for_status()
        for s in r.json().get("symbols", []):
            _symbol_cache[s["symbol"]] = s

    def _sym(self, symbol: str) -> Optional[dict]:
        self._load_info()
        return _symbol_cache.get(symbol)

    def round_qty(self, symbol: str, qty: float) -> float:
        info = self._sym(symbol)
        if info:
            for f in info.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step  = f["stepSize"]
                    stepf = float(step)
                    prec  = _step_precision(step)
                    return round(math.floor(qty / stepf) * stepf, prec)
        return round(math.floor(qty * 1000) / 1000, 3)

    def round_price(self, symbol: str, price: float) -> float:
        info = self._sym(symbol)
        if info:
            for f in info.get("filters", []):
                if f["filterType"] == "PRICE_FILTER":
                    tick  = f["tickSize"]
                    tickf = float(tick)
                    prec  = _step_precision(tick)
                    return round(round(price / tickf) * tickf, prec)
        return round(price, 4)

    def min_notional(self, symbol: str) -> float:
        info = self._sym(symbol)
        if info:
            for f in info.get("filters", []):
                if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                    return float(f.get("notional", f.get("minNotional", 5.0)))
        return 5.0

    # ── Account ───────────────────────────────────────────────
    def get_balance(self) -> float:
        """Available USDT balance."""
        data = self._get("/fapi/v2/account")
        for a in data.get("assets", []):
            if a["asset"] == "USDT":
                return float(a["availableBalance"])
        return 0.0

    def get_open_positions(self) -> list:
        """All non-zero positions with unrealized PnL."""
        data = self._get("/fapi/v2/account")
        out  = []
        for p in data.get("positions", []):
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            init_margin = float(p.get("initialMargin", 1)) or 1
            upnl        = float(p.get("unrealizedProfit", 0))
            out.append({
                "symbol":    p["symbol"],
                "direction": "LONG" if amt > 0 else "SHORT",
                "qty":       abs(amt),
                "entry":     float(p.get("entryPrice", 0)),
                "pnl":       round(upnl, 4),
                "pnl_pct":   round(upnl / init_margin * 100, 2),
            })
        return out

    def open_symbols(self) -> set:
        return {p["symbol"] for p in self.get_open_positions()}

    def get_account_snapshot(self) -> dict:
        """Single API call → balance + open positions with PnL."""
        data    = self._get("/fapi/v2/account")
        balance = 0.0
        for a in data.get("assets", []):
            if a["asset"] == "USDT":
                balance = float(a["availableBalance"])
                break
        positions = []
        for p in data.get("positions", []):
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            init_margin = float(p.get("initialMargin", 1)) or 1
            upnl        = float(p.get("unrealizedProfit", 0))
            positions.append({
                "symbol":    p["symbol"],
                "direction": "LONG" if amt > 0 else "SHORT",
                "qty":       abs(amt),
                "entry":     float(p.get("entryPrice", 0)),
                "pnl":       round(upnl, 4),
                "pnl_pct":   round(upnl / init_margin * 100, 2),
            })
        return {"balance": round(balance, 2), "positions": positions}

    # ── Orders ────────────────────────────────────────────────
    def set_leverage(self, symbol: str, leverage: int):
        try:
            self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
        except Exception:
            pass  # already set or no change needed

    def place_signal_order(
        self,
        symbol:      str,
        direction:   str,
        entry:       float,
        sl:          float,
        tp1:         float,
        tp2:         float,
        usdt_margin: float,
        leverage:    int,
    ) -> dict:
        """
        Set leverage → MARKET entry → STOP_MARKET SL → TAKE_PROFIT_MARKET TP1.
        Raises ValueError/RuntimeError on failure.
        """
        self.set_leverage(symbol, leverage)

        notional = usdt_margin * leverage
        qty      = self.round_qty(symbol, notional / entry)
        if qty <= 0:
            raise ValueError(f"qty 0 setelah rounding (notional={notional:.2f}, entry={entry})")

        actual_notional = qty * entry
        min_not = self.min_notional(symbol)
        if actual_notional < min_not:
            raise ValueError(
                f"Notional {actual_notional:.2f} < minimum {min_not} ({symbol})"
            )

        sl_px  = self.round_price(symbol, sl)
        tp1_px = self.round_price(symbol, tp1)
        tp2_px = self.round_price(symbol, tp2)

        side       = "BUY"  if direction == "LONG"  else "SELL"
        close_side = "SELL" if direction == "LONG"  else "BUY"

        entry_r = self._post("/fapi/v1/order", {
            "symbol":   symbol,
            "side":     side,
            "type":     "MARKET",
            "quantity": qty,
        })
        sl_r = self._post("/fapi/v1/order", {
            "symbol":     symbol,
            "side":       close_side,
            "type":       "STOP_MARKET",
            "quantity":   qty,
            "stopPrice":  sl_px,
            "reduceOnly": "true",
        })
        tp1_r = self._post("/fapi/v1/order", {
            "symbol":     symbol,
            "side":       close_side,
            "type":       "TAKE_PROFIT_MARKET",
            "quantity":   qty,
            "stopPrice":  tp1_px,
            "reduceOnly": "true",
        })

        return {
            "order_id":     str(entry_r.get("orderId", "—")),
            "symbol":       symbol,
            "direction":    direction,
            "qty":          qty,
            "entry":        entry,
            "sl":           sl_px,
            "tp1":          tp1_px,
            "tp2":          tp2_px,
            "margin_usdt":  round(usdt_margin, 2),
            "leverage":     leverage,
            "notional":     round(actual_notional, 2),
            "status":       "OPEN",
            "pnl":          0.0,
            "pnl_pct":      0.0,
            "created_at":   time.time(),
            "sl_order_id":  str(sl_r.get("orderId", "—")),
            "tp1_order_id": str(tp1_r.get("orderId", "—")),
        }
