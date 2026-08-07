"""Tests for handlers/approvals.py: card rendering, request creation/reuse, and
the restart-safe edit flow."""

from __future__ import annotations

from telegram_api import TelegramAPIError, TelegramPartialSendError

from handlers import approvals


# --- render_approval_card ----------------------------------------------------
def test_render_card_sensitive(db, make_message):
    conn = {"business_connection_id": "bc1"}
    card = approvals.render_approval_card(conn, make_message(chat_id=5),
                                          "Hi <there>", "A reply", True, 7)
    assert card.startswith("🚨")
    assert "Approval #7" in card
    assert "&lt;there&gt;" in card        # customer text HTML-escaped
    assert "chat <code>5</code>" in card


def test_render_card_not_sensitive(db, make_message):
    conn = {"business_connection_id": "bc1"}
    card = approvals.render_approval_card(conn, make_message(chat_id=5),
                                          "Hi", "R", False, 3)
    assert not card.startswith("🚨")
    assert "Approval #3" in card


# --- create_request ----------------------------------------------------------
def test_create_request_inserts_and_notifies(db, stub_bot, make_message, mocker):
    send = mocker.patch("telegram_api.send_reply")
    conn = {"business_connection_id": "bc1"}
    aid = approvals.create_request(conn, make_message(chat_id=5, message_id=22),
                                   "ctext", "atext", sensitive=False)
    assert db.get_approval(aid) is not None
    assert db.count_pending_approvals() == 1
    send.assert_called()  # owner was notified


def test_create_request_reuses_open_approval(db, stub_bot, make_message, mocker):
    mocker.patch("telegram_api.send_reply")
    conn = {"business_connection_id": "bc1"}
    msg = make_message(chat_id=5, message_id=22)
    a1 = approvals.create_request(conn, msg, "c", "a", sensitive=False)
    a2 = approvals.create_request(conn, msg, "c", "a", sensitive=False)
    assert a1 == a2
    assert db.count_pending_approvals() == 1


def test_send_failure_keeps_approval_pending(db, stub_bot, make_call, mocker):
    db.upsert_business_connection("bc1", 999, True, True)
    aid = db.insert_pending_approval("bc1", 5, 22, "customer", "reply")
    send = mocker.patch(
        "telegram_api.send_reply", side_effect=TelegramAPIError("unavailable")
    )
    answer = mocker.patch("telegram_api.answer_callback")

    approvals._do_send(make_call(data=f"apr:send:{aid}"), db.get_approval(aid))

    assert send.call_count == 1
    assert db.get_approval(aid)["status"] == "pending"
    assert db.count_messages_in_window("bc1", 5, "1970-01-01T00:00:00+00:00", "out") == 0
    assert "still pending" in answer.call_args.args[2]
    stub_bot.edit_message_reply_markup.assert_not_called()


def test_send_failure_with_lost_release_claim_is_not_reported_retryable(
    db, stub_bot, make_call, mocker
):
    db.upsert_business_connection("bc1", 999, True, True)
    aid = db.insert_pending_approval("bc1", 5, 22, "customer", "reply")
    mocker.patch("telegram_api.send_reply", side_effect=TelegramAPIError("unavailable"))
    mocker.patch("database.release_approval_delivery", return_value=False)
    answer = mocker.patch("telegram_api.answer_callback")

    approvals._do_send(make_call(data=f"apr:send:{aid}"), db.get_approval(aid))

    assert db.get_approval(aid)["status"] == "delivery_uncertain"
    assert "state could not be finalized" in answer.call_args.args[2]
    stub_bot.edit_message_reply_markup.assert_called_once()


