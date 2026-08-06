"""SQLite persistence: thread-safe connection management, schema creation, a
typed settings layer with coercion, and the data-access layer (DAL).

Threading: pyTelegramBotAPI dispatches updates on worker threads, so we use one
connection per thread (``threading.local``) with WAL mode, plus a module-level
write lock that serializes write transactions. Reads are lock-free (WAL lets
readers proceed alongside the single writer).
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterator

import utils

log = logging.getLogger("smartel.db")

_local = threading.local()
_WRITE_LOCK = threading.Lock()
_DB_PATH: Path | None = None
_SEED_DEFAULTS: dict[str, object] = {}


# =============================================================================
# Settings schema: key -> (python type, default value)
# =============================================================================
SETTINGS_SCHEMA: dict[str, tuple[type, object]] = {
    "openai_model": (str, "gpt-4.1-mini"),
    "temperature": (float, 0.4),
    "max_tokens": (int, 800),
    "auto_reply_enabled": (bool, False),
    "approval_mode_enabled": (bool, True),
    "kb_enabled": (bool, True),
    "strict_kb_mode": (bool, False),
    "memory_enabled": (bool, True),
    "owner_takeover_pause_minutes": (int, 30),
    "sensitive_requires_approval": (bool, True),
    "monitoring_mode": (bool, True),
    "max_messages_per_customer_per_hour": (int, 20),
    "public_direct_bot_chat_enabled": (bool, False),
    # Internal extra (not in the original spec list): allow processing of
    # non-text business messages. Off by default per spec ("text by default").
    "process_nontext_enabled": (bool, False),
    # Free-text instruction always added to the AI system prompt (language, tone,
    # persona). Empty by default. Set via /instructions.
    "custom_instructions": (str, ""),
}

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off", ""}


# =============================================================================
# Connection management
# =============================================================================
def init_db(cfg) -> int:
    """Initialise storage and quarantine delivery claims from an earlier process.

    Returns the number of interrupted approval deliveries that require manual
    review.
    """
    global _DB_PATH, _SEED_DEFAULTS
    _DB_PATH = Path(cfg.db_path)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.uploads_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.vector_store_dir).mkdir(parents=True, exist_ok=True)

    # Behavioral defaults seeded from .env DEFAULT_* (only the three the wizard asks for).
    _SEED_DEFAULTS = {k: v for k, (_t, v) in SETTINGS_SCHEMA.items()}
    _SEED_DEFAULTS["openai_model"] = cfg.default_model
    _SEED_DEFAULTS["auto_reply_enabled"] = cfg.default_auto_reply
    _SEED_DEFAULTS["approval_mode_enabled"] = cfg.default_approval_mode

    _create_schema()
    seed_default_settings()
    interrupted_deliveries = mark_interrupted_approval_deliveries_uncertain()
    log.info("database initialised at %s", _DB_PATH)
    return interrupted_deliveries


def _get_conn() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("Database not initialised. Call init_db(cfg) first.")
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(_DB_PATH), timeout=5.0, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


@contextlib.contextmanager
def read_cursor() -> Iterator[sqlite3.Cursor]:
    conn = _get_conn()
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


@contextlib.contextmanager
def write_tx() -> Iterator[sqlite3.Cursor]:
    conn = _get_conn()
    with _WRITE_LOCK:
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def _create_schema() -> None:
    with write_tx() as cur:
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS business_connections (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT UNIQUE NOT NULL,
                business_user_id       INTEGER,
                is_enabled             INTEGER DEFAULT 1,
                can_reply              INTEGER DEFAULT 1,
                created_at             TEXT,
                updated_at             TEXT
            );

            CREATE TABLE IF NOT EXISTS customers (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT,
                customer_chat_id       INTEGER,
                first_name             TEXT,
                last_name              TEXT,
                username               TEXT,
                created_at             TEXT,
                updated_at             TEXT,
                UNIQUE(business_connection_id, customer_chat_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT,
                customer_chat_id       INTEGER,
                telegram_message_id    INTEGER,
                direction              TEXT,
                role                   TEXT,
                content                TEXT,
                created_at             TEXT
            );

            CREATE TABLE IF NOT EXISTS conversation_memory (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT,
                customer_chat_id       INTEGER,
                role                   TEXT,
                content                TEXT,
                created_at             TEXT
            );

            CREATE TABLE IF NOT EXISTS paused_chats (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT,
                customer_chat_id       INTEGER,
                reason                 TEXT,
                paused_until           TEXT,
                created_at             TEXT,
                UNIQUE(business_connection_id, customer_chat_id)
            );

            CREATE TABLE IF NOT EXISTS pending_approvals (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT,
                customer_chat_id       INTEGER,
                customer_message_id    INTEGER,
                customer_message_text  TEXT,
                ai_reply_text          TEXT,
                status                 TEXT DEFAULT 'pending',
                created_at             TEXT,
                decided_at             TEXT
            );

            CREATE TABLE IF NOT EXISTS kb_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT,
                source_type TEXT,
                filename    TEXT,
                created_at  TEXT,
                chunk_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS kb_chunks (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id        INTEGER,
                chunk_index    INTEGER,
                content        TEXT,
                embedding_json TEXT,
                created_at     TEXT,
                FOREIGN KEY(item_id) REFERENCES kb_items(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                id                              INTEGER PRIMARY KEY AUTOINCREMENT,
                provider                        TEXT UNIQUE,
                masked_key                      TEXT,
                encrypted_key_or_plain_local_key TEXT,
                created_at                      TEXT,
                updated_at                      TEXT
            );

            CREATE TABLE IF NOT EXISTS owner_pending_input (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER UNIQUE,
                kind          TEXT,
                context_json  TEXT,
                created_at    TEXT,
                expires_at    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_messages_bc_chat_time
                ON messages(business_connection_id, customer_chat_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_memory_bc_chat_time
                ON conversation_memory(business_connection_id, customer_chat_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_paused_bc_chat
                ON paused_chats(business_connection_id, customer_chat_id);
            CREATE INDEX IF NOT EXISTS idx_pending_status
                ON pending_approvals(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_item
                ON kb_chunks(item_id);
            CREATE INDEX IF NOT EXISTS idx_customers_bc_chat
                ON customers(business_connection_id, customer_chat_id);
            """
        )


