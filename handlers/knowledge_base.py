"""Knowledge-base commands and menu: add text, upload files, search, list and
delete items, and clear the KB. Ingestion/retrieval live in services/kb_service.
"""

from __future__ import annotations

import logging

import database
import handlers
import keyboards
import security
import telegram_api
import utils
from services import kb_service
from services.file_service import FileServiceError
from services.openai_service import OpenAIServiceError

log = logging.getLogger("smartel.kb_handler")


def _args(message) -> str:
    text = getattr(message, "text", "") or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def register(bot, cfg) -> None:
    @bot.message_handler(commands=["kb"], func=security.is_owner_msg)
    def _kb(message):
        _safe(lambda: open_menu(message))

    @bot.message_handler(commands=["kb_add"], func=security.is_owner_msg)
    def _kb_add(message):
        _safe(lambda: cmd_add(message))

    @bot.message_handler(commands=["kb_upload"], func=security.is_owner_msg)
    def _kb_upload(message):
        _safe(lambda: cmd_upload(message))

    @bot.message_handler(commands=["kb_search"], func=security.is_owner_msg)
    def _kb_search(message):
        _safe(lambda: cmd_search(message))

    @bot.message_handler(commands=["kb_list"], func=security.is_owner_msg)
    def _kb_list(message):
        _safe(lambda: send_list(message.chat.id, 0))

    @bot.message_handler(commands=["kb_delete"], func=security.is_owner_msg)
    def _kb_delete(message):
        _safe(lambda: cmd_delete(message))

    @bot.message_handler(commands=["kb_clear"], func=security.is_owner_msg)
    def _kb_clear(message):
        _safe(lambda: cmd_clear(message))

    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("kb:"))
    @security.owner_only_callback
    def _kb_cb(call):
        _safe(lambda: on_callback(call))


def _safe(fn) -> None:
    try:
        fn()
    except Exception:
        log.exception("kb handler failed")


# =============================================================================
# Menu + commands
# =============================================================================
def open_menu(message) -> None:
    stats = kb_service.stats()
    text = (
        "📚 <b>Knowledge Base</b>\n"
        f"Items: <b>{stats['items']}</b> · Chunks: <b>{stats['chunks']}</b>\n"
        f"KB usage: <b>{'ON' if database.get_bool('kb_enabled') else 'OFF'}</b> · "
        f"Strict mode: <b>{'ON' if database.get_bool('strict_kb_mode') else 'OFF'}</b>"
    )
    handlers.bot.send_message(message.chat.id, text, parse_mode="HTML",
                              reply_markup=keyboards.kb_menu())


def cmd_add(message) -> None:
    text = _args(message)
    if not text:
        database.set_owner_pending_input(security.owner_id(), "kb_add", {}, 10)
        return handlers.notify_owner("Send the text to add to the knowledge base, or /cancel.")
    _do_add_text(text)


def cmd_upload(message) -> None:
    database.set_owner_pending_input(security.owner_id(), "kb_upload", {}, 10)
    handlers.notify_owner(
        "Send a document to add to the knowledge base "
        "(.txt, .md, .pdf, or .docx), or /cancel."
    )


def cmd_search(message) -> None:
    query = _args(message)
    if not query:
        database.set_owner_pending_input(security.owner_id(), "kb_search", {}, 10)
        return handlers.notify_owner("Send a search query, or /cancel.")
    _do_search(message.chat.id, query)


def cmd_delete(message) -> None:
    raw = _args(message)
    try:
        item_id = int(raw)
    except ValueError:
        return handlers.bot.send_message(message.chat.id, "Usage: /kb_delete <item_id>")
    ok = kb_service.delete_item(item_id)
    handlers.bot.send_message(message.chat.id,
                              f"Deleted item #{item_id}." if ok else f"No item #{item_id}.")


def cmd_clear(message) -> None:
    stats = kb_service.stats()
    if stats["items"] == 0:
        return handlers.bot.send_message(message.chat.id, "Knowledge base is already empty.")
    handlers.bot.send_message(
        message.chat.id,
        f"Delete <b>all {stats['items']}</b> KB items and {stats['chunks']} chunks?",
        parse_mode="HTML", reply_markup=keyboards.confirm("kbclear"),
    )


def send_list(chat_id: int, page: int) -> None:
    items = kb_service.list_items()
    if not items:
        return handlers.bot.send_message(chat_id, "Knowledge base is empty. Use /kb_add or /kb_upload.")
    handlers.bot.send_message(
        chat_id, "📃 <b>KB items</b> (tap to delete):", parse_mode="HTML",
        reply_markup=keyboards.kb_list_keyboard(items, page),
    )


