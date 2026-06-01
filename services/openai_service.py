"""OpenAI client lifecycle, chat + embedding calls, API-key storage (with
optional Fernet encryption at rest), and clean error mapping.

This is the only module that imports ``openai`` exceptions and the only place
that catches/translates them, so the rest of the app never sees raw OpenAI
errors (and customers never see error detail).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading

import database
import utils

log = logging.getLogger("smartel.openai")

# OpenAI SDK imports (defensive: tolerate older/newer layouts).
try:
    from openai import OpenAI
except Exception as e:  # pragma: no cover
    OpenAI = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

try:
    from openai import (
        APIConnectionError,
        APIError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        RateLimitError,
    )
except Exception:  # pragma: no cover - fall back to base Exception
    APIError = APIConnectionError = APITimeoutError = AuthenticationError = \
        BadRequestError = RateLimitError = Exception

PROVIDER = "openai"
DEFAULT_EMBED_MODEL = "text-embedding-3-small"
GENERIC_CUSTOMER_APOLOGY = "Sorry, I'm having trouble responding right now. Someone will get back to you shortly."

_client_lock = threading.Lock()
_client = None
_client_fingerprint: str | None = None


class OpenAIServiceError(Exception):
    """Internal, sanitized error. ``owner_message`` is safe to show the OWNER
    (never shown verbatim to a customer)."""

    def __init__(self, owner_message: str, *, kind: str, cause: Exception | None = None):
        super().__init__(owner_message)
        self.owner_message = owner_message
        self.kind = kind
        self.cause = cause


# =============================================================================
# API-key protection (at rest)
# =============================================================================
def _fernet():
    key = (os.environ.get("SMARTEL_FERNET_KEY") or "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception as e:  # missing lib or bad key
        log.warning("Fernet unavailable (%s); using plain-local key storage.", type(e).__name__)
        return None


def _protect(api_key: str) -> str:
    f = _fernet()
    if f is not None:
        try:
            return "fernet:" + f.encrypt(api_key.encode()).decode()
        except Exception:
            log.warning("Fernet encryption failed; storing plain-local.")
    return "plain:" + api_key


def _unprotect(stored: str | None) -> str | None:
    if not stored:
        return None
    if stored.startswith("fernet:"):
        f = _fernet()
        if f is None:
            return None
        try:
            return f.decrypt(stored[len("fernet:"):].encode()).decode()
        except Exception:
            log.warning("Could not decrypt stored API key (wrong/lost SMARTEL_FERNET_KEY).")
            return None
    if stored.startswith("plain:"):
        return stored[len("plain:"):]
    return stored  # legacy / unprefixed


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


# =============================================================================
# Key resolution & storage
# =============================================================================
def _resolve_api_key() -> str | None:
    env_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if env_key:
        return env_key
    row = database.get_api_key_row(PROVIDER)
    if row is not None:
        return _unprotect(row["encrypted_key_or_plain_local_key"])
    return None


def _resolve_base_url() -> str | None:
    """Custom OpenAI-compatible endpoint (e.g. a local Codex-backed proxy, a
    self-hosted model, or another provider). None => the default OpenAI API."""
    return (os.environ.get("OPENAI_BASE_URL") or "").strip() or None


def using_custom_endpoint() -> bool:
    return _resolve_base_url() is not None


def has_api_key() -> bool:
    return bool(_resolve_api_key())


def is_ready() -> bool:
    """Whether the AI backend can be used: a key is set, OR a custom endpoint is
    configured (which may not require a key)."""
    return bool(_resolve_api_key()) or using_custom_endpoint()


def backend_label() -> str:
    base = _resolve_base_url()
    if not base:
        return "OpenAI API"
    try:
        from urllib.parse import urlparse
        host = urlparse(base).netloc or base
    except Exception:
        host = base
    return f"custom endpoint ({host})"


def masked_active_key() -> str:
    key = _resolve_api_key()
    return utils.mask_key(key) if key else "<not set>"


def store_api_key(api_key: str, *, persist_env: bool = True) -> None:
    """Persist a new OpenAI API key: process env, encrypted DB copy, and .env."""
    api_key = api_key.strip()
    os.environ["OPENAI_API_KEY"] = api_key
    database.upsert_api_key(PROVIDER, utils.mask_key(api_key), _protect(api_key))
    if persist_env:
        try:
            import config
            cfg = config.Config.from_env()
            config.write_env(cfg)
        except Exception as e:  # don't fail the whole command if .env write fails
            log.warning("Could not persist key to .env: %s", type(e).__name__)
    _invalidate_client()
    log.info("OpenAI API key updated (%s)", utils.mask_key(api_key))


def disconnect_api_key() -> None:
    os.environ.pop("OPENAI_API_KEY", None)
    database.delete_api_key(PROVIDER)
    _invalidate_client()
    log.info("OpenAI API key disconnected.")


# =============================================================================
# Client lifecycle
# =============================================================================
def _invalidate_client() -> None:
    global _client, _client_fingerprint
    with _client_lock:
        _client = None
        _client_fingerprint = None


def get_client():
    global _client, _client_fingerprint
    if OpenAI is None:
        raise OpenAIServiceError(
            "The openai package is not installed. Run: pip install -r requirements.txt",
            kind="unknown", cause=_IMPORT_ERROR,
        )
    base_url = _resolve_base_url()
    key = _resolve_api_key()
    if not key:
        if base_url:
            # Proxies / local servers that ignore auth still need a non-empty
            # string (the SDK rejects an empty api_key at construction).
            key = "sk-no-key-required"
        else:
            raise OpenAIServiceError(
                "No OpenAI API key configured. Use /connect_openai to add one, "
                "or set OPENAI_BASE_URL to a custom endpoint.",
                kind="auth",
            )
    fp = _fingerprint(f"{key}|{base_url or ''}")
    with _client_lock:
        if _client is None or _client_fingerprint != fp:
            _client = OpenAI(api_key=key, base_url=base_url, timeout=60.0)
            _client_fingerprint = fp
        return _client


# =============================================================================
# Error mapping
# =============================================================================
def _map_error(e: Exception) -> OpenAIServiceError:
    if isinstance(e, AuthenticationError):
        return OpenAIServiceError("OpenAI rejected the API key. Check /connect_openai.",
                                  kind="auth", cause=e)
    if isinstance(e, RateLimitError):
        return OpenAIServiceError("OpenAI rate limit or quota reached. Try again shortly.",
                                  kind="rate_limit", cause=e)
    if isinstance(e, BadRequestError):
        return OpenAIServiceError("OpenAI rejected the request (check the model name/parameters).",
                                  kind="bad_request", cause=e)
    if isinstance(e, APITimeoutError):
        return OpenAIServiceError("OpenAI timed out. Try again.", kind="timeout", cause=e)
    if isinstance(e, APIConnectionError):
        return OpenAIServiceError("Could not reach OpenAI. Check the network connection.",
                                  kind="connection", cause=e)
    return OpenAIServiceError("OpenAI error. See logs for detail.", kind="unknown", cause=e)


# =============================================================================
# Chat
# =============================================================================
def chat(messages: list[dict], *, model: str | None = None,
         temperature: float | None = None, max_tokens: int | None = None) -> str:
    client = get_client()
    model = model or database.get_str("openai_model")
    temperature = database.get_float("temperature") if temperature is None else temperature
    max_tokens = database.get_int("max_tokens") if max_tokens is None else max_tokens

    try:
        return _chat_once(client, model, messages, temperature, "max_tokens", max_tokens)
    except BadRequestError as e:
        # Some newer (reasoning) models reject `max_tokens` and require
        # `max_completion_tokens`. Retry once in that specific case.
        if "max_tokens" in str(e).lower():
            log.info("Retrying chat with max_completion_tokens for model %s", model)
            try:
                return _chat_once(client, model, messages, temperature,
                                  "max_completion_tokens", max_tokens)
            except Exception as e2:
                raise _map_error(e2) from e2
        log.exception("OpenAI bad request")
        raise _map_error(e) from e
    except Exception as e:
        log.exception("OpenAI chat failed")
        raise _map_error(e) from e


def _chat_once(client, model, messages, temperature, token_kw: str, token_val: int) -> str:
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        token_kw: token_val,
    }
    resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content
    return (content or "").strip()


# =============================================================================
# Embeddings
# =============================================================================
def embed(texts: list[str], *, model: str = DEFAULT_EMBED_MODEL) -> list[list[float]]:
    if not texts:
        return []
    client = get_client()
    try:
        resp = client.embeddings.create(model=model, input=texts)
        return [d.embedding for d in resp.data]
    except Exception as e:
        log.exception("OpenAI embeddings failed")
        raise _map_error(e) from e


# =============================================================================
# Key validation (used by /connect_openai)
# =============================================================================
def validate_key(api_key: str) -> tuple[bool, str]:
    """Lightweight live check of an API key. Returns (ok, message)."""
    if OpenAI is None:
        return False, "openai package not installed."
    api_key = api_key.strip()
    base_url = _resolve_base_url()
    if not base_url and not api_key.startswith("sk-"):
        return False, "Key should start with 'sk-'."
    try:
        client = OpenAI(api_key=api_key or "sk-no-key-required", base_url=base_url, timeout=20.0)
        client.models.list()
        return True, "Key validated."
    except Exception as e:
        return False, _map_error(e).owner_message