def test_send_failure_with_release_error_is_not_reported_retryable(
    db, stub_bot, make_call, mocker
):
    db.upsert_business_connection("bc1", 999, True, True)
    aid = db.insert_pending_approval("bc1", 5, 22, "customer", "reply")
    mocker.patch("telegram_api.send_reply", side_effect=TelegramAPIError("unavailable"))
    mocker.patch(
        "database.release_approval_delivery", side_effect=RuntimeError("db unavailable")
    )
    answer = mocker.patch("telegram_api.answer_callback")

    approvals._do_send(make_call(data=f"apr:send:{aid}"), db.get_approval(aid))

    assert db.get_approval(aid)["status"] == "delivery_uncertain"
    assert "state could not be finalized" in answer.call_args.args[2]
    stub_bot.edit_message_reply_markup.assert_called_once()


def test_send_success_finalizes_after_delivery(db, stub_bot, make_call, mocker):
    db.upsert_business_connection("bc1", 999, True, True)
    aid = db.insert_pending_approval("bc1", 5, 22, "customer", "reply")
    mocker.patch("telegram_api.send_reply")
    answer = mocker.patch("telegram_api.answer_callback")

    approvals._do_send(make_call(data=f"apr:send:{aid}"), db.get_approval(aid))

    assert db.get_approval(aid)["status"] == "sent"
    assert db.count_messages_in_window("bc1", 5, "1970-01-01T00:00:00+00:00", "out") == 1
    assert answer.call_args.args[2] == "Sent ✅"


def test_partial_send_is_not_made_retryable(db, stub_bot, make_call, mocker):
    db.upsert_business_connection("bc1", 999, True, True)
    aid = db.insert_pending_approval("bc1", 5, 22, "customer", "reply")
    mocker.patch(
        "telegram_api.send_reply",
        side_effect=TelegramPartialSendError(1, 2),
    )
    answer = mocker.patch("telegram_api.answer_callback")

    approvals._do_send(make_call(data=f"apr:send:{aid}"), db.get_approval(aid))

    assert db.get_approval(aid)["status"] == "delivery_uncertain"
    assert "Part of the reply" in answer.call_args.args[2]
    stub_bot.edit_message_reply_markup.assert_called_once()


def test_partial_send_still_warns_when_uncertain_state_write_fails(
    db, stub_bot, make_call, mocker
):
    db.upsert_business_connection("bc1", 999, True, True)
    aid = db.insert_pending_approval("bc1", 5, 22, "customer", "reply")
    mocker.patch(
        "telegram_api.send_reply",
        side_effect=TelegramPartialSendError(1, 2),
    )
    mocker.patch(
        "database.mark_approval_delivery_uncertain",
        side_effect=RuntimeError("db unavailable"),
    )
    answer = mocker.patch("telegram_api.answer_callback")

    approvals._do_send(make_call(data=f"apr:send:{aid}"), db.get_approval(aid))

    assert "Part of the reply" in answer.call_args.args[2]
    stub_bot.edit_message_reply_markup.assert_called_once()


def test_finalization_error_marks_delivery_uncertain(
    db, stub_bot, make_call, mocker
):
    db.upsert_business_connection("bc1", 999, True, True)
    aid = db.insert_pending_approval("bc1", 5, 22, "customer", "reply")
    mocker.patch("telegram_api.send_reply")
    mocker.patch(
        "database.complete_approval_delivery", side_effect=RuntimeError("db unavailable")
    )
    answer = mocker.patch("telegram_api.answer_callback")

    approvals._do_send(make_call(data=f"apr:send:{aid}"), db.get_approval(aid))

    assert db.get_approval(aid)["status"] == "delivery_uncertain"
    assert "state could not be finalized" in answer.call_args.args[2]


def test_lost_finalization_claim_marks_delivery_uncertain(
    db, stub_bot, make_call, mocker
):
    db.upsert_business_connection("bc1", 999, True, True)
    aid = db.insert_pending_approval("bc1", 5, 22, "customer", "reply")
    mocker.patch("telegram_api.send_reply")
    mocker.patch("database.complete_approval_delivery", return_value=False)

    approvals._do_send(make_call(data=f"apr:send:{aid}"), db.get_approval(aid))

    assert db.get_approval(aid)["status"] == "delivery_uncertain"


