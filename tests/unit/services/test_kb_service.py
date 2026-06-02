"""Tests for kb_service.py: chunking, context-block formatting, retrieval
(vector floor + keyword fallback), search_for_prompt heuristics, and add_text."""

from __future__ import annotations

from services import kb_service as kb
from services.openai_service import OpenAIServiceError


# --- _chunk_text -------------------------------------------------------------
def test_chunk_empty():
    assert kb._chunk_text("") == []
    assert kb._chunk_text("   ") == []


def test_chunk_short_single():
    assert kb._chunk_text("a short note") == ["a short note"]


def test_chunk_collapses_whitespace():
    assert kb._chunk_text("a    b\tc") == ["a b c"]


def test_chunk_packs_with_overlap():
    para1 = "A" * 500
    para2 = "B" * 500
    chunks = kb._chunk_text(f"{para1}\n\n{para2}")
    assert len(chunks) == 2
    assert chunks[0] == para1
    assert chunks[1].startswith("A" * kb.CHUNK_OVERLAP)  # overlap tail carried over
    assert para2 in chunks[1]
    assert all(len(c) <= kb.CHUNK_SIZE for c in chunks)


def test_split_long_hard_splits_oversized_sentence():
    pieces = kb._split_long("X" * 1000)
    assert all(len(p) <= kb.CHUNK_SIZE for p in pieces)
    assert "".join(pieces) == "X" * 1000


# --- build_context_block -----------------------------------------------------
def test_context_block_empty():
    assert kb.build_context_block([]) == ""


def test_context_block_numbers_and_footer():
    chunks = [{"content": "first"}, {"content": "second"}]
    block = kb.build_context_block(chunks)
    assert block.startswith("KNOWLEDGE BASE CONTEXT:")
    assert "[1] first" in block and "[2] second" in block
    assert block.rstrip().endswith("customer's question.")


def test_context_block_respects_budget():
    chunks = [{"content": "C" * 1000} for _ in range(10)]
    block = kb.build_context_block(chunks)
    assert block.startswith("KNOWLEDGE BASE CONTEXT:")
    assert len(block) < kb.CONTEXT_CHAR_BUDGET + 300


# --- search ------------------------------------------------------------------
def test_search_kb_disabled(db):
    db.set_setting("kb_enabled", False)
    assert kb.search("q") == []


def test_search_no_chunks(db):
    assert kb.search("q") == []


def test_search_vector_below_floor_falls_back_to_keyword(db, monkeypatch):
    item = db.insert_kb_item("t", "text", None)
    db.insert_kb_chunks_bulk([(item, 0, "our refund policy", None)])
    chunks = db.get_all_chunks()
    monkeypatch.setattr("services.embedding_service.search_chunks",
                        lambda q, c, k: [(chunks[0], 0.05)])  # below MIN_VECTOR_SCORE
    res = kb.search("refund")
    assert len(res) == 1 and res[0]["content"] == "our refund policy"


def test_search_vector_hit(db, monkeypatch):
    item = db.insert_kb_item("t", "text", None)
    db.insert_kb_chunks_bulk([(item, 0, "our refund policy", None)])
    chunks = db.get_all_chunks()
    monkeypatch.setattr("services.embedding_service.search_chunks",
                        lambda q, c, k: [(chunks[0], 0.9)])
    res = kb.search("unrelated wording")
    assert len(res) == 1 and res[0]["content"] == "our refund policy"


def test_search_embedding_error_falls_back(db, monkeypatch):
    item = db.insert_kb_item("t", "text", None)
    db.insert_kb_chunks_bulk([(item, 0, "our refund policy", None)])

    def boom(q, c, k):
        raise OpenAIServiceError("no key", kind="auth")

    monkeypatch.setattr("services.embedding_service.search_chunks", boom)
    assert len(kb.search("refund")) == 1


# --- search_for_prompt -------------------------------------------------------
def test_search_for_prompt_small_kb_includes_all(db):
    item = db.insert_kb_item("t", "text", None)
    db.insert_kb_chunks_bulk([(item, 0, "fact one", None), (item, 1, "fact two", None)])
    chunks, short = kb.search_for_prompt("totally unrelated query")
    assert short is None and len(chunks) == 2


def test_search_for_prompt_strict_no_chunks(db):
    db.set_setting("strict_kb_mode", True)
    chunks, short = kb.search_for_prompt("q")
    assert chunks == [] and short == kb.STRICT_NO_INFO


def test_search_for_prompt_kb_disabled(db):
    db.set_setting("kb_enabled", False)
    assert kb.search_for_prompt("q") == ([], None)


# --- add_text ----------------------------------------------------------------
def test_add_text_stores_chunks(db, monkeypatch):
    monkeypatch.setattr("services.embedding_service.embed_texts", lambda chunks: [])
    result = kb.add_text("My note", "Hello world. This is a stored fact.")
    assert result["chunk_count"] >= 1
    assert db.count_kb_chunks() == result["chunk_count"]


def test_add_text_empty(db):
    result = kb.add_text("Empty note", "   ")
    assert result["chunk_count"] == 0
