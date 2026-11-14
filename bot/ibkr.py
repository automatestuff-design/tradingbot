"""Interactive Brokers adapter (TWS or IB Gateway via the socket API, using ib_async).

Symbols:  RY.TO / SHOP.TO -> TSX (CAD)   ABC.V -> TSX Venture (CAD)   AAPL -> US (USD)
          Share classes use a dot: BBD.B.TO -> IB symbol "BBD B" on TSE.

ib_async runs on its own event loop in a background thread; every public method is a plain
blocking call (the engine already runs broker calls in worker threads), so nothing else changes.
"""
import asyncio
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ib_async import IB, Forex, MarketOrder, Stock

EASTERN = ZoneInfo("America/Toronto")
# informational "data farm connection is OK" style codes that aren't worth logging
QUIET_CODES = {2104, 2106, 2107, 2108, 2119, 2158, 2100, 2150}


def to_contract(sym):
    s = sym.strip().upper()
    if s.endswith(".TO"):
        return Stock(s[:-3].replace(".", " "), "SMART", "CAD", primaryExchange="TSE")
    if s.endswith(".V"):
        return Stock(s[:-2].replace(".", " "), "SMART", "CAD", primaryExchange="VENTURE")
    return Stock(s, "SMART", "USD")


def from_contract(c):
    base = c.symbol.replace(" ", ".")
    if c.currency == "CAD":
        return base + (".V" if c.primaryExchange == "VENTURE" else ".TO")
    return base


def parse_liquid_hours(text, tz_id):
    """IB liquidHours, e.g. '20261116:0930-20261116:1600;20261115:CLOSED' -> list of (open_dt, close_dt)."""
    try:
        tz = ZoneInfo(tz_id)
    except Exception:
        tz = EASTERN
    sessions = []
    for seg in filter(None, (text or "").split(";")):
        if "CLOSED" in seg or "-" not in seg:
            continue
        a, b = seg.split("-")
        sessions.append((datetime.strptime(a, "%Y%m%d:%H%M").replace(tzinfo=tz),
                         datetime.strptime(b, "%Y%m%d:%H%M").replace(tzinfo=tz)))
    return sessions


