"""Tests for business_service.py: can_reply resolution, display names, and pause
lifecycle (incl. lazy expiry cleanup)."""

from __future__ import annotations

from types import SimpleNamespace

from freezegun import freeze_time

from services import business_service as bs


# --- resolve_can_reply -------------------------------------------------------
def test_resolve_can_reply_rights_true():
    bc = SimpleNamespace(rights=SimpleNamespace(can_reply=True))
    assert bs.resolve_can_reply(bc) is True


def test_resolve_can_reply_rights_false():
    bc = SimpleNamespace(rights=SimpleNamespace(can_reply=False))
    assert bs.resolve_can_reply(bc) is False


def test_resolve_can_reply_legacy_fallback():
    bc = SimpleNamespace(rights=None, can_reply=True)
    assert bs.resolve_can_reply(bc) is True


def test_resolve_can_reply_default_true():
    assert bs.resolve_can_reply(SimpleNamespace()) is True


# --- connections -------------------------------------------------------------
def test_can_reply_requires_enabled_and_permission(db):
    db.upsert_business_connection("bc1", 999, True, True)
    assert bs.can_reply("bc1") is True
    db.upsert_business_connection("bc1", 999, False, True)
    assert bs.can_reply("bc1") is False
    assert bs.can_reply("missing") is False


# --- display names -----------------------------------------------------------
def test_display_name_full(db):
    db.upsert_customer("bc1", 5, "Ann", "Lee", "annlee")
    assert bs.customer_display_name("bc1", 5) == "Ann Lee (@annlee)"


def test_display_name_first_only(db):
    db.upsert_customer("bc1", 6, "Bob", None, None)
    assert bs.customer_display_name("bc1", 6) == "Bob"


def test_display_name_username_only(db):
    db.upsert_customer("bc1", 7, None, None, "ghost")
    assert bs.customer_display_name("bc1", 7) == "(@ghost)"


def test_display_name_no_row(db):
    assert bs.customer_display_name("bc1", 999) == "chat 999"


# --- pause lifecycle ---------------------------------------------------------
def test_pause_indefinite(db):
    bs.pause_chat("bc1", 5, "manual", None)
    assert bs.is_chat_paused("bc1", 5) is True


def test_pause_timed_expiry_lazy_cleanup(db):
    with freeze_time("2024-01-01 12:00:00"):
        bs.pause_chat("bc1", 5, "rate_limit", 30)
        assert bs.is_chat_paused("bc1", 5) is True
    with freeze_time("2024-01-01 13:00:00"):
        assert bs.is_chat_paused("bc1", 5) is False
        assert db.get_pause("bc1", 5) is None  # lazily deleted on read


def test_no_pause_row(db):
    assert bs.is_chat_paused("bc1", 5) is False


def test_resume(db):
    bs.pause_chat("bc1", 5, "manual", None)
    assert bs.resume_chat("bc1", 5) is True
    assert bs.resume_chat("bc1", 5) is False
