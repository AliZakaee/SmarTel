"""Inline keyboard builders. Callback-data scheme (kept well under Telegram's
64-byte limit; never embeds free text):

  panel:<action>            control panel
  set:<action>[:key[:val]]  settings menu
  apr:<action>:<id>         approval actions
  kb:<action>[:id[:page]]   knowledge base
  confirm:<yes|no>:<token>  destructive confirmations
"""

from __future__ import annotations

from telebot import types


def cb(*parts) -> str:
    data = ":".join(str(p) for p in parts)
    if len(data.encode("utf-8")) > 64:
        raise ValueError(f"callback_data too long: {data!r}")
    return data


def _btn(text: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text, callback_data=data)


# =============================================================================
# Control panel
# =============================================================================
def control_panel(auto_on: bool, approval_on: bool) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        _btn("📊 Status", cb("panel", "status")),
        _btn("❓ Help", cb("panel", "help")),
    )
    kb.add(
        _btn(f"🤖 Auto-reply: {'ON' if auto_on else 'OFF'}", cb("panel", "auto_toggle")),
        _btn(f"✅ Approval: {'ON' if approval_on else 'OFF'}", cb("panel", "appr_toggle")),
    )
    kb.add(
        _btn("📚 Knowledge Base", cb("kb", "menu")),
        _btn("⚙️ Settings", cb("set", "root")),
    )
    kb.add(
        _btn("📨 Pending Approvals", cb("panel", "pending")),
        _btn("🧹 Clear Memory", cb("panel", "mem_clear")),
    )
    return kb


# =============================================================================
# Approvals
# =============================================================================
def approval_actions(approval_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        _btn("✅ Send", cb("apr", "send", approval_id)),
        _btn("✏️ Edit", cb("apr", "edit", approval_id)),
    )
    kb.add(
        _btn("🚫 Reject", cb("apr", "reject", approval_id)),
        _btn("⏸ Pause Chat", cb("apr", "pause", approval_id)),
    )
    return kb


# =============================================================================
# Settings
# =============================================================================
def settings_root(rows: list[dict]) -> types.InlineKeyboardMarkup:
    """rows: list of {"label": str, "callback_data": str}."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    for r in rows:
        kb.add(_btn(r["label"], r["callback_data"]))
    kb.add(_btn("✖ Close", cb("set", "close")))
    return kb


def settings_enum(key: str, choices: list[str], current: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for choice in choices:
        mark = "• " if choice == current else ""
        kb.add(_btn(f"{mark}{choice}", cb("set", "enumval", key, choice)))
    kb.add(_btn("« Back", cb("set", "root")))
    return kb


# =============================================================================
# Knowledge base
# =============================================================================
def kb_menu() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        _btn("➕ Add text", cb("kb", "add")),
        _btn("📎 Upload file", cb("kb", "upload")),
    )
    kb.add(
        _btn("🔍 Search", cb("kb", "search")),
        _btn("📃 List items", cb("kb", "list", 0)),
    )
    kb.add(_btn("🗑 Clear all", cb("kb", "clear")))
    kb.add(_btn("✖ Close", cb("kb", "close")))
    return kb


def kb_list_keyboard(items: list, page: int, per_page: int = 5) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    start = page * per_page
    page_items = items[start : start + per_page]
    for item in page_items:
        title = (item["title"] or "Untitled")[:30]
        kb.add(_btn(f"🗑 #{item['id']} {title} ({item['chunk_count']})",
                    cb("kb", "del", item["id"], page)))
    nav = []
    if page > 0:
        nav.append(_btn("« Prev", cb("kb", "list", page - 1)))
    if start + per_page < len(items):
        nav.append(_btn("Next »", cb("kb", "list", page + 1)))
    if nav:
        kb.row(*nav)
    kb.add(_btn("« Back", cb("kb", "menu")))
    return kb


# =============================================================================
# Confirmation
# =============================================================================
def confirm(token: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        _btn("✅ Yes", cb("confirm", "yes", token)),
        _btn("✖ No", cb("confirm", "no", token)),
    )
    return kb