# =============================================================================
# Callbacks
# =============================================================================
def on_callback(call) -> None:
    bot = handlers.bot
    parts = call.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "menu":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        open_menu(call.message)
        return telegram_api.answer_callback(bot, call.id)

    if action == "add":
        database.set_owner_pending_input(security.owner_id(), "kb_add", {}, 10)
        handlers.notify_owner("Send the text to add to the knowledge base, or /cancel.")
        return telegram_api.answer_callback(bot, call.id, "Send text…")

    if action == "upload":
        database.set_owner_pending_input(security.owner_id(), "kb_upload", {}, 10)
        handlers.notify_owner("Send a .txt/.md/.pdf/.docx document, or /cancel.")
        return telegram_api.answer_callback(bot, call.id, "Send a file…")

    if action == "search":
        database.set_owner_pending_input(security.owner_id(), "kb_search", {}, 10)
        handlers.notify_owner("Send a search query, or /cancel.")
        return telegram_api.answer_callback(bot, call.id, "Send a query…")

    if action == "list":
        page = int(parts[2]) if len(parts) > 2 else 0
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        send_list(call.message.chat.id, page)
        return telegram_api.answer_callback(bot, call.id)

    if action == "del":
        item_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        kb_service.delete_item(item_id)
        items = kb_service.list_items()
        try:
            if items:
                bot.edit_message_reply_markup(
                    call.message.chat.id, call.message.message_id,
                    reply_markup=keyboards.kb_list_keyboard(items, page),
                )
            else:
                bot.edit_message_text("Knowledge base is now empty.",
                                      call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return telegram_api.answer_callback(bot, call.id, f"Deleted #{item_id}")

    if action == "clear":
        bot.send_message(call.message.chat.id, "Delete the entire knowledge base?",
                         reply_markup=keyboards.confirm("kbclear"))
        return telegram_api.answer_callback(bot, call.id)

    if action == "close":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return telegram_api.answer_callback(bot, call.id, "Closed.")

    telegram_api.answer_callback(bot, call.id)


# =============================================================================
# Pending-input application (routed from approvals.on_owner_pending_input)
# =============================================================================
def apply_pending(message, kind: str, context: dict) -> None:
    database.clear_owner_pending_input(security.owner_id())
    if kind == "kb_add":
        text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
        if not text:
            return handlers.notify_owner("Nothing to add (empty message).")
        _do_add_text(text)
    elif kind == "kb_upload":
        _do_upload(message)
    elif kind == "kb_search":
        query = (getattr(message, "text", "") or "").strip()
        if not query:
            return handlers.notify_owner("Empty query.")
        _do_search(message.chat.id, query)


def _do_add_text(text: str) -> None:
    title = text.splitlines()[0][:60] if text.strip() else "Note"
    result = kb_service.add_text(title, text)
    note = "" if result["embedded"] else " (stored for keyword search; embeddings unavailable)"
    handlers.notify_owner(
        f"✅ Added KB item #{result['item_id']} — {result['chunk_count']} chunk(s){note}."
    )


def _do_upload(message) -> None:
    doc = getattr(message, "document", None)
    if doc is None:
        return handlers.notify_owner("Please send a <b>document</b> (.txt/.md/.pdf/.docx).")
    file_name = getattr(doc, "file_name", "upload")
    try:
        data = telegram_api.download_file_bytes(doc.file_id)
    except Exception as e:
        log.warning("file download failed: %s", type(e).__name__)
        return handlers.notify_owner("Could not download the file from Telegram. Try again.")
    try:
        result = kb_service.add_file(data, file_name)
    except FileServiceError as e:
        return handlers.notify_owner(f"⚠️ {utils.escape(str(e))}")
    except OpenAIServiceError as e:
        return handlers.notify_owner(f"⚠️ {utils.escape(e.owner_message)}")
    note = "" if result["embedded"] else " (keyword-search only; embeddings unavailable)"
    handlers.notify_owner(
        f"✅ Added <b>{utils.escape(file_name)}</b> as item #{result['item_id']} — "
        f"{result['chunk_count']} chunk(s){note}."
    )


def _do_search(chat_id: int, query: str) -> None:
    try:
        chunks = kb_service.search(query)
    except OpenAIServiceError as e:
        return handlers.bot.send_message(chat_id, f"⚠️ {utils.escape(e.owner_message)}")
    if not chunks:
        return handlers.bot.send_message(chat_id, "No relevant knowledge-base entries found.")
    lines = [f"🔍 Top {len(chunks)} result(s) for <i>{utils.escape(query[:80])}</i>:"]
    for i, ch in enumerate(chunks, start=1):
        lines.append(f"\n<b>[{i}]</b> {utils.escape((ch['content'] or '')[:400])}")
    handlers.bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
