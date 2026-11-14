"""Trading loop: pull bars -> risk checks -> exits -> strategy signals -> entries. Long-only."""
import asyncio
import json
import math
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .fx import symbol_currency
from . import ai_summary
from .strategies import NAMES, STRATEGIES, BUY, SELL, explain, sma_series, rsi

TRADE_LOG = Path(__file__).resolve().parent.parent / "trades.jsonl"


class Engine:
    def __init__(self, settings, broker):
        self.settings = settings
        self.broker = broker
        self.running = False
        self.halted = None           # reason string when the daily-loss breaker trips
        self.bars = {}
        self.account = {}
        self.positions = {}
        self.market_open = False
        self.trades = deque(maxlen=300)
        self.equity_curve = deque(maxlen=3000)
        self.logs = deque(maxlen=200)
        self.day = None
        self.day_start_equity = None
        self.ticks = 0
        self.last_exit = {}          # sym -> bar timestamp of last sell, for re-entry cooldown
        self.last_eval = {}          # sym -> bar timestamp the strategy last ran on
        self.fx = {}                 # currency -> base-currency units per 1 unit, refreshed each tick
        self.entry_fx = {}           # sym -> FX rate when the position was opened
        self.lock = asyncio.Lock()
        self.persist = True          # append fills to trades.jsonl (off for backtests)
        self.approvals_enabled = True  # the backtester turns this off
        self.pending = {}            # request id -> trade waiting for Approve / Reject
        self.approval_log = deque(maxlen=50)
        self.snooze = {}             # (sym, side) -> bar timestamp of a rejected/expired request
        self._bg = set()             # running AI-summary tasks

    def now(self):
        """Current time in epoch seconds. The backtester overrides this with the replay clock."""
        return time.time()

    @property
    def interval(self):
        if self.settings.tick_seconds > 0:
            return self.settings.tick_seconds
        return getattr(self.broker, "default_interval", 30.0)

    def log(self, msg, level="info"):
        self.logs.appendleft({"t": self.now(), "level": level, "msg": msg})

    async def call(self, fn, *a):
        return await asyncio.to_thread(fn, *a)

    # ---------- main loop ----------
    async def run_forever(self):
        self.log(f"Engine ready in {self.broker.name.upper()} mode. Press Start to trade.")
        while True:
            try:
                await self.refresh(trade=self.running and not self.halted)
            except Exception as e:  # keep the loop alive; surface the error
                self.log(f"tick error: {e}", "error")
            await asyncio.sleep(self.interval)

    async def refresh(self, trade):
        async with self.lock:
            s = self.settings
            await self.call(self.broker.advance, s.symbols)
            self.bars = await self.call(self.broker.get_bars, s.symbols, 300)
            self.account = await self.call(self.broker.get_account)
            self.positions = await self.call(self.broker.get_positions)
            self.market_open = await self.call(self.broker.market_open)
            curs = {symbol_currency(x) for x in list(s.symbols) + list(self.positions)}
            fx_fn = getattr(self.broker, "fx_rates", None)
            self.fx = await self.call(fx_fn, curs) if fx_fn else {c: 1.0 for c in curs}
            for level, msg in getattr(self.broker, "drain_messages", lambda: [])():
                self.log(msg, level)
            eq = self.account["equity"]
            self.equity_curve.append((self.now(), round(eq, 2)))
            self._roll_day(eq)
            self.ticks += 1
            self._expire_pending()
            if not trade:
                return
            if not self.market_open:
                if self.ticks % 20 == 0:
                    self.log("Market closed - waiting.")
                return
            if await self._check_daily_loss(eq):
                return
            # Strategy signals run once per new completed bar (matches the backtester);
            # stop loss / take profit are checked on every tick.
            fresh = {sym for sym, b in self.bars.items() if b and b[-1][0] != self.last_eval.get(sym)}
            await self._exits(fresh)
            await self._entries(fresh)
            for sym in fresh:
                self.last_eval[sym] = self.bars[sym][-1][0]

    def _roll_day(self, eq):
        today = datetime.fromtimestamp(self.now(), timezone.utc).date()
        if self.day != today:
            self.day = today
            self.day_start_equity = self.account.get("last_equity") or eq

    async def _check_daily_loss(self, eq):
        dd = (eq - self.day_start_equity) / self.day_start_equity * 100
        if dd <= -self.settings.max_daily_loss_pct:
            self.halted = f"Daily loss limit hit ({dd:.2f}%). Flattened and halted."
            self.log(self.halted, "error")
            await self._flatten("daily loss limit")
            return True
        return False

    # ---------- trading ----------
    def _closes(self, sym):
        return [c for _, c in self.bars.get(sym, [])]

    def _fx(self, sym):
        """Base-currency units per 1 unit of the symbol's trading currency (None if unknown)."""
        return self.fx.get(symbol_currency(sym))

    def _bars_since(self, sym, ts):
        return sum(1 for t, _ in self.bars.get(sym, []) if t > ts)

    async def _exits(self, fresh):
        s = self.settings
        fn = STRATEGIES[s.strategy]
        for sym, p in list(self.positions.items()):
            chg = (p["price"] - p["avg"]) / p["avg"] * 100
            reason = None
            if chg <= -s.stop_loss_pct:
                reason = f"stop loss ({chg:.2f}%)"
            elif chg >= s.take_profit_pct:
                reason = f"take profit ({chg:.2f}%)"
            elif sym in fresh and sym in s.symbols and fn(self._closes(sym), s) == SELL:
                if self._asks("sell"):
                    if not self._pending_for(sym) and not self._snoozed(sym, "sell"):
                        self._request(sym, "sell", p["qty"], f"{s.strategy} sell signal")
                    continue
                reason = f"{s.strategy} sell signal"
            if reason:
                # protective exits never wait for approval; drop any sell request they make redundant
                for r in self._pending_for(sym):
                    self._resolve(r, "superseded", reason)
                await self._order(sym, p["qty"], "sell", reason, entry=p["avg"])

    async def _entries(self, fresh):
        s = self.settings
        fn = STRATEGIES[s.strategy]
        # a buy waiting for approval counts as a position so limits can't be overshot
        held = set(self.positions) | {r["symbol"] for r in self.pending.values() if r["side"] == "buy"}
        for sym in s.symbols:
            if sym not in fresh or sym in held or len(held) >= s.max_positions:
                continue
            if sym in self.last_exit and self._bars_since(sym, self.last_exit[sym]) < s.cooldown_bars:
                continue
            if self._snoozed(sym, "buy"):
                continue
            closes = self._closes(sym)
            if not closes or fn(closes, s) != BUY:
                continue
            fx = self._fx(sym)
            if not fx:
                self.log(f"{sym}: buy signal but no {symbol_currency(sym)} exchange rate - skipped", "warn")
                continue
            # budget is in the account's base currency; convert the share price into it before sizing
            budget = min(self.account["equity"] * s.position_pct / 100, self.account["buying_power"])
            qty = math.floor(budget / (closes[-1] * fx))
            if qty < 1:
                self.log(f"{sym}: buy signal but budget too small", "warn")
                continue
            if self._asks("buy"):
                self._request(sym, "buy", qty, f"{s.strategy} buy signal")
                held.add(sym)
            elif await self._order(sym, qty, "buy", f"{s.strategy} buy signal"):
                held.add(sym)

    async def _order(self, sym, qty, side, reason, entry=None):
        price = self._closes(sym)[-1] if self._closes(sym) else None
        try:
            res = await self.call(self.broker.submit, sym, qty, side)
        except Exception as e:
            self.log(f"{side.upper()} {qty} {sym} failed: {e}", "error")
            return False
        fill = res.get("filled_price", price)
        cur, fx = symbol_currency(sym), self._fx(sym) or 1.0
        pnl = pnl_base = None
        if side == "buy":
            self.entry_fx[sym] = fx
        elif entry:
            pnl = round((fill - entry) * qty, 2)  # in the stock's currency
            # base-currency P&L includes the FX move between entry and exit
            pnl_base = round(qty * (fill * fx - entry * self.entry_fx.pop(sym, fx)), 2)
        bar_t = self.bars[sym][-1][0] if self.bars.get(sym) else None
        if side == "sell" and bar_t is not None:
            self.last_exit[sym] = bar_t
        t = {"t": self.now(), "bar_t": bar_t, "symbol": sym, "side": side, "qty": qty, "price": fill,
             "currency": cur, "fx": round(fx, 6), "reason": reason, "pnl": pnl, "pnl_base": pnl_base,
             "mode": self.broker.name}
        self.trades.appendleft(t)
        if self.persist:
            with TRADE_LOG.open("a") as f:
                f.write(json.dumps(t) + "\n")
        base = self.account.get("currency", "")
        pnl_txt = ""
        if pnl is not None:
            pnl_txt = f" | P&L {pnl:+.2f} {cur}" + (f" ({pnl_base:+.2f} {base})" if cur != base else "")
        self.log(f"{side.upper()} {qty} {sym} @ {fill:.2f} {cur} - {reason}{pnl_txt}")
        self.positions = await self.call(self.broker.get_positions)
        self.account = await self.call(self.broker.get_account)
        return True

    async def _flatten(self, reason):
        self.cancel_pending(reason)
        for sym, p in list(self.positions.items()):
            await self._order(sym, p["qty"], "sell", reason, entry=p["avg"])

    # ---------- approvals ----------
    def _asks(self, side):
        s = self.settings
        if not (self.approvals_enabled and s.require_approval):
            return False
        return side == "buy" or s.approve_exits

    def _pending_for(self, sym):
        return [r for r in self.pending.values() if r["symbol"] == sym]

    def _snoozed(self, sym, side):
        t = self.snooze.get((sym, side))
        return t is not None and self._bars_since(sym, t) < max(self.settings.cooldown_bars, 5)

    def _request(self, sym, side, qty, reason):
        """Queue a trade for the person to approve, with the numbers behind it."""
        s, closes = self.settings, self._closes(sym)
        price, cur, fx = closes[-1], symbol_currency(sym), self._fx(sym) or 1.0
        base = self.account.get("currency", "") or cur
        eq = self.account.get("equity") or 0
        start = self.day_start_equity or eq or 1
        value_base = qty * price * fx

        def move(n):
            return round((closes[-1] - closes[-n - 1]) / closes[-n - 1] * 100, 2) if len(closes) > n else None
        recent = closes[-60:]
        facts = {
            "order": {"side": side.upper(), "symbol": sym, "qty": qty, "price": round(price, 4), "currency": cur,
                      "value": round(qty * price, 2), "value_in_account_currency": round(value_base, 2),
                      "account_currency": base, "pct_of_equity": round(value_base / eq * 100, 1) if eq else None},
            "strategy": {"name": NAMES.get(s.strategy, s.strategy), "reasons": explain(s.strategy, closes, s)},
            "recent_moves_pct": {"last_5_bars": move(5), "last_15_bars": move(15), "last_60_bars": move(60)},
            "last_60_bar_range": [round(min(recent), 4), round(max(recent), 4)] if recent else None,
            "account": {"positions_used": len(self.positions), "max_positions": s.max_positions,
                        "day_pnl_pct": round((eq - start) / start * 100, 2),
                        "daily_loss_limit_pct": -s.max_daily_loss_pct},
        }
        if side == "buy":
            stop, target = price * (1 - s.stop_loss_pct / 100), price * (1 + s.take_profit_pct / 100)
            facts["risk"] = {"stop_loss": round(stop, 2), "stop_loss_pct": -s.stop_loss_pct,
                             "take_profit": round(target, 2), "take_profit_pct": s.take_profit_pct,
                             "loss_at_stop_in_account_currency": round(qty * (price - stop) * fx, 2)}
            if cur != base:
                facts["risk"]["currency_note"] = f"Priced in {cur}; gains and losses also move with the {cur}/{base} rate."
        else:
            p = self.positions.get(sym, {})
            if p:
                facts["position"] = {"avg_cost": p["avg"], "change_pct": round((price - p["avg"]) / p["avg"] * 100, 2),
                                     "unrealized_in_account_currency": round(p["unrealized"] * fx, 2)}
        now = self.now()
        r = {"id": uuid.uuid4().hex[:8], "symbol": sym, "side": side, "qty": qty, "price": price, "currency": cur,
             "fx": fx, "reason": reason, "bar_t": self.bars[sym][-1][0], "created": now,
             "expires": now + s.approval_timeout_s, "status": "pending", "facts": facts,
             "ai": None, "ai_status": None, "note": ""}
        self.pending[r["id"]] = r
        self.log(f"APPROVAL NEEDED: {side.upper()} {qty} {sym} @ {price:.2f} {cur} - {reason} "
                 f"(expires in {s.approval_timeout_s}s)", "warn")
        if s.ai_summary and ai_summary.available():
            r["ai_status"] = "working"
            task = asyncio.create_task(self._summarize(r))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
        return r

    async def _summarize(self, r):
        try:
            r["ai"] = await asyncio.to_thread(ai_summary.summarize, r["facts"])
            r["ai_status"] = "done"
        except Exception as e:
            r["ai_status"], r["ai"] = "error", f"Summary unavailable: {str(e)[:160]}"

    def _resolve(self, r, status, note=""):
        self.pending.pop(r["id"], None)
        r.update(status=status, resolved=self.now(), note=note)
        self.approval_log.appendleft(r)
        if status in ("rejected", "expired", "stale"):
            bars = self.bars.get(r["symbol"])
            self.snooze[(r["symbol"], r["side"])] = bars[-1][0] if bars else r["bar_t"]
        if status != "approved":
            self.log(f"{r['side'].upper()} {r['qty']} {r['symbol']} {status}" + (f": {note}" if note else ""),
                     "warn" if status in ("stale", "failed") else "info")

    def _expire_pending(self):
        now = self.now()
        for r in list(self.pending.values()):
            if now >= r["expires"]:
                self._resolve(r, "expired", "no answer in time")

    def cancel_pending(self, why):
        for r in list(self.pending.values()):
            self._resolve(r, "cancelled", why)

    async def approve(self, rid):
        """Send an approved request to the broker after re-checking it is still sensible."""
        async with self.lock:
            r = self.pending.get(rid)
            if not r:
                raise ValueError("That request is no longer waiting (it expired or was already answered).")
            if self.now() >= r["expires"]:
                self._resolve(r, "expired", "no answer in time")
                raise ValueError("That request just expired.")
            if self.halted:
                raise ValueError("Trading is halted by the daily loss limit.")
            if not self.market_open:
                raise ValueError("The market is closed.")
            sym, side, s = r["symbol"], r["side"], self.settings
            price = self._closes(sym)[-1]
            if side == "buy":
                drift = (price - r["price"]) / r["price"] * 100
                if drift > s.max_drift_pct:
                    self._resolve(r, "stale", f"price rose {drift:.2f}% since the request (limit {s.max_drift_pct:g}%)")
                    raise ValueError(f"Not sent: the price rose {drift:.2f}% since the request.")
                if sym in self.positions:
                    self._resolve(r, "cancelled", "already holding it")
                    raise ValueError(f"Already holding {sym}.")
                fx = self._fx(sym) or r["fx"]
                qty = min(r["qty"], math.floor(self.account.get("buying_power", 0) / (price * fx)))
                if qty < 1:
                    self._resolve(r, "failed", "not enough buying power")
                    raise ValueError("Not enough buying power.")
                ok = await self._order(sym, qty, "buy", f"{r['reason']} (approved)")
            else:
                p = self.positions.get(sym)
                if not p:
                    self._resolve(r, "cancelled", "position already closed")
                    raise ValueError(f"{sym} is no longer held.")
                ok = await self._order(sym, p["qty"], "sell", f"{r['reason']} (approved)", entry=p["avg"])
            self._resolve(r, "approved" if ok else "failed", "" if ok else "broker rejected the order")
            if not ok:
                raise ValueError("The broker didn't accept the order; see the activity log.")
            return r

    def reject(self, rid):
        r = self.pending.get(rid)
        if not r:
            raise ValueError("That request is no longer waiting.")
        self._resolve(r, "rejected", "rejected by you")
        return r

    def _public(self, r):
        out = dict(r)
        out["remaining"] = max(0, round(r["expires"] - self.now())) if r["status"] == "pending" else 0
        return out

    # ---------- controls ----------
    async def flatten(self):
        async with self.lock:
            self.positions = await self.call(self.broker.get_positions)
            await self._flatten("manual flatten")

    def reset_halt(self):
        self.halted = None
        self.day_start_equity = self.account.get("equity", self.day_start_equity)
        self.log("Halt cleared; daily loss baseline reset to current equity.", "warn")

    # ---------- dashboard snapshot ----------
    def snapshot(self, symbol=None):
        s = self.settings
        eq = self.account.get("equity", 0)
        closed = [t for t in self.trades if t.get("pnl_base") is not None]
        wins = sum(1 for t in closed if t["pnl_base"] > 0)
        positions = {}
        for sym, p in self.positions.items():
            fx = self._fx(sym) or 1.0
            positions[sym] = {**p, "currency": symbol_currency(sym), "unrealized_base": p["unrealized"] * fx}
        symbol = symbol if symbol in self.bars else (s.symbols[0] if s.symbols else None)
        chart = None
        if symbol:
            bars = self.bars[symbol][-150:]
            closes_all = self._closes(symbol)
            off = len(closes_all) - len(bars)
            fast = sma_series(closes_all, s.fast)[off:]
            slow = sma_series(closes_all, s.slow)[off:]
            t0 = bars[0][0] if bars else 0
            marks = [t for t in self.trades if t["symbol"] == symbol and (t.get("bar_t") or 0) >= t0]
            chart = {"symbol": symbol, "t": [b[0] for b in bars], "c": [b[1] for b in bars],
                     "fast": fast, "slow": slow, "rsi": rsi(closes_all, s.rsi_period),
                     "marks": [{"t": m["bar_t"], "side": m["side"], "price": m["price"]} for m in marks[:40]]}
        watch = []
        for sym in s.symbols:
            cl = self._closes(sym)
            if len(cl) >= 2:
                watch.append({"symbol": sym, "price": cl[-1], "currency": symbol_currency(sym), "chg": (cl[-1] - cl[-2]) / cl[-2] * 100,
                              "signal": STRATEGIES[s.strategy](cl, s), "held": sym in self.positions})
        return {
            "mode": self.broker.name, "running": self.running, "halted": self.halted,
            "market_open": self.market_open, "interval": self.interval,
            "account": self.account, "day_pnl": eq - (self.day_start_equity or eq),
            "day_pnl_pct": (eq - (self.day_start_equity or eq)) / (self.day_start_equity or eq or 1) * 100,
            "positions": positions, "trades": list(self.trades)[:100], "fx": self.fx,
            "base_currency": self.account.get("currency", ""),
            "realized": round(sum(t["pnl_base"] for t in closed), 2),
            "win_rate": (wins / len(closed) * 100) if closed else None, "closed_count": len(closed),
            "equity_curve": list(self.equity_curve)[-600:], "logs": list(self.logs)[:60],
            "settings": s.to_dict(), "chart": chart, "watch": watch,
            "pending": [self._public(r) for r in sorted(self.pending.values(), key=lambda r: r["created"])],
            "approval_log": [self._public(r) for r in list(self.approval_log)[:20]],
            "ai_available": ai_summary.available(),
        }
