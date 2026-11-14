"""Backtester: replays historical 1-minute bars through the *same* Engine used for live trading,
so strategy, sizing, stops, cooldown and the daily-loss breaker behave identically.

Data sources:
  synthetic  - generated trending random walk (for testing the plumbing, NOT for judging a strategy)
  csv        - data/<SYMBOL>.csv with columns timestamp,close  (e.g. data/SHOP.TO.csv, data/RY.TO.csv)
               timestamp may be ISO-8601 or epoch seconds; ISO without an offset is read as Toronto/New York time.

CLI:  python -m bot.backtest --source synthetic --days 10 --strategy rsi_reversion
"""
import argparse
import asyncio
import bisect
import csv
import math
import random
import statistics
from collections import deque, defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .brokers import SimBroker
from .config import Settings
from .fx import cross, symbol_currency
from .engine import Engine

ET = ZoneInfo("America/Toronto")  # TSX and US exchanges share 09:30-16:00 Eastern
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OPEN, CLOSE = dtime(9, 30), dtime(16, 0)


# ---------------------------------------------------------------- data
def synthetic_data(symbols, days=10, seed=1):
    rng = random.Random(seed)
    d, sessions = date.today(), []
    while len(sessions) < days:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            sessions.append(d)
    sessions.reverse()
    out = {}
    for sym in symbols:
        p, drift, bars = rng.uniform(20, 300), 0.0, []
        for day in sessions:
            p *= math.exp(rng.gauss(0, 0.01))  # overnight gap
            t0 = datetime.combine(day, OPEN, ET)
            for m in range(390):
                if rng.random() < 0.03:
                    drift = rng.gauss(0, 0.0012)
                p *= math.exp(drift + rng.gauss(0, 0.004))
                bars.append((int((t0 + timedelta(minutes=m)).timestamp()), round(p, 2)))
        out[sym] = bars
    return out


def _parse_ts(v):
    v = v.strip()
    try:
        return int(float(v))
    except ValueError:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return int(dt.timestamp())


def csv_data(symbols, start=None, end=None, regular_hours=True):
    out, missing = {}, []
    for sym in symbols:
        f = DATA_DIR / f"{sym}.csv"
        if not f.exists():
            missing.append(f.name)
            continue
        with f.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            continue
        cols = {k.lower().strip(): k for k in rows[0]}
        tcol = next((cols[c] for c in ("timestamp", "datetime", "time", "date", "t") if c in cols), None)
        ccol = next((cols[c] for c in ("close", "c", "adj close") if c in cols), None)
        if not tcol or not ccol:
            raise ValueError(f"{f.name}: need a timestamp column and a close column (found {list(rows[0])})")
        bars = sorted((_parse_ts(r[tcol]), float(r[ccol])) for r in rows if r[ccol])
        out[sym] = _filter(bars, start, end, regular_hours)
    if missing:
        raise FileNotFoundError(f"No data for: {', '.join(missing)}. Put CSV files in {DATA_DIR}")
    return out


def _filter(bars, start, end, regular_hours):
    res = []
    for ts, c in bars:
        dt = datetime.fromtimestamp(ts, ET)
        if start and dt.date() < start:
            continue
        if end and dt.date() > end:
            continue
        if regular_hours and not (OPEN <= dt.time() < CLOSE):
            continue
        res.append((ts, c))
    return res


def available_csvs():
    return sorted(p.stem for p in DATA_DIR.glob("*.csv") if p.stem != FX_FILE) if DATA_DIR.exists() else []


FX_FILE = "USDCAD"  # data/USDCAD.csv: timestamp,close = CAD per 1 USD (written by bot.ib_download)


def fx_series():
    f = DATA_DIR / f"{FX_FILE}.csv"
    return csv_data([FX_FILE], regular_hours=False)[FX_FILE] if f.exists() else []


