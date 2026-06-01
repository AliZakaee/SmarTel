"""Settings menu: a declarative registry rendered as an inline keyboard with
in-place editing. Booleans toggle inline; enums open a choice list; numbers are
captured via the owner pending-input flow.
"""

from __future__ import annotations

import logging

import database
import handlers
import keyboards
import security
import telegram_api

log = logging.getLogger("smartel.settings")

# Declarative registry. type: bool | enum | int | float.
SETTINGS = [
    {"key": "auto_reply_enabled", "label": "Auto-reply", "type": "bool"},
    {"key": "approval_mode_enabled", "label": "Approval mode", "type": "bool"},
    {"key": "sensitive_requires_approval", "label": "Sensitive → approval", "type": "bool"},
    {"key": "monitoring_mode", "label": "Monitoring mode", "type": "bool"},
    {"key": "kb_enabled", "label": "Knowledge base", "type": "bool"},
    {"key": "strict_kb_mode", "label": "Strict KB mode", "type": "bool"},
    {"key": "memory_enabled", "label": "Conversation memory", "type": "bool"},
    {"key": "public_direct_bot_chat_enabled", "label": "Direct-DM notice", "type": "bool"},
    {"key": "process_nontext_enabled", "label": "Process captions", "type": "bool"},
    {"key": "openai_model", "label": "AI model", "type": "enum",
     "choices": ["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "gpt-4o"]},
    {"key": "temperature", "label": "Temperature", "type": "float", "min": 0.0, "max": 2.0},
    {"key": "max_tokens", "label": "Max tokens", "type": "int", "min": 1, "max": 4096},
    {"key": "owner_takeover_pause_minutes", "label": "Takeover pause (min)", "type": "int",
     "min": 1, "max": 100000},
    {"key": "max_messages_per_customer_per_hour", "label": "Rate limit / hour", "type": "int",
     "min": 1, "max": 100000},
]
_BY_KEY = {s["key"]: s for s in SETTINGS}


def register(bot, cfg) -> None:
    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("set:"))
    @security.owner_only_callback
    def _on_settings_callback(call):
        try:
            on_settings_callback(call)
        except Exception:
            log.exception("settings callback failed")
            telegram_api.answer_callback(bot, call.id, "Error.")


# =============================================================================
# Rendering
# =============================================================================
def _format_value(spec) -> str:
    value = database.get_setting(spec["key"])
    if spec["type"] == "bool":
        return "ON" if value else "OFF"
    return str(value)


def _root_rows() -> list[dict]:
    rows = []
    for spec in SETTINGS:
        label = f"{spec['label']}: {_format_value(spec)}"
        if spec["type"] == "bool":
            data = keyboards.cb("set", "toggle", spec["key"])
        elif spec["type"] == "enum":
            data = keyboards.cb("set", "enum", spec["key"])
        else:
            data = keyboards.cb("set", "num", spec["key"])
        rows.append({"label": label, "callback_data": data})
    return rows


_ROOT_TEXT = "⚙️ <b>Settings</b>\nTap a row to change it."


def open_root(message) -> None:
    handlers.bot.send_message(message.chat.id, _ROOT_TEXT, parse_mode="HTML",
                              reply_markup=keyboards.settings_root(_root_rows()))


def _edit_root(call) -> None:
    try:
        handlers.bot.edit_message_text(
            _ROOT_TEXT, call.message.chat.id, call.message.message_id,
            parse_mode="HTML", reply_markup=keyboards.settings_root(_root_rows()),
        )
    except Exception:
        pass  # "message is not modified" etc.


# =============================================================================
# Callback handling
# =============================================================================
def on_settings_callback(call) -> None:
    bot = handlers.bot
    parts = call.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "root":
        _edit_root(call)
        return telegram_api.answer_callback(bot, call.id)

    if action == "close":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return telegram_api.answer_callback(bot, call.id, "Closed.")

    if action == "toggle":
        key = parts[2]
        new_value = not database.get_bool(key)
        database.set_setting(key, new_value)
        _edit_root(call)
        return telegram_api.answer_callback(bot, call.id, f"{key} → {'ON' if new_value else 'OFF'}")

    if action == "enum":
        key = parts[2]
        spec = _BY_KEY[key]
        choices = list(spec.get("choices", []))
        current = database.get_str(key)
        if current not in choices:
            choices = [current] + choices
        try:
            bot.edit_message_text(
                f"Select <b>{spec['label']}</b>:", call.message.chat.id, call.message.message_id,
                parse_mode="HTML", reply_markup=keyboards.settings_enum(key, choices, current),
            )
        except Exception:
            pass
        return telegram_api.answer_callback(bot, call.id)

    if action == "enumval":
        key, choice = parts[2], parts[3]
        database.set_setting(key, choice)
        _edit_root(call)
        return telegram_api.answer_callback(bot, call.id, f"{key} → {choice}")

    if action == "num":
        key = parts[2]
        spec = _BY_KEY[key]
        kind = "set_int" if spec["type"] == "int" else "set_float"
        database.set_owner_pending_input(security.owner_id(), kind, {"key": key}, 10)
        rng = ""
        if "min" in spec and "max" in spec:
            rng = f" (allowed {spec['min']}–{spec['max']})"
        handlers.notify_owner(
            f"Send the new value for <b>{spec['label']}</b>{rng}, or /cancel to abort."
        )
        return telegram_api.answer_callback(bot, call.id, "Waiting for a value…")

    telegram_api.answer_callback(bot, call.id)


# =============================================================================
# Pending-input application (numbers)
# =============================================================================
def apply_pending(message, kind: str, context: dict) -> None:
    owner = security.owner_id()
    database.clear_owner_pending_input(owner)
    key = context.get("key")
    spec = _BY_KEY.get(key)
    if spec is None:
        return
    raw = (getattr(message, "text", "") or "").strip()
    try:
        value = int(raw) if kind == "set_int" else float(raw)
    except ValueError:
        return handlers.notify_owner(f"'{raw}' is not a valid number. {spec['label']} unchanged.")
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and value < lo or hi is not None and value > hi:
        return handlers.notify_owner(
            f"Value must be between {lo} and {hi}. {spec['label']} unchanged."
        )
    database.set_setting(key, value)
    handlers.notify_owner(f"✅ {spec['label']} set to <b>{value}</b>.")
