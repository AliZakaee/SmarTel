"""Incoming Telegram Business updates: connection changes and the customer-
message decision pipeline, plus edited/deleted message handlers.

Every handler is wrapped so a single bad update can never crash polling.
"""

from __future__ import annotations

import logging

import database
import handlers
import telegram_api
import utils
from handlers import approvals
from services import (
    business_service,
    memory_service,
    openai_service,
    prompt_service,
    rate_limit_service,
)
from services.openai_service import OpenAIServiceError

log = logging.getLogger("smartel.business")

# Content types we ask Telegram to deliver. Non-text are gated inside the
# pipeline (captions are processed; pure media is skipped/monitored).
_CONTENT_TYPES = [
    "text", "photo", "document", "voice", "audio", "video",
    "video_note", "sticker", "location", "contact", "animation",
]


def register(bot, cfg) -> None:
    @bot.business_connection_handler()
    def _on_business_connection(bc):
        try:
            on_business_connection(bc)
        except Exception:
            log.exception("business_connection handler failed")

    @bot.business_message_handler(content_types=_CONTENT_TYPES)
    def _on_business_message(message):
        try:
            process_business_message(message)
        except Exception:
            log.exception("business_message pipeline crashed (swallowed)")

    @bot.edited_business_message_handler(content_types=_CONTENT_TYPES)
    def _on_edited_business_message(message):
        try:
            on_edited_business_message(message)
        except Exception:
            log.exception("edited_business_message handler failed")

    @bot.deleted_business_messages_handler()
    def _on_deleted_business_messages(deleted):
        try:
            on_deleted_business_messages(deleted)
        except Exception:
            log.exception("deleted_business_messages handler failed")


# =============================================================================
# Connection updates
# =============================================================================
def on_business_connection(bc) -> None:
    business_service.upsert_connection(bc)
    is_enabled = bool(getattr(bc, "is_enabled", True))
    can_reply = business_service.resolve_can_reply(bc)
    if is_enabled:
        handlers.notify_owner(
            "🔗 <b>Business connection enabled.</b>\n"
            f"Owner account ID: <code>{utils.escape(getattr(getattr(bc, 'user', None), 'id', '?'))}</code>\n"
            f"Bot can reply: <b>{'yes' if can_reply else 'no'}</b>\n\n"
            "The bot will now receive this account's customer messages. "
            "Use /status any time."
        )
        if not can_reply:
            handlers.notify_owner(
                "⚠️ This connection does <b>not</b> grant reply permission, so the "
                "bot can read but not send. Enable replies for the bot in your "
                "Telegram Business → Chatbots settings."
            )
    else:
        handlers.notify_owner("🔌 <b>Business connection disabled or removed.</b>")


# =============================================================================
# Customer message pipeline
# =============================================================================
def process_business_message(message) -> None:
    bcid = getattr(message, "business_connection_id", None)
    if not bcid:
        log.warning("business_message without business_connection_id; ignoring")
        return

    conn = business_service.get_connection(bcid)
    if conn is None:
        conn = business_service.fetch_and_store_via_raw(bcid)
        if conn is None:
            log.warning("unknown business_connection_id=%s; ignoring", utils.mask_connection(bcid))
            return

    customer_chat_id = message.chat.id
    sender = getattr(message, "from_user", None)
    sender_id = sender.id if sender else None

    # Guard 1: the bot's own outgoing business message echoed back.
    if getattr(message, "sender_business_bot", None) is not None:
        log.debug("ignoring sender_business_bot echo")
        return

    # Guard 2: the OWNER typed manually in this chat -> takeover, pause, never reply.
    owner_user_id = conn["business_user_id"]
    if sender_id is not None and owner_user_id is not None and sender_id == owner_user_id:
        minutes = database.get_int("owner_takeover_pause_minutes")
        business_service.pause_chat(bcid, customer_chat_id, reason="owner_takeover", minutes=minutes)
        log.info("owner takeover chat=%s -> paused %d min",
                 utils.mask_id(customer_chat_id), minutes)
        return

    # It's a genuine customer message — record the customer (keep names fresh).
    business_service.upsert_customer_from_message(bcid, message)

    # Extract text: plain text, or a media caption when caption processing is
    # enabled. Pure media (or captions while disabled) is skipped/monitored.
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        caption = (getattr(message, "caption", None) or "").strip()
        if caption and database.get_bool("process_nontext_enabled"):
            text = caption
    if not text:
        if database.get_bool("monitoring_mode"):
            name = business_service.customer_display_name(bcid, customer_chat_id)
            handlers.notify_owner(
                f"📎 <b>{utils.escape(name)}</b> sent a non-text "
                f"<i>{utils.escape(message.content_type)}</i> message "
                f"(chat <code>{customer_chat_id}</code>). Not auto-handled."
            )
        return

    # Record the inbound message (history + rate-limit window).
    database.insert_message(bcid, customer_chat_id, message.message_id, "in", "user", text)

    # Global automation switch.
    if not database.get_bool("auto_reply_enabled"):
        if database.get_bool("monitoring_mode"):
            _notify_incoming(bcid, customer_chat_id, text)
        return

    # Paused (owner takeover / manual pause / rate-limit pause).
    if business_service.is_chat_paused(bcid, customer_chat_id):
        log.debug("chat paused; skipping auto-reply")
        return

    # Rate limit.
    decision = rate_limit_service.enforce(bcid, customer_chat_id)
    if not decision.allowed:
        name = business_service.customer_display_name(bcid, customer_chat_id)
        handlers.notify_owner(
            f"🚦 <b>{utils.escape(name)}</b> hit the message limit "
            f"({decision.used}/{decision.limit} in the last hour). "
            "Auto-replies for this chat are paused for 1 hour."
        )
        return

    # Typing indicator (best effort).
    telegram_api.chat_action(handlers.bot, customer_chat_id, "typing", business_connection_id=bcid)

    # Build the prompt (handles strict-KB short-circuit) and generate.
    messages, short = prompt_service.build_messages(bcid, customer_chat_id, text)
    if short is not None:
        reply_text = short
    else:
        try:
            reply_text = openai_service.chat(messages)
        except OpenAIServiceError as e:
            name = business_service.customer_display_name(bcid, customer_chat_id)
            handlers.notify_owner(
                f"⚠️ Could not generate a reply for <b>{utils.escape(name)}</b> "
                f"(chat <code>{customer_chat_id}</code>): {utils.escape(e.owner_message)}\n\n"
                "No message was sent to the customer."
            )
            return
        if not reply_text:
            log.warning("empty AI reply; skipping")
            return

    # Sensitive handling + dispatch.
    sensitive = prompt_service.is_sensitive(text)
    force_approval = sensitive and database.get_bool("sensitive_requires_approval")

    if database.get_bool("approval_mode_enabled") or force_approval:
        approvals.create_request(conn, message, text, reply_text, sensitive=sensitive)
    else:
        _deliver_auto(conn, message, text, reply_text)


