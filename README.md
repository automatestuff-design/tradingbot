# Trading Bot + Dashboard

A long-only, short-term (1-minute bar) stock trading bot with a live web dashboard and a backtester. It's built for **Interactive Brokers**, covering TSX, TSX Venture and US stocks.

```
pip install -r requirements.txt
python run.py        # open http://127.0.0.1:8000
```

## Modes (set `MODE` in `.env`; copy `.env.example`)
| Mode | Data | Money |
|---|---|---|
| `sim` (default) | Synthetic random-walk market, 1 bar per second | Fake $100k, in memory |
| `paper` | Real IBKR 1-min bars, checked every 15s | IBKR paper account (`DU...`) |
| `live` | Real | **Real.** Also requires `I_UNDERSTAND_LIVE_TRADING=yes` and a live port |

`BROKER=alpaca` (US only) is still supported, but `ibkr` is the default.

## Interactive Brokers setup
1. **Paper account:** in Client Portal, go to Settings > Paper Trading Account. You get a separate paper username, and its account ID starts with `DU`.
2. **Market data:** buy subscriptions for the exchanges you trade, for example Canadian TSX/TSX-V data and, if needed, US data. Paper accounts share the live account's subscriptions. Without them, bar requests fail and the dashboard log shows the IBKR error.
3. **IB Gateway (recommended) or TWS:** install it, then log in with the paper username.
4. **Enable the API:** go to Configure > Settings > API > Settings and tick "Enable ActiveX and Socket Clients". Untick "Read-Only API" so the bot can place orders. Keep "Allow connections from localhost only" on.
5. Set `MODE=paper` in `.env` and run `python run.py`.

**Ports:** IB Gateway uses 4002 (paper) and 4001 (live); TWS uses 7497 (paper) and 7496 (live). With `IB_PORT=0` the bot picks the Gateway port for the mode.

**Safety checks:**
- `MODE=paper` refuses to run against a live account.
- `MODE=live` refuses to run against a paper account or on a paper port.
- Live mode also needs `I_UNDERSTAND_LIVE_TRADING=yes`.

**Symbols:**
- `RY.TO`, `SHOP.TO` trade on the TSX in CAD.
- `ABC.V` trades on TSX Venture.
- `AAPL` trades in the US in USD.
- Share classes use a dot, so `BBD.B.TO` becomes IB's `BBD B`.

Orders are market orders, valid for the day. The bot waits up to 20 seconds for a fill; if the order isn't filled by then, it's cancelled and the error is logged.

## Currencies (CAD and USD)
Everything is measured in the account's **base currency**, which the bot reads from IBKR and which is CAD for most Canadian accounts:
- **Rates:** IBKR's live USD/CAD midpoint, refreshed every 5 minutes. If that isn't available, the bot falls back to the account's own exchange rate. If neither is available, it skips buys in that currency rather than guess.
- **Sizing:** `position_pct` of equity in CAD is converted to USD before working out the share count. 10% of C$100k at 1.38 buys about US$7,250 of a US stock.
- **P&L:** each trade records P&L in the stock's currency and in CAD. The CAD figure uses the rate at entry and at exit, so it includes any currency gain or loss. Realized P&L, win rate and the dashboard totals are all in CAD.
- **Backtests:** `bot.ib_download` also saves `data/USDCAD.csv` (1-minute midpoints) whenever your list includes a US symbol. The backtester uses that history; without it, it uses the fixed rate you give (`--usdcad`, or the field on the page) and shows a warning. The buy & hold benchmark includes the currency move too. Commissions are charged in the stock's currency.

## Trade approvals
By default the bot **asks before every new buy**. The order waits in the Trade approvals panel at the top of the dashboard with Approve and Reject buttons and a countdown.

Each request explains itself:
- **Why:** the strategy's exact numbers, for example "The 9-bar average (171.12) just crossed above the 21-bar average (170.85)".
- **Risk plan:** stop loss and take profit prices, and roughly how much you'd lose in CAD if the stop is hit.
- **Context:** recent price moves, positions in use, and today's P&L against the daily loss limit.
- **Claude summary (optional):** with `ANTHROPIC_API_KEY` set in `.env` and `pip install anthropic`, Claude adds two or three plain sentences. It only rewords the facts above and points out anything that argues against the trade. It doesn't add news or predictions, and it isn't advice. Without a key, requests still show everything else.

**Rules:**
- Requests expire after `approval_timeout_s` (90s) and are dropped, never filled late.
- When you approve, the bot re-checks first. It won't send a buy if the price rose more than `max_drift_pct` (0.5%) since the request, the market closed, trading halted, or buying power ran out.
- Rejected or expired symbols aren't offered again for at least 5 bars (or `cooldown_bars`, if larger).
- A buy waiting for approval counts toward `max_positions`.
- **Stop loss, take profit, the daily loss limit and Flatten never wait for approval.** Strategy sell signals can also ask first if you tick "Ask before strategy sells".
- Stopping the bot cancels anything waiting.
- "Turn on alerts" shows a desktop notification for each new request while the dashboard is open.

Turn approvals off with "Ask before buying" in Strategy & risk. The backtester never asks.

## Phone control (Telegram)
Run the bot from your iPhone: start and stop trading, check status and positions, and get each approval request as a message with **Approve** and **Reject** buttons. You also get a message for every fill, the daily loss halt, and errors such as a lost IBKR connection.

