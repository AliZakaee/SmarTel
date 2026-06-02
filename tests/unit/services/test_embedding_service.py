"""Tests for embedding_service.py: cosine similarity, tokenization, keyword
search, embedding-matrix loading, and vector search (with embed mocked)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from services import embedding_service as es


# --- cosine_similarity -------------------------------------------------------
def test_cosine_identical():
    v = np.array([1.0, 2.0, 3.0])
    assert es.cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal():
    assert es.cosine_similarity(np.array([1.0, 0.0]), np.array([0.0, 1.0])) == pytest.approx(0.0, abs=1e-9)


def test_cosine_opposite():
    assert es.cosine_similarity(np.array([1.0, 0.0]), np.array([-1.0, 0.0])) == pytest.approx(-1.0)


def test_cosine_zero_vector_is_safe():
    # No division by zero thanks to the +1e-12 epsilon.
    result = es.cosine_similarity(np.array([0.0, 0.0]), np.array([1.0, 0.0]))
    assert np.isfinite(result)
    assert result == pytest.approx(0.0, abs=1e-6)


# --- _tokenize ---------------------------------------------------------------
def test_tokenize_drops_stopwords_and_short_tokens():
    assert es._tokenize("How do I get a Refund?") == {"get", "refund"}


def test_tokenize_empty():
    assert es._tokenize("") == set()
    assert es._tokenize(None) == set()


# --- keyword_search ----------------------------------------------------------
def test_keyword_search_ranks_overlap():
    chunks = [
        {"content": "Our refund policy allows returns within 30 days"},
        {"content": "We ship worldwide on weekdays"},
    ]
    hits = es.keyword_search("refund policy", chunks)
    assert hits[0][0] is chunks[0]
    assert hits[0][1] > 0


def test_keyword_search_empty_query_returns_empty():
    assert es.keyword_search("the a an", [{"content": "anything"}]) == []


def test_keyword_search_respects_top_k():
    chunks = [{"content": f"refund number {i}"} for i in range(10)]
    assert len(es.keyword_search("refund", chunks, top_k=3)) == 3


# --- _load_matrix ------------------------------------------------------------
def test_load_matrix_filters_bad_rows(monkeypatch):
    monkeypatch.setattr(es, "EMBED_DIM", 3)
    chunks = [
        {"content": "good", "embedding_json": json.dumps([1.0, 0.0, 0.0])},
        {"content": "missing", "embedding_json": None},
        {"content": "malformed", "embedding_json": "{not json"},
        {"content": "wrong dim", "embedding_json": json.dumps([1.0, 2.0])},
    ]
    matrix, kept = es._load_matrix(chunks)
    assert matrix.shape == (1, 3)
    assert [c["content"] for c in kept] == ["good"]


def test_load_matrix_none_when_no_usable():
    matrix, kept = es._load_matrix([{"content": "x", "embedding_json": None}])
    assert matrix is None and kept == []


# --- search_chunks -----------------------------------------------------------
def test_search_chunks_no_embeddings_returns_empty(monkeypatch):
    monkeypatch.setattr(es, "EMBED_DIM", 3)
    # No usable embeddings -> [] so the caller can fall back to keyword search.
    assert es.search_chunks("q", [{"content": "x", "embedding_json": None}]) == []


def test_search_chunks_sorted_by_score(monkeypatch):
    monkeypatch.setattr(es, "EMBED_DIM", 3)
    monkeypatch.setattr(es, "embed_texts", lambda texts: [[1.0, 0.0, 0.0]])
    chunks = [
        {"content": "match", "embedding_json": json.dumps([1.0, 0.0, 0.0])},
        {"content": "nomatch", "embedding_json": json.dumps([0.0, 1.0, 0.0])},
    ]
    hits = es.search_chunks("q", chunks, top_k=2)
    assert hits[0][0]["content"] == "match"
    assert hits[0][1] > hits[1][1]
