"""Shared utilities: HTML escaping, message splitting, sensitive-content
detection, secret masking + a logging redaction filter, and time helpers.

This module intentionally has no project-internal dependencies so it can be
imported from anywhere without creating import cycles.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone

# --- Telegram message length limit -------------------------------------------
TELEGRAM_MAX_MESSAGE_LEN = 4096

# Optional reuse of pyTelegramBotAPI helpers. Fall back to stdlib equivalents so
# this module never hard-fails if the library layout changes between versions.
try:  # pragma: no cover - exercised indirectly
    from telebot.util import smart_split as _smart_split
except Exception:  # pragma: no cover
    _smart_split = None

try:  # pragma: no cover
    from telebot.formatting import escape_html as _tb_escape_html
except Exception:  # pragma: no cover
    _tb_escape_html = None


# =============================================================================
# HTML escaping
# =============================================================================
def escape(text) -> str:
    """Escape user-generated content for safe insertion into Telegram HTML.

    Escapes ``& < >`` (and quotes) so customer text can never inject markup.
    """
    if text is None:
        return ""
    s = str(text)
    if _tb_escape_html is not None:
        try:
            return _tb_escape_html(s)
        except Exception:
            pass
    return html.escape(s, quote=False)


# =============================================================================
# Message splitting (Telegram caps messages at 4096 chars)
# =============================================================================
def split_message(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LEN) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` characters.

    Prefers pyTelegramBotAPI's ``smart_split`` (splits on natural boundaries);
    falls back to a safe hard splitter.
    """
    if text is None:
        return [""]
    text = str(text)
    if len(text) <= limit:
        return [text] if text else [""]

    if _smart_split is not None:
        try:
            parts = list(_smart_split(text, chars_per_string=limit))
            # smart_split can, in rare cases, still produce an oversized piece;
            # defensively re-split any such piece.
            out: list[str] = []
            for p in parts:
                if len(p) <= limit:
                    out.append(p)
                else:
                    out.extend(_hard_split(p, limit))
            return out or [""]
        except Exception:
            pass
    return _hard_split(text, limit)


def _hard_split(text: str, limit: int) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [""]


# =============================================================================
# Sensitive-content detection (conservative heuristic, NOT a classifier)
# =============================================================================
# Category -> list of regex patterns (compiled case-insensitively below).
_SENSITIVE_PATTERNS: dict[str, list[str]] = {
    "legal": [r"\blawsuit\b", r"\battorney\b", r"\blawyer\b", r"\bsue\b",
              r"\bsuing\b", r"\bliabilit", r"\blegal action\b", r"\bcourt\b"],
    "medical": [r"\bdiagnos", r"\bprescri", r"\bdosage\b", r"\bsymptom",
                r"\bmedication\b", r"\bmedical advice\b", r"\btreatment\b"],
    "financial": [r"\binvest", r"\bfinancial advice\b", r"\btax advice\b",
                  r"\bchargeback\b", r"\bmoney back\b"],
    "refund": [r"\brefund", r"\breimburs", r"\bcancel.* (order|subscription)\b",
               r"\bdispute\b"],
    "anger_threat": [r"\bi('?| wi)ll sue\b", r"\breport you\b", r"\bscam\b",
                     r"\bfraud\b", r"\bawful\b", r"\bterrible service\b",
                     r"\bthreat", r"\blawyer up\b"],
    "personal_data": [r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b",            # email
                      r"\b(?:\+?\d[\s-]?){10,}\b",                 # phone-ish
                      r"\b\d{3}-\d{2}-\d{4}\b",                    # SSN-like
                      r"\b(?:\d[ -]?){13,16}\b"],                  # card-like
    "credentials": [r"\bpassword\b", r"\bpasscode\b", r"\bone[- ]?time code\b",
                    r"\botp\b", r"\baccount access\b", r"\bapi[- ]?key\b",
                    r"\blog ?in (details|credentials)\b", r"\b2fa\b"],
}

_COMPILED_SENSITIVE = {
    cat: [re.compile(p, re.IGNORECASE) for p in pats]
    for cat, pats in _SENSITIVE_PATTERNS.items()
}


def detect_sensitive_categories(text: str) -> list[str]:
    """Return the list of sensitive categories matched in ``text`` (possibly empty)."""
    if not text:
        return []
    matched: list[str] = []
    for cat, patterns in _COMPILED_SENSITIVE.items():
        if any(p.search(text) for p in patterns):
            matched.append(cat)
    # Heuristic: shouting (lots of caps + exclamation) reads as an angry customer.
    letters = [c for c in text if c.isalpha()]
    if letters:
        caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if len(letters) >= 12 and caps_ratio > 0.7 and "!" in text:
            if "anger_threat" not in matched:
                matched.append("anger_threat")
    return matched


def detect_sensitive(text: str) -> bool:
    """True if the message touches any sensitive category."""
    return bool(detect_sensitive_categories(text))


# =============================================================================
# Secret masking + logging redaction
# =============================================================================
def mask_token(token: str | None) -> str:
    if not token:
        return "<none>"
    token = str(token)
    return f"...{token[-4:]}" if len(token) > 4 else "****"


def mask_key(key: str | None) -> str:
    if not key:
        return "<none>"
    key = str(key)
    return f"{key[:3]}...{key[-4:]}" if len(key) > 8 else "****"


def mask_id(value) -> str:
    """Mask a numeric id (chat/user) for non-debug logs, keeping the last 3 digits."""
    if value is None:
        return "<none>"
    s = str(value)
    return f"***{s[-3:]}" if len(s) > 3 else "***"


def mask_connection(bcid: str | None) -> str:
    if not bcid:
        return "<none>"
    bcid = str(bcid)
    if len(bcid) <= 6:
        return "****"
    return f"{bcid[:4]}..{bcid[-2:]}"


class SecretRedactionFilter(logging.Filter):
    """Defense-in-depth: redact known secrets if they ever reach a log record."""

    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 8]

    def add_secret(self, secret: str | None) -> None:
        if secret and len(secret) >= 8 and secret not in self._secrets:
            self._secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            try:
                msg = record.getMessage()
                redacted = msg
                for s in self._secrets:
                    if s in redacted:
                        redacted = redacted.replace(s, "***REDACTED***")
                if redacted != msg:
                    record.msg = redacted
                    record.args = ()
            except Exception:
                pass
        return True


# =============================================================================
# Time helpers (UTC everywhere)
# =============================================================================
def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """UTC ISO-8601 timestamp string, used for all DB ``*_at`` columns."""
    return now().isoformat()


def now_ts() -> int:
    """Epoch seconds (Telegram uses epoch seconds)."""
    return int(now().timestamp())


def future_iso(minutes: float) -> str:
    return (now() + timedelta(minutes=minutes)).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def is_expired(iso_value: str | None) -> bool:
    """True if ``iso_value`` is a timestamp in the past. None/blank => not expired."""
    dt = parse_iso(iso_value)
    if dt is None:
        return False
    return dt <= now()


def seconds_until(iso_value: str | None) -> int:
    dt = parse_iso(iso_value)
    if dt is None:
        return 0
    return max(0, int((dt - now()).total_seconds()))


def fmt_dt(iso_value: str | None) -> str:
    dt = parse_iso(iso_value)
    if dt is None:
        return "n/a"
    return dt.strftime("%Y-%m-%d %H:%M UTC")
