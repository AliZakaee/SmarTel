"""Tests for utils.py: HTML escaping, message splitting, sensitive-content
detection, secret masking + redaction, and UTC time helpers."""

from __future__ import annotations

import logging

import pytest
from freezegun import freeze_time

import utils


# --- escape ------------------------------------------------------------------
def test_escape_none_returns_empty():
    assert utils.escape(None) == ""


def test_escape_escapes_markup():
    out = utils.escape("a<b>&c")
    assert "&lt;" in out and "&gt;" in out and "&amp;" in out
    assert "<b>" not in out


def test_escape_coerces_non_str():
    assert utils.escape(123) == "123"


# --- split_message -----------------------------------------------------------
def test_split_none_and_empty():
    assert utils.split_message(None) == [""]
    assert utils.split_message("") == [""]


def test_split_short_single_chunk():
    assert utils.split_message("hello", limit=10) == ["hello"]


def test_split_exact_limit_single_chunk():
    text = "x" * 10
    assert utils.split_message(text, limit=10) == [text]


def test_split_over_limit_chunks_bounded():
    text = "y" * 25
    chunks = utils.split_message(text, limit=10)
    assert len(chunks) >= 3
    assert all(len(c) <= 10 for c in chunks)
    assert "".join(chunks) == text


# --- sensitive detection -----------------------------------------------------
@pytest.mark.parametrize(
    "text,category",
    [
        ("I will contact my attorney about this", "legal"),
        ("I need a prescription refilled", "medical"),
        ("Can you give me investment advice", "financial"),
        ("Please process a refund for my order", "refund"),
        ("What is the password for my account", "credentials"),
        ("email me at john@example.com", "personal_data"),
        ("my ssn is 123-45-6789", "personal_data"),
        ("this is a total scam", "anger_threat"),
    ],
)
def test_detect_sensitive_categories(text, category):
    assert category in utils.detect_sensitive_categories(text)


def test_detect_sensitive_shouting_heuristic():
    assert "anger_threat" in utils.detect_sensitive_categories("THIS IS COMPLETELY UNACCEPTABLE!")


def test_detect_sensitive_benign_is_empty():
    assert utils.detect_sensitive_categories("hello, nice to meet you") == []
    assert utils.detect_sensitive_categories("") == []
    assert utils.detect_sensitive_categories(None) == []


def test_detect_sensitive_bool_wrapper():
    assert utils.detect_sensitive("please refund me") is True
    assert utils.detect_sensitive("hello there") is False


# --- masking -----------------------------------------------------------------
def test_mask_token():
    assert utils.mask_token(None) == "<none>"
    assert utils.mask_token("abc") == "****"
    assert utils.mask_token("1234567890") == "...7890"


def test_mask_key():
    assert utils.mask_key(None) == "<none>"
    assert utils.mask_key("short") == "****"
    assert utils.mask_key("sk-abcdef1234") == "sk-...1234"


def test_mask_id():
    assert utils.mask_id(None) == "<none>"
    assert utils.mask_id(12) == "***"
    assert utils.mask_id(123456) == "***456"


def test_mask_connection():
    assert utils.mask_connection(None) == "<none>"
    assert utils.mask_connection("abcdef") == "****"
    assert utils.mask_connection("abcdefghij") == "abcd..ij"


# --- redaction filter --------------------------------------------------------
def _record(msg):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)


def test_redaction_filter_replaces_known_secret():
    f = utils.SecretRedactionFilter(["supersecrettoken"])
    rec = _record("leaking supersecrettoken now")
    assert f.filter(rec) is True
    assert "supersecrettoken" not in rec.getMessage()
    assert "***REDACTED***" in rec.getMessage()


def test_redaction_filter_ignores_short_secret():
    f = utils.SecretRedactionFilter(["short"])  # < 8 chars, not registered
    rec = _record("contains short word")
    f.filter(rec)
    assert rec.getMessage() == "contains short word"


def test_redaction_filter_add_secret_dedups():
    f = utils.SecretRedactionFilter()
    f.add_secret("longsecret123")
    f.add_secret("longsecret123")
    assert f._secrets.count("longsecret123") == 1
    f.add_secret("tiny")  # too short
    assert "tiny" not in f._secrets


# --- time helpers ------------------------------------------------------------
@freeze_time("2024-06-01 12:00:00")
def test_now_iso_and_ts():
    assert utils.now_iso().startswith("2024-06-01T12:00:00")
    assert utils.now_ts() == int(utils.now().timestamp())


@freeze_time("2024-06-01 12:00:00")
def test_future_iso_is_later():
    assert utils.parse_iso(utils.future_iso(10)) > utils.now()


def test_parse_iso_handles_bad_input():
    assert utils.parse_iso(None) is None
    assert utils.parse_iso("not-a-date") is None


def test_parse_iso_attaches_utc():
    dt = utils.parse_iso("2024-06-01T12:00:00")
    assert dt is not None and dt.tzinfo is not None


@freeze_time("2024-06-01 12:00:00")
def test_is_expired_and_seconds_until():
    past = utils.future_iso(-5)
    future = utils.future_iso(5)
    assert utils.is_expired(past) is True
    assert utils.is_expired(future) is False
    assert utils.is_expired(None) is False
    assert utils.seconds_until(past) == 0
    assert utils.seconds_until(future) > 0


def test_fmt_dt():
    assert utils.fmt_dt(None) == "n/a"
    assert utils.fmt_dt("2024-06-01T12:00:00+00:00").endswith("UTC")
