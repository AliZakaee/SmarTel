"""Bridge between Telegram business objects and the database: connection and
customer upserts, owner-id lookup, pause helpers, and overall status.
"""

from __future__ import annotations

import logging

import database
import utils

log = logging.getLogger("smartel.business")


# =============================================================================
# can_reply resolution (rights.can_reply for Bot API 9.0+, else can_reply)
# =============================================================================
def resolve_can_reply(bc) -> bool:
    rights = getattr(bc, "rights", None)
    if rights is not None:
        val = getattr(rights, "can_reply", None)
        if val is not None:
            return bool(val)
    val = getattr(bc, "can_reply", None)
    if val is not None:
        return bool(val)
    # Default to True: if the field is absent the connection generally can reply.
    return True


# =============================================================================
# Connections
# =============================================================================
def upsert_connection(bc) -> None:
    """Store/refresh a connection from a telebot BusinessConnection object."""
    business_user_id = getattr(getattr(bc, "user", None), "id", None)
    is_enabled = bool(getattr(bc, "is_enabled", True))
    can_reply = resolve_can_reply(bc)
    database.upsert_business_connection(bc.id, business_user_id, is_enabled, can_reply)
    log.info("connection upsert id=%s owner=%s enabled=%s can_reply=%s",
             utils.mask_connection(bc.id), utils.mask_id(business_user_id), is_enabled, can_reply)


def get_connection(business_connection_id: str):
    return database.get_business_connection(business_connection_id)


def get_owner_user_id(business_connection_id: str) -> int | None:
    row = database.get_business_connection(business_connection_id)
    return row["business_user_id"] if row else None


def set_enabled(business_connection_id: str, enabled: bool) -> None:
    database.set_bc_enabled(business_connection_id, enabled)


def can_reply(business_connection_id: str) -> bool:
    row = database.get_business_connection(business_connection_id)
    if row is None:
        return False
    return bool(row["is_enabled"]) and bool(row["can_reply"])


def fetch_and_store_via_raw(business_connection_id: str):
    """Fallback: fetch a connection over raw HTTP (getBusinessConnection) and
    store it, for the case where we receive a message before the connection
    update (or after a restart)."""
    try:
        import telegram_api
        result = telegram_api.get_business_connection(business_connection_id)
    except Exception as e:
        log.warning("raw getBusinessConnection failed: %s", type(e).__name__)
        return None
    if not result:
        return None
    try:
        business_user_id = (result.get("user") or {}).get("id")
        is_enabled = bool(result.get("is_enabled", True))
        rights = result.get("rights")
        if isinstance(rights, dict) and "can_reply" in rights:
            can_reply_val = bool(rights.get("can_reply"))
        else:
            can_reply_val = bool(result.get("can_reply", True))
        database.upsert_business_connection(business_connection_id, business_user_id,
                                            is_enabled, can_reply_val)
        return database.get_business_connection(business_connection_id)
    except Exception:
        log.exception("could not parse raw business connection")
        return None


# =============================================================================
# Customers
# =============================================================================
def upsert_customer_from_message(business_connection_id: str, message) -> None:
    chat_id = message.chat.id
    user = getattr(message, "from_user", None)
    first = getattr(user, "first_name", None) if user else None
    last = getattr(user, "last_name", None) if user else None
    username = getattr(user, "username", None) if user else None
    database.upsert_customer(business_connection_id, chat_id, first, last, username)


def find_bcid_for_chat(customer_chat_id: int) -> str | None:
    return database.find_bcid_for_chat(customer_chat_id)


def customer_display_name(business_connection_id: str, customer_chat_id: int) -> str:
    row = database.get_customer(business_connection_id, customer_chat_id)
    if row is None:
        return f"chat {customer_chat_id}"
    parts = [p for p in (row["first_name"], row["last_name"]) if p]
    name = " ".join(parts).strip()
    if row["username"]:
        name = f"{name} (@{row['username']})".strip()
    return name or f"chat {customer_chat_id}"


# =============================================================================
# Pausing
# =============================================================================
def pause_chat(business_connection_id: str, customer_chat_id: int, reason: str,
               minutes: float | None) -> None:
    paused_until = utils.future_iso(minutes) if minutes else None
    database.upsert_pause(business_connection_id, customer_chat_id, reason, paused_until)
    log.info("paused chat=%s reason=%s minutes=%s",
             utils.mask_id(customer_chat_id), reason, minutes)


def resume_chat(business_connection_id: str, customer_chat_id: int) -> bool:
    removed = database.delete_pause(business_connection_id, customer_chat_id) > 0
    if removed:
        log.info("resumed chat=%s", utils.mask_id(customer_chat_id))
    return removed


def is_chat_paused(business_connection_id: str, customer_chat_id: int) -> bool:
    row = database.get_pause(business_connection_id, customer_chat_id)
    if row is None:
        return False
    paused_until = row["paused_until"]
    if paused_until is None:
        return True  # indefinite pause
    if utils.is_expired(paused_until):
        database.delete_pause(business_connection_id, customer_chat_id)  # lazy cleanup
        return False
    return True


# =============================================================================
# Status
# =============================================================================
def get_status() -> dict:
    connections = database.list_business_connections()
    return {
        "auto_reply_enabled": database.get_bool("auto_reply_enabled"),
        "approval_mode_enabled": database.get_bool("approval_mode_enabled"),
        "monitoring_mode": database.get_bool("monitoring_mode"),
        "model": database.get_str("openai_model"),
        "connections": connections,
        "paused_count": len(database.list_active_pauses()),
        "pending_approvals": database.count_pending_approvals(),
        "kb": {"items": len(database.list_kb_items()), "chunks": database.count_kb_chunks()},
    }
