"""Download 1-minute bars from Interactive Brokers into data/<SYMBOL>.csv for the backtester.

    python -m bot.ib_download --symbols RY.TO,TD.TO,SHOP.TO --days 60

Needs TWS / IB Gateway running (paper login is fine, read-only is enough) and market data
permissions for the exchanges you request. Re-running merges with existing files.
"""
import argparse
import asyncio
import csv
import math
from datetime import datetime, timezone

from ib_async import IB, Forex

from .backtest import DATA_DIR
from .config import Connection
from .backtest import FX_FILE
from .fx import symbol_currency
from .ibkr import QUIET_CODES, to_contract

CHUNK_DAYS = 5  # trading days per request; keeps each 1-min request well inside IB's limits


def _load(path):
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {int(datetime.fromisoformat(r["timestamp"]).timestamp()): r for r in csv.DictReader(f)}


async def download(symbols, days, rth=True, host=None, port=None, client_id=None, fx=True):
    conn = Connection()
    ib = IB()
    ib.errorEvent += lambda rid, code, msg, c=None, *_: code in QUIET_CODES or print(f"  IBKR {code}: {msg}")
    await ib.connectAsync(host or conn.ib_host, port or conn.ib_port, clientId=(client_id or conn.ib_client_id) + 100,
                          timeout=10, readonly=True)
    DATA_DIR.mkdir(exist_ok=True)
    try:
        jobs = [(sym, to_contract(sym), "TRADES", rth) for sym in symbols]
        if fx and any(symbol_currency(s) == "USD" for s in symbols):
            # CAD per USD, all hours (FX trades ~24h), for converting US positions in backtests
            jobs.append((FX_FILE, Forex("USDCAD"), "MIDPOINT", False))
        for sym, contract, what, use_rth in jobs:
            q = await ib.qualifyContractsAsync(contract)
            if not q or q[0] is None or not q[0].conId:
                print(f"{sym}: not recognised by IBKR, skipped")
                continue
            c, rows, end = q[0], {}, ""
            for i in range(math.ceil(days / CHUNK_DAYS)):
                bars = await ib.reqHistoricalDataAsync(c, end, f"{CHUNK_DAYS} D", "1 min", what,
                                                       useRTH=use_rth, formatDate=2, timeout=120)
                if not bars:
                    break
                for b in bars:
                    ts = int(b.date.timestamp())
                    rows[ts] = {"timestamp": b.date.astimezone(timezone.utc).isoformat(), "open": b.open,
                                "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
                end = bars[0].date
                print(f"\r{sym}: {len(rows):,} bars back to {end:%Y-%m-%d}", end="", flush=True)
                await asyncio.sleep(2)  # stay clear of IB's historical-data pacing limits
            print()
            path = DATA_DIR / f"{sym}.csv"
            merged = _load(path) | rows
            if not merged:
                print(f"{sym}: no data returned (check market data permissions)")
                continue
            with path.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
                w.writeheader()
                for ts in sorted(merged):
                    w.writerow(merged[ts])
            print(f"{sym}: wrote {len(merged):,} bars to {path.name}")
    finally:
        ib.disconnect()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", required=True, help="comma separated, e.g. RY.TO,TD.TO,SHOP.TO")
    ap.add_argument("--days", type=int, default=30, help="trading days of history (default 30)")
    ap.add_argument("--all-hours", action="store_true", help="include pre/post-market bars")
    ap.add_argument("--port", type=int, help="override IB_PORT")
    ap.add_argument("--no-fx", action="store_true", help="don't fetch USD/CAD history when US symbols are included")
    a = ap.parse_args()
    try:
        asyncio.run(download([s.strip().upper() for s in a.symbols.split(",") if s.strip()], a.days,
                             rth=not a.all_hours, port=a.port, fx=not a.no_fx))
    except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
        raise SystemExit(f"Can't reach TWS / IB Gateway ({e.__class__.__name__}). Is it running and logged in, "
                         "with the API enabled and IB_PORT set correctly?")


if __name__ == "__main__":
    main()
