"""Approval-before-send: build approval cards, handle Send/Edit/Reject/Pause
callbacks, and run the restart-safe Edit-flow via a DB-backed pending-input
state. Also hosts the single owner pending-input catcher that routes edited
replies, the OpenAI key, KB inputs and settings inputs.
"""

from __future__ import annotations

import logging

import database
import handlers
import keyboards
import security
import telegram_api
import utils
from services import business_service, memory_service

log = logging.getLogger("smartel.approvals")

EDIT_TTL_MINUTES = 15


def register(bot, cfg) -> None:
    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("apr:"))
    @security.owner_only_callback
    def _on_approval_callback(call):
        try:
            on_approval_callback(call)
        except Exception:
            log.exception("approval callback failed")
            telegram_api.answer_callback(bot, call.id, "Something went wrong.", show_alert=True)

    # The single owner pending-input catcher. Registered before the owner
    # fallback (main controls order). Ignores slash-commands so explicit
    # commands still reach their own handlers.
    @bot.message_handler(
        func=lambda m: (
            security.is_owner_msg(m)
            and database.has_owner_pending_input(m.from_user.id)
            and not (getattr(m, "text", "") or "").startswith("/")
        ),
        content_types=["text", "document", "photo"],
    )
    def _on_owner_pending_input(message):
        try:
            on_owner_pending_input(message)
        except Exception:
            log.exception("pending-input handler failed")


# =============================================================================
# Card rendering + creation
# =============================================================================
def render_approval_card(conn, message, customer_text: str, ai_text: str,
                         sensitive: bool, approval_id: int) -> str:
    bcid = conn["business_connection_id"]
    name = business_service.customer_display_name(bcid, message.chat.id)
    banner = "🚨 <b>SENSITIVE</b> — please review carefully\n\n" if sensitive else ""
    return (
        f"{banner}📨 <b>Approval #{approval_id}</b>\n"
        f"From: <b>{utils.escape(name)}</b> (chat <code>{message.chat.id}</code>)\n\n"
        f"<b>Customer:</b>\n<i>{utils.escape(customer_text[:1500])}</i>\n\n"
        f"<b>Proposed reply:</b>\n{utils.escape(ai_text[:2500])}"
    )


def create_request(conn, message, customer_text: str, ai_text: str, *, sensitive: bool) -> int:
    bcid = conn["business_connection_id"]
    existing = database.find_open_approval(bcid, message.chat.id, message.message_id)
    if existing is not None:
        return existing["id"]
    aid = database.insert_pending_approval(bcid, message.chat.id, message.message_id,
                                           customer_text, ai_text)
    card = render_approval_card(conn, message, customer_text, ai_text, sensitive, aid)
    handlers.notify_owner(card, reply_markup=keyboards.approval_actions(aid))
    log.info("approval created id=%s chat=%s sensitive=%s",
             aid, utils.mask_id(message.chat.id), sensitive)
    return aid


# =============================================================================
# Callback dispatch
# =============================================================================
def on_approval_callback(call) -> None:
    bot = handlers.bot
    try:
        _, action, aid_str = call.data.split(":", 2)
        aid = int(aid_str)
    except (ValueError, AttributeError):
        return telegram_api.answer_callback(bot, call.id, "Bad action.")

    appr = database.get_approval(aid)
    if appr is None:
        return telegram_api.answer_callback(bot, call.id, "Approval not found.", show_alert=True)
    if appr["status"] != "pending":
        if appr["status"] == "sending":
            return telegram_api.answer_callback(
                bot, call.id, "Delivery is already in progress.", show_alert=True
            )
        if appr["status"] == "delivery_uncertain":
            _strip_buttons(call)
            return telegram_api.answer_callback(
                bot,
                call.id,
                "Delivery outcome is uncertain. Check the customer chat before replying again.",
                show_alert=True,
            )
        _strip_buttons(call)
        return telegram_api.answer_callback(bot, call.id, f"Already {appr['status']}.",
                                            show_alert=True)

    if action == "send":
        _do_send(call, appr)
    elif action == "reject":
        _do_reject(call, appr)
    elif action == "pause":
        _do_pause(call, appr)
    elif action == "edit":
        _start_edit(call, appr)
    else:
        telegram_api.answer_callback(bot, call.id, "Unknown action.")


