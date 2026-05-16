from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


from workspace_paths import DEFAULT_RAG_ROOT, load_workspace_env, resolve_rag_root, RAG_BASE, resolve_pdf_path

DEFAULT_ROOT = DEFAULT_RAG_ROOT
DB_NAME = "articles.sqlite3"
TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".rst",
    ".py",
    ".ipynb",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".sh",
    ".ps1",
    ".bat",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".cpp",
    ".cc",
    ".c",
    ".h",
    ".hpp",
    ".cu",
    ".m",
    ".go",
    ".rs",
    ".r",
}
SKIP_DIRS = {
    ".git",
    ".github",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    "outputs",
    "runs",
    "wandb",
    "data",
    "datasets",
    "checkpoints",
    "weights",
    "pretrained",
}
MAX_FILES = 160
MAX_FILE_BYTES = 180_000
MAX_CHARS_PER_FILE = 18_000


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_url(url: str) -> str:
    url = url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    return url.rstrip("/")


def repo_slug(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{parts[-2]}__{parts[-1]}")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", normalize_url(url))[:80]


def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS github_sources (
            id INTEGER PRIMARY KEY,
            paper_title TEXT,
            repo_url TEXT NOT NULL UNIQUE,
            normalized_url TEXT NOT NULL UNIQUE,
            local_path TEXT,
            rag_md_path TEXT,
            commit_hash TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def run(command: list[str], cwd: Path | None = None) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode, completed.stdout


def add_repo(root: Path, url: str, paper_title: str | None, *, pdf: Path | None = None, book: str | None = None) -> None:
    pdf_path = resolve_pdf_path(RAG_BASE, pdf, book=book) if (pdf is not None or book) else None
    root = resolve_rag_root(root, pdf=pdf_path, book=book)
    load_workspace_env(root)
    conn = connect(root)
    normalized = normalize_url(url)
    slug = repo_slug(url)
    local_path = root / "code" / slug
    rag_md_path = root / "code_md" / f"{slug}.md"
    stamp = now_iso()
    conn.execute(
        """
        INSERT INTO github_sources (paper_title, repo_url, normalized_url, local_path, rag_md_path, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'registered', ?, ?)
        ON CONFLICT(normalized_url) DO UPDATE SET
            paper_title=COALESCE(excluded.paper_title, paper_title),
            repo_url=excluded.repo_url,
            local_path=excluded.local_path,
            rag_md_path=excluded.rag_md_path,
            updated_at=excluded.updated_at
        """,
        (paper_title, url, normalized, str(local_path), str(rag_md_path), stamp, stamp),
    )
    conn.commit()
    print(f"registered repo {url} -> {local_path}")


def sync_repo(row: sqlite3.Row) -> tuple[str | None, str]:
    url = row["repo_url"]
    local_path = Path(row["local_path"])
    local_path.parent.mkdir(parents=True, exist_ok=True)
    if local_path.exists():
        rc, output = run(["git", "pull", "--ff-only"], cwd=local_path)
        if rc != 0:
            return None, f"pull failed: {output[-500:]}"
    else:
        rc, output = run(["git", "clone", "--depth", "1", url, str(local_path)])
        if rc != 0:
            return None, f"clone failed: {output[-500:]}"
    rc, commit = run(["git", "rev-parse", "HEAD"], cwd=local_path)
    if rc != 0:
        return None, f"rev-parse failed: {commit[-500:]}"
    return commit.strip(), "ok"


def should_include(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if any(part in SKIP_DIRS for part in rel.parts):
        return False
    if path.suffix.lower() not in TEXT_EXTENSIONS:
        return False
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return False
    except OSError:
        return False
    return True


def file_priority(path: Path) -> tuple[int, str]:
    rel = path.as_posix().lower()
    name = path.name.lower()
    if "/trackers/" in rel and rel.endswith(".py"):
        return (0, str(path))
    if rel.endswith("/3. tracker/run.py") or rel.endswith("/tracker/run.py"):
        return (0, str(path))
    if name.startswith("readme"):
        return (1, str(path))
    if name in {"requirements.txt", "environment.yml", "pyproject.toml", "setup.py", "config.yaml", "config.yml"}:
        return (2, str(path))
    if "train" in name or "eval" in name or "test" in name or "demo" in name or "infer" in name:
        return (3, str(path))
    return (4, str(path))


def read_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".ipynb":
        text = re.sub(r'"image/[^"]+":\s*"[^"]+"', '"image/...": "<omitted>"', text)
    return text[:MAX_CHARS_PER_FILE]


def build_repo_markdown(row: sqlite3.Row, commit_hash: str) -> Path:
    local_path = Path(row["local_path"])
    output_path = Path(row["rag_md_path"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    files = [path for path in local_path.rglob("*") if path.is_file() and should_include(path, local_path)]
    files = sorted(files, key=file_priority)[:MAX_FILES]
    digest = hashlib.sha256()
    lines = [
        f"# GitHub Source: {local_path.name}",
        "",
        f"- Repository: {row['repo_url']}",
        f"- Commit: {commit_hash}",
    ]
    if row["paper_title"]:
        lines.append(f"- Related paper: {row['paper_title']}")
    lines.extend(["", "## Files Included", ""])
    for path in files:
        rel = path.relative_to(local_path).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(path.read_bytes())
        lines.append(f"- `{rel}`")
    lines.extend(["", "## Source Files"])
    for path in files:
        rel = path.relative_to(local_path).as_posix()
        language = path.suffix.lower().lstrip(".") or "text"
        content = read_text(path)
        lines.extend(["", f"### `{rel}`", "", f"```{language}", content, "```"])
    lines.extend(["", f"<!-- source_digest: {digest.hexdigest()} -->", ""])
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def sync_all(root: Path, *, pdf: Path | None = None, book: str | None = None) -> None:
    root = resolve_rag_root(root, pdf=resolve_pdf_path(RAG_BASE, pdf, book=book) if (pdf is not None or book) else None, book=book)
    load_workspace_env(root)
    conn = connect(root)
    rows = conn.execute("SELECT * FROM github_sources ORDER BY id").fetchall()
    for row in rows:
        commit_hash, message = sync_repo(row)
        status = "ready" if commit_hash else "failed"
        rag_md_path = row["rag_md_path"]
        if commit_hash:
            rag_md_path = str(build_repo_markdown(row, commit_hash))
        conn.execute(
            """
            UPDATE github_sources
            SET commit_hash=?, rag_md_path=?, status=?, updated_at=?
            WHERE id=?
            """,
            (commit_hash, rag_md_path, status, now_iso(), row["id"]),
        )
        conn.commit()
        print(f"{status} {row['repo_url']} {message}")
    try:
        from paper_rag_links import ensure_links_table, ensure_official_repo_links

        ensure_links_table(conn)
        created = ensure_official_repo_links(conn)
        if created:
            print(f"paper-code document links created: {created}")
    except ImportError:
        pass


def list_repos(root: Path, *, pdf: Path | None = None, book: str | None = None) -> None:
    root = resolve_rag_root(root, pdf=resolve_pdf_path(RAG_BASE, pdf, book=book) if (pdf is not None or book) else None, book=book)
    conn = connect(root)
    for row in conn.execute("SELECT * FROM github_sources ORDER BY id"):
        print(f"{row['id']}. {row['status']} {row['repo_url']} commit={row['commit_hash']} md={row['rag_md_path']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Register GitHub repos related to papers and prepare code Markdown for LightRAG.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="RAG base or book folder (default: articles/rag).")
    parser.add_argument("--pdf", type=Path, help="PDF under articles/pdf (book folder = stem unless --book).")
    parser.add_argument("--book", help="Book folder name under articles/rag/.")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Register a GitHub repository.")
    add.add_argument("url")
    add.add_argument("--paper-title")

    sub.add_parser("sync", help="Clone/pull registered repositories and generate code Markdown.")
    sub.add_parser("list", help="List registered repositories.")

    args = parser.parse_args()
    if args.command == "add":
        add_repo(args.root, args.url, args.paper_title, pdf=args.pdf, book=args.book)
        return 0
    if args.command == "sync":
        sync_all(args.root, pdf=args.pdf, book=args.book)
        return 0
    if args.command == "list":
        list_repos(args.root, pdf=args.pdf, book=args.book)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