def _deliver_auto(conn, message, customer_text: str, reply_text: str) -> None:
    bcid = conn["business_connection_id"]
    customer_chat_id = message.chat.id
    if not (conn["is_enabled"] and conn["can_reply"]):
        handlers.notify_owner(
            "⚠️ Cannot auto-reply: the business connection is disabled or lacks "
            "reply permission. Check Telegram Business → Chatbots."
        )
        return
    try:
        telegram_api.send_reply(
            handlers.bot,
            customer_chat_id,
            reply_text,
            business_connection_id=bcid,
            reply_to_message_id=message.message_id,
        )
    except telegram_api.TelegramPartialSendError as exc:
        log.error(
            "auto-reply partially delivered chat=%s chunks=%d/%d",
            utils.mask_id(customer_chat_id),
            exc.sent_count,
            exc.total_count,
        )
        name = business_service.customer_display_name(bcid, customer_chat_id)
        handlers.notify_owner(
            f"⚠️ An automatic reply to <b>{utils.escape(name)}</b> was only "
            f"partially delivered ({exc.sent_count}/{exc.total_count} parts). "
            "Check the customer chat before sending anything else."
        )
        return
    except Exception as exc:
        log.warning(
            "auto-reply delivery unconfirmed chat=%s error=%s",
            utils.mask_id(customer_chat_id),
            type(exc).__name__,
        )
        name = business_service.customer_display_name(bcid, customer_chat_id)
        handlers.notify_owner(
            f"⚠️ Delivery of an automatic reply to <b>{utils.escape(name)}</b> "
            "could not be confirmed. No outbound history was recorded; check "
            "the customer chat before retrying."
        )
        return
    database.insert_message(bcid, customer_chat_id, None, "out", "assistant", reply_text)
    memory_service.append_exchange(bcid, customer_chat_id, customer_text, reply_text)
    log.info("auto-replied chat=%s len=%d", utils.mask_id(customer_chat_id), len(reply_text))
    if database.get_bool("monitoring_mode"):
        name = business_service.customer_display_name(bcid, customer_chat_id)
        handlers.notify_owner(
            f"🤖 Auto-replied to <b>{utils.escape(name)}</b>:\n"
            f"<i>“{utils.escape(customer_text[:200])}”</i>\n"
            f"➡️ {utils.escape(reply_text[:500])}"
        )


def _notify_incoming(bcid: str, customer_chat_id: int, text: str) -> None:
    name = business_service.customer_display_name(bcid, customer_chat_id)
    handlers.notify_owner(
        f"📥 <b>{utils.escape(name)}</b> (chat <code>{customer_chat_id}</code>):\n"
        f"<i>{utils.escape(text[:500])}</i>\n\n"
        "<i>Auto-reply is OFF — reply yourself or enable it with /auto_on.</i>"
    )


# =============================================================================
# Edited / deleted
# =============================================================================
def on_edited_business_message(message) -> None:
    # Informational only: do NOT generate a new reply (prevents edit loops).
    if not database.get_bool("monitoring_mode"):
        return
    bcid = getattr(message, "business_connection_id", None)
    if not bcid:
        return
    conn = business_service.get_connection(bcid)
    owner_user_id = conn["business_user_id"] if conn else None
    sender = getattr(message, "from_user", None)
    if sender and owner_user_id and sender.id == owner_user_id:
        return  # owner edited their own message
    name = business_service.customer_display_name(bcid, message.chat.id)
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    handlers.notify_owner(
        f"✏️ <b>{utils.escape(name)}</b> edited a message: "
        f"<i>{utils.escape(text[:300])}</i>"
    )


def on_deleted_business_messages(deleted) -> None:
    bcid = getattr(deleted, "business_connection_id", None)
    chat = getattr(deleted, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_ids = list(getattr(deleted, "message_ids", []) or [])
    if not bcid or chat_id is None:
        return
    invalidated = database.invalidate_approvals_for_messages(bcid, chat_id, message_ids)
    log.info("customer deleted %d msgs chat=%s (invalidated %d approvals)",
             len(message_ids), utils.mask_id(chat_id), invalidated)
    if invalidated and database.get_bool("monitoring_mode"):
        handlers.notify_owner(
            "🗑 A customer deleted a message that had a pending approval; "
            "that approval was cancelled."
        )
