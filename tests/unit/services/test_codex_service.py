"""Tests for codex_service.py: prompt building, flag-set selection, error
classification, timeout parsing, and the chat flow (subprocess fully mocked)."""

from __future__ import annotations

import pytest

from services import codex_service as cx
from services.openai_service import OpenAIServiceError


# --- binary / availability ---------------------------------------------------
def test_binary_default_and_override(monkeypatch):
    assert cx.binary() == "codex"
    monkeypatch.setenv("CODEX_BIN", "/opt/codex")
    assert cx.binary() == "/opt/codex"


def test_is_available(monkeypatch):
    monkeypatch.setattr(cx.shutil, "which", lambda b: "/usr/bin/codex")
    assert cx.is_available() is True
    monkeypatch.setattr(cx.shutil, "which", lambda b: None)
    monkeypatch.setenv("CODEX_BIN", "definitely-not-a-real-binary-xyz")
    assert cx.is_available() is False


# --- _build_prompt -----------------------------------------------------------
def test_build_prompt_structure():
    messages = [
        {"role": "system", "content": "Be nice"},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "Bye"},
    ]
    p = cx._build_prompt(messages)
    assert cx.CODEX_INSTRUCTION in p
    assert "Business assistant instructions:\nBe nice" in p
    assert "--- Conversation ---" in p and "--- End conversation ---" in p
    assert p.index("Customer: Hi") < p.index("Assistant: Hello") < p.index("Customer: Bye")


def test_build_prompt_without_system():
    p = cx._build_prompt([{"role": "user", "content": "Hi"}])
    assert "Business assistant instructions" not in p
    assert "Customer: Hi" in p


# --- _flag_sets --------------------------------------------------------------
def test_flag_sets_default_then_minimal(monkeypatch):
    sets = cx._flag_sets()
    assert sets == [cx._DEFAULT_EXEC_FLAGS, cx._MINIMAL_EXEC_FLAGS]


def test_flag_sets_override(monkeypatch):
    monkeypatch.setenv("CODEX_EXEC_ARGS", "--foo bar")
    assert cx._flag_sets() == [["--foo", "bar"]]


# --- error classification ----------------------------------------------------
def test_is_arg_error():
    assert cx._is_arg_error("error: unexpected argument '--sandbox'") is True
    assert cx._is_arg_error("error: bad thing\nusage: codex exec") is True
    assert cx._is_arg_error("something else entirely") is False


def test_looks_like_auth_error():
    assert cx._looks_like_auth_error("401 Unauthorized") is True
    assert cx._looks_like_auth_error("please run codex login") is True
    assert cx._looks_like_auth_error("disk full") is False


def test_timeout(monkeypatch):
    assert cx._timeout() == cx.DEFAULT_TIMEOUT
    monkeypatch.setenv("CODEX_EXEC_TIMEOUT", "30")
    assert cx._timeout() == 30
    monkeypatch.setenv("CODEX_EXEC_TIMEOUT", "not-a-number")
    assert cx._timeout() == cx.DEFAULT_TIMEOUT


# --- chat --------------------------------------------------------------------
def test_chat_unavailable_raises(monkeypatch):
    monkeypatch.setattr(cx, "is_available", lambda: False)
    with pytest.raises(OpenAIServiceError) as exc:
        cx.chat([{"role": "user", "content": "hi"}])
    assert exc.value.kind == "unknown"


def test_chat_happy_path(monkeypatch):
    monkeypatch.setattr(cx, "is_available", lambda: True)
    monkeypatch.setattr(cx, "_run_exec", lambda *a, **k: (0, "the reply", ""))
    assert cx.chat([{"role": "user", "content": "hi"}]) == "the reply"


def test_chat_retries_on_arg_error(monkeypatch):
    monkeypatch.setattr(cx, "is_available", lambda: True)
    seq = [(1, "", "error: unexpected argument '--sandbox'"), (0, "fallback reply", "")]
    calls = {"n": 0}

    def fake_run(*a, **k):
        result = seq[calls["n"]]
        calls["n"] += 1
        return result

    monkeypatch.setattr(cx, "_run_exec", fake_run)
    assert cx.chat([{"role": "user", "content": "hi"}]) == "fallback reply"
    assert calls["n"] == 2


def test_chat_auth_error(monkeypatch):
    monkeypatch.setattr(cx, "is_available", lambda: True)
    monkeypatch.setattr(cx, "_run_exec", lambda *a, **k: (1, "", "please run codex login"))
    with pytest.raises(OpenAIServiceError) as exc:
        cx.chat([{"role": "user", "content": "hi"}])
    assert exc.value.kind == "auth"


def test_chat_empty_reply(monkeypatch):
    monkeypatch.setattr(cx, "is_available", lambda: True)
    monkeypatch.setattr(cx, "_run_exec", lambda *a, **k: (0, "", ""))
    with pytest.raises(OpenAIServiceError) as exc:
        cx.chat([{"role": "user", "content": "hi"}])
    assert "empty" in exc.value.owner_message.lower()