class IBKRBroker:
    default_interval = 15.0

    def __init__(self, host, port, client_id, mode, account=""):
        self.name = mode  # "paper" | "live"
        self.host, self.port, self.client_id, self.want_account = host, port, client_id, account
        self.msgs = deque(maxlen=100)
        self.contracts = {}       # sym -> qualified Contract
        self.subs = {}            # sym -> BarDataList kept up to date by IB
        self.retry_at = {}        # sym -> time after which to retry a failed subscription
        self.sessions = []        # cached trading sessions for market_open()
        self.sessions_at = 0
        self.last_reconnect = 0
        self.base_currency = "CAD"  # replaced by the account's real base currency on first account read
        self.fx_cache = {}        # cur -> (rate, fetched_at)
        self.fx_contracts = {}    # "USDCAD" -> qualified Forex contract (or None)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="ibkr-loop", daemon=True).start()
        self.ib = self._run(self._make_ib())
        self._run(self._connect())

    # ---------- plumbing ----------
    def _run(self, coro, timeout=90):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _make_ib(self):
        ib = IB()
        ib.errorEvent += self._on_error
        return ib

    def _on_error(self, req_id, code, text, contract=None, *_):
        if code in QUIET_CODES:
            return
        who = f" [{from_contract(contract)}]" if contract is not None and getattr(contract, "symbol", "") else ""
        self.msgs.append(("warn" if code >= 2000 else "error", f"IBKR {code}{who}: {text}"))

    def drain_messages(self):
        out = list(self.msgs)
        self.msgs.clear()
        return out

    async def _connect(self):
        try:
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=10)
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            raise ConnectionError(
                f"Can't reach TWS / IB Gateway at {self.host}:{self.port} ({e.__class__.__name__}). "
                "Start it, log in, enable the API (Configure > Settings > API > 'Enable ActiveX and Socket Clients'), "
                "and check IB_PORT (Gateway: 4002 paper / 4001 live; TWS: 7497 paper / 7496 live).") from None
        accounts = self.ib.managedAccounts()
        if self.want_account and self.want_account not in accounts:
            raise ConnectionError(f"IB_ACCOUNT {self.want_account} not found; this login has {accounts}")
        self.account = self.want_account or accounts[0]
        is_paper = self.account.startswith("D")  # IB paper accounts are DU..., live are U...
        if self.name == "paper" and not is_paper:
            self.ib.disconnect()
            raise ConnectionError(f"MODE=paper but the gateway is logged into LIVE account {self.account}. "
                                  "Log in with your paper trading username instead.")
        if self.name == "live" and is_paper:
            self.ib.disconnect()
            raise ConnectionError(f"MODE=live but {self.account} is a paper account.")
        self.ib.reqMarketDataType(1)  # live data; needs TSX / US data subscriptions on the account
        self.subs.clear()
        self.msgs.append(("info", f"Connected to IBKR account {self.account} ({'paper' if is_paper else 'LIVE'})"))

    async def _ensure(self):
        if self.ib.isConnected():
            return
        if time.time() - self.last_reconnect < 30:
            raise ConnectionError("IBKR disconnected; retrying shortly")
        self.last_reconnect = time.time()
        self.msgs.append(("warn", "IBKR connection lost - reconnecting"))
        await self._connect()

    async def _qualify(self, sym):
        if sym in self.contracts:
            return self.contracts[sym]
        c = to_contract(sym)
        q = await self.ib.qualifyContractsAsync(c)
        if not q or q[0] is None or not q[0].conId:
            # some Canadian listings don't resolve via SMART; try the listing exchange directly
            if c.currency == "CAD":
                c = Stock(c.symbol, c.primaryExchange, "CAD")
                q = await self.ib.qualifyContractsAsync(c)
            if not q or q[0] is None or not q[0].conId:
                raise ValueError(f"IBKR doesn't recognise {sym}")
        self.contracts[sym] = q[0]
        return q[0]

    # ---------- broker interface ----------
    def advance(self, symbols):
        pass

    def market_open(self):
        return self._run(self._market_open())

    async def _market_open(self):
        await self._ensure()
        now = datetime.now(timezone.utc)
        if not self.sessions or time.time() - self.sessions_at > 3600:
            try:
                sym = next(iter(self.contracts), None) or "RY.TO"
                det = await self.ib.reqContractDetailsAsync(await self._qualify(sym))
                self.sessions = parse_liquid_hours(det[0].liquidHours, det[0].timeZoneId) if det else []
                self.sessions_at = time.time()
            except Exception as e:
                self.msgs.append(("warn", f"Couldn't fetch trading hours ({e}); using 09:30-16:00 ET"))
        if self.sessions:
            return any(a <= now < b for a, b in self.sessions)
        et = now.astimezone(EASTERN)
        return et.weekday() < 5 and (9, 30) <= (et.hour, et.minute) < (16, 0)

    def get_bars(self, symbols, limit=300):
        return self._run(self._get_bars(symbols, limit))

    async def _get_bars(self, symbols, limit):
        await self._ensure()
        for sym in set(self.subs) - set(symbols):
            self.ib.cancelHistoricalData(self.subs.pop(sym))
        out, cutoff = {}, datetime.now(timezone.utc) - timedelta(minutes=1)
        for sym in symbols:
            if sym not in self.subs:
                if time.time() < self.retry_at.get(sym, 0):
                    continue
                try:
                    c = await self._qualify(sym)
                    bars = await self.ib.reqHistoricalDataAsync(
                        c, "", "2 D", "1 min", "TRADES", useRTH=True, formatDate=2, keepUpToDate=True, timeout=30)
                except Exception as e:
                    bars = None
                    self.msgs.append(("error", f"{sym}: {e}"))
                if not bars:
                    self.retry_at[sym] = time.time() + 120
                    self.msgs.append(("error", f"{sym}: no bar data (market data subscription? symbol?) - retry in 2 min"))
                    continue
                self.subs[sym] = bars
            # only completed bars go to the strategy; the forming bar's close is still the latest price
            bl = self.subs[sym]
            done = [(int(b.date.timestamp()), float(b.close)) for b in bl if b.date <= cutoff]
            out[sym] = done[-limit:]
        return out

    def _last(self, sym):
        bl = self.subs.get(sym)
        return float(bl[-1].close) if bl else None

    def get_account(self):
        return self._run(self._account())

    async def _account(self):
        await self._ensure()
        vals = {v.tag: v for v in await self.ib.accountSummaryAsync(self.account)}
        f = lambda tag: float(vals[tag].value) if tag in vals and vals[tag].value not in ("", None) else 0.0
        if "NetLiquidation" in vals and vals["NetLiquidation"].currency:
            self.base_currency = vals["NetLiquidation"].currency
        return {"equity": f("NetLiquidation"), "cash": f("TotalCashValue"), "buying_power": f("AvailableFunds"),
                "currency": self.base_currency}

    # ---------- currency ----------
    def fx_rates(self, currencies):
        return self._run(self._fx_rates(set(currencies)))

    async def _fx_rates(self, currencies):
        """Base-currency units per 1 unit of each currency. Live FX midpoint (cached 5 min),
        falling back to the account's own ExchangeRate values."""
        await self._ensure()
        base, out = self.base_currency, {}
        acct = {v.currency: v.value for v in self.ib.accountValues(self.account) if v.tag == "ExchangeRate"}
        for cur in currencies:
            if cur == base:
                out[cur] = 1.0
                continue
            rate, at = self.fx_cache.get(cur, (None, 0))
            if not rate or time.time() - at > 300:
                rate = await self._fx_quote(cur, base)
                if not rate:
                    try:
                        rate = float(acct.get(cur) or 0) or None
                    except ValueError:
                        rate = None
                if rate:
                    self.fx_cache[cur] = (rate, time.time())
                else:
                    rate = self.fx_cache.get(cur, (None, 0))[0]  # stale beats none
            if rate:
                out[cur] = rate
            else:
                self.msgs.append(("error", f"No {cur}/{base} exchange rate available"))
        return out

    async def _fx_quote(self, cur, base):
        for pair, invert in ((cur + base, False), (base + cur, True)):
            if pair not in self.fx_contracts:
                q = await self.ib.qualifyContractsAsync(Forex(pair))
                self.fx_contracts[pair] = q[0] if q and q[0] is not None and q[0].conId else None
            c = self.fx_contracts[pair]
            if not c:
                continue
            try:
                bars = await self.ib.reqHistoricalDataAsync(c, "", "2 D", "5 mins", "MIDPOINT",
                                                            useRTH=False, formatDate=2, timeout=20)
            except Exception:
                bars = None
            if bars:
                px = float(bars[-1].close)
                return 1 / px if invert else px
        return None

    def get_positions(self):
        return self._run(self._positions())

    async def _positions(self):
        await self._ensure()
        out = {}
        for it in self.ib.portfolio(self.account):
            if not it.position or it.contract.secType != "STK":
                continue
            sym = from_contract(it.contract)
            self.contracts.setdefault(sym, it.contract)
            qty = int(it.position)
            price = self._last(sym) or float(it.marketPrice)
            avg = float(it.averageCost)
            out[sym] = {"qty": qty, "avg": avg, "price": price, "unrealized": (price - avg) * qty}
        return out

    def submit(self, sym, qty, side):
        return self._run(self._submit(sym, qty, side))

    async def _submit(self, sym, qty, side, wait=20):
        await self._ensure()
        c = await self._qualify(sym)
        order = MarketOrder("BUY" if side == "buy" else "SELL", qty, tif="DAY", account=self.account)
        trade = self.ib.placeOrder(c, order)
        deadline = time.time() + wait
        while not trade.isDone() and time.time() < deadline:
            await asyncio.sleep(0.25)
        st = trade.orderStatus
        if st.status != "Filled":
            if not trade.isDone():
                self.ib.cancelOrder(order)
            why = trade.log[-1].message if trade.log and trade.log[-1].message else st.status
            raise RuntimeError(f"order not filled ({st.status}, filled {st.filled:g}/{qty}): {why}")
        return {"filled_price": float(st.avgFillPrice), "order_id": order.orderId}

    def close_all(self):
        for sym, p in self.get_positions().items():
            self.submit(sym, abs(p["qty"]), "sell" if p["qty"] > 0 else "buy")