**One-time setup**
1. Install Telegram on your iPhone. Message **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`. BotFather replies with a token like `123456:ABC...`.
2. Put it in `.env` as `TELEGRAM_BOT_TOKEN=...` and restart the bot (`python run.py`).
3. Open your new bot in Telegram and send it any message. It replies with your chat ID.
4. Put that in `.env` as `TELEGRAM_CHAT_ID=...` and restart. The bot messages you that it's up, with buttons at the bottom of the chat.

**Commands:** ▶️ Start trading (`/go`), ⏸ Stop trading (`/stop`), 📊 Status (`/status`), 📋 Positions (`/positions`), `/flatten` (sells everything after you confirm).

**Keep it running.** The phone controls a server on this PC, so the PC has to be on and awake, with IB Gateway logged in.
- `powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1` starts the server a minute after you log in to Windows and restarts it if it stops. Output goes to `logs\bot.log`. Trading always starts **stopped** until you tap Start. `scripts\remove_autostart.ps1` undoes it.
- If IB Gateway isn't logged in, the server can't start; you get one Telegram message saying so, and it keeps retrying every minute.
- Set Windows to never sleep while plugged in (Settings > System > Power & sleep).
- In IB Gateway, turn on Configure > Settings > Lock and Exit > **Auto restart** so it stays logged in through IBKR's nightly restart. IBKR still asks you to log in again about once a week.

**Security:** the bot only obeys the chat IDs in `TELEGRAM_CHAT_ID` and ignores everyone else. It polls Telegram over outbound HTTPS, so no port on this PC is opened to the internet. Approval details (symbol, size, prices) pass through Telegram's servers. Keep the bot token secret; if it leaks, send `/revoke` to @BotFather and update `.env`.

## How a tick works (`bot/engine.py`)
1. Pull bars, account and positions from the broker.
2. **Daily loss breaker:** if equity is down `max_daily_loss_pct` on the day, it sells everything and halts.
3. **Exits:** stop loss and take profit are checked every tick. The strategy's SELL signal is checked once per new completed bar.
4. **Entries:** a strategy BUY signal on a new bar, for a stock not already held. Each position is `position_pct` of equity, up to `max_positions`. After an exit there's a re-entry cooldown, counted in bars.

Because signals only run on completed bars, live, paper and the backtest all make the same decisions on the same data.

Strategies (`bot/strategies.py`): SMA crossover, RSI mean reversion, breakout. To add one, write a function `(closes, settings) -> "BUY" | "SELL" | None` and register it in `STRATEGIES`.

Every live or paper fill is appended to `trades.jsonl`.

## Backtesting
The backtester replays 1-minute bars through the **same engine**, with fees and slippage folded into fill prices so per-trade P&L is net.

**1. Get real history from IBKR** (Gateway running; a paper login is fine):
```
python -m bot.ib_download --symbols RY.TO,TD.TO,SHOP.TO,ENB.TO --days 60
```
This writes `data/RY.TO.csv` and so on. Re-running merges with existing files. You can also supply your own CSVs: one per symbol, named for the symbol, with `timestamp,close` columns. ISO times without an offset are read as Eastern time.

**2. Run it:**
- **Web:** open http://127.0.0.1:8000/backtest. You can compare runs in the run history, then "Apply these settings to live bot".
- **CLI:**
  ```
  python -m bot.backtest --source csv --symbols RY.TO,TD.TO,SHOP.TO --start 2026-09-01 --end 2026-10-31 \
      --strategy breakout --commission 0.01 --min-commission 1 --set stop_loss_pct=1 --set take_profit_pct=2
  ```
  Set commissions to match your IBKR pricing plan.

`--source synthetic` generates fake trending prices. Use it only to check the plumbing, never to judge a strategy.

Backtest assumptions: fills at the bar close plus slippage, no partial fills, no short selling, flat at the close unless `--hold-overnight`, and the daily-loss halt resets each session.

## Files
- `bot/ibkr.py`: Interactive Brokers adapter (ib_async on a background event loop)
- `bot/ib_download.py`: historical bar downloader for backtests
- `bot/ib_check.py`: read-only connection check (`python -m bot.ib_check`); never places orders
- `bot/fx.py`: currency of each symbol and USD/CAD conversion
- `bot/ai_summary.py`: optional Claude summary for approval requests
- `bot/telegram_bot.py`: phone control and approvals through Telegram
- `scripts/`: start the server at Windows log-in (`install_autostart.ps1`, `start_bot.cmd`)
- `bot/brokers.py`: `SimBroker` and `AlpacaBroker`
- `bot/engine.py`: trading loop, risk checks, dashboard snapshot
- `bot/backtest.py`: replay broker, data loaders, metrics, CLI
- `bot/server.py`: FastAPI JSON API, dashboard and backtest jobs
- `bot/static/`: dashboard and backtest pages

## Known limitations
- Long only. No shorting, options or bracket orders. Stops are checked by the bot, not held at the broker, so they don't protect you if the bot or Gateway is down.
- **Holding USD:** the bot converts amounts for sizing and P&L, but it doesn't exchange cash. In IBKR, buying a US stock without USD cash either borrows USD (margin account) or is rejected (cash accounts, TFSA, RRSP). Convert some CAD to USD in IBKR first if you trade US stocks.
- FX moves don't trigger stops. Stop loss and take profit look at the stock's own price change.
- IB Gateway logs out daily and needs a weekly re-login; the bot reconnects every 30 seconds until it's back.
- Settings changed in the dashboard reset when the server restarts.
- Approvals happen on the dashboard or in Telegram. Requests that nobody answers expire.
- While trading is stopped, the bot does not watch stop losses or take profits on open positions.
- The IBKR adapter has been tested against a simulated IB client, but not yet against a real Gateway.