# ---------------------------------------------------------------- replay
class ReplayBroker(SimBroker):
    name = "backtest"

    def __init__(self, data, cash, slippage_bps, commission_per_share, min_commission,
                 base_currency="CAD", usdcad=1.38, fx=None):
        super().__init__(starting_cash=cash, slippage_bps=slippage_bps, base_currency=base_currency, usdcad=usdcad)
        self.fx_data = fx or []                     # [(ts, CAD per USD)], used when available
        self.fx_ts = [t for t, _ in self.fx_data]
        self.data = {s: b for s, b in data.items() if b}
        self.ts = {s: [b[0] for b in bars] for s, bars in self.data.items()}
        self.timeline = sorted({t for lst in self.ts.values() for t in lst})
        self.i = -1
        self.now_ts = self.timeline[0] if self.timeline else 0
        self.commission = commission_per_share
        self.min_commission = min_commission
        self.fees = 0.0

    def advance(self, symbols):
        self.i += 1
        self.now_ts = self.timeline[self.i]

    def market_open(self):
        return True

    def get_bars(self, symbols, limit=200):
        out = {}
        for s in symbols:
            if s in self.data:
                j = bisect.bisect_right(self.ts[s], self.now_ts)
                out[s] = self.data[s][max(0, j - limit):j]
        return out

    def last(self, sym):
        j = bisect.bisect_right(self.ts[sym], self.now_ts)
        return self.data[sym][max(j - 1, 0)][1]

    def usdcad_at(self, t):
        if not self.fx_data:
            return self.usdcad
        j = bisect.bisect_right(self.fx_ts, t)
        return self.fx_data[max(j - 1, 0)][1]

    def current_usdcad(self):
        return self.usdcad_at(self.now_ts)

    def submit(self, sym, qty, side):
        res = super().submit(sym, qty, side)
        # commissions are charged in the stock's currency, then converted to the account's base currency
        fee = max(self.commission * qty, self.min_commission) if (self.commission or self.min_commission) else 0.0
        self.cash -= fee * self.fx_of(sym)
        self.fees += fee * self.fx_of(sym)
        per_share = fee / qty
        # fold fees into prices so per-trade P&L in the engine is net of costs
        if side == "buy":
            self.positions[sym]["avg"] += per_share * qty / self.positions[sym]["qty"]
            res["filled_price"] += per_share
        else:
            res["filled_price"] -= per_share
        return res


class BacktestEngine(Engine):
    def __init__(self, settings, broker):
        super().__init__(settings, broker)
        self.trades = deque()
        self.equity_curve = deque()
        self.persist = False
        self.warn_counts = defaultdict(int)

    def log(self, msg, level="info"):
        if level != "info":  # tally instead of flooding: "SHOP.TO: buy signal but budget too small"
            self.warn_counts[msg.split(": ", 1)[-1]] += 1
        super().log(msg, level)

    def now(self):
        return self.broker.now_ts

    async def call(self, fn, *a):
        return fn(*a)


# ---------------------------------------------------------------- run
def run_backtest(params, progress=None, cancelled=lambda: False):
    """params: dict with source, symbols, days/seed or start/end, cash, slippage_bps,
    commission_per_share, min_commission, flatten_eod, regular_hours, settings{...}."""
    settings = Settings()
    settings.update(params.get("settings", {}))
    symbols = [s.strip().upper() for s in (params.get("symbols") or settings.symbols) if s.strip()]
    settings.symbols = symbols
    src = params.get("source", "synthetic")
    start = date.fromisoformat(params["start"]) if params.get("start") else None
    end = date.fromisoformat(params["end"]) if params.get("end") else None
    if src == "synthetic":
        data = synthetic_data(symbols, int(params.get("days", 10)), int(params.get("seed", 1)))
    elif src == "csv":
        data = csv_data(symbols, start, end, params.get("regular_hours", True))
    else:
        raise ValueError(f"unknown source {src!r}")
    cash = float(params.get("cash", 100_000))
    base_ccy = (params.get("base_currency") or "CAD").upper()
    needs_fx = any(symbol_currency(s) != base_ccy for s in symbols)
    fx = fx_series() if (needs_fx and src == "csv") else []
    broker = ReplayBroker(data, cash, float(params.get("slippage_bps", 2)),
                          float(params.get("commission_per_share", 0.0)), float(params.get("min_commission", 0.0)),
                          base_currency=base_ccy, usdcad=float(params.get("usdcad") or 1.38), fx=fx)
    if not broker.timeline:
        raise ValueError("No bars in the selected range")
    eng = BacktestEngine(settings, broker)
    eng.running = True
    flatten_eod = params.get("flatten_eod", True)

    tl = broker.timeline
    day_of = [datetime.fromtimestamp(t, ET).date() for t in tl]
    last_of_day = {i for i in range(len(tl)) if i == len(tl) - 1 or day_of[i + 1] != day_of[i]}
    exposed = 0

    async def loop():
        nonlocal exposed
        for i in range(len(tl)):
            if i and day_of[i] != day_of[i - 1]:
                eng.halted = None  # breaker resets next session, like a manual restart
            await eng.refresh(trade=not eng.halted)
            if flatten_eod and i in last_of_day and eng.positions:
                await eng._flatten("end of day")
            if eng.positions:
                exposed += 1
            if i % 500 == 0:
                if progress:
                    progress(i / len(tl))
                if cancelled():
                    raise RuntimeError("cancelled")
        if eng.positions:
            await eng._flatten("end of backtest")

    asyncio.run(loop())
    if progress:
        progress(1.0)
    fx_note = None
    if needs_fx:
        fx_note = (f"USD/CAD history ({len(fx):,} points)" if fx else f"fixed USD/CAD rate {broker.usdcad:g}")
    return _report(eng, broker, data, cash, exposed, params, settings, fx_note)


