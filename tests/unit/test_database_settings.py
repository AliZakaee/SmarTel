"""Tests for database.py settings layer: coercion, serialization, typed
round-trips, fallbacks, and default seeding."""

from __future__ import annotations

import pytest

import database


# --- _coerce / _serialize (pure) --------------------------------------------
def test_coerce_bool():
    assert database._coerce("true", bool) is True
    assert database._coerce("off", bool) is False
    assert database._coerce("", bool) is False
    with pytest.raises(ValueError):
        database._coerce("maybe", bool)


def test_coerce_numeric_and_str():
    assert database._coerce("42", int) == 42
    assert database._coerce("0.4", float) == 0.4
    assert database._coerce("hi", str) == "hi"


def test_serialize():
    assert database._serialize(True, bool) == "true"
    assert database._serialize(False, bool) == "false"
    assert database._serialize(42, int) == "42"
    assert database._serialize("x", str) == "x"


# --- typed round-trips -------------------------------------------------------
def test_setting_roundtrips(db):
    db.set_setting("temperature", 0.7)
    assert db.get_float("temperature") == 0.7
    db.set_setting("max_tokens", 500)
    assert db.get_int("max_tokens") == 500
    db.set_setting("auto_reply_enabled", True)
    assert db.get_bool("auto_reply_enabled") is True
    db.set_setting("openai_model", "gpt-4o")
    assert db.get_str("openai_model") == "gpt-4o"


def test_empty_string_setting_roundtrip(db):
    db.set_setting("custom_instructions", "")
    assert db.get_str("custom_instructions") == ""


# --- error handling ----------------------------------------------------------
def test_unknown_key_raises(db):
    with pytest.raises(KeyError):
        db.get_setting("nope")
    with pytest.raises(KeyError):
        db.set_setting("nope", 1)


def test_corrupt_value_falls_back_to_default(db):
    with db.write_tx() as cur:
        cur.execute("UPDATE settings SET value='notanint' WHERE key='max_tokens'")
    assert db.get_int("max_tokens") == 800  # schema default


# --- seeding -----------------------------------------------------------------
def test_seed_does_not_overwrite_existing(db):
    db.set_setting("openai_model", "custom-model")
    db.seed_default_settings()  # INSERT OR IGNORE -> keeps the user value
    assert db.get_str("openai_model") == "custom-model"


def test_init_seeds_config_defaults(db):
    # fake_config seeds default_model gpt-4.1-mini, approval mode True, auto False.
    assert db.get_str("openai_model") == "gpt-4.1-mini"
    assert db.get_bool("approval_mode_enabled") is True
    assert db.get_bool("auto_reply_enabled") is False


def test_get_all_settings_has_every_key(db):
    assert set(db.get_all_settings().keys()) == set(database.SETTINGS_SCHEMA.keys())