def _do_send(call, appr) -> None:
    bot = handlers.bot
    conn = business_service.get_connection(appr["business_connection_id"])
    if conn is None or not (conn["is_enabled"] and conn["can_reply"]):
        return telegram_api.answer_callback(bot, call.id, "Connection unavailable / cannot reply.",
                                            show_alert=True)
    outcome = _claim_and_send(conn, appr, appr["ai_reply_text"], "sent")
    if outcome == "not_claimed":
        return _answer_not_claimed(call, appr["id"])
    if outcome == "delivery_failed":
        return telegram_api.answer_callback(
            bot, call.id, "Send failed; the approval is still pending. Please retry.",
            show_alert=True,
        )
    if outcome == "partial_delivery":
        _strip_buttons(call)
        return telegram_api.answer_callback(
            bot,
            call.id,
            "Part of the reply was delivered. Check the customer chat before sending more.",
            show_alert=True,
        )
    if outcome == "state_failed":
        _strip_buttons(call)
        return telegram_api.answer_callback(
            bot, call.id, "Delivered, but the approval state could not be finalized.",
            show_alert=True,
        )
    _strip_buttons(call)
    telegram_api.answer_callback(bot, call.id, "Sent ✅")


def _do_reject(call, appr) -> None:
    bot = handlers.bot
    if not database.mark_approval(appr["id"], "rejected"):
        _strip_buttons(call)
        return telegram_api.answer_callback(bot, call.id, "Already handled.", show_alert=True)
    _strip_buttons(call)
    telegram_api.answer_callback(bot, call.id, "Rejected 🚫")
    log.info("approval %s rejected", appr["id"])


def _do_pause(call, appr) -> None:
    bot = handlers.bot
    business_service.pause_chat(appr["business_connection_id"], appr["customer_chat_id"],
                               reason="manual_pause", minutes=None)
    database.mark_approval(appr["id"], "rejected_paused")
    _strip_buttons(call)
    telegram_api.answer_callback(bot, call.id, "Chat paused ⏸ (use /resume_chat to resume).",
                                 show_alert=True)


def _start_edit(call, appr) -> None:
    bot = handlers.bot
    database.set_owner_pending_input(security.owner_id(), "edit_approval",
                                     {"approval_id": appr["id"]}, EDIT_TTL_MINUTES)
    telegram_api.answer_callback(bot, call.id, "Send me the edited reply text.")
    handlers.notify_owner(
        f"✏️ <b>Editing reply for approval #{appr['id']}</b> "
        f"(chat <code>{appr['customer_chat_id']}</code>).\n"
        "Send the new reply text as your next message, or /cancel to abort."
    )


# =============================================================================
# Edit-flow application (called by the pending-input catcher)
# =============================================================================
def _apply_edit(message, context: dict) -> None:
    owner = security.owner_id()
    database.clear_owner_pending_input(owner)  # one-shot, clear first
    aid = context.get("approval_id")
    appr = database.get_approval(aid) if aid is not None else None
    if appr is None or appr["status"] != "pending":
        return handlers.notify_owner("That approval is no longer pending.")
    edited = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if not edited:
        return handlers.notify_owner("Empty edit ignored. The approval is still pending.")
    conn = business_service.get_connection(appr["business_connection_id"])
    if conn is None or not (conn["is_enabled"] and conn["can_reply"]):
        return handlers.notify_owner("Connection unavailable; the edited reply was not sent.")
    outcome = _claim_and_send(conn, appr, edited, "sent_edited", update_reply=True)
    if outcome == "not_claimed":
        return handlers.notify_owner("That approval was already handled.")
    if outcome == "delivery_failed":
        return handlers.notify_owner(
            "The edited reply could not be delivered. The approval is still pending; please retry."
        )
    if outcome == "partial_delivery":
        return handlers.notify_owner(
            "Part of the edited reply was delivered. Check the customer chat before sending more."
        )
    if outcome == "state_failed":
        return handlers.notify_owner(
            "The edited reply was delivered, but its approval state could not be finalized."
        )
    handlers.notify_owner("Edited reply sent ✅")


