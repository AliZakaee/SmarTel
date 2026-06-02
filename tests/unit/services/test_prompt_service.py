"""Tests for prompt_service.py: sensitive wrappers and build_messages assembly,
including the strict-KB short-circuit."""

from __future__ import annotations

from services import kb_service, prompt_service as ps


def test_sensitive_wrappers():
    assert ps.is_sensitive("please refund me") is True
    assert ps.is_sensitive("hello there") is False
    assert "refund" in ps.sensitive_categories("please refund me")


def test_build_messages_basic(db):
    messages, short = ps.build_messages("bc1", 5, "Hello")
    assert short is None
    assert messages[0]["role"] == "system"
    assert ps.BASE_SYSTEM_PROMPT in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "Hello"}


def test_build_messages_custom_instructions(db):
    db.set_setting("custom_instructions", "Reply in Persian.")
    messages, _ = ps.build_messages("bc1", 5, "Hi")
    assert "Reply in Persian." in messages[0]["content"]


def test_build_messages_includes_memory_chronologically(db):
    db.insert_memory("bc1", 5, "user", "earlier question")
    db.insert_memory("bc1", 5, "assistant", "earlier answer")
    messages, _ = ps.build_messages("bc1", 5, "now")
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "earlier question"
    assert messages[-1]["content"] == "now"


def test_build_messages_strict_with_chunks(db):
    item = db.insert_kb_item("policy", "text", None)
    db.insert_kb_chunks_bulk([(item, 0, "Refund window is 30 days", None)])
    db.set_setting("strict_kb_mode", True)
    messages, short = ps.build_messages("bc1", 5, "refund?")
    assert short is None
    assert "STRICT KNOWLEDGE BASE MODE" in messages[0]["content"]
    assert "KNOWLEDGE BASE CONTEXT" in messages[0]["content"]


def test_build_messages_strict_short_circuit(db):
    db.set_setting("strict_kb_mode", True)
    messages, short = ps.build_messages("bc1", 5, "anything")
    assert messages == [] and short == kb_service.STRICT_NO_INFO
