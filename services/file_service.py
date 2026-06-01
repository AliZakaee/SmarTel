"""Knowledge-base file handling: extension allowlist, filename sanitization,
safe storage, and text extraction (.txt/.md/.pdf/.docx).

Files are stored under storage/uploads and are NEVER executed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

log = logging.getLogger("smartel.file")

ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

_uploads_dir: Path | None = None


class FileServiceError(Exception):
    """Unsupported, too large, corrupt, or empty file."""


def init(uploads_dir) -> None:
    global _uploads_dir
    _uploads_dir = Path(uploads_dir)
    _uploads_dir.mkdir(parents=True, exist_ok=True)


def _dir() -> Path:
    if _uploads_dir is None:
        raise RuntimeError("file_service not initialised; call init(uploads_dir).")
    return _uploads_dir


def sanitize_filename(name: str) -> str:
    """Strip any path components and unsafe characters; bound the length."""
    base = Path(str(name or "")).name  # drops directories and ../
    base = _SAFE_CHARS.sub("_", base).strip("._") or "upload"
    stem = Path(base).stem[:100] or "upload"
    ext = Path(base).suffix.lower()
    return f"{stem}{ext}"


def is_allowed(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in ALLOWED_EXTENSIONS


def save_upload(data: bytes, original_filename: str) -> Path:
    if not is_allowed(original_filename):
        raise FileServiceError(
            f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
    if data is None or len(data) == 0:
        raise FileServiceError("The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise FileServiceError(f"File too large (max {MAX_FILE_BYTES // (1024 * 1024)} MB).")

    safe = sanitize_filename(original_filename)
    digest = hashlib.sha256(data).hexdigest()[:16]
    path = _dir() / f"{digest}_{safe}"
    path.write_bytes(data)  # plain bytes; never marked executable, never run
    log.info("saved KB upload %s (%d bytes)", path.name, len(data))
    return path


def extract_text(path: Path) -> str:
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix in (".txt", ".md"):
            text = path.read_text(encoding="utf-8", errors="replace")
        elif suffix == ".pdf":
            text = _extract_pdf(path)
        elif suffix == ".docx":
            text = _extract_docx(path)
        else:
            raise FileServiceError(f"Unsupported file type: {suffix}")
    except FileServiceError:
        raise
    except Exception as e:
        log.warning("extraction failed for %s: %s", path.name, type(e).__name__)
        raise FileServiceError(f"Could not read '{path.name}' (unsupported or corrupt).") from e

    if not text or not text.strip():
        raise FileServiceError(
            f"No readable text found in '{path.name}'. "
            "Scanned/image-only PDFs are not supported (no OCR)."
        )
    return text


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts)


def _extract_docx(path: Path) -> str:
    import docx
    doc = docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs)
