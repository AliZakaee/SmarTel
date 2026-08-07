"""Tests for handlers/business_updates.py: the customer-message decision
pipeline, branch by branch. Network/model calls are mocked; the DB is real."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from handlers import business_updates as bu
from services.openai_service import OpenAIServiceError
from services.rate_limit_service import RateLimitDecision

EPOCH = "1970-01-01T00:00:00+00:00"


@pytest.fixture
def penv(db, stub_bot, mocker):
    """Pipeline environment: real DB + a registered connection, with the
    outbound Telegram calls patched. Owner (business_user_id) is 999."""
    send = mocker.patch("telegram_api.send_reply")
    chat_action = mocker.patch("telegram_api.chat_action")
    db.upsert_business_connection("bc1", 999, True, True)
    return SimpleNamespace(db=db, send=send, chat_action=chat_action, bot=stub_bot)


def _out_count(db, bcid="bc1", chat=5):
    return db.count_messages_in_window(bcid, chat, EPOCH, direction="out")


# --- early guards ------------------------------------------------------------
def test_no_bcid_ignored(penv, make_message):
    bu.process_business_message(make_message(business_connection_id=None))
    penv.send.assert_not_called()


def test_unknown_connection_ignored(penv, make_message, mocker):
    mocker.patch("services.business_service.fetch_and_store_via_raw", return_value=None)
    bu.process_business_message(make_message(business_connection_id="unknown"))
    penv.send.assert_not_called()


def test_echo_guard(penv, make_message):
    msg = make_message(business_connection_id="bc1", sender_business_bot=object())
    bu.process_business_message(msg)
    penv.send.assert_not_called()


def test_owner_takeover_pauses(penv, make_message):
    msg = make_message(business_connection_id="bc1", user_id=999, chat_id=5)
    bu.process_business_message(msg)
    assert penv.db.get_pause("bc1", 5) is not None
    penv.send.assert_not_called()


# --- non-text / monitoring ---------------------------------------------------
def test_non_text_monitoring_notice(penv, make_message):
    msg = make_message(business_connection_id="bc1", user_id=10, chat_id=5,
                       text=None, content_type="photo")
    bu.process_business_message(msg)
    penv.send.assert_called()


def test_auto_off_notifies_incoming(penv, make_message):
    # auto_reply default OFF, monitoring default ON
    msg = make_message(business_connection_id="bc1", user_id=10, chat_id=5, text="hi there")
    bu.process_business_message(msg)
    penv.send.assert_called()
    assert penv.db.count_pending_approvals() == 0


# --- paused / rate-limited ---------------------------------------------------
def test_paused_chat_skips(penv, make_message):
    penv.db.set_setting("auto_reply_enabled", True)
    penv.db.upsert_pause("bc1", 5, "manual", None)  # indefinite
    msg = make_message(business_connection_id="bc1", user_id=10, chat_id=5, text="hi")
    bu.process_business_message(msg)
    penv.send.assert_not_called()


def test_rate_limit_exceeded_notifies(penv, make_message, mocker):
    penv.db.set_setting("auto_reply_enabled", True)
    mocker.patch("services.rate_limit_service.enforce",
                 return_value=RateLimitDecision(False, 21, 20, 0))
    msg = make_message(business_connection_id="bc1", user_id=10, chat_id=5, text="hi")
    bu.process_business_message(msg)
    penv.send.assert_called()
    assert penv.db.count_pending_approvals() == 0


# --- generation paths --------------------------------------------------------
def test_strict_kb_short_circuit_no_model_call(penv, make_message, mocker):
    penv.db.set_setting("auto_reply_enabled", True)
    penv.db.set_setting("approval_mode_enabled", False)
    penv.db.set_setting("strict_kb_mode", True)  # no chunks -> short circuit
    chat_spy = mocker.patch("services.openai_service.chat")
    msg = make_message(business_connection_id="bc1", user_id=10, chat_id=5, text="anything")
    bu.process_business_message(msg)
    chat_spy.assert_not_called()
    assert _out_count(penv.db) == 1  # short-circuit reply delivered


def test_openai_error_notifies_owner(penv, make_message, mocker):
    penv.db.set_setting("auto_reply_enabled", True)
    mocker.patch("services.openai_service.chat",
                 side_effect=OpenAIServiceError("bad key", kind="auth"))
    msg = make_message(business_connection_id="bc1", user_id=10, chat_id=5, text="hello")
    bu.process_business_message(msg)
    penv.send.assert_called()
    assert penv.db.count_pending_approvals() == 0
    assert _out_count(penv.db) == 0


# --- dispatch: approval vs auto ---------------------------------------------
def test_approval_mode_creates_request(penv, make_message, mocker):
    penv.db.set_setting("auto_reply_enabled", True)
    penv.db.set_setting("approval_mode_enabled", True)
    mocker.patch("services.openai_service.chat", return_value="AI reply")
    msg = make_message(business_connection_id="bc1", user_id=10, chat_id=5, text="hello")
    bu.process_business_message(msg)
    assert penv.db.count_pending_approvals() == 1
    assert _out_count(penv.db) == 0  # not delivered yet, awaiting approval


def test_auto_delivery(penv, make_message, mocker):
    penv.db.set_setting("auto_reply_enabled", True)
    penv.db.set_setting("approval_mode_enabled", False)
    mocker.patch("services.openai_service.chat", return_value="AI reply")
    msg = make_message(business_connection_id="bc1", user_id=10, chat_id=5, text="hello")
    bu.process_business_message(msg)
    assert penv.db.count_pending_approvals() == 0
    assert _out_count(penv.db) == 1


def test_sensitive_forces_approval(penv, make_message, mocker):
    penv.db.set_setting("auto_reply_enabled", True)
    penv.db.set_setting("approval_mode_enabled", False)  # approval OFF...
    penv.db.set_setting("sensitive_requires_approval", True)
    mocker.patch("services.openai_service.chat", return_value="AI reply")
    # ...but a sensitive message forces the approval path
    msg = make_message(business_connection_id="bc1", user_id=10, chat_id=5,
                       text="I want a refund")
    bu.process_business_message(msg)
    assert penv.db.count_pending_approvals() == 1


# --- _deliver_auto permission guard -----------------------------------------
def test_deliver_auto_without_permission(db, stub_bot, make_message, mocker):
    send = mocker.patch("telegram_api.send_reply")
    db.upsert_business_connection("bc1", 999, True, False)  # can_reply False
    conn = db.get_business_connection("bc1")
    bu._deliver_auto(conn, make_message(chat_id=5), "ctext", "reply")
    send.assert_called()  # owner warned
    assert _out_count(db) == 0  # nothing recorded as sent to customer


def test_deliver_auto_records_outbound(db, stub_bot, make_message, mocker):
    mocker.patch("telegram_api.send_reply")
    db.upsert_business_connection("bc1", 999, True, True)
    conn = db.get_business_connection("bc1")
    bu._deliver_auto(conn, make_message(chat_id=5, message_id=22), "ctext", "reply text")
    assert _out_count(db) == 1


def test_deliver_auto_warns_on_partial_delivery(db, stub_bot, make_message, mocker):
    mocker.patch(
        "telegram_api.send_reply",
        side_effect=bu.telegram_api.TelegramPartialSendError(1, 2),
    )
    notify = mocker.patch("handlers.notify_owner")
    db.upsert_business_connection("bc1", 999, True, True)
    conn = db.get_business_connection("bc1")

    bu._deliver_auto(conn, make_message(chat_id=5), "ctext", "reply text")

    assert "partially delivered (1/2 parts)" in notify.call_args.args[0]
    assert _out_count(db) == 0
    assert db.get_recent_memory("bc1", 5, 10) == []


def test_deliver_auto_warns_when_delivery_is_unconfirmed(
    db, stub_bot, make_message, mocker
):
    mocker.patch(
        "telegram_api.send_reply",
        side_effect=bu.telegram_api.TelegramAPIError("unavailable"),
    )
    notify = mocker.patch("handlers.notify_owner")
    db.upsert_business_connection("bc1", 999, True, True)
    conn = db.get_business_connection("bc1")

    bu._deliver_auto(conn, make_message(chat_id=5), "ctext", "reply text")

    assert "could not be confirmed" in notify.call_args.args[0]
    assert _out_count(db) == 0
    assert db.get_recent_memory("bc1", 5, 10) == []


# --- deleted messages --------------------------------------------------------
def test_deleted_invalidates_approval(penv):
    aid = penv.db.insert_pending_approval("bc1", 5, 22, "c", "r")
    deleted = SimpleNamespace(business_connection_id="bc1",
                              chat=SimpleNamespace(id=5), message_ids=[22])
    bu.on_deleted_business_messages(deleted)
    assert penv.db.get_approval(aid)["status"] == "invalidated"
