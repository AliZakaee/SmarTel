"""Tests for memory_service.py: chronological retrieval, trimming, and the
memory-enabled gate."""

from __future__ import annotations

from services import memory_service as mem


def test_get_recent_is_chronological(db):
    mem.append("bc1", 5, "user", "u1")
    mem.append("bc1", 5, "assistant", "a1")
    mem.append("bc1", 5, "user", "u2")
    recent = mem.get_recent("bc1", 5)
    assert [m["content"] for m in recent] == ["u1", "a1", "u2"]
    assert recent[0]["role"] == "user"


def test_append_trims_to_max(db):
    for i in range(mem.MAX_MEMORY_TURNS + 3):
        mem.append("bc1", 5, "user", f"m{i}")
    rows = db.get_recent_memory("bc1", 5, 100)
    assert len(rows) == mem.MAX_MEMORY_TURNS
    recent = mem.get_recent("bc1", 5, 100)
    assert recent[-1]["content"] == f"m{mem.MAX_MEMORY_TURNS + 2}"  # newest kept


def test_append_exchange(db):
    mem.append_exchange("bc1", 5, "question", "answer")
    recent = mem.get_recent("bc1", 5)
    assert [m["content"] for m in recent] == ["question", "answer"]


def test_memory_disabled_is_noop(db):
    db.set_setting("memory_enabled", False)
    mem.append("bc1", 5, "user", "ignored")
    db.set_setting("memory_enabled", True)
    assert mem.get_recent("bc1", 5) == []  # nothing was stored while disabled


def test_clear(db):
    mem.append("bc1", 5, "user", "x")
    mem.clear("bc1", 5)
    assert mem.get_recent("bc1", 5) == []