# ---------------------------------------------------------------- metrics
def _report(eng, broker, data, cash, exposed, params, settings, fx_note=None):
    curve = list(eng.equity_curve)
    eqs = [e for _, e in curve]
    final = eqs[-1] if eqs else cash
    # max drawdown
    peak, mdd = -1, 0.0
    for e in eqs:
        peak = max(peak, e)
        mdd = min(mdd, (e - peak) / peak)
    # daily returns -> Sharpe
    daily = {}
    for t, e in curve:
        daily[datetime.fromtimestamp(t, ET).date()] = e
    closes = [cash] + list(daily.values())
    rets = [b / a - 1 for a, b in zip(closes, closes[1:])]
    sharpe = (statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(252)) if len(rets) >= 2 and statistics.stdev(rets) > 0 else None
    # benchmark: equal-weight buy & hold of the same symbols
    syms = [s for s in settings.symbols if s in broker.data]
    # value each holding in the base currency, so the benchmark also carries the FX move
    rate = lambda s, t: cross(broker.base_currency, symbol_currency(s), broker.usdcad_at(t)) or 1.0
    base = {s: broker.data[s][0][1] * rate(s, broker.data[s][0][0]) for s in syms}

    def bench_at(t):
        vals = []
        for s in syms:
            j = bisect.bisect_right(broker.ts[s], t)
            vals.append(broker.data[s][j - 1][1] * rate(s, t) / base[s] if j else 1.0)
        return cash * (sum(vals) / len(vals)) if vals else cash

    step = max(1, len(curve) // 1500)
    sampled = curve[::step] + ([curve[-1]] if curve and (len(curve) - 1) % step else [])
    equity = [[t, e, round(bench_at(t), 2)] for t, e in sampled]
    bench_final = bench_at(curve[-1][0]) if curve else cash
    # trades
    trades = list(reversed(eng.trades))
    closed = [t for t in trades if t.get("pnl_base") is not None]
    wins = [t["pnl_base"] for t in closed if t["pnl_base"] > 0]
    losses = [t["pnl_base"] for t in closed if t["pnl_base"] <= 0]
    holds, open_at = [], {}
    per = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        if t["side"] == "buy":
            open_at[t["symbol"]] = t["bar_t"]
        else:
            if t["symbol"] in open_at:
                holds.append((t["bar_t"] - open_at.pop(t["symbol"])) / 60)
            p = per[t["symbol"]]
            p["trades"] += 1
            if t.get("pnl_base") is not None:
                p["wins"] += t["pnl_base"] > 0
                p["pnl"] += t["pnl_base"]
    per_symbol = []
    for s in syms:
        p = per.get(s, {"trades": 0, "wins": 0, "pnl": 0.0})
        bars = broker.data[s]
        per_symbol.append({"symbol": s, "currency": symbol_currency(s), "trades": p["trades"],
                           "win_rate": (p["wins"] / p["trades"] * 100) if p["trades"] else None, "pnl": round(p["pnl"], 2),
                           "buy_hold_pct": (bars[-1][1] * rate(s, bars[-1][0]) / base[s] - 1) * 100})
    reasons = defaultdict(int)
    for t in closed:
        reasons[t["reason"].split(" (")[0]] += 1
    summary = {
        "start": curve[0][0] if curve else None, "end": curve[-1][0] if curve else None,
        "bars": len(broker.timeline), "sessions": len(daily),
        "starting_equity": cash, "final_equity": round(final, 2),
        "total_return_pct": (final / cash - 1) * 100,
        "buy_hold_return_pct": (bench_final / cash - 1) * 100,
        "max_drawdown_pct": mdd * 100, "sharpe": sharpe,
        "round_trips": len(closed), "win_rate": (len(wins) / len(closed) * 100) if closed else None,
        "avg_win": statistics.mean(wins) if wins else None, "avg_loss": statistics.mean(losses) if losses else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "avg_hold_min": statistics.mean(holds) if holds else None,
        "exposure_pct": exposed / len(broker.timeline) * 100, "fees": round(broker.fees, 2),
        "exit_reasons": dict(reasons), "base_currency": broker.base_currency, "fx": fx_note,
        "warnings": _warnings(eng, closed, syms, settings, fx_note),
    }
    return {"summary": summary, "equity": equity, "trades": trades[-2000:], "per_symbol": per_symbol,
            "settings": settings.to_dict(), "params": {k: v for k, v in params.items() if k != "settings"}}


def _warnings(eng, closed, syms, settings, fx_note=None):
    w = []
    if fx_note and fx_note.startswith("fixed"):
        w.append(f"Mixed currencies converted at a {fx_note} for the whole test. Run bot.ib_download with a "
                 f"US symbol to also fetch USD/CAD history (data/USDCAD.csv) for real rates.")
    budget = eng.warn_counts.pop("buy signal but budget too small", 0)
    if budget:
        w.append(f"{budget} buy signals skipped because position size ({settings.position_pct}% of equity) "
                 f"couldn't buy one share. Raise starting cash or position size.")
    w += [f"{msg} (x{n})" for msg, n in eng.warn_counts.items() if "Daily loss limit" not in msg]
    missing = [s for s in settings.symbols if s not in syms]
    if missing:
        w.append(f"No bars for {', '.join(missing)}; they were skipped.")
    if not closed and not budget:
        w.append("No round trips. The strategy never signalled; try other parameters or more data.")
    return w


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Backtest the trading bot on historical 1-minute bars")
    ap.add_argument("--source", choices=["synthetic", "csv"], default="synthetic")
    ap.add_argument("--symbols", help="comma separated, e.g. SHOP.TO,RY.TO,TD.TO")
    ap.add_argument("--days", type=int, default=10, help="synthetic: sessions to generate")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--start"), ap.add_argument("--end")
    ap.add_argument("--cash", type=float, default=100_000)
    ap.add_argument("--slippage-bps", type=float, default=2)
    ap.add_argument("--commission", type=float, default=0.0, help="per share")
    ap.add_argument("--min-commission", type=float, default=0.0, help="per order")
    ap.add_argument("--base-currency", default="CAD", choices=["CAD", "USD"], help="account currency")
    ap.add_argument("--usdcad", type=float, default=1.38, help="fixed CAD-per-USD rate if data/USDCAD.csv is missing")
    ap.add_argument("--hold-overnight", action="store_true", help="don't flatten at the close")
    ap.add_argument("--strategy")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="any strategy/risk setting")
    a = ap.parse_args()
    st = dict(kv.split("=", 1) for kv in a.set)
    if a.strategy:
        st["strategy"] = a.strategy
    params = {"source": a.source, "days": a.days, "seed": a.seed, "start": a.start, "end": a.end,
              "cash": a.cash, "slippage_bps": a.slippage_bps, "commission_per_share": a.commission,
              "min_commission": a.min_commission, "flatten_eod": not a.hold_overnight, "settings": st,
              "base_currency": a.base_currency, "usdcad": a.usdcad}
    if a.symbols:
        params["symbols"] = a.symbols.split(",")
    r = run_backtest(params, progress=lambda p: print(f"\r  {p * 100:5.1f}%", end="", flush=True))
    print()
    s = r["summary"]
    fmt = lambda v, f="{:.2f}": "-" if v is None else f.format(v)
    rows = [("Sessions / bars", f"{s['sessions']} / {s['bars']}"),
            ("Final equity", f"{s['final_equity']:,.2f} {s['base_currency']}"),
            ("FX", s["fx"] or "single currency"),
            ("Total return", fmt(s["total_return_pct"], "{:+.2f}%")),
            ("Buy & hold", fmt(s["buy_hold_return_pct"], "{:+.2f}%")),
            ("Max drawdown", fmt(s["max_drawdown_pct"], "{:.2f}%")),
            ("Sharpe (ann.)", fmt(s["sharpe"])),
            ("Round trips", s["round_trips"]), ("Win rate", fmt(s["win_rate"], "{:.1f}%")),
            ("Avg win / loss", f"{fmt(s['avg_win'])} / {fmt(s['avg_loss'])}"),
            ("Profit factor", fmt(s["profit_factor"])), ("Avg hold (min)", fmt(s["avg_hold_min"], "{:.1f}")),
            ("Exposure", fmt(s["exposure_pct"], "{:.1f}%")), ("Fees", f"{s['fees']:,.2f}"),
            ("Exits", ", ".join(f"{k}: {v}" for k, v in s["exit_reasons"].items()))]
    for k, v in rows:
        print(f"  {k:<16}{v}")
    for msg in s["warnings"]:
        print(f"  WARNING: {msg}")


if __name__ == "__main__":
    main()
