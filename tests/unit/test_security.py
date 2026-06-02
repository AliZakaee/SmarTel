"""Tests for security.py: ownership checks, the owner-only decorators, and the
throttled non-owner notice."""

from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time

import security


# --- ownership predicates ----------------------------------------------------
def test_is_owner(stub_bot):
    # stub_bot wires security.init(bot, 12345)
    assert security.is_owner(12345) is True
    assert security.is_owner(1) is False
    assert security.is_owner(None) is False


def test_is_owner_msg_and_call(stub_bot, make_message, make_call):
    assert security.is_owner_msg(make_message(user_id=12345)) is True
    assert security.is_owner_msg(make_message(user_id=99)) is False
    assert security.is_owner_call(make_call(user_id=12345)) is True
    assert security.is_owner_call(make_call(user_id=99)) is False


# --- decorators --------------------------------------------------------------
def test_owner_only_allows_owner(stub_bot, make_message):
    called = []

    @security.owner_only
    def handler(message):
        called.append(message)
        return "ok"

    assert handler(make_message(user_id=12345)) == "ok"
    assert len(called) == 1


def test_owner_only_blocks_non_owner(stub_bot, db, make_message, mocker):
    notice = mocker.patch("security.handle_non_owner_dm")
    called = []

    @security.owner_only
    def handler(message):
        called.append(message)

    handler(make_message(user_id=99))
    assert called == []
    notice.assert_called_once()


def test_owner_only_callback_blocks_non_owner(stub_bot, make_call):
    called = []

    @security.owner_only_callback
    def handler(call):
        called.append(call)

    handler(make_call(user_id=99))
    assert called == []
    stub_bot.answer_callback_query.assert_called_once()


# --- non-owner notice throttle ----------------------------------------------
def test_non_owner_notice_throttled(stub_bot, db, make_message):
    security._last_notice.clear()
    msg = make_message(user_id=99, chat_id=99)
    with freeze_time("2024-01-01 00:00:00") as frozen:
        security.handle_non_owner_dm(msg)
        assert stub_bot.send_message.call_count == 1
        security.handle_non_owner_dm(msg)  # within cooldown -> suppressed
        assert stub_bot.send_message.call_count == 1
        frozen.tick(timedelta(seconds=601))  # past 600s cooldown
        security.handle_non_owner_dm(msg)
        assert stub_bot.send_message.call_count == 2
