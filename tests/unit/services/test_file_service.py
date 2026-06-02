"""Tests for file_service.py: filename sanitization, extension allowlist, safe
storage, and text extraction (with binary extractors mocked)."""

from __future__ import annotations

from pathlib import Path

import pytest

from services import file_service as fs
from services.file_service import FileServiceError


# --- sanitize_filename -------------------------------------------------------
def test_sanitize_strips_path_traversal():
    assert fs.sanitize_filename("../../etc/passwd") == "passwd"
    assert fs.sanitize_filename("a/b/c.txt") == "c.txt"


def test_sanitize_replaces_unsafe_chars():
    out = fs.sanitize_filename("My Document (draft).docx")
    assert out.endswith(".docx")
    assert " " not in out and "(" not in out and ")" not in out


def test_sanitize_empty_defaults_to_upload():
    assert fs.sanitize_filename("") == "upload"
    assert fs.sanitize_filename(None) == "upload"


def test_sanitize_lowercases_extension():
    assert fs.sanitize_filename("notes.TXT").endswith(".txt")


# --- is_allowed --------------------------------------------------------------
@pytest.mark.parametrize("name,allowed", [
    ("a.txt", True), ("a.MD", True), ("a.pdf", True), ("a.docx", True),
    ("a.exe", False), ("a.zip", False), ("noext", False),
])
def test_is_allowed(name, allowed):
    assert fs.is_allowed(name) is allowed


# --- save_upload -------------------------------------------------------------
def test_save_upload_rejects_bad_type(file_service_init):
    with pytest.raises(FileServiceError):
        fs.save_upload(b"data", "bad.exe")


def test_save_upload_rejects_empty(file_service_init):
    with pytest.raises(FileServiceError):
        fs.save_upload(b"", "ok.txt")


def test_save_upload_rejects_oversize(file_service_init, monkeypatch):
    monkeypatch.setattr(fs, "MAX_FILE_BYTES", 10)
    with pytest.raises(FileServiceError):
        fs.save_upload(b"x" * 11, "ok.txt")


def test_save_upload_writes_file(file_service_init):
    path = fs.save_upload(b"hello", "notes.txt")
    assert path.exists()
    assert path.read_bytes() == b"hello"
    assert path.name.endswith("_notes.txt")


def test_save_upload_dedupes_by_content(file_service_init):
    p1 = fs.save_upload(b"same", "a.txt")
    p2 = fs.save_upload(b"same", "a.txt")
    assert p1 == p2  # content hash prefix is identical


# --- extract_text ------------------------------------------------------------
def test_extract_text_txt(file_service_init, tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello world", encoding="utf-8")
    assert fs.extract_text(p) == "hello world"


def test_extract_text_unknown_suffix(file_service_init):
    with pytest.raises(FileServiceError):
        fs.extract_text(Path("a.xyz"))


def test_extract_text_empty_raises(file_service_init, tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   ", encoding="utf-8")
    with pytest.raises(FileServiceError):
        fs.extract_text(p)


def test_extract_text_pdf_uses_extractor(file_service_init, monkeypatch):
    monkeypatch.setattr(fs, "_extract_pdf", lambda path: "pdf body text")
    assert fs.extract_text(Path("doc.pdf")) == "pdf body text"


def test_extract_text_extractor_failure_wrapped(file_service_init, monkeypatch):
    def boom(path):
        raise ValueError("corrupt")

    monkeypatch.setattr(fs, "_extract_docx", boom)
    with pytest.raises(FileServiceError):
        fs.extract_text(Path("doc.docx"))
