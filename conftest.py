"""Shared pytest fixtures for the SmarTel test suite.

Lives at the repo root so its fixtures are visible to every test module and so
the repo root is importable (the app uses flat imports like ``import database``).

Design notes:
* ``database`` keeps module-level state (``_DB_PATH``, ``_SEED_DEFAULTS`` and a
  ``threading.local`` connection cache). The ``db`` fixture initialises a fresh
  temp DB per test and tears that state down so tests never leak into each other.
* ``clean_env`` (autouse) strips the developer's real ``.env``/shell secrets so
  service routing (OpenAI vs Codex vs custom endpoint) is deterministic.
* Telegram ``Message``/``CallbackQuery`` objects are duck-typed with
  ``SimpleNamespace`` — the code only ever uses attribute access, so we don't
  need to build real telebot payloads.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402  (import after sys.path setup)
import database  # noqa: E402


# Env vars the services read at runtime. Cleared before each test so a real
# local .env / shell environment can never change a test's outcome.
_MANAGED_ENV_VARS = [
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL_BACKEND", "SMARTEL_FERNET_KEY",
    "CODEX_MODEL", "CODEX_BIN", "CODEX_EXEC_ARGS", "CODEX_EXEC_TIMEOUT",
    "CODEX_HOME", "DB_PATH", "UPLOADS_DIR", "VECTOR_STORE_DIR", "WORKER_THREADS",
    "DEBUG", "BOT_TOKEN", "OWNER_USER_ID", "DEFAULT_MODEL", "DEFAULT_AUTO_REPLY",
    "DEFAULT_APPROVAL_MODE",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Strip managed env vars so runtime routing is deterministic per test."""
    for name in _MANAGED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def fake_config(tmp_path):
    """A real frozen Config built directly (never from .env), pointed at tmp_path."""
    return config.Config(
        bot_token="123456789:" + "A" * 35,
        owner_user_id=12345,
        openai_api_key="",
        openai_base_url="",
        model_backend="openai",
        codex_model="",
        fernet_key="",
        default_model="gpt-4.1-mini",
        default_auto_reply=False,
        default_approval_mode=True,
        db_path=tmp_path / "smartel.db",
        uploads_dir=tmp_path / "uploads",
        vector_store_dir=tmp_path / "vector_store",
        worker_threads=4,
        debug=False,
    )


@pytest.fixture
def db(fake_config):
    """A fully initialised temp SQLite database; resets module state on teardown."""
    database.init_db(fake_config)
    try:
        yield database
    finally:
        conn = getattr(database._local, "conn", None)
        if conn is not None:
            conn.close()
            del database._local.conn
        database._DB_PATH = None
        database._SEED_DEFAULTS = {}


@pytest.fixture
def fernet_env(monkeypatch):
    """Set a fresh Fernet key in the environment; returns the key string."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SMARTEL_FERNET_KEY", key)
    return key


@pytest.fixture
def make_message():
    """Factory for a duck-typed Telegram Message (override any field via kwargs)."""
    def _make(**kw):
        from_user = SimpleNamespace(
            id=kw.pop("user_id", 777),
            first_name=kw.pop("first_name", "Cara"),
            last_name=kw.pop("last_name", None),
            username=kw.pop("username", None),
        )
        fields = dict(
            chat=SimpleNamespace(id=kw.pop("chat_id", 5001)),
            from_user=from_user,
            message_id=kw.pop("message_id", 9001),
            text=kw.pop("text", "hello"),
            caption=kw.pop("caption", None),
            content_type=kw.pop("content_type", "text"),
            business_connection_id=kw.pop("business_connection_id", None),
            sender_business_bot=kw.pop("sender_business_bot", None),
        )
        fields.update(kw)
        return SimpleNamespace(**fields)
    return _make


@pytest.fixture
def make_call():
    """Factory for a duck-typed Telegram CallbackQuery."""
    def _make(**kw):
        return SimpleNamespace(
            id=kw.pop("id", "cb1"),
            data=kw.pop("data", "apr:send:1"),
            from_user=SimpleNamespace(id=kw.pop("user_id", 12345)),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=kw.pop("chat_id", 12345)),
                message_id=kw.pop("message_id", 4242),
            ),
        )
    return _make


@pytest.fixture
def stub_bot(fake_config):
    """A MagicMock TeleBot wired into handlers + security; resets globals after."""
    import handlers
    import security
    bot = MagicMock(name="TeleBot")
    handlers.init(bot, fake_config)
    security.init(bot, fake_config.owner_user_id)
    try:
        yield bot
    finally:
        handlers.bot = None
        handlers.cfg = None
        security._owner_id = None
        security._bot = None
        security._last_notice.clear()


@pytest.fixture
def file_service_init(fake_config):
    """Initialise file_service against the temp uploads dir; resets on teardown."""
    from services import file_service
    file_service.init(fake_config.uploads_dir)
    try:
        yield file_service
    finally:
        file_service._uploads_dir = None
