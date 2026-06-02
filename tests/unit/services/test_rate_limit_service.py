"""Tests for rate_limit_service.py: rolling-window check and enforce (pause)."""

from __future__ import annotations

from freezegun import freeze_time

from services import rate_limit_service as rl


def test_check_at_and_over_limit(db):
    db.set_setting("max_messages_per_customer_per_hour", 3)
    with freeze_time("2024-01-01 12:00:00"):
        for _ in range(3):
            db.insert_message("bc1", 5, None, "in", "user", "hi")
        decision = rl.check("bc1", 5)
        assert decision.used == 3 and decision.allowed and decision.remaining == 0
        db.insert_message("bc1", 5, None, "in", "user", "hi")
        over = rl.check("bc1", 5)
        assert over.used == 4 and over.allowed is False


def test_check_excludes_old_messages(db):
    db.set_setting("max_messages_per_customer_per_hour", 3)
    with freeze_time("2024-01-01 10:00:00"):
        db.insert_message("bc1", 5, None, "in", "user", "old")
    with freeze_time("2024-01-01 12:00:00"):
        db.insert_message("bc1", 5, None, "in", "user", "new")
        assert rl.check("bc1", 5).used == 1


def test_check_ignores_outbound(db):
    db.set_setting("max_messages_per_customer_per_hour", 3)
    with freeze_time("2024-01-01 12:00:00"):
        db.insert_message("bc1", 5, None, "out", "assistant", "reply")
        assert rl.check("bc1", 5).used == 0


def test_enforce_pauses_when_exceeded(db):
    db.set_setting("max_messages_per_customer_per_hour", 1)
    with freeze_time("2024-01-01 12:00:00"):
        db.insert_message("bc1", 5, None, "in", "user", "1")
        db.insert_message("bc1", 5, None, "in", "user", "2")
        decision = rl.enforce("bc1", 5)
    assert decision.allowed is False
    assert db.get_pause("bc1", 5) is not None


def test_enforce_no_pause_within_limit(db):
    db.set_setting("max_messages_per_customer_per_hour", 5)
    with freeze_time("2024-01-01 12:00:00"):
        db.insert_message("bc1", 5, None, "in", "user", "1")
        decision = rl.enforce("bc1", 5)
    assert decision.allowed is True
    assert db.get_pause("bc1", 5) is None