# =============================================================================
# Settings: typed get/set with coercion
# =============================================================================
def _coerce(value: str, typ: type):
    if typ is bool:
        s = str(value).strip().lower()
        if s in _BOOL_TRUE:
            return True
        if s in _BOOL_FALSE:
            return False
        raise ValueError(f"not a bool: {value!r}")
    if typ is int:
        return int(value)
    if typ is float:
        return float(value)
    return str(value)


def _serialize(value, typ) -> str:
    if typ is bool:
        return "true" if value else "false"
    return str(value)


def get_setting(key: str):
    """Return a setting coerced to its declared type, falling back to default."""
    if key not in SETTINGS_SCHEMA:
        raise KeyError(f"unknown setting: {key}")
    typ, default = SETTINGS_SCHEMA[key]
    default = _SEED_DEFAULTS.get(key, default)
    with read_cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
    if row is None or row["value"] is None or str(row["value"]).strip() == "":
        return default
    try:
        return _coerce(row["value"], typ)
    except (ValueError, TypeError):
        log.warning("setting %s has invalid stored value %r; using default", key, row["value"])
        return default


def get_bool(key: str) -> bool:
    return bool(get_setting(key))


def get_int(key: str) -> int:
    return int(get_setting(key))


def get_float(key: str) -> float:
    return float(get_setting(key))


def get_str(key: str) -> str:
    return str(get_setting(key))


def set_setting(key: str, value) -> None:
    if key not in SETTINGS_SCHEMA:
        raise KeyError(f"unknown setting: {key}")
    typ, _default = SETTINGS_SCHEMA[key]
    serialized = _serialize(value, typ)
    with write_tx() as cur:
        cur.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, serialized, utils.now_iso()),
        )


