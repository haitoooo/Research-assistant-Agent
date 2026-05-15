from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ROOT = Path(".nanobot/workspace/articles")
DB_NAME = "articles.sqlite3"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.casefold()
    value = re.sub(r"https?://(dx\.)?doi\.org/", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_pdf_signature(path: Path) -> str:
    with path.open("rb") as handle:
        return handle.read(4).decode("ascii", errors="replace")


def title_from_filename(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"^\d{4}[_ -]+", "", stem)
    return re.sub(r"[_-]+", " ", stem).strip()


def title_from_markdown(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            text = line.strip()
            if not text:
                continue
            if text.startswith("#"):
                return text.lstrip("#").strip()
            if len(text) > 20:
                return text[:240]
    except OSError:
        return None
    return None


def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            doi TEXT,
            normalized_doi TEXT,
            source_url TEXT,
            normalized_url TEXT,
            pdf_path TEXT,
            md_path TEXT,
            sha256 TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'downloaded',
            size_bytes INTEGER,
            pdf_mtime REAL,
            md_mtime REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_papers_normalized_title ON papers(normalized_title);
        CREATE INDEX IF NOT EXISTS idx_papers_normalized_doi ON papers(normalized_doi);
        CREATE INDEX IF NOT EXISTS idx_papers_normalized_url ON papers(normalized_url);
        """
    )
    conn.commit()


def upsert_paper(
    conn: sqlite3.Connection,
    *,
    title: str,
    pdf_path: Path | None = None,
    md_path: Path | None = None,
    sha256: str | None = None,
    source_url: str | None = None,
    doi: str | None = None,
    status: str = "downloaded",
) -> int:
    normalized_title = normalize_text(title)
    normalized_url = normalize_text(source_url)
    normalized_doi = normalize_text(doi)
    existing = None
    if sha256:
        existing = conn.execute("SELECT * FROM papers WHERE sha256=?", (sha256,)).fetchone()
    if existing is None and normalized_doi:
        existing = conn.execute("SELECT * FROM papers WHERE normalized_doi=?", (normalized_doi,)).fetchone()
    if existing is None and normalized_url:
        existing = conn.execute("SELECT * FROM papers WHERE normalized_url=?", (normalized_url,)).fetchone()
    if existing is None and normalized_title:
        existing = conn.execute("SELECT * FROM papers WHERE normalized_title=?", (normalized_title,)).fetchone()

    stamp = now_iso()
    pdf_mtime = pdf_path.stat().st_mtime if pdf_path and pdf_path.exists() else None
    md_mtime = md_path.stat().st_mtime if md_path and md_path.exists() else None
    size_bytes = pdf_path.stat().st_size if pdf_path and pdf_path.exists() else None

    if existing:
        paper_id = int(existing["id"])
        conn.execute(
            """
            UPDATE papers
            SET title=COALESCE(NULLIF(?, ''), title),
                normalized_title=COALESCE(NULLIF(?, ''), normalized_title),
                doi=COALESCE(NULLIF(?, ''), doi),
                normalized_doi=COALESCE(NULLIF(?, ''), normalized_doi),
                source_url=COALESCE(NULLIF(?, ''), source_url),
                normalized_url=COALESCE(NULLIF(?, ''), normalized_url),
                pdf_path=COALESCE(?, pdf_path),
                md_path=COALESCE(?, md_path),
                sha256=COALESCE(?, sha256),
                status=?,
                size_bytes=COALESCE(?, size_bytes),
                pdf_mtime=COALESCE(?, pdf_mtime),
                md_mtime=COALESCE(?, md_mtime),
                updated_at=?
            WHERE id=?
            """,
            (
                title,
                normalized_title,
                doi or "",
                normalized_doi,
                source_url or "",
                normalized_url,
                str(pdf_path) if pdf_path else None,
                str(md_path) if md_path else None,
                sha256,
                status,
                size_bytes,
                pdf_mtime,
                md_mtime,
                stamp,
                paper_id,
            ),
        )
    else:
        cursor = conn.execute(
            """
            INSERT INTO papers (
                title, normalized_title, doi, normalized_doi, source_url, normalized_url,
                pdf_path, md_path, sha256, status, size_bytes, pdf_mtime, md_mtime,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                normalized_title,
                doi,
                normalized_doi,
                source_url,
                normalized_url,
                str(pdf_path) if pdf_path else None,
                str(md_path) if md_path else None,
                sha256,
                status,
                size_bytes,
                pdf_mtime,
                md_mtime,
                stamp,
                stamp,
            ),
        )
        paper_id = int(cursor.lastrowid)
    conn.commit()
    return paper_id


def sync_library(root: Path) -> None:
    pdf_dir = root / "pdf"
    md_dir = root / "md"
    conn = connect(root)
    ready = 0
    skipped = 0
    invalid = 0

    for pdf_path in sorted(pdf_dir.glob("*.pdf")):
        if read_pdf_signature(pdf_path) != "%PDF":
            invalid += 1
            print(f"invalid-pdf {pdf_path}")
            continue
        digest = sha256_file(pdf_path)
        md_path = md_dir / f"{pdf_path.stem}.md"
        title = title_from_markdown(md_path) or title_from_filename(pdf_path)
        paper_id = upsert_paper(
            conn,
            title=title,
            pdf_path=pdf_path,
            md_path=md_path if md_path.exists() else None,
            sha256=digest,
            status="ready" if md_path.exists() else "downloaded",
        )
        if md_path.exists() and md_path.stat().st_size > 0:
            ready += 1
            print(f"registered {pdf_path.name} ready")
        else:
            skipped += 1
            print(f"registered {pdf_path.name} no-md")

    print(f"sync complete ready={ready} no_md={skipped} invalid={invalid} db={root / DB_NAME}")


def check_duplicate(root: Path, title: str | None, url: str | None, doi: str | None, file: Path | None) -> int:
    conn = connect(root)
    clauses: list[str] = []
    params: list[str] = []
    if file:
        digest = sha256_file(file)
        clauses.append("sha256=?")
        params.append(digest)
    if doi:
        clauses.append("normalized_doi=?")
        params.append(normalize_text(doi))
    if url:
        clauses.append("normalized_url=?")
        params.append(normalize_text(url))
    if title:
        clauses.append("normalized_title=?")
        params.append(normalize_text(title))
    if not clauses:
        print("provide --title, --url, --doi, or --file", file=sys.stderr)
        return 2
    rows = conn.execute(f"SELECT * FROM papers WHERE {' OR '.join(clauses)} ORDER BY updated_at DESC", params).fetchall()
    if not rows and title:
        tokens = [token for token in normalize_text(title).split() if len(token) >= 4]
        if tokens:
            important = tokens[:6]
            like_clause = " AND ".join("normalized_title LIKE ?" for _ in important)
            like_params = [f"%{token}%" for token in important]
            rows = conn.execute(
                f"SELECT * FROM papers WHERE {like_clause} ORDER BY updated_at DESC",
                like_params,
            ).fetchall()
        if not rows and tokens:
            # Fallback for partial titles: require at least two significant token hits.
            candidates = conn.execute("SELECT * FROM papers ORDER BY updated_at DESC").fetchall()
            scored = []
            wanted = set(tokens)
            for row in candidates:
                have = set(str(row["normalized_title"]).split())
                score = len(wanted & have)
                if score >= min(2, len(wanted)):
                    scored.append((score, row))
            rows = [row for _, row in sorted(scored, key=lambda item: item[0], reverse=True)]
    if not rows:
        print("not-found")
        return 1
    for row in rows:
        print(f"found id={row['id']} status={row['status']} title={row['title']} pdf={row['pdf_path']} md={row['md_path']}")
    return 0


def add_manual(root: Path, title: str, url: str | None, doi: str | None, reason: str) -> None:
    conn = connect(root)
    upsert_paper(conn, title=title, source_url=url, doi=doi, status="manual_needed")
    queue = root / "need_sysu_download.md"
    existing = queue.read_text(encoding="utf-8", errors="replace") if queue.exists() else ""
    key = normalize_text(doi or url or title)
    if key and key in normalize_text(existing):
        print(f"manual item already listed: {title}")
        return
    if not existing.strip():
        existing = "## Need SYSU Download\n\n"
    entry_no = len(re.findall(r"^\d+\.", existing, flags=re.MULTILINE)) + 1
    lines = [f"{entry_no}. {title}"]
    if url:
        lines.append(f"   Link: {url}")
    if doi:
        lines.append(f"   DOI: {doi}")
    lines.append(f"   Reason: {reason}")
    queue.write_text(existing.rstrip() + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"queued manual download: {title}")


def search(root: Path, query: str, limit: int) -> None:
    print("SQLite FTS search has moved to LightRAG.")
    print(
        "Use: python nanobot\\skills\\paper-ingest\\scripts\\lightrag_rag.py "
        f"--root {root} query {query!r} --mode hybrid"
    )


def stats(root: Path) -> None:
    conn = connect(root)
    papers = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    try:
        lightrag_docs = conn.execute("SELECT COUNT(*) FROM lightrag_docs").fetchone()[0]
    except sqlite3.OperationalError:
        lightrag_docs = 0
    by_status = conn.execute("SELECT status, COUNT(*) AS n FROM papers GROUP BY status ORDER BY status").fetchall()
    print(f"db={root / DB_NAME}")
    print(f"papers={papers} lightrag_docs={lightrag_docs}")
    for row in by_status:
        print(f"{row['status']}={row['n']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Maintain the paper-ingest SQLite/RAG index.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help="Scan pdf/ and md/, update paper metadata for duplicate detection.")

    check = sub.add_parser("check", help="Check whether a paper is already known.")
    check.add_argument("--title")
    check.add_argument("--url")
    check.add_argument("--doi")
    check.add_argument("--file", type=Path)

    manual = sub.add_parser("add-manual", help="Add a SYSU/manual download item and DB record.")
    manual.add_argument("--title", required=True)
    manual.add_argument("--url")
    manual.add_argument("--doi")
    manual.add_argument("--reason", default="requires SYSU federated login / institutional access")

    search_cmd = sub.add_parser("search", help="Deprecated. Use lightrag_rag.py query instead.")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--limit", type=int, default=8)

    sub.add_parser("stats", help="Show DB counts.")

    args = parser.parse_args()
    if args.command == "sync":
        sync_library(args.root)
        return 0
    if args.command == "check":
        return check_duplicate(args.root, args.title, args.url, args.doi, args.file)
    if args.command == "add-manual":
        add_manual(args.root, args.title, args.url, args.doi, args.reason)
        return 0
    if args.command == "search":
        search(args.root, args.query, args.limit)
        return 0
    if args.command == "stats":
        stats(args.root)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
