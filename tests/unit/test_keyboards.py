"""Tests for keyboards.py: callback_data length safety and builder integrity."""

from __future__ import annotations

import pytest

import keyboards


def _all_callback_data(markup):
    return [btn.callback_data for row in markup.keyboard for btn in row]


# --- cb() --------------------------------------------------------------------
def test_cb_joins_with_colon():
    assert keyboards.cb("apr", "send", 42) == "apr:send:42"


def test_cb_exactly_64_bytes_ok():
    data = keyboards.cb("x" * 64)
    assert len(data.encode("utf-8")) == 64


def test_cb_65_bytes_raises():
    with pytest.raises(ValueError):
        keyboards.cb("x" * 65)


def test_cb_multibyte_boundary():
    # "é" is 2 UTF-8 bytes: 32 chars == 64 bytes (ok), 33 chars == 66 bytes (raises).
    assert len(keyboards.cb("é" * 32).encode("utf-8")) == 64
    with pytest.raises(ValueError):
        keyboards.cb("é" * 33)


# --- builders emit safe callback data ----------------------------------------
def test_control_panel_buttons_within_limit():
    kb = keyboards.control_panel(auto_on=True, approval_on=False)
    data = _all_callback_data(kb)
    assert data and all(len(d.encode("utf-8")) <= 64 for d in data)


def test_approval_actions_buttons():
    kb = keyboards.approval_actions(999999)
    data = _all_callback_data(kb)
    assert "apr:send:999999" in data
    assert all(len(d.encode("utf-8")) <= 64 for d in data)


def test_settings_root_and_enum():
    root = keyboards.settings_root([{"label": "X", "callback_data": "set:open:x"}])
    assert all(len(d.encode("utf-8")) <= 64 for d in _all_callback_data(root))
    enum = keyboards.settings_enum("openai_model", ["gpt-4o", "gpt-4.1"], "gpt-4o")
    assert all(len(d.encode("utf-8")) <= 64 for d in _all_callback_data(enum))


def test_kb_menu_and_confirm():
    assert all(len(d.encode("utf-8")) <= 64 for d in _all_callback_data(keyboards.kb_menu()))
    assert all(len(d.encode("utf-8")) <= 64 for d in _all_callback_data(keyboards.confirm("tok123")))


# --- kb_list_keyboard pagination --------------------------------------------
def _items(n):
    return [{"id": i, "title": f"Item {i}", "chunk_count": i} for i in range(n)]


def test_kb_list_first_page_has_next_only():
    kb = keyboards.kb_list_keyboard(_items(12), page=0, per_page=5)
    data = _all_callback_data(kb)
    assert any(d == "kb:list:1" for d in data)          # Next
    assert not any(d == "kb:list:-1" for d in data)     # no Prev


def test_kb_list_middle_page_has_both():
    data = _all_callback_data(keyboards.kb_list_keyboard(_items(12), page=1, per_page=5))
    assert "kb:list:0" in data and "kb:list:2" in data


def test_kb_list_last_page_has_prev_only():
    data = _all_callback_data(keyboards.kb_list_keyboard(_items(12), page=2, per_page=5))
    assert "kb:list:1" in data
    assert "kb:list:3" not in data
