"""Control the bot and answer trade approvals from your phone through a private Telegram bot.

Setup (README > "Phone control"): make a bot with @BotFather, put TELEGRAM_BOT_TOKEN in .env,
restart, message the bot once to learn your chat ID, put TELEGRAM_CHAT_ID in .env, restart.

Only chats listed in TELEGRAM_CHAT_ID are obeyed; everyone else is ignored. The bot polls
Telegram (outbound HTTPS only), so nothing on this PC is opened to the internet.
"""
import asyncio
import html
import os
import time
from datetime import datetime
from pathlib import Path

import requests

START_BTN, STOP_BTN, STATUS_BTN, POS_BTN = "▶️ Start trading", "⏸ Stop trading", "📊 Status", "📋 Positions"
KEYBOARD = {"keyboard": [[START_BTN, STOP_BTN], [STATUS_BTN, POS_BTN]], "resize_keyboard": True, "is_persistent": True}
COMMANDS = [("status", "Account, P&L and what the bot is doing"), ("go", "Start trading"),
            ("stop", "Stop trading (keeps positions)"), ("positions", "Open positions"),
            ("flatten", "Sell everything and stop (asks to confirm)"), ("help", "What I can do")]
OUTCOME = {"approved": "✅ Approved and sent", "rejected": "❌ Rejected", "expired": "⌛ Expired (no answer)",
           "stale": "⚠️ Not sent: price moved", "cancelled": "🚫 Cancelled", "failed": "⚠️ Failed",
           "superseded": "🚫 Superseded by a stop loss / take profit"}


def esc(v):
    return html.escape(str(v if v is not None else ""), quote=False)


def num(v, d=2):
    return "–" if v is None else f"{v:,.{d}f}"


def pct(v):
    return "–" if v is None else f"{v:+.2f}%"


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


class TelegramError(Exception):
    pass


NOTICE_FLAG = Path(__file__).resolve().parent.parent / "logs" / ".startup_problem_sent"


