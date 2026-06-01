"""Prompt assembly: turn the system prompt + knowledge-base context + per-
customer memory + the current customer message into the messages list passed to
the model. Encodes strict-KB and sensitive handling.
"""

from __future__ import annotations

import logging

import database
import utils
from services import kb_service, memory_service

log = logging.getLogger("smartel.prompt")

BASE_SYSTEM_PROMPT = (
    "You are an AI assistant connected to a Telegram Business account. You help "
    "the business account owner reply to private customer messages. Answer "
    "clearly, politely, and accurately. If knowledge base context is provided, "
    "use it when relevant. If strict knowledge base mode is enabled, answer only "
    "from the provided knowledge base context. If the answer is not available in "
    "the provided context, say that you do not have enough information. Do not "
    "invent policies, prices, deadlines, promises, refunds, guarantees, legal "
    "claims, or medical claims. If a customer asks for something sensitive, ask "
    "the owner to review. Keep replies natural and suitable for Telegram."
)

STRICT_REINFORCEMENT = (
    "\n\nSTRICT KNOWLEDGE BASE MODE: Answer ONLY using the knowledge base context "
    "below. If the answer is not in that context, say you do not have enough "
    "information."
)


def is_sensitive(text: str) -> bool:
    return utils.detect_sensitive(text)


def sensitive_categories(text: str) -> list[str]:
    return utils.detect_sensitive_categories(text)


def build_messages(business_connection_id: str, customer_chat_id: int,
                   customer_text: str) -> tuple[list[dict], str | None]:
    """Returns (messages, short_circuit_reply).

    If short_circuit_reply is not None, the caller must use that text directly
    (e.g. strict-KB "no info") WITHOUT calling the model.
    """
    chunks: list = []
    if database.get_bool("kb_enabled"):
        chunks, short = kb_service.search_for_prompt(customer_text)
        if short is not None:
            return [], short

    system = BASE_SYSTEM_PROMPT
    if database.get_bool("strict_kb_mode"):
        system += STRICT_REINFORCEMENT
    context_block = kb_service.build_context_block(chunks)
    if context_block:
        system += "\n\n" + context_block

    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(memory_service.get_recent(business_connection_id, customer_chat_id))
    messages.append({"role": "user", "content": customer_text})
    return messages, None
