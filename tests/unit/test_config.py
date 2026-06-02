"""Tests for config.py: bool/int/path parsing, token & owner-id validators, and
Config.from_env / validate / masked_summary."""

from __future__ import annotations

from pathlib import Path

import pytest

import config

VALID_TOKEN = "123456789:" + "A" * 35


# --- _parse_bool -------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("yes", True), ("on", True), ("1", True), ("y", True), ("true", True),
    ("no", False), ("off", False), ("0", False), ("n", False), ("", False),
])
def test_parse_bool_known_values(value, expected):
    assert config._parse_bool(value) is expected


def test_parse_bool_none_and_junk_use_default():
    assert config._parse_bool(None, default=True) is True
    assert config._parse_bool("banana", default=True) is True
    assert config._parse_bool("banana", default=False) is False


def test_parse_bool_passes_through_actual_bool():
    assert config._parse_bool(True) is True
    assert config._parse_bool(False) is False


# --- _to_int -----------------------------------------------------------------
def test_to_int():
    assert config._to_int("42") == 42
    assert config._to_int(None, default=7) == 7
    assert config._to_int("nope", default=3) == 3


# --- _resolve_path -----------------------------------------------------------
def test_resolve_path_relative_under_base():
    p = config._resolve_path("storage/x.db", "fallback")
    assert p.is_absolute()
    assert p == config.BASE_DIR / "storage/x.db"


def test_resolve_path_absolute_unchanged():
    p = config._resolve_path("/tmp/abs.db", "fallback")
    assert p == Path("/tmp/abs.db")


def test_resolve_path_uses_fallback_when_blank():
    p = config._resolve_path("", "storage/smartel.db")
    assert p == config.BASE_DIR / "storage/smartel.db"


# --- validators --------------------------------------------------------------
def test_validate_token():
    assert config._validate_token(VALID_TOKEN)[0] is True
    assert config._validate_token("nope")[0] is False
    assert config._validate_token("123:short")[0] is False  # suffix < 30 chars


def test_validate_owner_id():
    assert config._validate_owner_id("123")[0] is True
    assert config._validate_owner_id("0")[0] is False
    assert config._validate_owner_id("-5")[0] is False
    assert config._validate_owner_id("abc")[0] is False


# --- Config.from_env ---------------------------------------------------------
def test_from_env_reads_environment(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", VALID_TOKEN)
    monkeypatch.setenv("OWNER_USER_ID", "555")
    monkeypatch.setenv("MODEL_BACKEND", "CODEX")  # should be lowercased
    monkeypatch.setenv("WORKER_THREADS", "0")     # floored to >= 1
    cfg = config.Config.from_env()
    assert cfg.bot_token == VALID_TOKEN
    assert cfg.owner_user_id == 555
    assert cfg.model_backend == "codex"
    assert cfg.worker_threads == 1


def test_from_env_defaults(monkeypatch):
    cfg = config.Config.from_env()
    assert cfg.bot_token == ""
    assert cfg.model_backend == "openai"
    assert cfg.default_model == "gpt-4.1-mini"


# --- Config.validate ---------------------------------------------------------
def _cfg(**overrides):
    base = dict(
        bot_token=VALID_TOKEN, owner_user_id=5, openai_api_key="", openai_base_url="",
        model_backend="openai", codex_model="", fernet_key="", default_model="m",
        default_auto_reply=False, default_approval_mode=True,
        db_path=Path("x"), uploads_dir=Path("u"), vector_store_dir=Path("v"),
        worker_threads=4, debug=False,
    )
    base.update(overrides)
    return config.Config(**base)


def test_validate_ok_without_openai_key():
    _cfg(openai_api_key="").validate()  # must not raise


def test_validate_missing_token_raises():
    with pytest.raises(config.ConfigError):
        _cfg(bot_token="").validate()


def test_validate_bad_token_raises():
    with pytest.raises(config.ConfigError):
        _cfg(bot_token="not-a-token").validate()


def test_validate_non_positive_owner_raises():
    with pytest.raises(config.ConfigError):
        _cfg(owner_user_id=0).validate()


# --- masked_summary ----------------------------------------------------------
def test_masked_summary_hides_token():
    summary = _cfg(bot_token=VALID_TOKEN).masked_summary()
    assert VALID_TOKEN not in summary
    assert "Bot token:" in summary