def notify_startup_problem(text):
    """Tell the phone the server can't start (e.g. IB Gateway isn't logged in). Sent once until the
    server starts properly again, so the auto-restart loop doesn't send one every minute."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    ids = [x for x in os.getenv("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()]
    if not token or not ids or NOTICE_FLAG.exists():
        return
    api = os.getenv("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/")
    for c in ids:
        try:
            requests.post(f"{api}/bot{token}/sendMessage", timeout=10,
                          json={"chat_id": int(c), "text": f"🔴 Trading bot can't start:\n{text}\n\nIt retries every minute."})
        except Exception:
            return
    NOTICE_FLAG.parent.mkdir(exist_ok=True)
    NOTICE_FLAG.write_text(str(time.time()))


class TelegramBot:
    def __init__(self, engine, token, chat_ids, api="https://api.telegram.org"):
        self.engine, self.token, self.chat_ids = engine, token, chat_ids
        self.base = f"{api.rstrip('/')}/bot{token}/"
        self.sent = {}           # request id -> {"chat": message_id, ..., "ai": ai_status when last drawn}
        self.last_trade_t = time.time()
        self.was_halted = None
        self.recent_errors = {}  # message -> time last sent, so a repeating error isn't spammed
        self.log_seen = time.time()

    @classmethod
    def from_env(cls, engine):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            return None
        ids = {int(x) for x in os.getenv("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
        return cls(engine, token, ids, os.getenv("TELEGRAM_API_URL", "https://api.telegram.org"))

    # ---------- Telegram API ----------
    def _call_sync(self, method, params, timeout):
        try:
            r = requests.post(self.base + method, json=params, timeout=timeout)
            data = r.json()
        except Exception as e:  # never let the token (it's in the URL) reach the logs
            raise TelegramError(f"{method}: {e.__class__.__name__}: {str(e).replace(self.token, '***')}") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', r.status_code)}")
        return data.get("result")

    async def call(self, method, timeout=15, **params):
        return await asyncio.to_thread(self._call_sync, method, params, timeout)

    async def quiet(self, method, **params):
        """For edits and acknowledgements, where failure (e.g. 'message is not modified') doesn't matter."""
        try:
            return await self.call(method, **params)
        except TelegramError:
            return None

    async def send(self, text, chat=None, **extra):
        out = []
        for c in ([chat] if chat else self.chat_ids):
            try:
                m = await self.call("sendMessage", chat_id=c, text=text, parse_mode="HTML",
                                    disable_web_page_preview=True, **extra)
                out.append((c, m["message_id"]))
            except TelegramError as e:
                self.engine.log(f"Telegram: couldn't send message ({e})", "warn")
        return out

    # ---------- main ----------
    async def run(self):
        try:
            me = await self.call("getMe")
            await self.quiet("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        except TelegramError as e:
            self.engine.log(f"Telegram disabled: {e}. Check TELEGRAM_BOT_TOKEN in .env.", "error")
            return
        NOTICE_FLAG.unlink(missing_ok=True)
        if self.chat_ids:
            self.engine.log(f"Telegram connected as @{me.get('username')}; approvals go to your phone.")
            await self.send(f"🟢 Trading bot server is up ({esc(self.engine.broker.name.upper())} mode). "
                            f"Trading is {'ON' if self.engine.running else 'stopped'}.\n"
                            f"Tap <b>{START_BTN}</b> when you're ready.", reply_markup=KEYBOARD)
        else:
            self.engine.log(f"Telegram: open @{me.get('username')} on your phone and send it any message to "
                            "get your chat ID, then set TELEGRAM_CHAT_ID in .env and restart.", "warn")
        await asyncio.gather(self._poll(), self._watch())

    async def _poll(self):
        offset, delay = None, 2
        while True:
            try:
                # long poll: Telegram holds the request up to 25s until something arrives
                ups = await asyncio.to_thread(self._call_sync, "getUpdates", {
                    "offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]}, 40)
                delay = 2
            except TelegramError as e:
                self.engine.log(f"Telegram: {e} (retrying in {delay}s)", "warn")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            for u in ups or []:
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        await self._on_button(u["callback_query"])
                    elif "message" in u:
                        await self._on_message(u["message"])
                except Exception as e:
                    self.engine.log(f"Telegram: error handling an update: {e}", "error")

    # ---------- incoming ----------
    async def _on_message(self, m):
        chat = m.get("chat", {}).get("id")
        text = (m.get("text") or "").strip()
        if not self.chat_ids:
            await self.send(f"Your chat ID is <code>{chat}</code>.\nAdd this line to the bot's .env file and restart it:\n"
                            f"<code>TELEGRAM_CHAT_ID={chat}</code>", chat=chat)
            return
        if chat not in self.chat_ids:
            self.engine.log(f"Telegram: ignored a message from an unknown chat ({chat})", "warn")
            return
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        e = self.engine
        if cmd in ("/go", "/run") or text == START_BTN:
            try:
                e.start("phone")
                await self.send("▶️ Trading started. New buys will come here for approval."
                                if e.settings.require_approval else "▶️ Trading started. ⚠️ Approvals are OFF: the bot buys without asking.",
                                chat=chat, reply_markup=KEYBOARD)
            except ValueError as err:
                await self.send(f"⚠️ {esc(err)}", chat=chat)
        elif cmd == "/stop" or text == STOP_BTN:
            e.stop("phone")
            await self.send("⏸ Trading stopped. Open positions are kept, but while stopped the bot doesn't watch "
                            "their stop losses or take profits. Use /flatten to sell everything.",
                            chat=chat, reply_markup=KEYBOARD)
        elif cmd == "/status" or text == STATUS_BTN:
            await self.send(self._status(), chat=chat, reply_markup=KEYBOARD)
        elif cmd == "/positions" or text == POS_BTN:
            await self.send(self._positions(), chat=chat, reply_markup=KEYBOARD)
        elif cmd == "/flatten":
            await self.send("Sell <b>every</b> position at market and stop trading?", chat=chat, reply_markup={
                "inline_keyboard": [[{"text": "Yes, sell everything", "callback_data": "fl:yes"},
                                     {"text": "No", "callback_data": "fl:no"}]]})
        else:
            await self.send("<b>Trading bot</b>\n" + "\n".join(f"/{c} - {d}" for c, d in COMMANDS) +
                            "\n\nApproval requests arrive here with Approve / Reject buttons.",
                            chat=chat, reply_markup=KEYBOARD)

    async def _on_button(self, q):
        msg = q.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        if chat not in self.chat_ids or q.get("from", {}).get("id") not in self.chat_ids | {chat}:
            await self.quiet("answerCallbackQuery", callback_query_id=q["id"], text="Not allowed")
            return
        kind, _, arg = (q.get("data") or "").partition(":")
        e = self.engine
        if kind == "fl":
            if arg == "yes":
                await self.quiet("answerCallbackQuery", callback_query_id=q["id"], text="Selling everything…")
                await self.quiet("editMessageText", chat_id=chat, message_id=msg["message_id"], text="Flattening…")
                e.running = False
                await e.flatten()
                e.log("Stopped and flattened all positions (from phone)", "warn")
                await self.quiet("editMessageText", chat_id=chat, message_id=msg["message_id"],
                                 text="✅ Sold everything and stopped trading.\n\n" + self._positions(), parse_mode="HTML")
            else:
                await self.quiet("answerCallbackQuery", callback_query_id=q["id"], text="Kept positions")
                await self.quiet("editMessageText", chat_id=chat, message_id=msg["message_id"], text="Flatten cancelled.")
            return
        if kind not in ("ap", "rj"):
            await self.quiet("answerCallbackQuery", callback_query_id=q["id"])
            return
        try:
            if kind == "ap":
                r = await e.approve(arg)
                note = f"Sent: {r['side'].upper()} {r['symbol']}"
            else:
                e.reject(arg)
                note = "Rejected"
        except ValueError as err:
            note = str(err)
        await self.quiet("answerCallbackQuery", callback_query_id=q["id"], text=note[:190], show_alert=kind == "ap" and not note.startswith("Sent"))
        await self._sync_requests()  # redraw the message with the outcome right away

    # ---------- outgoing ----------
    async def _watch(self):
        while True:
            try:
                await self._sync_requests()
                await self._notify_events()
            except Exception as e:
                self.engine.log(f"Telegram watcher: {e}", "error")
            await asyncio.sleep(1)

    async def _sync_requests(self):
        e = self.engine
        if not self.chat_ids:
            return
        for rid, r in list(e.pending.items()):
            info = self.sent.get(rid)
            if info is None:
                # wait up to 10s for the Claude summary so the first message is the complete one
                if r.get("ai_status") == "working" and e.now() - r["created"] < 10:
                    continue
                self.sent[rid] = {"msgs": await self.send(self._request_text(r), reply_markup=self._buttons(rid)),
                                  "ai": r.get("ai_status")}
            elif info["ai"] != r.get("ai_status"):
                info["ai"] = r.get("ai_status")
                for chat, mid in info["msgs"]:
                    await self.quiet("editMessageText", chat_id=chat, message_id=mid, text=self._request_text(r),
                                     parse_mode="HTML", reply_markup=self._buttons(rid))
        for rid in [k for k in self.sent if k not in e.pending]:
            info = self.sent.pop(rid)
            r = next((x for x in e.approval_log if x["id"] == rid), None)
            if not r:
                continue
            tail = f"\n\n<b>{OUTCOME.get(r['status'], r['status'])}</b>" + (f" · {esc(r['note'])}" if r.get("note") else "")
            for chat, mid in info["msgs"]:
                await self.quiet("editMessageText", chat_id=chat, message_id=mid, parse_mode="HTML",
                                 text=self._request_text(r, final=True) + tail)

    async def _notify_events(self):
        e = self.engine
        if not self.chat_ids:
            return
        new = [t for t in e.trades if t["t"] > self.last_trade_t]
        if new:
            self.last_trade_t = max(t["t"] for t in new)
            base = e.account.get("currency", "")
            for t in reversed(new):
                pnl = ""
                if t.get("pnl_base") is not None:
                    pnl = f"\nP&L <b>{t['pnl_base']:+,.2f} {esc(base)}</b>"
                icon = "🟢" if t["side"] == "buy" else "🔴"
                await self.send(f"{icon} <b>Filled: {t['side'].upper()} {t['qty']} {esc(t['symbol'])}</b> @ "
                                f"{num(t['price'])} {esc(t['currency'])}\n{esc(t['reason'])}{pnl}")
        if e.halted != self.was_halted:
            if e.halted:
                await self.send(f"🛑 <b>{esc(e.halted)}</b>\nClear the halt on the dashboard to trade again.")
            self.was_halted = e.halted
        now = time.time()
        for entry in reversed([x for x in e.logs if x["t"] > self.log_seen]):
            if entry["level"] == "error" and now - self.recent_errors.get(entry["msg"], 0) > 600:
                self.recent_errors[entry["msg"]] = now
                if not entry["msg"].startswith("Telegram"):
                    await self.send(f"⚠️ {esc(entry['msg'])}")
        if e.logs:
            self.log_seen = max(self.log_seen, e.logs[0]["t"])

    # ---------- text ----------
    @staticmethod
    def _buttons(rid):
        return {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"ap:{rid}"},
                                     {"text": "❌ Reject", "callback_data": f"rj:{rid}"}]]}

    def _request_text(self, r, final=False):
        f = r.get("facts", {})
        o, risk, pos = f.get("order", {}), f.get("risk"), f.get("position")
        acct, mv = f.get("account", {}), f.get("recent_moves_pct", {})
        base = o.get("account_currency", "")
        icon = "🟢" if r["side"] == "buy" else "🔴"
        lines = [f"{icon} <b>{'Approve ' if not final else ''}{r['side'].upper()} {r['qty']} {esc(r['symbol'])}</b> "
                 f"@ {num(r['price'])} {esc(r['currency'])}",
                 f"≈ {num(o.get('value_in_account_currency'))} {esc(base)}"
                 + (f" ({o['pct_of_equity']}% of equity)" if o.get("pct_of_equity") is not None else ""),
                 "", f"<b>Why ({esc(f.get('strategy', {}).get('name', r['reason']))})</b>"]
        lines += [f"• {esc(x)}" for x in f.get("strategy", {}).get("reasons", [])] or [f"• {esc(r['reason'])}"]
        if risk:
            lines += ["", "<b>Risk</b>",
                      f"• Stop {num(risk['stop_loss'])} ({risk['stop_loss_pct']}%) · target {num(risk['take_profit'])} (+{risk['take_profit_pct']}%)",
                      f"• Loss if stopped: about <b>{num(risk['loss_at_stop_in_account_currency'])} {esc(base)}</b>"]
            if risk.get("currency_note"):
                lines.append(f"• {esc(risk['currency_note'])}")
        elif pos:
            lines += ["", "<b>Position</b>", f"• Bought at {num(pos['avg_cost'])}, now {pct(pos['change_pct'])}",
                      f"• Unrealized {num(pos['unrealized_in_account_currency'])} {esc(base)}"]
        lines += ["", "<b>Context</b>",
                  f"• Moves: 5 bars {pct(mv.get('last_5_bars'))}, 15 {pct(mv.get('last_15_bars'))}, 60 {pct(mv.get('last_60_bars'))}",
                  f"• Positions {acct.get('positions_used')}/{acct.get('max_positions')} · today {pct(acct.get('day_pnl_pct'))} "
                  f"(halts at {acct.get('daily_loss_limit_pct')}%)"]
        if r.get("ai_status") == "working":
            lines += ["", "<i>Claude is writing a summary…</i>"]
        elif r.get("ai"):
            lines += ["", (f"<b>Claude:</b> {esc(r['ai'])}" if r.get("ai_status") == "done" else f"<i>{esc(r['ai'])}</i>")]
        if not final:
            lines += ["", f"⏳ Expires at {clock(r['expires'])}"]
        return "\n".join(lines)

    def _status(self):
        e = self.engine
        a, base = e.account, e.account.get("currency", "")
        eq = a.get("equity", 0)
        start = e.day_start_equity or eq or 1
        state = ("🛑 HALTED: " + esc(e.halted)) if e.halted else ("▶️ Trading" if e.running else "⏸ Stopped")
        return "\n".join([
            f"<b>{esc(e.broker.name.upper())}</b> · {state}",
            f"Market {'open' if e.market_open else 'closed'} · strategy {esc(e.settings.strategy)} · "
            f"approvals {'on' if e.settings.require_approval else 'OFF'}",
            f"Equity {num(eq)} {esc(base)} · today {num(eq - start)} ({pct((eq - start) / start * 100)})",
            f"Cash {num(a.get('cash'))} · buying power {num(a.get('buying_power'))}",
            f"Positions {len(e.positions)}/{e.settings.max_positions} · waiting for approval {len(e.pending)}",
        ])

    def _positions(self):
        e = self.engine
        if not e.positions:
            return "No open positions."
        base = e.account.get("currency", "")
        out = ["<b>Open positions</b>"]
        for sym, p in e.positions.items():
            fx = e._fx(sym) or 1.0
            chg = (p["price"] - p["avg"]) / p["avg"] * 100 if p["avg"] else 0
            out.append(f"• {esc(sym)} {p['qty']} @ {num(p['avg'])} → {num(p['price'])} ({pct(chg)}) · "
                       f"{num(p['unrealized'] * fx)} {esc(base)}")
        return "\n".join(out)
