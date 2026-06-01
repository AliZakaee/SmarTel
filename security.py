"""Owner access control: guards for commands and callbacks, and the polite
notice shown to non-owner users who message the bot directly.

Note: customer messages arrive as *business_message* updates (different handler
path) — they never reach these command/DM handlers. This module only governs
direct (non-business) chats with the bot.
"""

from __future__ import annotations

import functools
import logging
import threading

import database
import utils

log = logging.getLogger("smartel.security")

_owner_id: int | None = None
_bot = None

# Throttle the non-owner notice so the bot can't be used as a reflector.
_notice_lock = threading.Lock()
_last_notice: dict[int, float] = {}
_NOTICE_COOLDOWN_SECONDS = 600

NON_OWNER_NOTICE = (
    "👋 This is a <b>managed Telegram Business assistant</b>. It does not chat "
    "directly here.\n\nTo reach the business, please message them through their "
    "Telegram Business profile. The owner connects this bot via "
    "<b>Telegram Settings → Business → Chatbots</b>."
)


def init(bot, owner_id: int) -> None:
    global _owner_id, _bot
    _bot = bot
    _owner_id = int(owner_id)


def owner_id() -> int | None:
    return _owner_id


def is_owner(user_id) -> bool:
    return _owner_id is not None and user_id == _owner_id


def is_owner_msg(message) -> bool:
    user = getattr(message, "from_user", None)
    return bool(user) and is_owner(user.id)


def is_owner_call(call) -> bool:
    user = getattr(call, "from_user", None)
    return bool(user) and is_owner(user.id)


def owner_only(handler):
    """Decorator for message handlers registered WITHOUT a func predicate."""
    @functools.wraps(handler)
    def wrapper(message, *args, **kwargs):
        if not is_owner_msg(message):
            handle_non_owner_dm(message)
            return None
        return handler(message, *args, **kwargs)
    return wrapper


def owner_only_callback(handler):
    """Decorator for callback-query handlers."""
    @functools.wraps(handler)
    def wrapper(call, *args, **kwargs):
        if not is_owner_call(call):
            try:
                _bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
            except Exception:
                pass
            return None
        return handler(call, *args, **kwargs)
    return wrapper


def handle_non_owner_dm(message) -> None:
    """Send the managed-assistant notice to a non-owner (throttled)."""
    user = getattr(message, "from_user", None)
    uid = user.id if user else None
    if uid is None:
        return
    now = utils.now_ts()
    with _notice_lock:
        last = _last_notice.get(uid, 0)
        if now - last < _NOTICE_COOLDOWN_SECONDS:
            return
        _last_notice[uid] = now
    log.info("non-owner DM from uid=%s", utils.mask_id(uid))
    try:
        _bot.send_message(message.chat.id, NON_OWNER_NOTICE, parse_mode="HTML")
    except Exception:
        log.debug("could not send non-owner notice", exc_info=True)
    # public_direct_bot_chat_enabled is a safety flag: when False (default) the
    # bot does nothing beyond this notice (never acts as a public chatbot).
    _ = database.get_bool("public_direct_bot_chat_enabled")
