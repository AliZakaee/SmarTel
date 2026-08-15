"""Vector mechanics for knowledge-base retrieval: embedding, cosine similarity,
top-k search over stored chunks, and a keyword-search fallback.

Storage and the vector-vs-keyword decision live in kb_service; this module is
pure mechanics over rows handed to it.
"""

from __future__ import annotations

import json
import logging
import re

import numpy as np

from services import openai_service

log = logging.getLogger("smartel.embedding")

EMBED_DIM = 1536
KB_TOP_K = 4

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
    "to", "of", "in", "on", "for", "with", "at", "by", "from", "as", "it",
    "this", "that", "these", "those", "i", "you", "we", "they", "do", "does",
    "how", "what", "when", "where", "why", "can", "could", "would", "should",
    "my", "your", "our", "me", "us",
}


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts (raises OpenAIServiceError on failure)."""
    return openai_service.embed(texts)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) + 1e-12
    return float(np.dot(a, b) / denom)


def _load_matrix(chunks):
    """Build a (N, EMBED_DIM) matrix from chunks that have a valid embedding.

    Returns (matrix, kept_chunks). Skips chunks whose embedding is missing,
    malformed, or the wrong dimension (guards against numpy broadcast errors).
    """
    vectors = []
    kept = []
    for ch in chunks:
        raw = ch["embedding_json"]
        if not raw:
            continue
        try:
            vec = json.loads(raw)
            if not isinstance(vec, list) or len(vec) != EMBED_DIM:
                continue
            array = np.asarray(vec, dtype=np.float64)
        except (json.JSONDecodeError, TypeError, ValueError, OverflowError):
            continue
        if array.shape != (EMBED_DIM,) or not np.all(np.isfinite(array)):
            continue
        if np.any(np.abs(array) > np.finfo(np.float32).max):
            continue
        vectors.append(array.astype(np.float32))
        kept.append(ch)
    if not vectors:
        return None, []
    return np.vstack(vectors), kept


def search_chunks(query: str, chunks, top_k: int = KB_TOP_K) -> list[tuple]:
    """Embed the query and return [(chunk, score), ...] sorted by cosine desc.

    Returns [] (so the caller can fall back to keyword search) when there are no
    usable embeddings. Raises OpenAIServiceError if embedding the query fails.
    """
    matrix, kept = _load_matrix(chunks)
    if matrix is None:
        return []
    q = np.asarray(embed_texts([query])[0], dtype=np.float32)
    q_norm = np.linalg.norm(q) + 1e-12
    row_norms = np.linalg.norm(matrix, axis=1) + 1e-12
    scores = (matrix @ q) / (row_norms * q_norm)
    order = np.argsort(scores)[::-1][:top_k]
    return [(kept[i], float(scores[i])) for i in order]


def _tokenize(text: str) -> set[str]:
    tokens = re.split(r"\W+", (text or "").lower())
    return {t for t in tokens if len(t) >= 2 and t not in _STOPWORDS}


def keyword_search(query: str, chunks, top_k: int = KB_TOP_K) -> list[tuple]:
    """Fallback overlap scorer when embeddings are unavailable."""
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    scored = []
    for ch in chunks:
        c_tokens = _tokenize(ch["content"])
        if not c_tokens:
            continue
        overlap = len(q_tokens & c_tokens)
        if overlap:
            scored.append((ch, overlap / len(q_tokens)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]
