"""Owner commands, the /start control panel, status/help, OpenAI connect flow,
toggles, pause/resume, confirmations, and the catch-all fallback handler.

Only the configured owner can use these (func=is_owner_msg). Non-owner direct
DMs fall through to the fallback, which shows the managed-assistant notice.
"""

from __future__ import annotations

import logging

import database
import handlers
import keyboards
import security
import telegram_api
import utils
from handlers import approvals
from services import business_service, memory_service, openai_service

log = logging.getLogger("smartel.owner")

_FALLBACK_CONTENT = ["text", "document", "photo", "voice", "audio", "video", "sticker"]


def register(bot, cfg) -> None:
    def cmd(commands):
        def deco(fn):
            bot.message_handler(commands=commands, func=security.is_owner_msg)(
                lambda message, _fn=fn: _safe(lambda: _fn(message))
            )
            return fn
        return deco

    cmd(["start"])(cmd_start)
    cmd(["help"])(cmd_help)
    cmd(["status"])(cmd_status)
    cmd(["settings"])(cmd_settings)
    cmd(["connect_openai"])(cmd_connect_openai)
    cmd(["disconnect_openai"])(cmd_disconnect_openai)
    cmd(["auto_on"])(lambda m: _set_toggle(m, "auto_reply_enabled", True, "Automatic replies"))
    cmd(["auto_off"])(lambda m: _set_toggle(m, "auto_reply_enabled", False, "Automatic replies"))
    cmd(["approval_on"])(lambda m: _set_toggle(m, "approval_mode_enabled", True, "Approval mode"))
    cmd(["approval_off"])(lambda m: _set_toggle(m, "approval_mode_enabled", False, "Approval mode"))
    cmd(["memory_clear"])(cmd_memory_clear)
    cmd(["pause_chat"])(cmd_pause_chat)
    cmd(["resume_chat"])(cmd_resume_chat)
    cmd(["reset"])(cmd_reset)
    cmd(["cancel"])(cmd_cancel)

    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("panel:"))
    @security.owner_only_callback
    def _panel(call):
        _safe(lambda: on_panel_callback(call))

    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("confirm:"))
    @security.owner_only_callback
    def _confirm(call):
        _safe(lambda: on_confirm_callback(call))


def register_fallback(bot, cfg) -> None:
    """The catch-all message handler. MUST be registered last."""
    @bot.message_handler(func=lambda m: True, content_types=_FALLBACK_CONTENT)
    def _fallback(message):
        _safe(lambda: on_fallback(message))


def _safe(fn) -> None:
    try:
        fn()
    except Exception:
        log.exception("owner command failed")


def _args(message) -> str:
    text = getattr(message, "text", "") or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


# =============================================================================
# Commands
# =============================================================================
def cmd_start(message) -> None:
    auto = database.get_bool("auto_reply_enabled")
    appr = database.get_bool("approval_mode_enabled")
    handlers.bot.send_message(
        message.chat.id,
        "👋 <b>SmarTel control panel</b>\n"
        "Your Telegram Business AI assistant. Use the buttons below or /help.",
        parse_mode="HTML", reply_markup=keyboards.control_panel(auto, appr),
    )


HELP_TEXT = (
    "🤖 <b>SmarTel — Telegram Business AI assistant</b>\n\n"
    "This bot is connected to your Telegram Business account and helps you reply "
    "to customers. It is <b>not</b> a normal chat bot — customers never talk to it "
    "directly.\n\n"
    "<b>Connect it (one-time):</b>\n"
    "1. Make sure Business Mode is enabled for your bot in @BotFather.\n"
    "2. Telegram → <b>Settings → Business → Chatbots</b>.\n"
    "3. Add this bot by its @username, allow it to reply, choose chats, save.\n"
    "4. Message the business account from another account to test.\n\n"
    "<b>Commands</b>\n"
    "/start — control panel\n"
    "/status — bot, OpenAI &amp; connection status\n"
    "/settings — open settings\n"
    "/connect_openai — add/update OpenAI key\n"
    "/disconnect_openai — remove OpenAI key\n"
    "/auto_on /auto_off — toggle automatic replies\n"
    "/approval_on /approval_off — toggle approval-before-send\n"
    "/kb /kb_add /kb_upload /kb_search /kb_list /kb_delete /kb_clear — knowledge base\n"
    "/memory_clear — clear conversation memory\n"
    "/pause_chat &lt;chat_id&gt; · /resume_chat &lt;chat_id&gt; — pause/resume a customer\n"
    "/reset — clear pending approvals/pauses (confirmation)\n"
    "/cancel — cancel a pending input prompt\n\n"
    "<i>Note:</i> /reset only clears transient state. To change secrets/identity, "
    "stop the bot and run <code>python main.py --reset</code> in the terminal.\n\n"
    "<b>Owner takeover:</b> when you reply to a customer yourself, auto-replies for "
    "that chat pause automatically for a while."
)


def cmd_help(message) -> None:
    handlers.bot.send_message(message.chat.id, HELP_TEXT, parse_mode="HTML")


