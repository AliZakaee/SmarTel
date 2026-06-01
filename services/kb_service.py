"""Knowledge base: ingest text/files, chunk, embed, store, and retrieve.

Retrieval uses OpenAI embeddings + cosine similarity (embedding_service) with an
automatic keyword-search fallback when embeddings are unavailable. Strict-KB
mode short-circuits with a canned message when nothing relevant is found.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import database
from services import embedding_service, file_service
from services.openai_service import OpenAIServiceError

log = logging.getLogger("smartel.kb")

CHUNK_SIZE = 800       # characters (~200 tokens)
CHUNK_OVERLAP = 150    # characters
KB_TOP_K = embedding_service.KB_TOP_K
MIN_VECTOR_SCORE = 0.15
CONTEXT_CHAR_BUDGET = 6000
# When the whole KB fits in this many chunks, include ALL of it in the prompt
# instead of relying on retrieval. Short identity/persona facts (e.g. "I am from
# Iran") then always reach the model — important on backends without embeddings
# (Codex), where retrieval is keyword-only and misses short/stop-word questions.
SMALL_KB_ALWAYS_INCLUDE = 12
STRICT_NO_INFO = "I don't have enough information about that in the knowledge base."


# =============================================================================
# Chunking
# =============================================================================
def _chunk_text(text: str) -> list[str]:
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    if not text:
        return []
    # Split into paragraphs, then pack into ~CHUNK_SIZE windows with overlap.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for para in paragraphs:
        if len(para) <= CHUNK_SIZE:
            pieces.append(para)
        else:
            pieces.extend(_split_long(para))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif len(current) + 1 + len(piece) <= CHUNK_SIZE:
            current = f"{current}\n{piece}"
        else:
            chunks.append(current)
            tail = current[-CHUNK_OVERLAP:] if CHUNK_OVERLAP else ""
            current = (tail + "\n" + piece).strip() if tail else piece
    if current:
        chunks.append(current)
    return [c for c in (c.strip() for c in chunks) if c]


def _split_long(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    current = ""
    for s in sentences:
        if len(s) > CHUNK_SIZE:
            # Hard-split an oversized sentence on word boundaries.
            for i in range(0, len(s), CHUNK_SIZE):
                out.append(s[i : i + CHUNK_SIZE])
            current = ""
            continue
        if not current:
            current = s
        elif len(current) + 1 + len(s) <= CHUNK_SIZE:
            current = f"{current} {s}"
        else:
            out.append(current)
            current = s
    if current:
        out.append(current)
    return out


# =============================================================================
# Ingestion
# =============================================================================
def _store_chunks(item_id: int, chunks: list[str]) -> bool:
    """Embed and store chunks. Returns True if embeddings were stored, False if
    chunks were stored without embeddings (keyword-searchable only)."""
    embedded = False
    embeddings: list[list[float] | None] = [None] * len(chunks)
    try:
        vectors = embedding_service.embed_texts(chunks)
        if len(vectors) == len(chunks):
            embeddings = vectors
            embedded = True
    except OpenAIServiceError as e:
        log.warning("KB embedding failed (%s); storing chunks for keyword search only.", e.kind)

    rows = [
        (item_id, idx, content,
         json.dumps(embeddings[idx]) if embeddings[idx] is not None else None)
        for idx, content in enumerate(chunks)
    ]
    database.insert_kb_chunks_bulk(rows)
    database.set_kb_item_chunk_count(item_id, len(chunks))
    return embedded


def add_text(title: str, text: str) -> dict:
    chunks = _chunk_text(text)
    item_id = database.insert_kb_item(title or "Untitled note", "text", None)
    if not chunks:
        database.set_kb_item_chunk_count(item_id, 0)
        return {"item_id": item_id, "chunk_count": 0, "embedded": False}
    embedded = _store_chunks(item_id, chunks)
    log.info("KB add_text item=%s chunks=%d embedded=%s", item_id, len(chunks), embedded)
    return {"item_id": item_id, "chunk_count": len(chunks), "embedded": embedded}


def add_file(data: bytes, original_filename: str, title: str | None = None) -> dict:
    """Save an uploaded file, extract text, chunk, embed and store. Raises
    FileServiceError on unsupported/corrupt/empty files."""
    path = file_service.save_upload(data, original_filename)
    text = file_service.extract_text(path)
    chunks = _chunk_text(text)
    item_id = database.insert_kb_item(title or Path(original_filename).name, "file",
                                      Path(original_filename).name)
    if not chunks:
        database.set_kb_item_chunk_count(item_id, 0)
        return {"item_id": item_id, "chunk_count": 0, "embedded": False}
    embedded = _store_chunks(item_id, chunks)
    log.info("KB add_file item=%s file=%s chunks=%d embedded=%s",
             item_id, path.name, len(chunks), embedded)
    return {"item_id": item_id, "chunk_count": len(chunks), "embedded": embedded}


# =============================================================================
# Retrieval
# =============================================================================
def search(query: str, top_k: int = KB_TOP_K) -> list:
    """Return relevant chunk rows (possibly empty). Vector first, keyword
    fallback, with a minimum-similarity floor for vector matches."""
    if not database.get_bool("kb_enabled"):
        return []
    chunks = database.get_all_chunks()
    if not chunks:
        return []

    try:
        vector_hits = embedding_service.search_chunks(query, chunks, top_k)
    except OpenAIServiceError as e:
        log.info("KB vector search unavailable (%s); using keyword search.", e.kind)
        vector_hits = []

    hits = [(ch, score) for ch, score in vector_hits if score >= MIN_VECTOR_SCORE]
    if not hits:
        hits = embedding_service.keyword_search(query, chunks, top_k)
    return [ch for ch, _score in hits]


def search_for_prompt(query: str) -> tuple[list, str | None]:
    """Returns (chunks, short_circuit_reply).

    In strict KB mode, if no relevant chunks are found, returns ([], STRICT_NO_INFO)
    so the caller can answer without calling the model.
    """
    if not database.get_bool("kb_enabled"):
        return [], None
    all_chunks = database.get_all_chunks()
    if all_chunks and len(all_chunks) <= SMALL_KB_ALWAYS_INCLUDE:
        # Small KB → always include the whole thing (no retrieval miss).
        chunks = list(all_chunks)
    else:
        chunks = search(query)
    if database.get_bool("strict_kb_mode") and not chunks:
        return [], STRICT_NO_INFO
    return chunks, None


def build_context_block(chunks) -> str:
    if not chunks:
        return ""
    lines = ["KNOWLEDGE BASE CONTEXT:"]
    total = 0
    for i, ch in enumerate(chunks, start=1):
        content = (ch["content"] or "").strip()
        snippet = f"[{i}] {content}"
        if total + len(snippet) > CONTEXT_CHAR_BUDGET:
            snippet = snippet[: max(0, CONTEXT_CHAR_BUDGET - total)]
            lines.append(snippet)
            break
        lines.append(snippet)
        total += len(snippet)
    lines.append("Use the above context only when relevant to the customer's question.")
    return "\n".join(lines)


# =============================================================================
# Management
# =============================================================================
def list_items() -> list:
    return database.list_kb_items()


def delete_item(item_id: int) -> bool:
    return database.delete_kb_item(item_id) > 0


def clear_all() -> None:
    database.clear_kb()


def stats() -> dict:
    return {"items": len(database.list_kb_items()), "chunks": database.count_kb_chunks()}
