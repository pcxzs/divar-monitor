"""Telegram admin panel.

Everything in config.json is reachable from here: add a Divar link by pasting
it, rename/enable/disable/delete searches, edit any individual filter on a
saved link (max floor, rent, deposit, rooms, elevator, ...), change the check
interval, manage alert recipients and admins, pause the watcher, or trigger a
scan on demand.
"""

import html
import secrets
import threading
import time

import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import log, search_url as su
from .config import MIN_SEARCH_GAP

logger = log.get(__name__)

PENDING_TTL = 600           # abandoned "send me a value" prompts expire
MAX_NAME_LEN = 48
MAX_NOTE_LEN = 200
ASK_COOLDOWN = 3600         # after a decline, before the same user may ask again

INTERVAL_CHOICES = [(300, "5m"), (600, "10m"), (900, "15m"), (1200, "20m"),
                    (1800, "30m"), (2700, "45m"), (3600, "1h"), (7200, "2h")]


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if sec < 10 else f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def kb(rows) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    for row in rows:
        markup.row(*[InlineKeyboardButton(text, callback_data=data) for text, data in row])
    return markup


class AdminPanel:
    def __init__(self, bot: telebot.TeleBot, cfg, state, monitor):
        self.bot = bot
        self.cfg = cfg
        self.state = state
        self.monitor = monitor
        self.pending: dict[tuple, dict] = {}      # (chat_id, user_id) -> prompt context
        self.pending_links: dict[str, str] = {}   # short id -> pasted URL
        self.claim_code = secrets.token_hex(3) if not cfg.has_admins() else ""
        self._lock = threading.Lock()

    # ==================== registration ====================

    def register(self):
        bot = self.bot
        bot.message_handler(commands=["start", "help"])(self.cmd_start)
        bot.message_handler(commands=["admin", "panel", "menu"])(self.cmd_panel)
        bot.message_handler(commands=["id", "myid"])(self.cmd_id)
        bot.message_handler(commands=["claim"])(self.cmd_claim)
        bot.message_handler(commands=["status"])(self.cmd_status)
        bot.message_handler(commands=["add"])(self.cmd_add)
        bot.message_handler(commands=["cancel"])(self.cmd_cancel)
        bot.message_handler(commands=["scan"])(self.cmd_scan)
        bot.message_handler(commands=["ask", "subscribe"])(self.cmd_ask)
        bot.message_handler(commands=["pause"])(self.cmd_pause)
        bot.message_handler(commands=["resume"])(self.cmd_resume)
        bot.message_handler(func=lambda m: True, content_types=["text"])(self.on_text)
        bot.callback_query_handler(func=lambda c: True)(self.on_callback)
        try:
            bot.set_my_commands([
                telebot.types.BotCommand("admin", "Open the admin panel"),
                telebot.types.BotCommand("status", "Watcher status"),
                telebot.types.BotCommand("add", "Add a Divar search link"),
                telebot.types.BotCommand("scan", "Scan every search now"),
                telebot.types.BotCommand("ask", "Ask an admin for listing alerts"),
                telebot.types.BotCommand("id", "Show my Telegram id"),
            ])
        except Exception as e:
            logger.warning(f"set_my_commands failed: {e}")

    # ==================== auth ====================

    def is_admin(self, user_id) -> bool:
        return self.cfg.is_admin(user_id)

    def guard(self, message) -> bool:
        if self.is_admin(message.from_user.id):
            return True
        self._reply(message, "⛔️ Admin only.\n\nYour Telegram id: "
                             f"<code>{esc(message.from_user.id)}</code>\n"
                             "Ask an admin to add it, or set <code>admin_ids</code> in config.json.")
        return False

    def guard_cb(self, call) -> bool:
        if self.is_admin(call.from_user.id):
            return True
        self._answer(call, "⛔️ Admin only", alert=True)
        return False

    # ==================== low-level telegram helpers ====================

    def _reply(self, message, text, markup=None):
        try:
            return self.bot.send_message(message.chat.id, text, parse_mode="HTML",
                                         reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"send_message failed: {e}")

    def _answer(self, call, text="", alert=False):
        try:
            self.bot.answer_callback_query(call.id, text=text[:190], show_alert=alert)
        except Exception:
            pass

    def _render(self, call, screen):
        """Replace the panel message with a screen: (text, markup)."""
        text, markup = screen
        try:
            self.bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                       parse_mode="HTML", reply_markup=markup,
                                       disable_web_page_preview=True)
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" in str(e):
                return
            logger.warning(f"edit_message_text failed: {e}")
            try:
                self.bot.send_message(call.message.chat.id, text, parse_mode="HTML",
                                      reply_markup=markup, disable_web_page_preview=True)
            except Exception as e2:
                logger.warning(f"panel resend failed: {e2}")

    def _show(self, chat_id, screen):
        text, markup = screen
        try:
            return self.bot.send_message(chat_id, text, parse_mode="HTML",
                                         reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"panel send failed: {e}")

    def _prompt(self, call, text, context: dict):
        """Ask the admin to type a value; the answer routes back via `context`."""
        key = (call.message.chat.id, call.from_user.id)
        context["panel_msg_id"] = call.message.message_id
        context["ts"] = time.time()
        with self._lock:
            self.pending[key] = context
        try:
            msg = self.bot.send_message(
                call.message.chat.id,
                f"{text}\n\n<i>Send the value as a message, or /cancel.</i>",
                parse_mode="HTML")
            context["prompt_msg_id"] = msg.message_id
        except Exception as e:
            logger.warning(f"prompt failed: {e}")

    def _clear_pending(self, chat_id, user_id, delete_prompt=True):
        with self._lock:
            ctx = self.pending.pop((chat_id, user_id), None)
        if ctx and delete_prompt and ctx.get("prompt_msg_id"):
            try:
                self.bot.delete_message(chat_id, ctx["prompt_msg_id"])
            except Exception:
                pass
        return ctx

    def _refresh_panel(self, chat_id, msg_id, screen):
        text, markup = screen
        try:
            self.bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML",
                                       reply_markup=markup, disable_web_page_preview=True)
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                self._show(chat_id, screen)
        except Exception:
            self._show(chat_id, screen)

    # ==================== commands ====================

    def cmd_start(self, message):
        uid = message.from_user.id
        if self.is_admin(uid):
            self._show(message.chat.id, self.screen_main())
            return
        if not self.cfg.has_admins():
            self._reply(message,
                        "👋 <b>Divar Watchdog</b> is running but has no admin yet.\n\n"
                        "Send <code>/claim &lt;code&gt;</code> with the code printed in the "
                        "server console to take ownership.\n\n"
                        f"Your Telegram id: <code>{esc(uid)}</code>")
            return
        enabled = self.cfg.enabled_searches()
        self._reply(message,
                    "👋 <b>سلام! Divar Watchdog فعال است.</b>\n\n"
                    f"🔎 جستجوهای فعال: {len(enabled)}\n"
                    f"📦 آگهی‌های دنبال‌شده: {len(self.state)}\n"
                    f"⏱ بازه بررسی: {fmt_duration(self.cfg.get('check_interval', 900))}\n\n"
                    "این ربات آگهی‌های <b>جدید</b> و <b>به‌روز شده</b> را با عکس و تغییرات قیمت می‌فرستد.\n\n"
                    + ("✅ شما در فهرست دریافت‌کنندگان هستید.\n"
                       if str(message.chat.id) in self.cfg.get("telegram_chat_ids", [])
                       else "برای دریافت آگهی‌ها دستور /ask را بفرستید تا ادمین تأیید کند.\n")
                    + f"\nشناسه شما: <code>{esc(uid)}</code>")

    def cmd_panel(self, message):
        if not self.guard(message):
            return
        self._show(message.chat.id, self.screen_main())

    def cmd_id(self, message):
        self._reply(message,
                    f"👤 User id: <code>{esc(message.from_user.id)}</code>\n"
                    f"💬 Chat id: <code>{esc(message.chat.id)}</code>")

    def cmd_claim(self, message):
        if self.cfg.has_admins():
            self._reply(message, "This bot already has an admin.")
            return
        parts = (message.text or "").split()
        if len(parts) < 2 or parts[1].strip() != self.claim_code:
            self._reply(message, "❌ Wrong code. Check the server console for the claim code.")
            return
        uid = str(message.from_user.id)
        self.cfg.set("admin_ids", [uid])
        chat_ids = self.cfg.get("telegram_chat_ids", [])
        if str(message.chat.id) not in chat_ids:
            chat_ids.append(str(message.chat.id))
            self.cfg.set("telegram_chat_ids", chat_ids)
        self.claim_code = ""
        logger.info(f"Admin claimed by user {uid}")
        self._reply(message, "✅ You are now the admin.")
        self._show(message.chat.id, self.screen_main())

    def cmd_status(self, message):
        if not self.guard(message):
            return
        self._show(message.chat.id, self.screen_status())

    def cmd_cancel(self, message):
        ctx = self._clear_pending(message.chat.id, message.from_user.id)
        self._reply(message, "Cancelled." if ctx else "Nothing to cancel.")

    def cmd_add(self, message):
        if not self.guard(message):
            return
        url = su.extract_link(message.text or "")
        if not url:
            self._reply(message, "Usage: <code>/add https://divar.ir/s/...</code>\n"
                                 "Or just paste the link straight into the chat.")
            return
        self.offer_link(message.chat.id, url)

    def cmd_scan(self, message):
        if not self.guard(message):
            return
        self._reply(message, "🔍 Scanning every enabled search now...")
        threading.Thread(target=self._scan_all_and_report, args=(message.chat.id,),
                         daemon=True).start()

    def cmd_pause(self, message):
        if not self.guard(message):
            return
        self.cfg.set("paused", True)
        self._reply(message, "⏸ Paused. No scans until you resume.")

    def cmd_resume(self, message):
        if not self.guard(message):
            return
        self.cfg.set("paused", False)
        self._reply(message, "▶️ Resumed.")

    # ==================== access requests (/ask) ====================

    def cmd_ask(self, message):
        uid, chat_id = str(message.from_user.id), str(message.chat.id)
        if chat_id in self.cfg.get("telegram_chat_ids", []):
            self._reply(message, "✅ شما همین حالا هم آگهی‌ها را دریافت می‌کنید.")
            return
        if not self.cfg.has_admins():
            self._reply(message, "⚠️ هنوز ادمینی برای این ربات تنظیم نشده است.")
            return
        if self.is_admin(uid):
            self._reply(message, "You are an admin — add yourself from "
                                 "/admin → 🔔 Recipients.")
            return

        pending = self.cfg.get("pending_requests", {})
        previous = pending.get(uid) or {}
        if previous.get("status") == "pending":
            self._reply(message, "⏳ درخواست شما ثبت شده و در انتظار تأیید ادمین است.")
            return
        waited = time.time() - float(previous.get("resolved_at") or 0)
        if previous.get("status") == "denied" and waited < ASK_COOLDOWN:
            self._reply(message, f"❌ درخواست قبلی شما رد شد. "
                                 f"{fmt_duration(ASK_COOLDOWN - waited)} دیگر می‌توانید دوباره تلاش کنید.")
            return

        note = (message.text or "").partition(" ")[2].strip()[:MAX_NOTE_LEN]
        user = message.from_user
        display = " ".join(x for x in (user.first_name, user.last_name) if x) or "—"
        record = {
            "user_id": uid, "chat_id": chat_id, "name": display,
            "username": user.username or "", "note": note,
            "ts": time.time(), "status": "pending", "pins": {},
        }
        card = (
            "📥 <b>Access request</b>\n\n"
            f"👤 {esc(display)}"
            + (f" (@{esc(user.username)})" if user.username else "") + "\n"
            f"🆔 <code>{esc(uid)}</code>\n"
            + (f"💬 {esc(note)}\n" if note else "")
            + "\nApprove to start sending them every listing alert."
        )
        markup = kb([[("✅ Accept", f"ask.ok:{uid}"), ("❌ Decline", f"ask.no:{uid}")]])
        delivered = 0
        for admin in self.cfg.get("admin_ids", []):
            msg = self._show(admin, (card, markup))
            if not msg:
                continue
            delivered += 1
            record["pins"][str(admin)] = msg.message_id
            # Pin it so the request does not scroll away unanswered.
            try:
                self.bot.pin_chat_message(admin, msg.message_id, disable_notification=False)
            except Exception as e:
                logger.warning(f"Could not pin access request in {admin}: {e}")
        if not delivered:
            self._reply(message, "⚠️ نتوانستم به ادمین پیام بدهم. بعداً دوباره تلاش کنید.")
            return

        pending[uid] = record
        self.cfg.set("pending_requests", pending)
        logger.info(f"Access request from {uid} ({display}) sent to {delivered} admin(s)")
        self._reply(message, "📨 درخواست شما برای ادمین ارسال شد. پس از تأیید، آگهی‌ها را دریافت می‌کنید.")

    def _resolve_request(self, uid: str, accept: bool, by) -> str:
        """Accept or decline a pending request: update config, unpin every
        admin's copy, rewrite the cards and tell the user."""
        pending = self.cfg.get("pending_requests", {})
        record = pending.get(str(uid))
        if not record:
            return "That request is no longer pending."
        verdict = "accepted" if accept else "denied"
        name = record.get("name") or uid

        if accept:
            chat_ids = self.cfg.get("telegram_chat_ids", [])
            target = str(record.get("chat_id") or uid)
            if target not in chat_ids:
                chat_ids.append(target)
                self.cfg.set("telegram_chat_ids", chat_ids)

        stamp = ("✅ <b>Accepted</b>" if accept else "❌ <b>Declined</b>")
        for admin, msg_id in (record.get("pins") or {}).items():
            try:
                self.bot.unpin_chat_message(admin, msg_id)
            except Exception as e:
                logger.debug(f"unpin failed in {admin}: {e}")
            try:
                self.bot.edit_message_text(
                    f"{stamp} — access request from {esc(name)} "
                    f"(<code>{esc(uid)}</code>)\nHandled by <code>{esc(by)}</code>.",
                    admin, msg_id, parse_mode="HTML", reply_markup=None)
            except Exception as e:
                logger.debug(f"card rewrite failed in {admin}: {e}")

        try:
            self.bot.send_message(
                record.get("chat_id") or uid,
                "✅ درخواست شما تأیید شد؛ از این پس آگهی‌ها را دریافت می‌کنید."
                if accept else "❌ درخواست شما برای دریافت آگهی‌ها پذیرفته نشد.",
                parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Could not notify requester {uid}: {e}")

        record["status"] = verdict
        record["resolved_at"] = time.time()
        record["resolved_by"] = str(by)
        record["pins"] = {}
        pending[str(uid)] = record
        self.cfg.set("pending_requests", pending)
        logger.info(f"Access request {uid} {verdict} by {by}")
        return f"{'Accepted' if accept else 'Declined'} {name}"

    def pending_requests(self) -> list:
        return [r for r in (self.cfg.get("pending_requests", {}) or {}).values()
                if r.get("status") == "pending"]

    # ==================== free text ====================

    def on_text(self, message):
        uid = message.from_user.id
        key = (message.chat.id, uid)
        with self._lock:
            ctx = self.pending.get(key)
            if ctx and time.time() - ctx.get("ts", 0) > PENDING_TTL:
                self.pending.pop(key, None)
                ctx = None
        if ctx:
            if not self.is_admin(uid):
                return
            self._handle_pending(message, ctx)
            return

        url = su.extract_link(message.text or "")
        if url:
            if not self.guard(message):
                return
            self.offer_link(message.chat.id, url)
            return

        if self.is_admin(uid) and getattr(message.chat, "type", "private") == "private":
            self._reply(message, "Send a Divar <code>/s/</code> link to watch it, "
                                 "or open /admin.")

    def _handle_pending(self, message, ctx):
        value = (message.text or "").strip()
        chat_id, uid = message.chat.id, message.from_user.id
        self._clear_pending(chat_id, uid)
        try:
            self.bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        handler = getattr(self, f"_apply_{ctx['kind']}", None)
        if handler is None:
            logger.warning(f"No handler for pending kind {ctx['kind']}")
            return
        try:
            screen, note = handler(ctx, value, chat_id)
        except Exception as e:
            logger.exception(f"pending handler {ctx['kind']} failed: {e}")
            self._show(chat_id, (f"❌ Could not apply that: {esc(e)}", None))
            return
        if note:
            self._show(chat_id, (note, None))
        if screen:
            self._refresh_panel(chat_id, ctx.get("panel_msg_id"), screen)

    # ---------- pending appliers ----------

    def _apply_rename(self, ctx, value, chat_id):
        name = value[:MAX_NAME_LEN].strip()
        if not name:
            return self.screen_search(ctx["sid"]), "❌ Empty name ignored."
        self.cfg.update_search(ctx["sid"], name=name)
        return self.screen_search(ctx["sid"]), f"✏️ Renamed to <b>{esc(name)}</b>."

    def _apply_filter_number(self, ctx, value, chat_id):
        sid, key, edge = ctx["sid"], ctx["key"], ctx["edge"]
        search = self.cfg.find(sid)
        if not search:
            return self.screen_searches(), "❌ Search is gone."
        spec = su.spec_for(key)
        cleared = value.strip() in ("-", "any", "none", "clear", "*")
        num = None
        if not cleared:
            num = su.parse_money_input(value) if spec.get("money") else su.parse_int_input(value)
            if num is None:
                return self.screen_filter(sid, key), "❌ Not a number — nothing changed."
        lo, hi = su.parse_range(su.get_param(search["url"], key))
        if edge == "min":
            lo = None if cleared else str(num)
        else:
            hi = None if cleared else str(num)
        url = su.set_param(search["url"], key, su.join_range(lo, hi))
        self.cfg.update_search(sid, url=url)
        return (self.screen_filter(sid, key),
                f"✅ {esc(spec['label'])} → {esc(su.describe_value(key, su.get_param(url, key)))}")

    def _apply_filter_text(self, ctx, value, chat_id):
        sid, key = ctx["sid"], ctx["key"]
        search = self.cfg.find(sid)
        if not search:
            return self.screen_searches(), "❌ Search is gone."
        new = None if value.strip() in ("-", "clear", "any") else value.strip()
        url = su.set_param(search["url"], key, new)
        self.cfg.update_search(sid, url=url)
        return self.screen_filter(sid, key), f"✅ {esc(key)} → {esc(new or 'removed')}"

    def _apply_custom_filter(self, ctx, value, chat_id):
        from .divar import probe_search
        sid = ctx["sid"]
        search = self.cfg.find(sid)
        if not search:
            return self.screen_searches(), "❌ Search is gone."
        try:
            key, val = su.parse_custom_filter(value)
        except ValueError as e:
            return self.screen_add_filter(sid), f"❌ {esc(e)}"
        url = su.set_param(search["url"], key, val)

        # Divar echoes back every filter it understood, so we can tell the admin
        # straight away whether this category supports the one they just added.
        verdict = ""
        try:
            before = probe_search(search["url"])
            after = probe_search(url)
            if key in after["echoed"]:
                verdict = (f"\n✅ Divar accepted it — {after['count']} listings match "
                           f"(was {before['count']}).")
            else:
                verdict = ("\n⚠️ Divar ignored this filter for this category, so it "
                           "will not narrow anything. It is saved anyway — remove it "
                           "from the filters screen if you don't want it.")
        except Exception as e:
            verdict = f"\n⚠️ Could not verify it with Divar right now ({esc(e)})."

        self.cfg.update_search(sid, url=url)
        return (self.screen_filter(sid, key),
                f"✅ Added <code>{esc(key)}={esc(val or '')}</code>.{verdict}")

    def _count_matches(self, chat_id, sid):
        from .divar import probe_search
        search = self.cfg.find(sid)
        if not search:
            return
        try:
            result = probe_search(search["url"])
        except Exception as e:
            self._show(chat_id, (f"⚠️ Could not reach Divar: {esc(e)}", None))
            return
        applied = sorted(k for k in result["echoed"] if k not in su.STRUCTURAL_PARAMS)
        ignored = [k for k in su.editable_params(search["url"])
                   if k not in result["echoed"] and k != "q"]
        text = (f"🔢 <b>{esc(search['name'])}</b>\n\n"
                f"{result['count']} listings match this link right now.\n\n"
                f"Filters Divar applied: <code>{esc(', '.join(applied))}</code>")
        if ignored:
            text += f"\n⚠️ Ignored by Divar: <code>{esc(', '.join(ignored))}</code>"
        text += ("\n\n<i>Note: rent/deposit are checked locally on both ends of a "
                 "convertible (قابل تبدیل) slider, so scans may surface listings "
                 "whose converted end matches even when Divar's own count excludes them.</i>")
        self._show(chat_id, (text, kb([[("⬅️ Search", f"s:{sid}")]])))

    def _apply_interval(self, ctx, value, chat_id):
        secs = su.parse_int_input(value)
        if secs is None or secs < 60:
            return self.screen_settings(), "❌ Give a number of seconds, at least 60."
        self.cfg.set("check_interval", secs)
        return self.screen_settings(), f"⏱ Interval set to {fmt_duration(secs)}."

    def _apply_pages(self, ctx, value, chat_id):
        n = su.parse_int_input(value)
        if n is None or not 1 <= n <= 20:
            return self.screen_settings(), "❌ Give a number between 1 and 20."
        if ctx.get("sid"):
            self.cfg.update_search(ctx["sid"], max_pages=n)
            return self.screen_search(ctx["sid"]), f"📄 Max pages for this search: {n}."
        self.cfg.set("max_pages_per_search", n)
        return self.screen_settings(), f"📄 Max pages per search: {n}."

    def _apply_proxy(self, ctx, value, chat_id):
        from .config import apply_proxy, mask_proxy
        proxy = "" if value.strip() in ("-", "none", "off", "clear") else value.strip()
        with self.cfg.lock:
            self.cfg.data["proxy"] = proxy
            apply_proxy(self.cfg.data)
            self.cfg.save()
        return (self.screen_settings(),
                f"🌐 Proxy: {esc(mask_proxy(proxy) or 'disabled')}.\n"
                "<i>Divar requests use it immediately; the Telegram connection "
                "picks it up after a restart.</i>")

    def _apply_add_recipient(self, ctx, value, chat_id):
        new = value.strip()
        if not new:
            return self.screen_recipients(), "❌ Empty."
        ids = self.cfg.get("telegram_chat_ids", [])
        if new in ids:
            return self.screen_recipients(), "Already on the list."
        ids.append(new)
        self.cfg.set("telegram_chat_ids", ids)
        return self.screen_recipients(), f"🔔 Added recipient <code>{esc(new)}</code>."

    def _apply_add_admin(self, ctx, value, chat_id):
        new = value.strip()
        if not new.lstrip("-").isdigit():
            return self.screen_admins(), "❌ Admin id must be numeric (use /id in their chat)."
        ids = self.cfg.get("admin_ids", [])
        if new in ids:
            return self.screen_admins(), "Already an admin."
        ids.append(new)
        self.cfg.set("admin_ids", ids)
        return self.screen_admins(), f"👑 Added admin <code>{esc(new)}</code>."

    INT_SETTINGS = ("max_retries", "max_deep_checks")

    def _apply_float_setting(self, ctx, value, chat_id):
        key = ctx["key"]
        try:
            val = float(value.strip().replace(",", "."))
        except ValueError:
            return self.screen_advanced(), "❌ Not a number."
        if val < 0:
            return self.screen_advanced(), "❌ Must be ≥ 0."
        if key in self.INT_SETTINGS:
            val = int(val)
        self.cfg.set(key, val)
        return self.screen_advanced(), f"✅ {esc(key)} = {val}"

    def _apply_prune(self, ctx, value, chat_id):
        days = su.parse_int_input(value)
        if days is None or days < 1:
            return self.screen_advanced(), "❌ Give a number of days ≥ 1."
        removed = self.state.prune(days)
        return self.screen_advanced(), f"🧹 Removed {removed} listings not seen in {days} days."

    # ==================== add-a-link flow ====================

    def offer_link(self, chat_id, url):
        try:
            parsed = su.parse_divar_search_url(url)
        except Exception as e:
            self._show(chat_id, (f"❌ Could not parse that link: {esc(e)}", None))
            return
        pid = secrets.token_hex(3)
        with self._lock:
            self.pending_links[pid] = url
        existing = [s for s in self.cfg.searches() if s.get("url") == url]
        warn = "\n\n⚠️ <i>This exact link is already on the watch list.</i>" if existing else ""
        n_after = len(self.cfg.enabled_searches()) + 1
        slot = max(MIN_SEARCH_GAP, self.cfg.get("check_interval", 900) / n_after)
        text = (
            "🔗 <b>New Divar search link</b>\n\n"
            f"🏙 City: <b>{esc(parsed['city_slug'] or '?')}</b>\n"
            f"🗂 Category: <b>{esc(parsed['api_category'] or '?')}</b>\n\n"
            f"<b>Filters</b>\n<pre>{esc(su.summarize(url))}</pre>\n"
            f"After adding: {n_after} searches, one scan every {fmt_duration(slot)}."
            f"{warn}"
        )
        markup = kb([
            [("🌱 Add (seed quietly)", f"add.ok:{pid}")],
            [("🔔 Add + alert on everything now", f"add.now:{pid}")],
            [("❌ Cancel", f"add.no:{pid}")],
        ])
        self._show(chat_id, (text, markup))

    def _add_link(self, chat_id, pid, seed: bool):
        with self._lock:
            url = self.pending_links.pop(pid, None)
        if not url:
            self._show(chat_id, ("❌ That link expired — paste it again.", None))
            return None
        name = su.auto_name(url, len(self.cfg.searches()) + 1)
        search = self.cfg.add_search(url, name)
        logger.info(f"Added search '{name}' ({search['id']}) via Telegram")
        mode = "seeding quietly" if seed else "alerting on everything found"
        self._show(chat_id, (f"✅ Added <b>{esc(name)}</b> — first scan running ({mode})...", None))
        threading.Thread(target=self._first_scan, args=(chat_id, search["id"], seed),
                         daemon=True).start()
        return search

    def _first_scan(self, chat_id, sid, seed: bool):
        search = self.cfg.find(sid)
        if not search:
            return
        stats = self.monitor.scan_search(search, seed_mode=seed)
        if stats.error:
            self._show(chat_id, (f"⚠️ First scan of <b>{esc(search['name'])}</b> failed: "
                                 f"{esc(stats.error)}", None))
        elif seed:
            self._show(chat_id, (f"🌱 <b>{esc(search['name'])}</b>: {stats.seeded} existing listings "
                                 f"recorded silently. You will be alerted on anything new from now on.", None))
        else:
            self._show(chat_id, (f"🔔 <b>{esc(search['name'])}</b>: {stats.new} alerts sent "
                                 f"({stats.total} listings found).", None))
        self._show(chat_id, self.screen_search(sid))

    def _scan_all_and_report(self, chat_id):
        stats = self.monitor.scan_all()
        self._show(chat_id, (
            f"✅ Scan finished — {stats.total} listings seen, {stats.new} new, "
            f"{stats.updated} updated, {stats.bumps_ignored} bumps ignored, "
            f"{stats.skipped_geo} outside the map area, "
            f"{stats.filtered_price} outside price ranges.", None))

    def _scan_one_and_report(self, chat_id, sid, panel_msg_id=None):
        search = self.cfg.find(sid)
        if not search:
            return
        stats = self.monitor.scan_search(search)
        lines = [f"✅ <b>{esc(search['name'])}</b> scanned in {fmt_duration(stats.duration)}",
                 f"{stats.total} listings · {stats.new} new · {stats.updated} updated "
                 f"· {stats.bumps_ignored} bumps ignored"]
        if stats.skipped_geo:
            lines.append(f"{stats.skipped_geo} skipped (outside the map area)")
        if stats.filtered_price:
            lines.append(f"{stats.filtered_price} skipped (outside rent/deposit ranges, both slider ends checked)")
        if stats.error:
            lines.append(f"⚠️ {esc(stats.error)}")
        for s in stats.samples:
            lines.append(f"• {esc(s)}")
        self._show(chat_id, ("\n".join(lines), None))
        if panel_msg_id:
            self._refresh_panel(chat_id, panel_msg_id, self.screen_search(sid))

    # ==================== screens ====================

    def screen_main(self):
        searches = self.cfg.searches()
        enabled = [s for s in searches if s.get("enabled", True)]
        interval = self.cfg.get("check_interval", 900)
        slot = self.monitor.slot_seconds(len(enabled))
        paused = self.cfg.get("paused", False)
        text = (
            "🛠 <b>Divar Watchdog — admin panel</b>\n\n"
            f"{'⏸ <b>PAUSED</b>' if paused else '🟢 Running'}\n"
            f"🔎 Searches: <b>{len(enabled)}</b> enabled / {len(searches)} saved\n"
            f"⏱ Every search every <b>{fmt_duration(interval)}</b> "
            f"→ one scan every <b>{fmt_duration(slot)}</b>\n"
            f"📦 Tracked listings: <b>{len(self.state)}</b>\n"
            f"🔔 Alert recipients: <b>{len(self.cfg.get('telegram_chat_ids', []))}</b>\n"
            f"🧵 Uptime: {fmt_duration(time.time() - self.monitor.started_at)}"
        )
        waiting = self.pending_requests()
        if waiting:
            text += f"\n📥 <b>{len(waiting)} access request(s) waiting</b>"
        rows = [
            [("🔎 Searches", "nav:searches"), ("⚙️ Settings", "nav:settings")],
            [("📊 Status", "nav:status"), ("🔔 Recipients", "nav:recips")],
            [("▶️ Resume" if paused else "⏸ Pause", "set.pause"),
             ("🔍 Scan all now", "act.scanall")],
            [("➕ Add a search link", "nav:addhelp")],
        ]
        if waiting:
            rows.insert(0, [(f"📥 Access requests ({len(waiting)})", "nav:requests")])
        return text, kb(rows)

    def screen_searches(self):
        searches = self.cfg.searches()
        if not searches:
            text = ("🔎 <b>Searches</b>\n\nNothing saved yet.\n\n"
                    "Paste a Divar map/search link (<code>divar.ir/s/...</code>) "
                    "into this chat to start watching it.")
            return text, kb([[("⬅️ Back", "nav:main")]])
        interval = self.cfg.get("check_interval", 900)
        enabled = [s for s in searches if s.get("enabled", True)]
        slot = self.monitor.slot_seconds(len(enabled))
        lines = ["🔎 <b>Searches</b>", "",
                 f"⏱ {fmt_duration(interval)} split across {len(enabled)} enabled "
                 f"→ one scan every {fmt_duration(slot)}", ""]
        rows = []
        for i, s in enumerate(searches, 1):
            rt = self.monitor.rt(s["id"])
            mark = "✅" if s.get("enabled", True) else "⏸"
            last = f"{fmt_duration(time.time() - rt.last_run)} ago" if rt.last_run else "never"
            lines.append(f"{mark} <b>{i}. {esc(s['name'])}</b>")
            lines.append(f"     last scan: {last} · {rt.last_total} listings · "
                         f"{rt.total_new} new so far")
            if rt.last_error:
                lines.append(f"     ⚠️ {esc(rt.last_error[:80])}")
            rows.append([(f"{mark} {s['name'][:28]}", f"s:{s['id']}")])
        rows.append([("⬅️ Back", "nav:main")])
        return "\n".join(lines), kb(rows)

    def screen_search(self, sid):
        s = self.cfg.find(sid)
        if not s:
            return self.screen_searches()
        rt = self.monitor.rt(sid)
        enabled = s.get("enabled", True)
        upd = self.cfg.notify_updates_for(s)
        pages = self.cfg.max_pages_for(s)
        last = f"{fmt_duration(time.time() - rt.last_run)} ago" if rt.last_run else "never"
        nxt = (fmt_duration(max(0, self.monitor.next_due(sid) - time.time()))
               if enabled and rt.last_run
               else ("waiting for its turn" if enabled else "disabled"))
        text = (
            f"🔎 <b>{esc(s['name'])}</b>\n"
            f"{'✅ Enabled' if enabled else '⏸ Disabled'} · "
            f"📄 {pages} pages · ♻️ updates {'on' if upd else 'off'}\n\n"
            f"<b>Filters</b>\n<pre>{esc(su.summarize(s['url']))}</pre>\n"
            f"🕒 Last scan: {last} ({rt.last_total} listings, {rt.last_new} new, "
            f"{rt.last_updated} updated)\n"
            f"⏭ Next scan in: {nxt}\n"
            f"📈 Lifetime: {rt.runs} scans · {rt.total_new} new · {rt.total_updated} updated"
        )
        if rt.last_error:
            text += f"\n⚠️ {esc(rt.last_error[:200])}"
        markup = kb([
            [("🎛 Edit filters", f"f:{sid}")],
            [("⏸ Disable" if enabled else "✅ Enable", f"s.tog:{sid}"),
             ("✏️ Rename", f"s.name:{sid}")],
            [("🔍 Scan now", f"s.scan:{sid}"), ("🔢 Count matches", f"s.count:{sid}")],
            [("🔗 Show link", f"s.url:{sid}")],
            [(f"📄 Pages: {pages}", f"s.pages:{sid}"),
             (f"♻️ Updates: {'on' if upd else 'off'}", f"s.upd:{sid}")],
            [("🌱 Re-seed (mute existing)", f"s.reseed:{sid}"), ("🗑 Delete", f"s.del:{sid}")],
            [("⬅️ Searches", "nav:searches")],
        ])
        return text, markup

    def screen_filters(self, sid):
        s = self.cfg.find(sid)
        if not s:
            return self.screen_searches()
        url = s["url"]
        keys = su.editable_params(url)
        lines = [f"🎛 <b>Filters — {esc(s['name'])}</b>", "",
                 "Tap a filter to change it. Changes take effect on the next scan.", ""]
        rows = []
        pair = []
        for key in keys:
            spec = su.spec_for(key)
            value = su.describe_value(key, su.get_param(url, key))
            lines.append(f"{spec['emoji']} <b>{esc(spec['label'])}</b>: {esc(value)}")
            pair.append((f"{spec['emoji']} {spec['label'].split(' (')[0]}",
                         f"f.e:{sid}:{su.filter_token(key)}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        if su.has_polygon(url):
            lines += ["", "🗺 The map polygon from the link is kept as-is and re-checked "
                          "on every alert. To change the area, paste a new link."]
        rows.append([("➕ Add a filter", f"f.add:{sid}")])
        rows.append([("⬅️ Back", f"s:{sid}")])
        return "\n".join(lines), kb(rows)

    def screen_filter(self, sid, key):
        s = self.cfg.find(sid)
        if not s:
            return self.screen_searches()
        url, spec = s["url"], su.spec_for(key)
        value = su.get_param(url, key)
        ftok = su.filter_token(key)
        head = (f"{spec['emoji']} <b>{esc(spec['label'])}</b>\n"
                f"Search: {esc(s['name'])}\n\n"
                f"Current: <b>{esc(su.describe_value(key, value))}</b>\n")
        rows = []

        if spec["type"] == su.RANGE:
            lo, hi = su.parse_range(value)
            unit = " " + spec["unit"] if spec.get("unit") else ""
            head += (f"\nMin: <code>{esc(lo or 'any')}</code>{unit}"
                     f"\nMax: <code>{esc(hi or 'any')}</code>{unit}")
            if spec.get("money"):
                head += "\n\n<i>Amounts in Toman. '40' means 40 million.</i>"
            rows.append([("✏️ Set min", f"f.set:{sid}:{ftok}:min"),
                         ("✏️ Set max", f"f.set:{sid}:{ftok}:max")])
            presets = spec.get("presets") or []
            if presets:
                head += f"\n\nQuick max ({esc(spec['label'].split(' (')[0].lower())}):"
                row = []
                for p in presets:
                    row.append((f"≤{p}", f"f.p:{sid}:{ftok}:{p}"))
                    if len(row) == 3:
                        rows.append(row)
                        row = []
                if row:
                    rows.append(row)
                rows.append([("♾ No max", f"f.p:{sid}:{ftok}:none")])
        elif spec["type"] == su.BOOL:
            cur = (value or "").lower()
            rows.append([("✅ Yes" + (" ✓" if cur == "true" else ""), f"f.set:{sid}:{ftok}:on"),
                         ("❌ No" + (" ✓" if cur == "false" else ""), f"f.set:{sid}:{ftok}:off")])
        elif spec["type"] == su.MULTI:
            selected = [v for v in (value or "").replace("|", ",").split(",") if v.strip()]
            head += "\nTap to toggle:"
            row = []
            for i, opt in enumerate(spec.get("options", [])):
                mark = "✅" if opt in selected else "▫️"
                row.append((f"{mark} {opt}", f"f.m:{sid}:{ftok}:{i}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
        else:
            rows.append([("✏️ Set value", f"f.set:{sid}:{ftok}:val")])

        rows.append([("🗑 Remove filter", f"f.set:{sid}:{ftok}:clear")])
        rows.append([("⬅️ Filters", f"f:{sid}")])
        return head, kb(rows)

    def screen_add_filter(self, sid):
        s = self.cfg.find(sid)
        if not s:
            return self.screen_searches()
        present = set(su.editable_params(s["url"]))
        rows, pair = [], []
        for key in su.ADDABLE_ORDER:
            if key in present:
                continue
            spec = su.spec_for(key)
            pair.append((f"{spec['emoji']} {spec['label'].split(' (')[0]}",
                         f"f.new:{sid}:{su.filter_token(key)}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        head = f"➕ <b>Add a filter — {esc(s['name'])}</b>\n\n"
        body = ("Divar supports these on any search link, whether or not the URL "
                "you pasted used them." if rows
                else "Every filter in the built-in list is already on this link.")
        rows.append([("✏️ Custom (key=value)", f"f.custom:{sid}")])
        rows.append([("⬅️ Back", f"f:{sid}")])
        return head + body, kb(rows)

    def screen_requests(self):
        waiting = self.pending_requests()
        lines = ["📥 <b>Access requests</b>", "",
                 "People who sent /ask and want to receive listing alerts.", ""]
        rows = []
        for r in waiting:
            handle = f" (@{esc(r['username'])})" if r.get("username") else ""
            lines.append(f"👤 <b>{esc(r.get('name'))}</b>{handle} — <code>{esc(r.get('user_id'))}</code>")
            if r.get("note"):
                lines.append(f"     💬 {esc(r['note'])}")
            lines.append(f"     ⏳ waiting {fmt_duration(time.time() - float(r.get('ts', 0)))}")
            rows.append([(f"✅ Accept {str(r.get('name'))[:14]}", f"ask.ok:{r['user_id']}"),
                         ("❌ Decline", f"ask.no:{r['user_id']}")])
        if not waiting:
            lines.append("<i>(nothing waiting)</i>")
        rows.append([("🔄 Refresh", "nav:requests"), ("⬅️ Back", "nav:main")])
        return "\n".join(lines), kb(rows)

    def screen_status(self):
        searches = self.cfg.searches()
        enabled = [s for s in searches if s.get("enabled", True)]
        interval = self.cfg.get("check_interval", 900)
        slot = self.monitor.slot_seconds(len(enabled))
        lines = [
            "📊 <b>Status</b>", "",
            f"{'⏸ Paused' if self.cfg.get('paused') else '🟢 Running'} · "
            f"uptime {fmt_duration(time.time() - self.monitor.started_at)}",
            f"⏱ Interval {fmt_duration(interval)} ÷ {len(enabled)} searches "
            f"= one scan every {fmt_duration(slot)}",
            f"📦 Tracked listings: {len(self.state)}",
            f"🔔 Recipients: {len(self.cfg.get('telegram_chat_ids', []))} · "
            f"👑 Admins: {len(self.cfg.get('admin_ids', []))}",
            f"📄 Max pages: {self.cfg.get('max_pages_per_search', 5)} · "
            f"♻️ Update alerts: {'on' if self.cfg.get('notify_updates', True) else 'off'}",
            "",
        ]
        if not searches:
            lines.append("No searches saved.")
        for i, s in enumerate(searches, 1):
            rt = self.monitor.rt(s["id"])
            mark = "✅" if s.get("enabled", True) else "⏸"
            last = f"{fmt_duration(time.time() - rt.last_run)} ago" if rt.last_run else "never"
            nxt = (fmt_duration(max(0, self.monitor.next_due(s["id"]) - time.time()))
                   if s.get("enabled", True) and rt.last_run else "its turn")
            lines.append(f"{mark} <b>{i}. {esc(s['name'])}</b>")
            lines.append(f"     last {last} · next in {nxt} · "
                         f"{rt.last_total} listings · {rt.total_new} new / {rt.total_updated} upd")
            if rt.scanning:
                lines.append("     🔄 scanning right now")
            if rt.last_error:
                lines.append(f"     ⚠️ {esc(rt.last_error[:80])}")
        markup = kb([[("🔄 Refresh", "nav:status"), ("⬅️ Back", "nav:main")]])
        return "\n".join(lines), markup

    def screen_settings(self):
        interval = self.cfg.get("check_interval", 900)
        enabled = len(self.cfg.enabled_searches())
        slot = self.monitor.slot_seconds(enabled)
        upd = self.cfg.get("notify_updates", True)
        paused = self.cfg.get("paused", False)
        from .config import mask_proxy
        proxy = mask_proxy(self.cfg.get("proxy", "")) or "off"
        text = (
            "⚙️ <b>Settings</b>\n\n"
            f"⏱ Check interval: <b>{fmt_duration(interval)}</b>\n"
            f"     with {enabled} enabled searches → a scan every <b>{fmt_duration(slot)}</b>\n"
            f"📄 Max pages per search: <b>{self.cfg.get('max_pages_per_search', 5)}</b>\n"
            f"♻️ Alert on updated listings: <b>{'on' if upd else 'off'}</b>\n"
            f"🌐 Proxy: <code>{esc(proxy)}</code>\n"
            f"{'⏸ Watcher is paused' if paused else '🟢 Watcher is running'}"
        )
        markup = kb([
            [("⏱ Interval", "set.int"), ("📄 Max pages", "set.pages")],
            [(f"♻️ Updates: {'on' if upd else 'off'}", "set.upd"),
             ("▶️ Resume" if paused else "⏸ Pause", "set.pause")],
            [("🔔 Recipients", "nav:recips"), ("👑 Admins", "nav:admins")],
            [("🌐 Proxy", "set.proxy"), ("🔧 Advanced", "nav:advanced")],
            [("⬅️ Back", "nav:main")],
        ])
        return text, markup

    def screen_interval(self):
        interval = self.cfg.get("check_interval", 900)
        enabled = max(1, len(self.cfg.enabled_searches()))
        lines = ["⏱ <b>Check interval</b>", "",
                 "How often every saved search is revisited. The searches are "
                 "spread evenly across it, so only one runs at a time.", "",
                 f"Current: <b>{fmt_duration(interval)}</b> ÷ {enabled} searches "
                 f"= one scan every <b>{fmt_duration(max(MIN_SEARCH_GAP, interval / enabled))}</b>"]
        rows, row = [], []
        for secs, label in INTERVAL_CHOICES:
            mark = "✓ " if secs == interval else ""
            row.append((f"{mark}{label}", f"set.int:{secs}"))
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([("✏️ Custom (seconds)", "set.int:custom")])
        rows.append([("⬅️ Settings", "nav:settings")])
        return "\n".join(lines), kb(rows)

    def screen_recipients(self):
        ids = self.cfg.get("telegram_chat_ids", [])
        lines = ["🔔 <b>Alert recipients</b>", "",
                 "Every new/updated listing is sent to these chats.", ""]
        rows = []
        for cid in ids:
            lines.append(f"• <code>{esc(cid)}</code>")
            rows.append([(f"🗑 {cid}", f"r.del:{cid}")])
        if not ids:
            lines.append("<i>(nobody — alerts go nowhere)</i>")
        waiting = self.pending_requests()
        rows.append([("➕ Add recipient", "r.add")])
        if waiting:
            rows.append([(f"📥 Requests ({len(waiting)})", "nav:requests")])
        rows.append([("⬅️ Back", "nav:main")])
        lines.append("\n<i>Outsiders can send /ask to request access; you get a "
                     "pinned card to accept or decline.</i>")
        return "\n".join(lines), kb(rows)

    def screen_admins(self):
        ids = self.cfg.get("admin_ids", [])
        lines = ["👑 <b>Admins</b>", "",
                 "Admins can use this panel and change every setting.", ""]
        rows = []
        for uid in ids:
            lines.append(f"• <code>{esc(uid)}</code>")
            rows.append([(f"🗑 {uid}", f"a.del:{uid}")])
        rows.append([("➕ Add admin", "a.add")])
        rows.append([("⬅️ Settings", "nav:settings")])
        lines.append("\n<i>Tip: the person can send /id to this bot to get their id.</i>")
        return "\n".join(lines), kb(rows)

    def screen_advanced(self):
        deep = self.cfg.get("deep_update_check", True)
        text = (
            "🔧 <b>Advanced</b>\n\n"
            f"🔬 Verify bumps: <b>{'on' if deep else 'off'}</b> "
            f"(max {self.cfg.get('max_deep_checks', 40)} per scan)\n"
            f"⏳ Delay between pages: <b>{self.cfg.get('page_fetch_delay', 0.8)}s</b>\n"
            f"⏳ Delay between searches (bulk scans): <b>{self.cfg.get('search_delay', 3.0)}s</b>\n"
            f"🔁 Max retries per request: <b>{self.cfg.get('max_retries', 5)}</b>\n"
            f"📦 Tracked listings in state: <b>{len(self.state)}</b>\n\n"
            "<i>A نردبان bump never triggers an alert. With “verify bumps” on, a "
            "bumped listing is opened once to check whether the price, specs or "
            "photos moved behind the bump; with it off, bumps are skipped without "
            "looking. Raising the delays makes Divar less likely to rate-limit you.</i>"
        )
        markup = kb([
            [(f"🔬 Verify bumps: {'on' if deep else 'off'}", "adv.deep"),
             ("🔢 Deep check budget", "adv.budget")],
            [("⏳ Page delay", "adv.page"), ("⏳ Search delay", "adv.search")],
            [("🔁 Retries", "adv.retries"), ("🧹 Prune old state", "adv.prune")],
            [("⬅️ Settings", "nav:settings")],
        ])
        return text, markup

    def screen_addhelp(self):
        text = (
            "➕ <b>Add a search</b>\n\n"
            "1. Open divar.ir, set your filters and map area\n"
            "2. Copy the address bar URL (it looks like "
            "<code>https://divar.ir/s/tehran/rent-apartment?...</code>)\n"
            "3. Paste it into this chat\n\n"
            "The link's filters, map polygon and sort order are used verbatim for "
            "the API calls. Afterwards you can tweak any filter from the panel "
            "without touching the URL."
        )
        return text, kb([[("⬅️ Back", "nav:main")]])

    # ==================== callback router ====================

    def on_callback(self, call):
        if not self.guard_cb(call):
            return
        data = call.data or ""
        try:
            self._route(call, data)
        except Exception as e:
            logger.exception(f"callback {data} failed: {e}")
            self._answer(call, f"Error: {e}", alert=True)

    def _route(self, call, data):
        chat_id = call.message.chat.id
        parts = data.split(":")
        head = parts[0]

        if head == "nav":
            self._answer(call)
            screens = {
                "main": self.screen_main, "searches": self.screen_searches,
                "settings": self.screen_settings, "status": self.screen_status,
                "recips": self.screen_recipients, "admins": self.screen_admins,
                "requests": self.screen_requests,
                "advanced": self.screen_advanced, "addhelp": self.screen_addhelp,
            }
            fn = screens.get(parts[1])
            if fn:
                self._render(call, fn())
            return

        if head == "s":
            self._answer(call)
            self._render(call, self.screen_search(parts[1]))
            return

        if head == "f":
            self._answer(call)
            self._render(call, self.screen_filters(parts[1]))
            return

        if head == "add.ok" or head == "add.now":
            self._answer(call, "Adding...")
            try:
                self.bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            self._add_link(chat_id, parts[1], seed=(head == "add.ok"))
            return

        if head in ("ask.ok", "ask.no"):
            note = self._resolve_request(parts[1], accept=(head == "ask.ok"),
                                         by=call.from_user.id)
            self._answer(call, note)
            self._render(call, self.screen_requests())
            return

        if head == "add.no":
            with self._lock:
                self.pending_links.pop(parts[1], None)
            self._answer(call, "Cancelled")
            self._render(call, ("❌ Link discarded.", kb([[("🛠 Panel", "nav:main")]])))
            return

        if head.startswith("s."):
            self._route_search(call, head, parts)
            return
        if head.startswith("f."):
            self._route_filter(call, head, parts)
            return
        if head.startswith("set."):
            self._route_settings(call, head, parts)
            return
        if head.startswith("adv."):
            self._route_advanced(call, head, parts)
            return
        if head.startswith("r.") or head.startswith("a."):
            self._route_people(call, head, parts)
            return
        if head == "act.scanall":
            self._answer(call, "Scanning every search...")
            threading.Thread(target=self._scan_all_and_report, args=(chat_id,), daemon=True).start()
            return
        if head == "noop":
            self._answer(call)
            return
        self._answer(call, "Unknown action")

    def _route_search(self, call, head, parts):
        sid = parts[1]
        chat_id = call.message.chat.id
        search = self.cfg.find(sid)
        if not search and head != "s.delok":
            self._answer(call, "That search is gone", alert=True)
            self._render(call, self.screen_searches())
            return

        if head == "s.tog":
            new_state = not search.get("enabled", True)
            self.cfg.update_search(sid, enabled=new_state)
            self._answer(call, "Enabled" if new_state else "Disabled")
            self._render(call, self.screen_search(sid))
        elif head == "s.name":
            self._answer(call)
            self._prompt(call, f"✏️ New name for <b>{esc(search['name'])}</b>?",
                         {"kind": "rename", "sid": sid})
        elif head == "s.url":
            self._answer(call)
            self._render(call, (f"🔗 <b>{esc(search['name'])}</b>\n\n"
                                f"<code>{esc(search['url'])}</code>",
                                kb([[("⬅️ Back", f"s:{sid}")]])))
        elif head == "s.count":
            self._answer(call, "Asking Divar...")
            threading.Thread(target=self._count_matches, args=(chat_id, sid),
                             daemon=True).start()
        elif head == "s.scan":
            self._answer(call, "Scanning now...")
            threading.Thread(target=self._scan_one_and_report,
                             args=(chat_id, sid, call.message.message_id), daemon=True).start()
        elif head == "s.pages":
            self._answer(call)
            self._prompt(call, "📄 How many result pages to read per scan? (1-20)",
                         {"kind": "pages", "sid": sid})
        elif head == "s.upd":
            current = self.cfg.notify_updates_for(search)
            self.cfg.update_search(sid, notify_updates=not current)
            self._answer(call, f"Update alerts {'off' if current else 'on'}")
            self._render(call, self.screen_search(sid))
        elif head == "s.reseed":
            self._answer(call, "Re-seeding quietly...")
            threading.Thread(target=self._reseed, args=(chat_id, sid), daemon=True).start()
        elif head == "s.del":
            self._answer(call)
            self._render(call, (f"🗑 Delete <b>{esc(search['name'])}</b>?\n\n"
                                "Tracked listings stay in the state file, so re-adding "
                                "the link will not re-alert them.",
                                kb([[("✅ Yes, delete", f"s.delok:{sid}"),
                                     ("❌ Keep it", f"s:{sid}")]])))
        elif head == "s.delok":
            removed = self.cfg.remove_search(sid)
            self._answer(call, f"Deleted {removed['name']}" if removed else "Already gone")
            self._render(call, self.screen_searches())

    def _reseed(self, chat_id, sid):
        search = self.cfg.find(sid)
        if not search:
            return
        stats = self.monitor.scan_search(search, seed_mode=True)
        self._show(chat_id, (f"🌱 <b>{esc(search['name'])}</b>: {stats.seeded} listings recorded "
                             "without alerts. Only changes from here on will be reported.", None))

    def _route_filter(self, call, head, parts):
        sid = parts[1]
        search = self.cfg.find(sid)
        if not search:
            self._answer(call, "That search is gone", alert=True)
            self._render(call, self.screen_searches())
            return

        if head == "f.add":
            self._answer(call)
            self._render(call, self.screen_add_filter(sid))
            return

        if head == "f.custom":
            self._answer(call)
            self._prompt(call,
                         "✏️ Send the filter as <code>key=value</code>, exactly as it "
                         "appears in a Divar URL.\n\n"
                         "Examples: <code>parking=true</code>, <code>size=60-90</code>, "
                         "<code>building-age=0-10</code>, <code>has-video=true</code>\n\n"
                         "I will ask Divar whether it accepts the filter and tell you "
                         "how many listings match.",
                         {"kind": "custom_filter", "sid": sid})
            return

        key = su.key_from_token(search["url"], parts[2]) if len(parts) > 2 else None
        if not key:
            self._answer(call, "Unknown filter", alert=True)
            self._render(call, self.screen_filters(sid))
            return
        spec = su.spec_for(key)

        if head == "f.e":
            self._answer(call)
            self._render(call, self.screen_filter(sid, key))
        elif head == "f.new":
            # Adding a filter: booleans default to true, everything else prompts.
            if spec["type"] == su.BOOL:
                url = su.set_param(search["url"], key, "true")
                self.cfg.update_search(sid, url=url)
                self._answer(call, f"{spec['label']} = yes")
                self._render(call, self.screen_filter(sid, key))
            else:
                self.cfg.update_search(sid, url=su.set_param(search["url"], key, ""))
                self._answer(call)
                self._render(call, self.screen_filter(sid, key))
        elif head == "f.set":
            what = parts[3]
            if what in ("min", "max"):
                unit = f" ({spec['unit']})" if spec.get("unit") else ""
                extra = ("\nToman — <code>40</code> = 40 million, <code>40m</code>, "
                         "<code>40000000</code> all work." if spec.get("money") else "")
                self._answer(call)
                self._prompt(call, f"✏️ New <b>{what}</b> for {esc(spec['label'])}{esc(unit)}?"
                                   f"{extra}\nSend <code>-</code> for no limit.",
                             {"kind": "filter_number", "sid": sid, "key": key, "edge": what})
            elif what in ("on", "off"):
                url = su.set_param(search["url"], key, "true" if what == "on" else "false")
                self.cfg.update_search(sid, url=url)
                self._answer(call, f"{spec['label']}: {'yes' if what == 'on' else 'no'}")
                self._render(call, self.screen_filter(sid, key))
            elif what == "val":
                self._answer(call)
                self._prompt(call, f"✏️ New value for {esc(spec['label'])}?"
                                   "\nSend <code>-</code> to remove the filter.",
                             {"kind": "filter_text", "sid": sid, "key": key})
            elif what == "clear":
                url = su.set_param(search["url"], key, None)
                self.cfg.update_search(sid, url=url)
                self._answer(call, f"Removed {spec['label']}")
                self._render(call, self.screen_filters(sid))
        elif head == "f.p":
            preset = parts[3]
            lo, _ = su.parse_range(su.get_param(search["url"], key))
            hi = None if preset == "none" else preset
            url = su.set_param(search["url"], key, su.join_range(lo, hi))
            self.cfg.update_search(sid, url=url)
            self._answer(call, f"{spec['label']} max: {hi or 'any'}")
            self._render(call, self.screen_filter(sid, key))
        elif head == "f.m":
            idx = int(parts[3])
            options = spec.get("options", [])
            if not 0 <= idx < len(options):
                self._answer(call, "Bad option")
                return
            opt = options[idx]
            current = [v.strip() for v in (su.get_param(search["url"], key) or "").replace("|", ",").split(",") if v.strip()]
            if opt in current:
                current.remove(opt)
            else:
                current.append(opt)
            ordered = [o for o in options if o in current]
            url = su.set_param(search["url"], key, ",".join(ordered) if ordered else None)
            self.cfg.update_search(sid, url=url)
            self._answer(call, f"{opt}: {'on' if opt in ordered else 'off'}")
            self._render(call, self.screen_filter(sid, key))

    def _route_settings(self, call, head, parts):
        if head == "set.int":
            if len(parts) == 1:
                self._answer(call)
                self._render(call, self.screen_interval())
            elif parts[1] == "custom":
                self._answer(call)
                self._prompt(call, "⏱ Interval in seconds (min 60)?", {"kind": "interval"})
            else:
                self.cfg.set("check_interval", int(parts[1]))
                self._answer(call, f"Interval: {fmt_duration(int(parts[1]))}")
                self._render(call, self.screen_interval())
        elif head == "set.pages":
            self._answer(call)
            self._prompt(call, "📄 Default number of result pages per scan? (1-20)", {"kind": "pages"})
        elif head == "set.upd":
            new = not self.cfg.get("notify_updates", True)
            self.cfg.set("notify_updates", new)
            self._answer(call, f"Update alerts {'on' if new else 'off'}")
            self._render(call, self.screen_settings())
        elif head == "set.pause":
            new = not self.cfg.get("paused", False)
            self.cfg.set("paused", new)
            self._answer(call, "Paused" if new else "Resumed")
            on_main = bool(call.message.text and call.message.text.startswith("🛠"))
            self._render(call, self.screen_main() if on_main else self.screen_settings())
        elif head == "set.proxy":
            self._answer(call)
            self._prompt(call, "🌐 Proxy URL (e.g. <code>http://127.0.0.1:8080</code>)?"
                               "\nSend <code>-</code> to disable.", {"kind": "proxy"})

    def _route_advanced(self, call, head, parts):
        if head == "adv.deep":
            new = not self.cfg.get("deep_update_check", True)
            self.cfg.set("deep_update_check", new)
            self._answer(call, f"Bump verification {'on' if new else 'off'}")
            self._render(call, self.screen_advanced())
            return
        self._answer(call)
        if head == "adv.budget":
            self._prompt(call, "🔢 How many bumped listings may be opened per scan?",
                         {"kind": "float_setting", "key": "max_deep_checks"})
        elif head == "adv.page":
            self._prompt(call, "⏳ Seconds to wait between result pages?",
                         {"kind": "float_setting", "key": "page_fetch_delay"})
        elif head == "adv.search":
            self._prompt(call, "⏳ Seconds to wait between searches in a bulk scan?",
                         {"kind": "float_setting", "key": "search_delay"})
        elif head == "adv.retries":
            self._prompt(call, "🔁 Max retries per request?",
                         {"kind": "float_setting", "key": "max_retries"})
        elif head == "adv.prune":
            self._prompt(call, "🧹 Forget listings not seen for how many days?", {"kind": "prune"})

    def _route_people(self, call, head, parts):
        if head == "r.add":
            self._answer(call)
            self._prompt(call, "🔔 Chat id to add (a user, group or channel id)?",
                         {"kind": "add_recipient"})
        elif head == "r.del":
            ids = [c for c in self.cfg.get("telegram_chat_ids", []) if c != parts[1]]
            self.cfg.set("telegram_chat_ids", ids)
            self._answer(call, f"Removed {parts[1]}")
            self._render(call, self.screen_recipients())
        elif head == "a.add":
            self._answer(call)
            self._prompt(call, "👑 Telegram user id to make admin?", {"kind": "add_admin"})
        elif head == "a.del":
            ids = self.cfg.get("admin_ids", [])
            if len(ids) <= 1:
                self._answer(call, "Cannot remove the last admin", alert=True)
                return
            self.cfg.set("admin_ids", [a for a in ids if a != parts[1]])
            self._answer(call, f"Removed {parts[1]}")
            self._render(call, self.screen_admins())
