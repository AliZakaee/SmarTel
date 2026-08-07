"""Tests for telegram_api.py: markup serialization, the chunking send adapter
with raw-HTTP fallback, and the low-level _post response handling."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import telegram_api as tg


@pytest.fixture(autouse=True)
def reset_api_state():
    yield
    tg._TOKEN = tg._API_BASE = tg._FILE_BASE = None


# --- _serialize_markup -------------------------------------------------------
def test_serialize_markup_variants():
    assert tg._serialize_markup(None) is None
    assert tg._serialize_markup({"a": 1}) == json.dumps({"a": 1})
    obj = SimpleNamespace(to_dict=lambda: {"k": "v"})
    assert tg._serialize_markup(obj) == json.dumps({"k": "v"})
    assert tg._serialize_markup("rawstring") == "rawstring"


# --- send_reply --------------------------------------------------------------
def test_send_reply_single_chunk():
    bot = MagicMock()
    tg.send_reply(bot, 5, "hello")
    bot.send_message.assert_called_once()
    args, kwargs = bot.send_message.call_args
    assert args[0] == 5 and args[1] == "hello"


def test_send_reply_chunks_long_text():
    bot = MagicMock()
    tg.send_reply(bot, 5, "x" * 5000, reply_markup="mk", reply_to_message_id=9)
    assert bot.send_message.call_count == 2
    first = bot.send_message.call_args_list[0]
    assert first.kwargs.get("reply_to_message_id") == 9
    assert "reply_markup" in first.kwargs
    # Only the first chunk carries reply context / markup.
    second = bot.send_message.call_args_list[1]
    assert "reply_markup" not in second.kwargs
    assert "reply_to_message_id" not in second.kwargs


def test_send_reply_falls_back_to_raw(monkeypatch):
    monkeypatch.setattr(tg, "_post", lambda method, data, **k: {"ok": True})
    bot = MagicMock()
    bot.send_message.side_effect = RuntimeError("telebot down")
    assert tg.send_reply(bot, 5, "hello") == [{"ok": True}]


def test_send_reply_reports_partial_chunk_delivery(monkeypatch):
    send_one = MagicMock(
        side_effect=[{"message_id": 1}, tg.TelegramAPIError("second chunk failed")]
    )
    monkeypatch.setattr(tg, "_send_one", send_one)

    with pytest.raises(tg.TelegramPartialSendError) as exc_info:
        tg.send_reply(MagicMock(), 5, "x" * 9000)

    assert exc_info.value.sent_count == 1
    assert exc_info.value.total_count == 3
    assert send_one.call_count == 2


# --- chat_action / answer_callback ------------------------------------------
def test_chat_action_uses_bot():
    bot = MagicMock()
    tg.chat_action(bot, 5, "typing", business_connection_id="bc1")
    bot.send_chat_action.assert_called_once()


def test_chat_action_never_raises(monkeypatch):
    bot = MagicMock()
    bot.send_chat_action.side_effect = RuntimeError("x")
    monkeypatch.setattr(tg, "send_chat_action", MagicMock(side_effect=RuntimeError("y")))
    tg.chat_action(bot, 5, "typing")  # swallowed; must not raise


def test_answer_callback_uses_bot():
    bot = MagicMock()
    tg.answer_callback(bot, "cb1", "ok")
    bot.answer_callback_query.assert_called_once()


# --- _post -------------------------------------------------------------------
def test_post_ok(monkeypatch):
    tg.init("123:abc")
    resp = MagicMock()
    resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
    monkeypatch.setattr(tg.requests, "post", lambda *a, **k: resp)
    assert tg._post("sendMessage", {"chat_id": 5}) == {"message_id": 1}


def test_post_error_raises(monkeypatch):
    tg.init("123:abc")
    resp = MagicMock()
    resp.json.return_value = {"ok": False, "error_code": 400, "description": "bad request"}
    monkeypatch.setattr(tg.requests, "post", lambda *a, **k: resp)
    with pytest.raises(tg.TelegramAPIError):
        tg._post("sendMessage", {"chat_id": 5})


def test_post_network_error(monkeypatch):
    tg.init("123:abc")

    def boom(*a, **k):
        raise tg.requests.RequestException("connection refused")

    monkeypatch.setattr(tg.requests, "post", boom)
    with pytest.raises(tg.TelegramAPIError):
        tg._post("sendMessage", {})


def test_post_requires_init():
    with pytest.raises(RuntimeError):
        tg._post("sendMessage", {})