def test_callback_while_sending_keeps_retry_buttons(db, stub_bot, make_call, mocker):
    aid = db.insert_pending_approval("bc1", 5, 22, "customer", "reply")
    assert db.claim_approval_delivery(aid) is True
    answer = mocker.patch("telegram_api.answer_callback")

    approvals.on_approval_callback(make_call(data=f"apr:send:{aid}"))

    assert "in progress" in answer.call_args.args[2]
    stub_bot.edit_message_reply_markup.assert_not_called()


def test_callback_for_uncertain_delivery_removes_retry_buttons(
    db, stub_bot, make_call, mocker
):
    aid = db.insert_pending_approval("bc1", 5, 22, "customer", "reply")
    assert db.claim_approval_delivery(aid) is True
    assert db.mark_approval_delivery_uncertain(aid) is True
    answer = mocker.patch("telegram_api.answer_callback")
    send = mocker.patch("telegram_api.send_reply")

    approvals.on_approval_callback(make_call(data=f"apr:send:{aid}"))

    assert "outcome is uncertain" in answer.call_args.args[2]
    send.assert_not_called()
    stub_bot.edit_message_reply_markup.assert_called_once()


# --- edit flow ---------------------------------------------------------------
def test_apply_edit_sends_edited_reply(db, stub_bot, make_message, mocker):
    send = mocker.patch("telegram_api.send_reply")
    db.upsert_business_connection("bc1", 999, True, True)
    aid = db.insert_pending_approval("bc1", 5, 22, "orig customer", "orig reply")
    db.set_owner_pending_input(12345, "edit_approval", {"approval_id": aid}, 15)

    approvals._apply_edit(make_message(text="the edited reply"), {"approval_id": aid})

    appr = db.get_approval(aid)
    assert appr["status"] == "sent_edited"
    assert appr["ai_reply_text"] == "the edited reply"
    send.assert_called()


def test_apply_edit_empty_keeps_pending(db, stub_bot, make_message, mocker):
    mocker.patch("telegram_api.send_reply")
    db.upsert_business_connection("bc1", 999, True, True)
    aid = db.insert_pending_approval("bc1", 5, 22, "c", "r")
    approvals._apply_edit(make_message(text="   ", caption=None), {"approval_id": aid})
    assert db.get_approval(aid)["status"] == "pending"


def test_apply_edit_delivery_failure_keeps_edited_reply_pending(
    db, stub_bot, make_message, mocker
):
    db.upsert_business_connection("bc1", 999, True, True)
    aid = db.insert_pending_approval("bc1", 5, 22, "customer", "original")
    db.set_owner_pending_input(12345, "edit_approval", {"approval_id": aid}, 15)

    def send(_bot, _chat_id, _text, **kwargs):
        if kwargs.get("business_connection_id"):
            raise TelegramAPIError("unavailable")
        return []

    mocker.patch("telegram_api.send_reply", side_effect=send)

    approvals._apply_edit(make_message(text="edited reply"), {"approval_id": aid})

    approval = db.get_approval(aid)
    assert approval["status"] == "pending"
    assert approval["ai_reply_text"] == "edited reply"
    assert db.count_messages_in_window(
        "bc1", 5, "1970-01-01T00:00:00+00:00", "out"
    ) == 0


def test_apply_edit_not_pending(db, stub_bot, make_message, mocker):
    mocker.patch("telegram_api.send_reply")
    aid = db.insert_pending_approval("bc1", 5, 22, "c", "r")
    db.mark_approval(aid, "sent")
    approvals._apply_edit(make_message(text="new text"), {"approval_id": aid})
    assert db.get_approval(aid)["status"] == "sent"  # unchanged


# --- pending list ------------------------------------------------------------
def test_render_pending_list(db):
    assert "No pending approvals" in approvals.render_pending_list()
    db.insert_pending_approval("bc1", 5, 22, "a customer message", "r")
    rendered = approvals.render_pending_list()
    assert "Pending approvals (1)" in rendered
    assert "a customer message" in rendered
