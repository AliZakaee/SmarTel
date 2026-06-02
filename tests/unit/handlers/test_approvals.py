"""Tests for handlers/approvals.py: card rendering, request creation/reuse, and
the restart-safe edit flow."""

from __future__ import annotations

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