def _claim_and_send(conn, appr, reply_text: str, final_status: str,
                    *, update_reply: bool = False) -> str:
    """Claim, deliver, and finalize an approval without false sent states."""
    aid = appr["id"]
    if not database.claim_approval_delivery(aid):
        return "not_claimed"

    if update_reply:
        try:
            database.update_approval_reply(aid, reply_text)
        except Exception:
            database.release_approval_delivery(aid)
            raise

    bcid = conn["business_connection_id"]
    chat_id = appr["customer_chat_id"]
    try:
        telegram_api.send_reply(handlers.bot, chat_id, reply_text,
                                business_connection_id=bcid,
                                reply_to_message_id=appr["customer_message_id"])
    except telegram_api.TelegramPartialSendError as exc:
        uncertain = _mark_delivery_uncertain(aid)
        log.error(
            "approval %s partially delivered (%s/%s chunks); marked_uncertain=%s",
            aid, exc.sent_count, exc.total_count, uncertain,
        )
        return "partial_delivery"
    except Exception as exc:
        released = _release_delivery_claim(aid)
        log.warning(
            "approval %s delivery failed (%s); released=%s",
            aid, type(exc).__name__, released,
        )
        if released:
            return "delivery_failed"
        _mark_delivery_uncertain(aid)
        return "state_failed"

    try:
        finalized = database.complete_approval_delivery(aid, final_status)
    except Exception:
        log.exception("approval %s delivered but state finalization raised", aid)
        _mark_delivery_uncertain(aid)
        return "state_failed"
    if not finalized:
        log.error("approval %s delivered but state finalization lost its claim", aid)
        _mark_delivery_uncertain(aid)
        return "state_failed"

    try:
        database.insert_message(bcid, chat_id, None, "out", "assistant", reply_text)
        memory_service.append_exchange(
            bcid, chat_id, appr["customer_message_text"], reply_text
        )
    except Exception:
        log.exception("approval %s delivered but local bookkeeping failed", aid)
    log.info("approval %s delivered to chat=%s", aid, utils.mask_id(chat_id))
    return "sent"


def _mark_delivery_uncertain(approval_id: int) -> bool:
    try:
        return database.mark_approval_delivery_uncertain(approval_id)
    except Exception:
        log.exception("approval %s could not be marked delivery-uncertain", approval_id)
        return False


def _release_delivery_claim(approval_id: int) -> bool:
    try:
        return database.release_approval_delivery(approval_id)
    except Exception:
        log.exception("approval %s delivery claim could not be released", approval_id)
        return False


def _answer_not_claimed(call, approval_id: int) -> None:
    current = database.get_approval(approval_id)
    if current is not None and current["status"] == "sending":
        return telegram_api.answer_callback(
            handlers.bot, call.id, "Delivery is already in progress.", show_alert=True
        )
    _strip_buttons(call)
    telegram_api.answer_callback(handlers.bot, call.id, "Already handled.", show_alert=True)


def _strip_buttons(call) -> None:
    try:
        handlers.bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                               reply_markup=None)
    except Exception:
        pass


# =============================================================================
# Owner pending-input router (single catcher for all owner input states)
# =============================================================================
def on_owner_pending_input(message) -> None:
    owner = security.owner_id()
    state = database.get_owner_pending_input(owner)
    if state is None:
        return
    kind = state["kind"]
    context = state["context"]

    if kind == "edit_approval":
        _apply_edit(message, context)
        return

    # Other kinds live in their own modules — import lazily to avoid cycles.
    if kind == "connect_openai":
        from handlers import owner_commands
        owner_commands.apply_pending_connect_openai(message, context)
    elif kind in ("kb_add", "kb_upload", "kb_search"):
        from handlers import knowledge_base
        knowledge_base.apply_pending(message, kind, context)
    elif kind in ("set_int", "set_float"):
        from handlers import settings as settings_handler
        settings_handler.apply_pending(message, kind, context)
    elif kind in ("pause_chat", "resume_chat"):
        from handlers import owner_commands
        owner_commands.apply_pending_pause(message, kind, context)
    else:
        database.clear_owner_pending_input(owner)


# =============================================================================
# Helpers used by owner_commands (pending approvals view)
# =============================================================================
def render_pending_list() -> str:
    rows = database.list_pending_approvals("pending")
    if not rows:
        return "No pending approvals. 🎉"
    lines = [f"<b>Pending approvals ({len(rows)}):</b>"]
    for r in rows:
        preview = utils.escape((r["customer_message_text"] or "")[:80])
        lines.append(f"• #{r['id']} chat <code>{r['customer_chat_id']}</code>: <i>{preview}</i>")
    lines.append("\nEach approval was sent as its own message with action buttons.")
    return "\n".join(lines)