def get_all_settings() -> dict[str, object]:
    return {key: get_setting(key) for key in SETTINGS_SCHEMA}


def seed_default_settings() -> None:
    now = utils.now_iso()
    with write_tx() as cur:
        for key, (typ, default) in SETTINGS_SCHEMA.items():
            value = _SEED_DEFAULTS.get(key, default)
            cur.execute(
                "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?,?,?)",
                (key, _serialize(value, typ), now),
            )


# =============================================================================
# DAL — business_connections
# =============================================================================
def upsert_business_connection(business_connection_id: str, business_user_id: int | None,
                               is_enabled: bool, can_reply: bool) -> None:
    now = utils.now_iso()
    with write_tx() as cur:
        cur.execute(
            """
            INSERT INTO business_connections
                (business_connection_id, business_user_id, is_enabled, can_reply, created_at, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(business_connection_id) DO UPDATE SET
                business_user_id = excluded.business_user_id,
                is_enabled       = excluded.is_enabled,
                can_reply        = excluded.can_reply,
                updated_at       = excluded.updated_at
            """,
            (business_connection_id, business_user_id, 1 if is_enabled else 0,
             1 if can_reply else 0, now, now),
        )


def get_business_connection(business_connection_id: str) -> sqlite3.Row | None:
    with read_cursor() as cur:
        cur.execute(
            "SELECT * FROM business_connections WHERE business_connection_id = ?",
            (business_connection_id,),
        )
        return cur.fetchone()


def set_bc_enabled(business_connection_id: str, enabled: bool) -> None:
    with write_tx() as cur:
        cur.execute(
            "UPDATE business_connections SET is_enabled=?, updated_at=? WHERE business_connection_id=?",
            (1 if enabled else 0, utils.now_iso(), business_connection_id),
        )


def list_business_connections() -> list[sqlite3.Row]:
    with read_cursor() as cur:
        cur.execute("SELECT * FROM business_connections ORDER BY id")
        return cur.fetchall()


# =============================================================================
# DAL — customers
# =============================================================================
def upsert_customer(business_connection_id: str, customer_chat_id: int,
                    first_name: str | None, last_name: str | None,
                    username: str | None) -> None:
    now = utils.now_iso()
    with write_tx() as cur:
        cur.execute(
            """
            INSERT INTO customers
                (business_connection_id, customer_chat_id, first_name, last_name, username, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(business_connection_id, customer_chat_id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                username   = excluded.username,
                updated_at = excluded.updated_at
            """,
            (business_connection_id, customer_chat_id, first_name, last_name, username, now, now),
        )


def get_customer(business_connection_id: str, customer_chat_id: int) -> sqlite3.Row | None:
    with read_cursor() as cur:
        cur.execute(
            "SELECT * FROM customers WHERE business_connection_id=? AND customer_chat_id=?",
            (business_connection_id, customer_chat_id),
        )
        return cur.fetchone()


def find_bcid_for_chat(customer_chat_id: int) -> str | None:
    """Best-effort: the most recently seen connection that has this customer.
    Falls back to the sole connection if exactly one exists."""
    with read_cursor() as cur:
        cur.execute(
            "SELECT business_connection_id FROM customers WHERE customer_chat_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (customer_chat_id,),
        )
        row = cur.fetchone()
    if row is not None:
        return row["business_connection_id"]
    conns = list_business_connections()
    return conns[0]["business_connection_id"] if len(conns) == 1 else None


