"""Broker adapters. SimBroker needs nothing; AlpacaBroker talks to Alpaca's REST API (paper or live)."""
import math
import random
import time
from datetime import datetime, timedelta, timezone

import requests

from .fx import cross, symbol_currency


class SimBroker:
    """Synthetic market: random-walk prices with drifting trends, instant fills with slippage."""

    name = "sim"
    default_interval = 1.0

    def __init__(self, starting_cash=100_000.0, slippage_bps=2.0, seed=None, base_currency="CAD", usdcad=1.38):
        self.rng = random.Random(seed)
        self.base_currency = base_currency
        self.usdcad = usdcad  # CAD per USD; drifts slightly in sim mode
        self.cash = starting_cash  # held in base currency; trades convert at the current rate
        self.positions = {}  # sym -> {"qty": int, "avg": float}
        self.slippage = slippage_bps / 10_000
        self.prices = {}     # sym -> list[(ts, close)]
        self.drift = {}
        self.t = int(time.time()) - 300 * 60

    def _ensure(self, sym):
        if sym in self.prices:
            return
        p = self.rng.uniform(40, 500)
        self.prices[sym] = []
        self.drift[sym] = 0.0
        for i in range(300):  # backfill history so indicators are warm
            p = self._step(sym, p)
            self.prices[sym].append((self.t + i * 60, round(p, 2)))

    def _step(self, sym, p):
        if self.rng.random() < 0.03:  # occasional regime change
            self.drift[sym] = self.rng.gauss(0, 0.0012)
        return p * math.exp(self.drift[sym] + self.rng.gauss(0, 0.004))

    def advance(self, symbols):
        self.usdcad *= math.exp(self.rng.gauss(0, 0.00005))
        for s in symbols:
            self._ensure(s)
            last_ts, last_p = self.prices[s][-1]
            self.prices[s].append((last_ts + 60, round(self._step(s, last_p), 2)))
            del self.prices[s][:-1000]

    def market_open(self):
        return True

    def get_bars(self, symbols, limit=200):
        for s in symbols:
            self._ensure(s)
        return {s: self.prices[s][-limit:] for s in symbols}

    def last(self, sym):
        return self.prices[sym][-1][1]

    # ---- currency
    def current_usdcad(self):
        return self.usdcad

    def fx_rates(self, currencies):
        out = {}
        for c in currencies:
            r = cross(self.base_currency, c, self.current_usdcad())
            if r:
                out[c] = r
        return out

    def fx_of(self, sym):
        r = cross(self.base_currency, symbol_currency(sym), self.current_usdcad())
        if r is None:
            raise RuntimeError(f"no {symbol_currency(sym)}/{self.base_currency} rate")
        return r

    def get_account(self):
        mv = sum(p["qty"] * self.last(s) * self.fx_of(s) for s, p in self.positions.items())
        return {"equity": self.cash + mv, "cash": self.cash, "buying_power": self.cash,
                "currency": self.base_currency}

    def get_positions(self):
        out = {}
        for s, p in self.positions.items():
            px = self.last(s)
            out[s] = {"qty": p["qty"], "avg": p["avg"], "price": px,
                      "unrealized": (px - p["avg"]) * p["qty"]}
        return out

    def submit(self, sym, qty, side):
        px = self.last(sym) * (1 + self.slippage if side == "buy" else 1 - self.slippage)
        fx = self.fx_of(sym)
        if side == "buy":
            cost = px * qty * fx
            if cost > self.cash:
                raise RuntimeError(f"insufficient cash for {qty} {sym}")
            pos = self.positions.get(sym, {"qty": 0, "avg": 0.0})
            new_qty = pos["qty"] + qty
            pos["avg"] = (pos["avg"] * pos["qty"] + px * qty) / new_qty
            pos["qty"] = new_qty
            self.positions[sym] = pos
            self.cash -= cost
        else:
            pos = self.positions.get(sym)
            if not pos or pos["qty"] < qty:
                raise RuntimeError(f"not enough {sym} to sell")
            pos["qty"] -= qty
            self.cash += px * qty * fx
            if pos["qty"] == 0:
                del self.positions[sym]
        return {"filled_price": round(px, 4)}

    def close_all(self):
        for s, p in list(self.positions.items()):
            self.submit(s, p["qty"], "sell")


class AlpacaBroker:
    """Alpaca REST adapter. Market orders, long-only, day time-in-force."""

    DATA_URL = "https://data.alpaca.markets"
    default_interval = 30.0

    def __init__(self, key, secret, live=False, feed="iex"):
        self.name = "live" if live else "paper"
        self.base = "https://api.alpaca.markets" if live else "https://paper-api.alpaca.markets"
        self.feed = feed
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _req(self, method, url, **kw):
        r = self.s.request(method, url, timeout=15, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {url.split('.markets')[-1]} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.text else {}

    def advance(self, symbols):
        pass  # real market moves on its own

    def market_open(self):
        return bool(self._req("GET", f"{self.base}/v2/clock").get("is_open"))

    def get_bars(self, symbols, limit=200):
        start = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        out = {}
        for sym in symbols:
            data = self._req("GET", f"{self.DATA_URL}/v2/stocks/{sym}/bars", params={
                "timeframe": "1Min", "limit": limit, "start": start, "sort": "desc", "feed": self.feed,
            })
            bars = list(reversed(data.get("bars") or []))
            out[sym] = [(int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()), b["c"]) for b in bars]
        return out

    def get_account(self):
        a = self._req("GET", f"{self.base}/v2/account")
        return {"equity": float(a["equity"]), "cash": float(a["cash"]),
                "buying_power": float(a["buying_power"]), "last_equity": float(a.get("last_equity") or 0),
                "currency": "USD"}

    def fx_rates(self, currencies):
        return {"USD": 1.0} if "USD" in currencies else {}  # Alpaca is US-only

    def get_positions(self):
        out = {}
        for p in self._req("GET", f"{self.base}/v2/positions"):
            out[p["symbol"]] = {"qty": int(float(p["qty"])), "avg": float(p["avg_entry_price"]),
                                "price": float(p["current_price"]), "unrealized": float(p["unrealized_pl"])}
        return out

    def submit(self, sym, qty, side):
        o = self._req("POST", f"{self.base}/v2/orders", json={
            "symbol": sym, "qty": str(qty), "side": side, "type": "market", "time_in_force": "day",
        })
        return {"order_id": o.get("id"), "status": o.get("status")}

    def close_all(self):
        self._req("DELETE", f"{self.base}/v2/positions", params={"cancel_orders": "true"})
