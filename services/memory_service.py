"""Per-customer short conversation memory, strictly isolated by
(business_connection_id, customer_chat_id). Bounded size; never mixes customers.
"""

from __future__ import annotations

import logging

import database

log = logging.getLogger("smartel.memory")

MAX_MEMORY_TURNS = 12  # newest N rows kept per (connection, chat)


def append(business_connection_id: str, customer_chat_id: int, role: str, content: str) -> None:
    if not database.get_bool("memory_enabled"):
        return
    database.insert_memory(business_connection_id, customer_chat_id, role, content)
    database.trim_memory(business_connection_id, customer_chat_id, MAX_MEMORY_TURNS)


def append_exchange(business_connection_id: str, customer_chat_id: int,
                    customer_text: str, assistant_text: str) -> None:
    append(business_connection_id, customer_chat_id, "user", customer_text)
    append(business_connection_id, customer_chat_id, "assistant", assistant_text)


def get_recent(business_connection_id: str, customer_chat_id: int,
               limit: int = MAX_MEMORY_TURNS) -> list[dict]:
    """Chronological list of {"role","content"} ready to splice into the
    chat messages list. Empty when memory is disabled."""
    if not database.get_bool("memory_enabled"):
        return []
    rows = database.get_recent_memory(business_connection_id, customer_chat_id, limit)
    rows = list(reversed(rows))  # DAL returns newest-first
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def clear(business_connection_id: str, customer_chat_id: int) -> None:
    database.delete_memory_for_chat(business_connection_id, customer_chat_id)


def clear_all() -> int:
    return database.clear_all_memory()
