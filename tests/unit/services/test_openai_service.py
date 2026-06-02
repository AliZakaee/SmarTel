"""Tests for openai_service.py: key protection, env routing, error mapping,
chat (incl. max_tokens retry), embeddings, and key validation. The OpenAI SDK is
never constructed against the network — get_client / OpenAI are patched."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services import openai_service as svc
from services.openai_service import OpenAIServiceError


def _resp(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


# --- fingerprint / protect / unprotect --------------------------------------
def test_fingerprint_is_deterministic():
    fp = svc._fingerprint("sk-abc")
    assert fp == svc._fingerprint("sk-abc")
    assert fp != svc._fingerprint("sk-xyz")
    assert len(fp) == 12


def test_protect_unprotect_fernet_roundtrip(fernet_env):
    stored = svc._protect("sk-secret-value")
    assert stored.startswith("fernet:")
    assert svc._unprotect(stored) == "sk-secret-value"


def test_protect_plain_fallback_without_key():
    stored = svc._protect("sk-secret")
    assert stored.startswith("plain:")
    assert svc._unprotect(stored) == "sk-secret"


def test_unprotect_handles_missing_and_legacy():
    assert svc._unprotect(None) is None
    assert svc._unprotect("legacy-unprefixed") == "legacy-unprefixed"


def test_unprotect_fernet_without_key_returns_none(monkeypatch):
    monkeypatch.delenv("SMARTEL_FERNET_KEY", raising=False)
    assert svc._unprotect("fernet:Z0FBQUFB") is None


def test_unprotect_wrong_key_returns_none(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SMARTEL_FERNET_KEY", Fernet.generate_key().decode())
    stored = svc._protect("sk-secret")
    monkeypatch.setenv("SMARTEL_FERNET_KEY", Fernet.generate_key().decode())  # different key
    assert svc._unprotect(stored) is None


# --- env routing -------------------------------------------------------------
def test_resolve_api_key_prefers_env(monkeypatch, db):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert svc._resolve_api_key() == "sk-from-env"


def test_resolve_api_key_from_db(db):
    db.upsert_api_key("openai", "sk-...end", "plain:sk-stored")
    assert svc._resolve_api_key() == "sk-stored"


def test_base_url_and_custom_endpoint(monkeypatch):
    assert svc.using_custom_endpoint() is False
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:1234/v1")
    assert svc._resolve_base_url() == "http://localhost:1234/v1"
    assert svc.using_custom_endpoint() is True


def test_is_ready(monkeypatch, db):
    assert svc.is_ready() is False  # no key, no endpoint
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert svc.is_ready() is True


def test_is_ready_codex_delegates(monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "codex")
    monkeypatch.setattr("services.codex_service.is_available", lambda: True)
    assert svc.is_ready() is True


def test_backend_label(monkeypatch):
    assert svc.backend_label() == "OpenAI API"
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:1234/v1")
    assert "custom endpoint" in svc.backend_label()
    monkeypatch.setenv("MODEL_BACKEND", "codex")
    assert svc.backend_label() == "ChatGPT via Codex"


# --- error mapping -----------------------------------------------------------
@pytest.mark.parametrize("attr,kind", [
    ("AuthenticationError", "auth"),
    ("RateLimitError", "rate_limit"),
    ("BadRequestError", "bad_request"),
    ("APITimeoutError", "timeout"),
    ("APIConnectionError", "connection"),
])
def test_map_error_each_type(monkeypatch, attr, kind):
    class DummyError(Exception):
        pass

    monkeypatch.setattr(svc, attr, DummyError)
    err = svc._map_error(DummyError("boom"))
    assert err.kind == kind
    assert isinstance(err.cause, DummyError)
    assert err.owner_message  # non-empty sanitized message


def test_map_error_unknown():
    err = svc._map_error(ValueError("weird"))
    assert err.kind == "unknown"


# --- chat --------------------------------------------------------------------
def test_chat_codex_delegates(monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "codex")
    monkeypatch.setattr("services.codex_service.chat", lambda messages, model=None: "codex reply")
    assert svc.chat([{"role": "user", "content": "hi"}]) == "codex reply"


def test_chat_happy_path(db, monkeypatch):
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: _resp(" hello ")))
    )
    monkeypatch.setattr(svc, "get_client", lambda: client)
    assert svc.chat([{"role": "user", "content": "hi"}]) == "hello"


def test_chat_retries_with_max_completion_tokens(db, monkeypatch):
    class BadReq(Exception):
        pass

    monkeypatch.setattr(svc, "BadRequestError", BadReq)
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if "max_tokens" in kwargs:
            raise BadReq("Unsupported parameter: 'max_tokens'")
        return _resp("ok")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    monkeypatch.setattr(svc, "get_client", lambda: client)

    assert svc.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert len(calls) == 2
    assert "max_completion_tokens" in calls[1] and "max_tokens" not in calls[1]


def test_chat_maps_errors(db, monkeypatch):
    def boom(**kwargs):
        raise ValueError("network down")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=boom))
    )
    monkeypatch.setattr(svc, "get_client", lambda: client)
    with pytest.raises(OpenAIServiceError) as exc:
        svc.chat([{"role": "user", "content": "hi"}])
    assert exc.value.kind == "unknown"


# --- embeddings --------------------------------------------------------------
def test_embed_empty_returns_empty():
    assert svc.embed([]) == []


def test_embed_returns_vectors(monkeypatch):
    data = SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2]),
                                 SimpleNamespace(embedding=[0.3, 0.4])])
    client = SimpleNamespace(
        embeddings=SimpleNamespace(create=lambda **kw: data)
    )
    monkeypatch.setattr(svc, "get_client", lambda: client)
    assert svc.embed(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]


# --- validate_key ------------------------------------------------------------
def test_validate_key_no_sdk(monkeypatch):
    monkeypatch.setattr(svc, "OpenAI", None)
    ok, msg = svc.validate_key("sk-x")
    assert ok is False and "not installed" in msg


def test_validate_key_rejects_non_sk(monkeypatch):
    ok, msg = svc.validate_key("bad-key")
    assert ok is False and "sk-" in msg


def test_validate_key_success(monkeypatch):
    fake_client = SimpleNamespace(models=SimpleNamespace(list=lambda: []))
    monkeypatch.setattr(svc, "OpenAI", lambda **kw: fake_client)
    ok, msg = svc.validate_key("sk-valid")
    assert ok is True
