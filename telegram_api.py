"""Telegram Bot API access.

Two layers:
  * Raw HTTP functions (``raw_*`` + ``get_business_connection``/file helpers)
    built on ``requests`` — used for endpoints TeleBot may not expose across
    versions (notably ``getBusinessConnection``) and as a fallback/escape hatch.
  * A thin send adapter (``send_reply`` / ``chat_action`` / ``answer_callback``)
    that handlers use. It prefers TeleBot (which natively supports
    ``business_connection_id``), applies message chunking, and falls back to raw
    HTTP if a TeleBot call raises.

Business replies pass ``business_connection_id``; owner/admin messages do not.
"""

from __future__ import annotations

import json
import logging
import time

import requests

import utils

log = logging.getLogger("smartel.telegram")

_TOKEN: str | None = None
_API_BASE: str | None = None
_FILE_BASE: str | None = None
_TIMEOUT = (5, 30)  # (connect, read)


class TelegramAPIError(Exception):
    def __init__(self, description: str, error_code: int | None = None):
        super().__init__(description)
        self.description = description
        self.error_code = error_code


def init(token: str) -> None:
    global _TOKEN, _API_BASE, _FILE_BASE
    _TOKEN = token
    _API_BASE = f"https://api.telegram.org/bot{token}"
    _FILE_BASE = f"https://api.telegram.org/file/bot{token}"


def _require_init() -> None:
    if not _API_BASE:
        raise RuntimeError("telegram_api not initialised; call init(token).")


def _serialize_markup(reply_markup):
    if reply_markup is None:
        return None
    if hasattr(reply_markup, "to_dict"):
        return json.dumps(reply_markup.to_dict())
    if isinstance(reply_markup, (dict, list)):
        return json.dumps(reply_markup)
    return reply_markup  # already a JSON string


def _post(method: str, data: dict, *, retry_429: bool = True) -> dict:
    _require_init()
    payload = {k: v for k, v in data.items() if v is not None}
    try:
        resp = requests.post(f"{_API_BASE}/{method}", data=payload, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise TelegramAPIError(f"network error calling {method}: {e}") from e

    try:
        body = resp.json()
    except ValueError:
        raise TelegramAPIError(f"{method}: non-JSON response (HTTP {resp.status_code})")

    if body.get("ok"):
        return body.get("result")

    error_code = body.get("error_code")
    description = body.get("description", "unknown error")
    if error_code == 429 and retry_429:
        retry_after = (body.get("parameters") or {}).get("retry_after", 1)
        time.sleep(min(int(retry_after) + 1, 30))
        return _post(method, data, retry_429=False)
    raise TelegramAPIError(description, error_code)


# =============================================================================
# Raw HTTP functions (spec signatures)
# =============================================================================
def send_message(chat_id, text, business_connection_id=None, reply_to_message_id=None,
                 parse_mode="HTML", reply_markup=None) -> dict:
    return _post("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "business_connection_id": business_connection_id,
        "reply_to_message_id": reply_to_message_id,
        "reply_markup": _serialize_markup(reply_markup),
    })


def send_chat_action(chat_id, action, business_connection_id=None) -> dict:
    return _post("sendChatAction", {
        "chat_id": chat_id,
        "action": action,
        "business_connection_id": business_connection_id,
    })


def answer_callback_query(callback_query_id, text=None, show_alert=False) -> dict:
    return _post("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": "true" if show_alert else None,
    })


def get_business_connection(business_connection_id) -> dict | None:
    """Fetch a business connection via the raw API (TeleBot lacks a stable
    wrapper across versions). Returns the result dict or None on failure."""
    try:
        return _post("getBusinessConnection",
                     {"business_connection_id": business_connection_id})
    except TelegramAPIError as e:
        log.warning("getBusinessConnection failed: %s", e.description)
        return None


def get_file(file_id) -> dict:
    return _post("getFile", {"file_id": file_id})


def download_file(file_path, dest_path) -> None:
    _require_init()
    url = f"{_FILE_BASE}/{file_path}"
    resp = requests.get(url, timeout=_TIMEOUT, stream=True)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def download_file_bytes(file_id) -> bytes:
    info = get_file(file_id)
    file_path = info["file_path"]
    _require_init()
    resp = requests.get(f"{_FILE_BASE}/{file_path}", timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.content


# =============================================================================
# Send adapter (TeleBot-first, raw fallback) — what handlers call
# =============================================================================
def send_reply(bot, chat_id, text, *, business_connection_id=None,
               reply_to_message_id=None, reply_markup=None, parse_mode="HTML") -> list:
    """Send a (possibly long) message, split into <=4096-char chunks.

    Only the first chunk carries reply_to_message_id and reply_markup. Prefers
    TeleBot; falls back to raw HTTP per chunk if TeleBot raises.
    """
    chunks = utils.split_message(text)
    sent = []
    for i, chunk in enumerate(chunks):
        rtm = reply_to_message_id if i == 0 else None
        markup = reply_markup if i == 0 else None
        sent.append(_send_one(bot, chat_id, chunk, business_connection_id, rtm, markup, parse_mode))
    return sent


def _send_one(bot, chat_id, text, business_connection_id, reply_to_message_id,
              reply_markup, parse_mode):
    try:
        kwargs = {"parse_mode": parse_mode}
        if business_connection_id is not None:
            kwargs["business_connection_id"] = business_connection_id
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        log.warning("TeleBot send failed (%s); falling back to raw HTTP.", type(e).__name__)
        return send_message(chat_id, text, business_connection_id=business_connection_id,
                            reply_to_message_id=reply_to_message_id, parse_mode=parse_mode,
                            reply_markup=reply_markup)


def chat_action(bot, chat_id, action, business_connection_id=None) -> None:
    """Best-effort typing indicator; never raises."""
    try:
        kwargs = {}
        if business_connection_id is not None:
            kwargs["business_connection_id"] = business_connection_id
        bot.send_chat_action(chat_id, action, **kwargs)
    except Exception:
        try:
            send_chat_action(chat_id, action, business_connection_id=business_connection_id)
        except Exception:
            pass


def answer_callback(bot, callback_query_id, text=None, show_alert=False) -> None:
    """Best-effort callback acknowledgement; never raises."""
    try:
        bot.answer_callback_query(callback_query_id, text=text, show_alert=show_alert)
    except Exception:
        try:
            answer_callback_query(callback_query_id, text=text, show_alert=show_alert)
        except Exception:
            pass