# =============================================================================
# DAL — messages
# =============================================================================
def insert_message(business_connection_id: str, customer_chat_id: int,
                   telegram_message_id: int | None, direction: str, role: str,
                   content: str) -> int:
    with write_tx() as cur:
        cur.execute(
            """
            INSERT INTO messages
                (business_connection_id, customer_chat_id, telegram_message_id, direction, role, content, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (business_connection_id, customer_chat_id, telegram_message_id, direction, role,
             content, utils.now_iso()),
        )
        return cur.lastrowid


def count_messages_in_window(business_connection_id: str, customer_chat_id: int,
                             since_iso: str, direction: str = "in") -> int:
    with read_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE business_connection_id=? AND customer_chat_id=? AND direction=? AND created_at >= ?",
            (business_connection_id, customer_chat_id, direction, since_iso),
        )
        return int(cur.fetchone()["n"])


def oldest_message_in_window(business_connection_id: str, customer_chat_id: int,
                             since_iso: str, direction: str = "in") -> str | None:
    with read_cursor() as cur:
        cur.execute(
            "SELECT MIN(created_at) AS t FROM messages "
            "WHERE business_connection_id=? AND customer_chat_id=? AND direction=? AND created_at >= ?",
            (business_connection_id, customer_chat_id, direction, since_iso),
        )
        row = cur.fetchone()
        return row["t"] if row else None


# =============================================================================
# DAL — conversation_memory
# =============================================================================
def insert_memory(business_connection_id: str, customer_chat_id: int, role: str,
                  content: str) -> int:
    with write_tx() as cur:
        cur.execute(
            "INSERT INTO conversation_memory(business_connection_id, customer_chat_id, role, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (business_connection_id, customer_chat_id, role, content, utils.now_iso()),
        )
        return cur.lastrowid


def get_recent_memory(business_connection_id: str, customer_chat_id: int,
                      limit: int) -> list[sqlite3.Row]:
    """Newest-first; caller reverses for chronological order."""
    with read_cursor() as cur:
        cur.execute(
            "SELECT * FROM conversation_memory "
            "WHERE business_connection_id=? AND customer_chat_id=? ORDER BY id DESC LIMIT ?",
            (business_connection_id, customer_chat_id, limit),
        )
        return cur.fetchall()


def trim_memory(business_connection_id: str, customer_chat_id: int, keep: int) -> None:
    with write_tx() as cur:
        cur.execute(
            """
            DELETE FROM conversation_memory
            WHERE business_connection_id=? AND customer_chat_id=?
              AND id NOT IN (
                SELECT id FROM conversation_memory
                WHERE business_connection_id=? AND customer_chat_id=?
                ORDER BY id DESC LIMIT ?
              )
            """,
            (business_connection_id, customer_chat_id, business_connection_id, customer_chat_id, keep),
        )


def delete_memory_for_chat(business_connection_id: str, customer_chat_id: int) -> None:
    with write_tx() as cur:
        cur.execute(
            "DELETE FROM conversation_memory WHERE business_connection_id=? AND customer_chat_id=?",
            (business_connection_id, customer_chat_id),
        )


def clear_all_memory() -> int:
    with write_tx() as cur:
        cur.execute("DELETE FROM conversation_memory")
        return cur.rowcount


# =============================================================================
# DAL — paused_chats
# =============================================================================
def upsert_pause(business_connection_id: str, customer_chat_id: int, reason: str,
                 paused_until: str | None) -> None:
    now = utils.now_iso()
    with write_tx() as cur:
        cur.execute(
            """
            INSERT INTO paused_chats(business_connection_id, customer_chat_id, reason, paused_until, created_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(business_connection_id, customer_chat_id) DO UPDATE SET
                reason = excluded.reason,
                paused_until = excluded.paused_until,
                created_at = excluded.created_at
            """,
            (business_connection_id, customer_chat_id, reason, paused_until, now),
        )


def get_pause(business_connection_id: str, customer_chat_id: int) -> sqlite3.Row | None:
    with read_cursor() as cur:
        cur.execute(
            "SELECT * FROM paused_chats WHERE business_connection_id=? AND customer_chat_id=?",
            (business_connection_id, customer_chat_id),
        )
        return cur.fetchone()


def delete_pause(business_connection_id: str, customer_chat_id: int) -> int:
    with write_tx() as cur:
        cur.execute(
            "DELETE FROM paused_chats WHERE business_connection_id=? AND customer_chat_id=?",
            (business_connection_id, customer_chat_id),
        )
        return cur.rowcount


def list_active_pauses() -> list[sqlite3.Row]:
    with read_cursor() as cur:
        cur.execute("SELECT * FROM paused_chats ORDER BY id")
        return cur.fetchall()


def clear_all_pauses() -> int:
    with write_tx() as cur:
        cur.execute("DELETE FROM paused_chats")
        return cur.rowcount


# =============================================================================
# DAL — pending_approvals
# =============================================================================
def insert_pending_approval(business_connection_id: str, customer_chat_id: int,
                            customer_message_id: int | None, customer_message_text: str,
                            ai_reply_text: str) -> int:
    with write_tx() as cur:
        cur.execute(
            """
            INSERT INTO pending_approvals
                (business_connection_id, customer_chat_id, customer_message_id,
                 customer_message_text, ai_reply_text, status, created_at)
            VALUES (?,?,?,?,?, 'pending', ?)
            """,
            (business_connection_id, customer_chat_id, customer_message_id,
             customer_message_text, ai_reply_text, utils.now_iso()),
        )
        return cur.lastrowid


def get_approval(approval_id: int) -> sqlite3.Row | None:
    with read_cursor() as cur:
        cur.execute("SELECT * FROM pending_approvals WHERE id=?", (approval_id,))
        return cur.fetchone()


def find_open_approval(business_connection_id: str, customer_chat_id: int,
                       customer_message_id: int | None) -> sqlite3.Row | None:
    with read_cursor() as cur:
        cur.execute(
            "SELECT * FROM pending_approvals "
            "WHERE business_connection_id=? AND customer_chat_id=? AND customer_message_id IS ? "
            "AND status='pending' ORDER BY id DESC LIMIT 1",
            (business_connection_id, customer_chat_id, customer_message_id),
        )
        return cur.fetchone()


def mark_approval(approval_id: int, status: str) -> bool:
    """Atomically transition a pending approval. Returns True iff this call won
    the transition (rowcount==1), guarding against double-taps."""
    with write_tx() as cur:
        cur.execute(
            "UPDATE pending_approvals SET status=?, decided_at=? WHERE id=? AND status='pending'",
            (status, utils.now_iso(), approval_id),
        )
        return cur.rowcount == 1


def claim_approval_delivery(approval_id: int) -> bool:
    """Atomically reserve a pending approval for delivery.

    The transient ``sending`` state preserves double-tap protection without
    claiming that Telegram accepted the message before the network call. The
    timestamp lets an interrupted process leave an auditable claim behind.
    """
    with write_tx() as cur:
        cur.execute(
            "UPDATE pending_approvals SET status='sending', decided_at=? "
            "WHERE id=? AND status='pending'",
            (utils.now_iso(), approval_id),
        )
        return cur.rowcount == 1


def complete_approval_delivery(approval_id: int, status: str) -> bool:
    """Finalize a claimed delivery after Telegram accepts the message."""
    if status not in {"sent", "sent_edited"}:
        raise ValueError(f"invalid delivery status: {status}")
    with write_tx() as cur:
        cur.execute(
            "UPDATE pending_approvals SET status=?, decided_at=? "
            "WHERE id=? AND status='sending'",
            (status, utils.now_iso(), approval_id),
        )
        return cur.rowcount == 1


def release_approval_delivery(approval_id: int) -> bool:
    """Return a failed delivery claim to pending so the owner can retry."""
    with write_tx() as cur:
        cur.execute(
            "UPDATE pending_approvals SET status='pending', decided_at=NULL "
            "WHERE id=? AND status='sending'",
            (approval_id,),
        )
        return cur.rowcount == 1


def mark_approval_delivery_uncertain(approval_id: int) -> bool:
    """Stop retries when Telegram may have accepted some or all content."""
    with write_tx() as cur:
        cur.execute(
            "UPDATE pending_approvals SET status='delivery_uncertain', decided_at=? "
            "WHERE id=? AND status='sending'",
            (utils.now_iso(), approval_id),
        )
        return cur.rowcount == 1


def mark_interrupted_approval_deliveries_uncertain() -> int:
    """Recover claims left by a previous process without risking duplicates.

    A process restart makes every remaining ``sending`` row ambiguous: Telegram
    may have accepted the message immediately before the process stopped. Mark
    those rows for manual review instead of automatically retrying them.
    """
    with write_tx() as cur:
        cur.execute(
            "UPDATE pending_approvals SET status='delivery_uncertain', decided_at=? "
            "WHERE status='sending'",
            (utils.now_iso(),),
        )
        return cur.rowcount


def update_approval_reply(approval_id: int, ai_reply_text: str) -> None:
    with write_tx() as cur:
        cur.execute(
            "UPDATE pending_approvals SET ai_reply_text=? WHERE id=?",
            (ai_reply_text, approval_id),
        )


def list_pending_approvals(status: str = "pending") -> list[sqlite3.Row]:
    with read_cursor() as cur:
        cur.execute(
            "SELECT * FROM pending_approvals WHERE status=? ORDER BY created_at",
            (status,),
        )
        return cur.fetchall()


def count_pending_approvals() -> int:
    with read_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM pending_approvals WHERE status='pending'")
        return int(cur.fetchone()["n"])


def invalidate_approvals_for_messages(business_connection_id: str, customer_chat_id: int,
                                      message_ids: list[int]) -> int:
    if not message_ids:
        return 0
    placeholders = ",".join("?" for _ in message_ids)
    with write_tx() as cur:
        cur.execute(
            f"UPDATE pending_approvals SET status='invalidated', decided_at=? "
            f"WHERE business_connection_id=? AND customer_chat_id=? "
            f"AND customer_message_id IN ({placeholders}) AND status='pending'",
            (utils.now_iso(), business_connection_id, customer_chat_id, *message_ids),
        )
        return cur.rowcount


def expire_stale_approvals(max_age_minutes: int) -> int:
    cutoff = utils.future_iso(-max_age_minutes)
    with write_tx() as cur:
        cur.execute(
            "UPDATE pending_approvals SET status='expired', decided_at=? "
            "WHERE status='pending' AND created_at < ?",
            (utils.now_iso(), cutoff),
        )
        return cur.rowcount


def cancel_all_pending_approvals() -> int:
    with write_tx() as cur:
        cur.execute(
            "UPDATE pending_approvals SET status='cancelled', decided_at=? WHERE status='pending'",
            (utils.now_iso(),),
        )
        return cur.rowcount


# =============================================================================
# DAL — kb_items / kb_chunks
# =============================================================================
def insert_kb_item(title: str, source_type: str, filename: str | None) -> int:
    with write_tx() as cur:
        cur.execute(
            "INSERT INTO kb_items(title, source_type, filename, created_at, chunk_count) "
            "VALUES (?,?,?,?,0)",
            (title, source_type, filename, utils.now_iso()),
        )
        return cur.lastrowid


def set_kb_item_chunk_count(item_id: int, chunk_count: int) -> None:
    with write_tx() as cur:
        cur.execute("UPDATE kb_items SET chunk_count=? WHERE id=?", (chunk_count, item_id))


def get_kb_item(item_id: int) -> sqlite3.Row | None:
    with read_cursor() as cur:
        cur.execute("SELECT * FROM kb_items WHERE id=?", (item_id,))
        return cur.fetchone()


def list_kb_items() -> list[sqlite3.Row]:
    with read_cursor() as cur:
        cur.execute("SELECT * FROM kb_items ORDER BY id")
        return cur.fetchall()


def delete_kb_item(item_id: int) -> int:
    with write_tx() as cur:
        cur.execute("DELETE FROM kb_items WHERE id=?", (item_id,))
        return cur.rowcount


def clear_kb() -> None:
    with write_tx() as cur:
        cur.execute("DELETE FROM kb_chunks")
        cur.execute("DELETE FROM kb_items")


def insert_kb_chunks_bulk(rows: list[tuple[int, int, str, str | None]]) -> None:
    """rows: list of (item_id, chunk_index, content, embedding_json)."""
    now = utils.now_iso()
    with write_tx() as cur:
        cur.executemany(
            "INSERT INTO kb_chunks(item_id, chunk_index, content, embedding_json, created_at) "
            "VALUES (?,?,?,?,?)",
            [(r[0], r[1], r[2], r[3], now) for r in rows],
        )


def get_all_chunks() -> list[sqlite3.Row]:
    with read_cursor() as cur:
        cur.execute("SELECT id, item_id, chunk_index, content, embedding_json FROM kb_chunks")
        return cur.fetchall()


def count_kb_chunks() -> int:
    with read_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM kb_chunks")
        return int(cur.fetchone()["n"])


# =============================================================================
# DAL — api_keys
# =============================================================================
def upsert_api_key(provider: str, masked_key: str, stored_key: str) -> None:
    now = utils.now_iso()
    with write_tx() as cur:
        cur.execute(
            """
            INSERT INTO api_keys(provider, masked_key, encrypted_key_or_plain_local_key, created_at, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(provider) DO UPDATE SET
                masked_key = excluded.masked_key,
                encrypted_key_or_plain_local_key = excluded.encrypted_key_or_plain_local_key,
                updated_at = excluded.updated_at
            """,
            (provider, masked_key, stored_key, now, now),
        )


def get_api_key_row(provider: str) -> sqlite3.Row | None:
    with read_cursor() as cur:
        cur.execute("SELECT * FROM api_keys WHERE provider=?", (provider,))
        return cur.fetchone()


def delete_api_key(provider: str) -> int:
    with write_tx() as cur:
        cur.execute("DELETE FROM api_keys WHERE provider=?", (provider,))
        return cur.rowcount


# =============================================================================
# DAL — owner_pending_input (single active row per owner)
# =============================================================================
def set_owner_pending_input(owner_user_id: int, kind: str, context: dict,
                            ttl_minutes: float) -> None:
    with write_tx() as cur:
        cur.execute(
            """
            INSERT INTO owner_pending_input(owner_user_id, kind, context_json, created_at, expires_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(owner_user_id) DO UPDATE SET
                kind = excluded.kind,
                context_json = excluded.context_json,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
            """,
            (owner_user_id, kind, json.dumps(context), utils.now_iso(), utils.future_iso(ttl_minutes)),
        )


def get_owner_pending_input(owner_user_id: int) -> dict | None:
    with read_cursor() as cur:
        cur.execute("SELECT * FROM owner_pending_input WHERE owner_user_id=?", (owner_user_id,))
        row = cur.fetchone()
    if row is None:
        return None
    if utils.is_expired(row["expires_at"]):
        clear_owner_pending_input(owner_user_id)
        return None
    try:
        context = json.loads(row["context_json"]) if row["context_json"] else {}
    except json.JSONDecodeError:
        context = {}
    return {"kind": row["kind"], "context": context}


def has_owner_pending_input(owner_user_id: int) -> bool:
    return get_owner_pending_input(owner_user_id) is not None


def clear_owner_pending_input(owner_user_id: int) -> None:
    with write_tx() as cur:
        cur.execute("DELETE FROM owner_pending_input WHERE owner_user_id=?", (owner_user_id,))


# =============================================================================
# Maintenance
# =============================================================================
def reset_runtime_state() -> None:
    """Reset behavioral settings to defaults and clear transient tables.
    Does NOT touch business connections, customers, KB, or message history."""
    with write_tx() as cur:
        cur.execute("DELETE FROM settings")
        cur.execute("DELETE FROM paused_chats")
        cur.execute("DELETE FROM owner_pending_input")
        cur.execute(
            "UPDATE pending_approvals SET status='cancelled', decided_at=? WHERE status='pending'",
            (utils.now_iso(),),
        )
    seed_default_settings()