def cmd_status(message) -> None:
    handlers.bot.send_message(message.chat.id, _status_text(), parse_mode="HTML")


def _status_text() -> str:
    st = business_service.get_status()
    lines = ["📊 <b>Status</b>", ""]
    lines.append(f"Auto-reply: <b>{'ON' if st['auto_reply_enabled'] else 'OFF'}</b>")
    lines.append(f"Approval mode: <b>{'ON' if st['approval_mode_enabled'] else 'OFF'}</b>")
    lines.append(f"Monitoring: <b>{'ON' if st['monitoring_mode'] else 'OFF'}</b>")
    lines.append(f"OpenAI: <b>{'connected' if openai_service.has_api_key() else 'not connected'}</b> "
                 f"({utils.escape(openai_service.masked_active_key())})")
    lines.append(f"Model: <code>{utils.escape(st['model'])}</code>")
    lines.append("")
    conns = st["connections"]
    if not conns:
        lines.append("Business connections: <b>none</b> — connect the bot in "
                     "Telegram → Settings → Business → Chatbots.")
    else:
        lines.append(f"Business connections: <b>{len(conns)}</b>")
        for c in conns:
            lines.append(
                f"  • owner <code>{utils.escape(c['business_user_id'])}</code> · "
                f"{'enabled' if c['is_enabled'] else 'disabled'} · "
                f"{'can reply' if c['can_reply'] else 'read-only'}"
            )
    lines.append("")
    lines.append(f"Paused chats: <b>{st['paused_count']}</b>")
    lines.append(f"Pending approvals: <b>{st['pending_approvals']}</b>")
    lines.append(f"Knowledge base: <b>{st['kb']['items']}</b> items / {st['kb']['chunks']} chunks")
    return "\n".join(lines)


def cmd_settings(message) -> None:
    from handlers import settings as settings_handler
    settings_handler.open_root(message)


def cmd_connect_openai(message) -> None:
    key = _args(message)
    if key:
        _do_connect_openai(message.chat.id, key, message_id=message.message_id)
        return
    database.set_owner_pending_input(security.owner_id(), "connect_openai", {}, 10)
    handlers.bot.send_message(
        message.chat.id,
        "Send your OpenAI API key (starts with <code>sk-</code>) as the next message, "
        "or /cancel. Tip: I'll delete the message after reading it.",
        parse_mode="HTML",
    )


def cmd_disconnect_openai(message) -> None:
    openai_service.disconnect_api_key()
    handlers.bot.send_message(message.chat.id, "OpenAI key disconnected. AI replies are off "
                                               "until you /connect_openai again.")


def _set_toggle(message, key: str, value: bool, label: str) -> None:
    database.set_setting(key, value)
    handlers.bot.send_message(message.chat.id, f"{label}: <b>{'ON' if value else 'OFF'}</b>",
                              parse_mode="HTML")


def cmd_memory_clear(message) -> None:
    handlers.bot.send_message(message.chat.id, "Clear <b>all</b> conversation memory?",
                              parse_mode="HTML", reply_markup=keyboards.confirm("memclear"))


def cmd_pause_chat(message) -> None:
    raw = _args(message)
    if not raw:
        database.set_owner_pending_input(security.owner_id(), "pause_chat", {}, 10)
        return handlers.bot.send_message(message.chat.id, "Send the customer chat ID to pause, or /cancel.")
    _do_pause(message.chat.id, raw, pause=True)


def cmd_resume_chat(message) -> None:
    raw = _args(message)
    if not raw:
        database.set_owner_pending_input(security.owner_id(), "resume_chat", {}, 10)
        return handlers.bot.send_message(message.chat.id, "Send the customer chat ID to resume, or /cancel.")
    _do_pause(message.chat.id, raw, pause=False)


def cmd_reset(message) -> None:
    handlers.bot.send_message(
        message.chat.id,
        "Reset transient state? This cancels all <b>pending approvals</b>, clears "
        "<b>pauses</b>, and clears any pending input. Settings, KB and history are kept.",
        parse_mode="HTML", reply_markup=keyboards.confirm("reset"),
    )


def cmd_cancel(message) -> None:
    database.clear_owner_pending_input(security.owner_id())
    handlers.bot.send_message(message.chat.id, "Cancelled. ✖")


# =============================================================================
# Pause/resume helper
# =============================================================================
def _do_pause(chat_id: int, raw: str, pause: bool) -> None:
    try:
        customer_chat_id = int(raw)
    except ValueError:
        return handlers.bot.send_message(chat_id, f"'{raw}' is not a valid chat ID.")
    bcid = business_service.find_bcid_for_chat(customer_chat_id)
    if bcid is None:
        return handlers.bot.send_message(
            chat_id, "Couldn't determine the business connection for that chat "
                     "(no messages seen yet, or multiple connections).")
    if pause:
        business_service.pause_chat(bcid, customer_chat_id, reason="manual_pause", minutes=None)
        handlers.bot.send_message(chat_id, f"⏸ Paused chat <code>{customer_chat_id}</code>.",
                                  parse_mode="HTML")
    else:
        ok = business_service.resume_chat(bcid, customer_chat_id)
        msg = "▶️ Resumed" if ok else "Chat was not paused"
        handlers.bot.send_message(chat_id, f"{msg} <code>{customer_chat_id}</code>.", parse_mode="HTML")


