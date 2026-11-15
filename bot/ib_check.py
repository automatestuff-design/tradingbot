"""Read-only check that the bot can talk to IB Gateway / TWS. Never places orders.

    python -m bot.ib_check
    python -m bot.ib_check --symbols RY.TO,AAPL --port 4002

Checks, in order: the API port answers, which account is logged in (paper or live),
balances, positions, that symbols resolve, and that 1-minute bars come back (market data).
"""
import argparse
import asyncio
import sys

from ib_async import IB, Forex

from .config import Connection
from .ibkr import QUIET_CODES, to_contract

OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"


async def check(symbols, host, port, client_id):
    conn = Connection()
    host, port = host or conn.ib_host, port or conn.ib_port
    errors = []
    ib = IB()
    ib.errorEvent += lambda rid, code, msg, c=None, *_: code in QUIET_CODES or errors.append((code, msg))

    print(f"Connecting to {host}:{port} (read-only, client id {client_id}) ...")
    try:
        await ib.connectAsync(host, port, clientId=client_id, timeout=10, readonly=True)
    except Exception as e:
        print(f"{BAD} No answer on {host}:{port} ({e.__class__.__name__}).")
        print("       Is IB Gateway running and logged in? In Gateway: Configure > Settings > API > Settings,")
        print("       tick 'Enable ActiveX and Socket Clients', socket port 4002 (paper), and add 127.0.0.1 to Trusted IPs.")
        return False
    print(f"{OK} API connection")
    good = True
    try:
        accounts = ib.managedAccounts()
        acct = conn.ib_account or (accounts[0] if accounts else "")
        if conn.ib_account and conn.ib_account not in accounts:
            print(f"{BAD} IB_ACCOUNT={conn.ib_account} isn't on this login; it has {accounts}")
            return False
        paper = acct.startswith("D")
        print(f"{OK} Logged in to {'PAPER' if paper else 'LIVE'} account {acct}"
              + (f" (login has {len(accounts)} accounts)" if len(accounts) > 1 else ""))
        if conn.mode == "paper" and not paper:
            print(f"{BAD} .env says MODE=paper but this is a LIVE account. Log out and use your paper username.")
            good = False

        vals = {v.tag: v for v in await ib.accountSummaryAsync(acct)}
        def show(tag):
            v = vals.get(tag)
            return f"{float(v.value):,.2f} {v.currency}" if v and v.value not in ("", None) else "n/a"
        print(f"{OK} Net liquidation {show('NetLiquidation')}, cash {show('TotalCashValue')}, "
              f"available funds {show('AvailableFunds')}")

        pos = [p for p in ib.portfolio(acct) if p.position]
        print(f"{OK} {len(pos)} open position(s)" + "".join(
            f"\n       {p.contract.symbol} {p.position:g} @ {p.averageCost:.2f} {p.contract.currency}" for p in pos))

        for sym in symbols:
            errors.clear()
            q = await ib.qualifyContractsAsync(to_contract(sym))
            if not q or q[0] is None or not q[0].conId:
                print(f"{BAD} {sym}: IBKR doesn't recognise this symbol")
                good = False
                continue
            c = q[0]
            bars = await ib.reqHistoricalDataAsync(c, "", "1 D", "1 min", "TRADES", useRTH=True,
                                                   formatDate=2, timeout=30)
            if bars:
                b = bars[-1]
                print(f"{OK} {sym}: {len(bars)} one-minute bars, last {b.date:%Y-%m-%d %H:%M} close {b.close} {c.currency}")
            else:
                why = "; ".join(f"{code}: {m}" for code, m in errors) or "no data returned"
                print(f"{BAD} {sym}: no bars ({why})")
                print("       Usually a missing market data subscription. Subscribe in Client Portal (live login):")
                print("       Settings > Market Data Subscriptions, then share them with the paper account.")
                good = False

        fx = await ib.qualifyContractsAsync(Forex("USDCAD"))
        bars = await ib.reqHistoricalDataAsync(fx[0], "", "1 D", "5 mins", "MIDPOINT", useRTH=False,
                                               formatDate=2, timeout=20) if fx and fx[0] else None
        print(f"{OK} USD/CAD {bars[-1].close:.4f}" if bars else f"{WARN} No USD/CAD quote; the bot will fall back to the account's rate")
    finally:
        ib.disconnect()
    return good


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbols", default="RY.TO,AAPL")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--client-id", type=int, default=Connection().ib_client_id + 200)
    a = ap.parse_args()
    syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    ok = asyncio.run(check(syms, a.host, a.port, a.client_id))
    print("\nAll checks passed. Start the bot with: python run.py" if ok else "\nFix the items marked FAIL, then run this again.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
