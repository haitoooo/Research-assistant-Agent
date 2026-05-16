from __future__ import annotations

import os
import re
from pathlib import Path

ARTICLES_ROOT = Path(".nanobot/workspace/articles")
PDF_DIR = ARTICLES_ROOT / "pdf"
RAG_BASE = ARTICLES_ROOT / "rag"
DEFAULT_RAG_ROOT = RAG_BASE


def sanitize_book_slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
    slug = slug.strip("._-")
    return slug or "paper"


def is_rag_base(path: Path) -> bool:
    path = path.resolve()
    return path.name == "rag" and path.parent.resolve() == ARTICLES_ROOT.resolve()


def is_book_rag_root(path: Path) -> bool:
    path = path.resolve()
    return path.parent.resolve() == RAG_BASE.resolve()


def rag_base_from(root: Path | None) -> Path:
    if root is None:
        return RAG_BASE.resolve()
    path = root.resolve()
    if is_rag_base(path):
        return path
    if is_book_rag_root(path):
        return path.parent
    if path == ARTICLES_ROOT.resolve():
        return RAG_BASE.resolve()
    return RAG_BASE.resolve()


def slug_from_pdf(pdf_path: Path) -> str:
    return sanitize_book_slug(pdf_path.stem)


def list_book_roots(rag_base: Path) -> list[Path]:
    if not rag_base.exists():
        return []
    return sorted(child for child in rag_base.iterdir() if child.is_dir() and not child.name.startswith("."))


def infer_book_rag_root(rag_base: Path, *, pdf: Path | None, book: str | None) -> Path:
    if book:
        return (rag_base / sanitize_book_slug(book)).resolve()
    if pdf is not None:
        return (rag_base / slug_from_pdf(pdf)).resolve()
    candidates = [b for b in list_book_roots(rag_base) if (b / "md").exists() or (b / DB_MARKER).exists()]
    if len(candidates) == 1:
        return candidates[0]
    names = ", ".join(b.name for b in list_book_roots(rag_base)[:12])
    if not names:
        raise RuntimeError(f"No book folder under {rag_base}. Run sync with --pdf first.")
    raise RuntimeError(f"Multiple book folders under {rag_base}; pass --pdf or --book. Found: {names}")


DB_MARKER = "articles.sqlite3"


def resolve_book_rag_root(
    root: Path | None = None,
    *,
    pdf: Path | None = None,
    book: str | None = None,
) -> Path:
    """Return articles/rag/<book_slug>/ as the per-book RAG workspace."""
    if root is not None:
        path = root.resolve()
        if is_book_rag_root(path):
            return path
    rag_base = rag_base_from(root)
    book_root = infer_book_rag_root(rag_base, pdf=pdf, book=book)
    book_root.mkdir(parents=True, exist_ok=True)
    return book_root


def resolve_rag_root(
    root: Path | None = None,
    *,
    pdf: Path | None = None,
    book: str | None = None,
) -> Path:
    return resolve_book_rag_root(root, pdf=pdf, book=book)


def articles_root_for(_rag_root: Path) -> Path:
    return ARTICLES_ROOT.resolve()


def pdf_dir_for(_rag_root: Path) -> Path:
    return PDF_DIR.resolve()


def resolve_pdf_path(
    _rag_root: Path,
    raw: Path | None,
    *,
    book: str | None = None,
) -> Path:
    pdf_dir = PDF_DIR
    if raw is None:
        pdfs = sorted(pdf_dir.glob("*.pdf"))
        if book:
            slug = sanitize_book_slug(book)
            matched = [p for p in pdfs if slug_from_pdf(p) == slug or sanitize_book_slug(p.stem) == slug]
            if len(matched) == 1:
                return matched[0]
        if len(pdfs) == 1:
            return pdfs[0]
        if not pdfs:
            raise FileNotFoundError(f"No PDF found in {pdf_dir}")
        names = ", ".join(p.name for p in pdfs[:8])
        raise RuntimeError(f"Multiple PDFs in {pdf_dir}; pass --pdf. Found: {names}")

    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.extend([raw, pdf_dir / raw, pdf_dir / f"{raw}.pdf"])
    found = next((candidate for candidate in candidates if candidate.exists() and candidate.is_file()), None)
    if not found:
        raise FileNotFoundError(f"PDF not found: {raw} (searched under {pdf_dir})")
    if found.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF path: {found}")
    return found


def load_workspace_env(rag_root: Path) -> None:
    rag_root = rag_root.resolve()
    rag_base = rag_base_from(rag_root)
    articles = ARTICLES_ROOT.resolve()
    for path in (rag_root / ".env", rag_base / ".env", articles / ".env"):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
