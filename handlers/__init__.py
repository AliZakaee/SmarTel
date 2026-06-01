"""Handler package. Holds the shared bot/config references and a couple of
cross-handler helpers (owner notifications)."""

from __future__ import annotations

import logging

log = logging.getLogger("smartel.handlers")

bot = None      # set via init(); the single TeleBot instance
cfg = None       # the Config object


def init(bot_instance, config) -> None:
    global bot, cfg
    bot = bot_instance
    cfg = config


def notify_owner(text: str, reply_markup=None) -> None:
    """Best-effort message to the bot owner (never with business_connection_id)."""
    import security
    import telegram_api
    oid = security.owner_id()
    if oid is None or bot is None:
        return
    try:
        telegram_api.send_reply(bot, oid, text, reply_markup=reply_markup)
    except Exception:
        log.exception("notify_owner failed")
