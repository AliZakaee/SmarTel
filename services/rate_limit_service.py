"""Per-customer rate limiting using a rolling 1-hour window computed from the
``messages`` table (no fixed-bucket reset edge). When the limit is exceeded,
auto-replies for that customer are paused and the owner should be notified.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import database
import utils
from services import business_service

log = logging.getLogger("smartel.ratelimit")

WINDOW_MINUTES = 60
PAUSE_ON_EXCEED_MINUTES = 60


class RateLimitDecision(NamedTuple):
    allowed: bool
    used: int
    limit: int
    remaining: int


def check(business_connection_id: str, customer_chat_id: int) -> RateLimitDecision:
    """Evaluate the rolling window. The inbound message is expected to have been
    inserted already, so ``used`` includes the current message."""
    limit = database.get_int("max_messages_per_customer_per_hour")
    since = utils.future_iso(-WINDOW_MINUTES)
    used = database.count_messages_in_window(business_connection_id, customer_chat_id,
                                             since, direction="in")
    allowed = used <= limit
    return RateLimitDecision(allowed=allowed, used=used, limit=limit,
                             remaining=max(0, limit - used))


def enforce(business_connection_id: str, customer_chat_id: int) -> RateLimitDecision:
    """Check the limit and, if exceeded, pause auto-replies for this customer."""
    decision = check(business_connection_id, customer_chat_id)
    if not decision.allowed:
        business_service.pause_chat(business_connection_id, customer_chat_id,
                                    reason="rate_limit", minutes=PAUSE_ON_EXCEED_MINUTES)
        log.info("rate limit exceeded chat=%s used=%d limit=%d",
                 utils.mask_id(customer_chat_id), decision.used, decision.limit)
    return decision
