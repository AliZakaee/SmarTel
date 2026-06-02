"""Tests for handlers/__init__.py: notify_owner best-effort delivery."""

from __future__ import annotations

import handlers


def test_notify_owner_sends_to_owner(stub_bot, mocker):
    send = mocker.patch("telegram_api.send_reply")
    handlers.notify_owner("hello owner")
    send.assert_called_once()
    # owner id (12345 from stub_bot) is the recipient
    assert send.call_args.args[1] == 12345


def test_notify_owner_no_bot_is_noop(mocker):
    send = mocker.patch("telegram_api.send_reply")
    handlers.bot = None  # not initialised
    handlers.notify_owner("ignored")
    send.assert_not_called()


def test_notify_owner_swallows_send_errors(stub_bot, mocker):
    mocker.patch("telegram_api.send_reply", side_effect=RuntimeError("network"))
    # Must not propagate — notify_owner is best-effort.
    handlers.notify_owner("hello")