# =============================================================================
# Callbacks: control panel
# =============================================================================
def on_panel_callback(call) -> None:
    bot = handlers.bot
    action = call.data.split(":", 1)[1]

    if action == "status":
        bot.send_message(call.message.chat.id, _status_text(), parse_mode="HTML")
        return telegram_api.answer_callback(bot, call.id)
    if action == "help":
        bot.send_message(call.message.chat.id, HELP_TEXT, parse_mode="HTML")
        return telegram_api.answer_callback(bot, call.id)
    if action == "pending":
        bot.send_message(call.message.chat.id, approvals.render_pending_list(), parse_mode="HTML")
        return telegram_api.answer_callback(bot, call.id)
    if action == "mem_clear":
        bot.send_message(call.message.chat.id, "Clear all conversation memory?",
                         reply_markup=keyboards.confirm("memclear"))
        return telegram_api.answer_callback(bot, call.id)
    if action in ("auto_toggle", "appr_toggle"):
        key = "auto_reply_enabled" if action == "auto_toggle" else "approval_mode_enabled"
        new_value = not database.get_bool(key)
        database.set_setting(key, new_value)
        _refresh_panel(call)
        label = "Auto-reply" if action == "auto_toggle" else "Approval mode"
        return telegram_api.answer_callback(bot, call.id, f"{label} {'ON' if new_value else 'OFF'}")
    telegram_api.answer_callback(bot, call.id)


def _refresh_panel(call) -> None:
    try:
        handlers.bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id,
            reply_markup=keyboards.control_panel(
                database.get_bool("auto_reply_enabled"),
                database.get_bool("approval_mode_enabled"),
            ),
        )
    except Exception:
        pass


# =============================================================================
# Callbacks: confirmations
# =============================================================================
def on_confirm_callback(call) -> None:
    bot = handlers.bot
    parts = call.data.split(":")
    decision = parts[1] if len(parts) > 1 else "no"
    token = parts[2] if len(parts) > 2 else ""

    if decision != "yes":
        _strip(call)
        return telegram_api.answer_callback(bot, call.id, "Cancelled.")

    if token == "kbclear":
        from services import kb_service
        kb_service.clear_all()
        result = "Knowledge base cleared."
    elif token == "memclear":
        n = memory_service.clear_all()
        result = f"Cleared conversation memory ({n} rows)."
    elif token == "reset":
        result = _do_reset_transient()
    else:
        result = "Done."

    _strip(call, result)
    telegram_api.answer_callback(bot, call.id, "Done ✅")


def _do_reset_transient() -> str:
    cancelled = database.cancel_all_pending_approvals()
    paused = database.clear_all_pauses()
    if security.owner_id() is not None:
        database.clear_owner_pending_input(security.owner_id())
    return f"Reset done: {cancelled} approval(s) cancelled, {paused} pause(s) cleared."


def _strip(call, new_text: str | None = None) -> None:
    try:
        if new_text:
            handlers.bot.edit_message_text(new_text, call.message.chat.id, call.message.message_id)
        else:
            handlers.bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                                   reply_markup=None)
    except Exception:
        pass


# =============================================================================
# Pending-input application (routed from approvals.on_owner_pending_input)
# =============================================================================
def apply_pending_connect_openai(message, context: dict) -> None:
    database.clear_owner_pending_input(security.owner_id())
    key = (getattr(message, "text", "") or "").strip()
    _do_connect_openai(message.chat.id, key, message_id=message.message_id)


def _do_connect_openai(chat_id: int, key: str, message_id: int | None = None) -> None:
    if not key:
        return handlers.bot.send_message(chat_id, "No key received.")
    ok, msg = openai_service.validate_key(key)
    if not ok:
        return handlers.bot.send_message(chat_id, f"⚠️ Key not accepted: {utils.escape(msg)}")
    openai_service.store_api_key(key)
    # Best-effort: delete the message containing the raw key.
    if message_id is not None:
        try:
            handlers.bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    handlers.bot.send_message(
        chat_id, f"✅ OpenAI key connected ({utils.escape(openai_service.masked_active_key())}).")


def apply_pending_pause(message, kind: str, context: dict) -> None:
    database.clear_owner_pending_input(security.owner_id())
    raw = (getattr(message, "text", "") or "").strip()
    _do_pause(message.chat.id, raw, pause=(kind == "pause_chat"))


# =============================================================================
# Fallback (registered last)
# =============================================================================
def on_fallback(message) -> None:
    if security.is_owner_msg(message):
        handlers.bot.send_message(message.chat.id,
                                  "I didn't recognise that. Use /start for the control panel "
                                  "or /help for commands.")
    else:
        security.handle_non_owner_dm(message)
