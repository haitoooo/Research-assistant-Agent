from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from paper_rag_links import (
    ensure_links_table,
    ensure_official_repo_links,
    fetch_linked_chunks,
    link_stats,
    sync_chunk_links,
)
from workspace_paths import (
    RAG_BASE,
    DEFAULT_RAG_ROOT,
    load_workspace_env,
    pdf_dir_for,
    rag_base_from,
    resolve_pdf_path,
    resolve_rag_root,
)


DEFAULT_ROOT = DEFAULT_RAG_ROOT


def open_workspace(
    root: Path | None = None,
    *,
    pdf: Path | None = None,
    book: str | None = None,
) -> tuple[Path, Path | None]:
    resolved_pdf = None
    if pdf is not None or book is not None:
        resolved_pdf = resolve_pdf_path(RAG_BASE, pdf, book=book)
    book_root = resolve_rag_root(root, pdf=resolved_pdf, book=book)
    load_workspace_env(book_root)
    return book_root, resolved_pdf
DB_NAME = "articles.sqlite3"
TRACE_NAME = "paper_rag_trace.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def load_json(path: Path | None) -> dict:
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def find_config(root: Path) -> Path | None:
    from workspace_paths import articles_root_for

    articles = articles_root_for(resolve_rag_root(root))
    for candidate in (articles.parent.parent / "config.json", Path(".nanobot/config.json"), Path("config.json")):
        if candidate.exists():
            return candidate
    return None


def safe_host(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    return parsed.netloc or url.split("/", 1)[0]


def api_url(base: str, endpoint: str) -> str:
    return base.rstrip("/") + "/" + endpoint.lstrip("/")


def resolve_config(root: Path) -> dict:
    config = load_json(find_config(root))
    defaults = config.get("agents", {}).get("defaults", {})
    providers = config.get("providers", {})
    llm_provider_name = defaults.get("provider") or "openai"
    embed_provider_name = defaults.get("embedProvider") or defaults.get("embed_provider") or llm_provider_name
    llm_provider = providers.get(llm_provider_name, {})
    embed_provider = providers.get(embed_provider_name, {})
    llm_model = (
        os.getenv("PAPER_RAG_LLM_MODEL")
        or defaults.get("model")
        or defaults.get("visual_model")
        or "gpt-4o-mini"
    )
    vlm_model = (
        os.getenv("PAPER_RAG_VLM_MODEL")
        or defaults.get("visual_model")
        or llm_model
    )
    return {
        "llm_provider": llm_provider_name,
        "llm_model": llm_model,
        "vlm_model": vlm_model,
        "llm_api_key": os.getenv("PAPER_RAG_LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or llm_provider.get("apiKey")
        or llm_provider.get("api_key")
        or "",
        "llm_api_base": os.getenv("PAPER_RAG_LLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or llm_provider.get("apiBase")
        or llm_provider.get("api_base")
        or "",
        "embedding_provider": embed_provider_name,
        "embedding_model": os.getenv("PAPER_RAG_EMBEDDING_MODEL")
        or defaults.get("embed_model")
        or "text-embedding-3-small",
        "embedding_api_key": os.getenv("PAPER_RAG_EMBEDDING_API_KEY")
        or embed_provider.get("apiKey")
        or embed_provider.get("api_key")
        or "",
        "embedding_api_base": os.getenv("PAPER_RAG_EMBEDDING_BASE_URL")
        or embed_provider.get("apiBase")
        or embed_provider.get("api_base")
        or "",
        "embedding_dim": int(os.getenv("PAPER_RAG_EMBEDDING_DIM") or "1536"),
    }


def trace(root: Path, event: str, **fields) -> None:
    if os.getenv("PAPER_RAG_TRACE", "1").lower() in {"0", "false", "no"}:
        return
    safe = {
        key: value
        for key, value in fields.items()
        if key.lower() not in {"api_key", "authorization", "token", "secret", "access_key", "secret_key"}
    }
    payload = {"ts": now_iso(), "event": event, **safe}
    line = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        with (root / TRACE_NAME).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass
    try:
        summary = {k: safe[k] for k in ("model", "provider", "path", "duration_ms", "error_type", "count") if k in safe}
        print(f"trace {event}: {json.dumps(summary, ensure_ascii=True, default=str)}", flush=True)
    except Exception:
        pass


def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_rag_sources (
            source_path TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            title TEXT,
            status TEXT NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_rag_chunks (
            chunk_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            source_type TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            heading TEXT,
            content TEXT NOT NULL,
            embedding_json TEXT,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_rag_cards (
            source_path TEXT PRIMARY KEY,
            card_json TEXT,
            card_md TEXT NOT NULL,
            embedding_json TEXT,
            model TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_rag_images (
            image_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            image_path TEXT NOT NULL,
            alt TEXT,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_rag_vision_cache (
            cache_id TEXT PRIMARY KEY,
            image_path TEXT NOT NULL,
            question TEXT NOT NULL,
            response TEXT NOT NULL,
            model TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS paper_rag_chunks_fts USING fts5(chunk_id UNINDEXED, source_path UNINDEXED, heading, content)"
        )
    except sqlite3.Error:
        pass
    ensure_column(conn, "paper_rag_cards", "embedding_json", "TEXT")
    ensure_links_table(conn)
    conn.commit()
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def is_noise_heading(line: str) -> bool:
    text = line.strip().lower().strip("# ").strip()
    return text in {"references", "reference", "acknowledgements", "acknowledgments", "bibliography"}


def title_from_markdown(path: Path, text: str) -> str:
    for line in text.splitlines():
        clean = line.strip()
        if clean.startswith("#") and len(clean.strip("# ")) > 3:
            return clean.strip("# ").strip()[:240]
    return path.stem


def clean_markdown(text: str, *, source_type: str) -> tuple[str, list[tuple[str, str]]]:
    images: list[tuple[str, str]] = []
    out: list[str] = []
    image_re = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
    for line in text.splitlines():
        if source_type == "paper_md" and is_noise_heading(line):
            break
        for match in image_re.finditer(line):
            images.append((match.group(2).strip().strip('"').strip("'"), match.group(1).strip()))
        line = image_re.sub("", line)
        if source_type == "paper_md" and line.strip().startswith("|") and line.count("|") >= 2:
            out.append(line)
            continue
        out.append(line)
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, images


MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
COMMAND_LINE_RE = re.compile(
    r"(?im)^\s*(?:\$|>|ps>|python(?:3)?(?:\s+-m)?|pip(?:3)?|conda|mamba|uv|poetry|pdm|"
    r"npm|pnpm|yarn|node|bash|sh|zsh|make|cmake|git|docker|docker-compose|"
    r"accelerate|torchrun|deepspeed|pytest|jupyter)\b|^\s*[\w./-]+\.py\b"
)


def is_markdown_heading(line: str) -> bool:
    return bool(MARKDOWN_HEADING_RE.match(line))


def has_command_text(text: str) -> bool:
    return bool(COMMAND_LINE_RE.search(text))


def split_chunks(text: str, *, source_type: str, max_chars: int) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current_heading = ""
    buffer: list[str] = []
    in_fence = False

    def flush_section() -> None:
        nonlocal buffer
        content = "\n".join(buffer).strip()
        if content:
            sections.append((current_heading, content))
        buffer = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            buffer.append(line)
            in_fence = not in_fence
            continue
        if not in_fence and is_markdown_heading(line):
            flush_section()
            current_heading = line.strip("# ").strip()[:200]
        buffer.append(line)
    flush_section()

    min_chars = 80 if source_type == "paper_md" else 40
    chunks: list[tuple[str, str]] = []
    for heading, content in sections:
        while len(content) > max_chars:
            cut = content.rfind("\n", 0, max_chars)
            if cut < max_chars // 2:
                cut = content.rfind(". ", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            chunks.append((heading, content[:cut].strip()))
            content = content[cut:].strip()
        if len(content) >= min_chars or (source_type == "code_md" and has_command_text(content)):
            chunks.append((heading, content))
    return chunks


def default_sources(root: Path, include_code: bool) -> list[tuple[Path, str]]:
    sources = [(p, "paper_md") for p in sorted((root / "md").rglob("*.md")) if p.stat().st_size > 0]
    if include_code:
        sources.extend((p, "code_md") for p in sorted((root / "code_md").rglob("*.md")) if p.stat().st_size > 0)
    return sources


def embed_texts(root: Path, cfg: dict, texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    if not cfg["embedding_api_key"] or not cfg["embedding_api_base"]:
        raise RuntimeError("Missing embedding API config. Use --skip-embedding for lexical-only indexing.")
    start = time.perf_counter()
    trace(
        root,
        "embedding.start",
        provider=cfg["embedding_provider"],
        model=cfg["embedding_model"],
        host=safe_host(cfg["embedding_api_base"]),
        count=len(texts),
        total_chars=sum(len(t) for t in texts),
    )
    try:
        response = requests.post(
            api_url(cfg["embedding_api_base"], "embeddings"),
            headers={"Authorization": f"Bearer {cfg['embedding_api_key']}", "Content-Type": "application/json"},
            json={"model": cfg["embedding_model"], "input": texts},
            timeout=120,
        )
        payload = response.json()
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {str(payload)[:500]}")
        vectors = [item["embedding"] for item in payload["data"]]
        trace(root, "embedding.end", model=cfg["embedding_model"], count=len(vectors), duration_ms=int((time.perf_counter() - start) * 1000))
        return vectors
    except Exception as exc:
        trace(root, "embedding.error", model=cfg["embedding_model"], error_type=type(exc).__name__, error=str(exc)[:500])
        raise


def chat_complete(
    root: Path,
    cfg: dict,
    messages: list[dict],
    *,
    max_tokens: int = 1200,
    temperature: float = 0.1,
    use_vlm: bool = False,
) -> str:
    if not cfg["llm_api_key"] or not cfg["llm_api_base"]:
        raise RuntimeError("Missing LLM API config.")
    model = cfg["vlm_model"] if use_vlm else cfg["llm_model"]
    event = "vlm" if use_vlm else "llm"
    start = time.perf_counter()
    prompt_chars = sum(len(json.dumps(m, ensure_ascii=False, default=str)) for m in messages)
    trace(
        root,
        f"{event}.start",
        provider=cfg["llm_provider"],
        model=model,
        host=safe_host(cfg["llm_api_base"]),
        prompt_chars=prompt_chars,
    )
    try:
        response = requests.post(
            api_url(cfg["llm_api_base"], "chat/completions"),
            headers={"Authorization": f"Bearer {cfg['llm_api_key']}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
            timeout=300,
        )
        payload = response.json()
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {str(payload)[:500]}")
        text = payload["choices"][0]["message"].get("content") or ""
        trace(root, f"{event}.end", model=model, duration_ms=int((time.perf_counter() - start) * 1000), response_chars=len(text))
        return text.strip()
    except Exception as exc:
        trace(root, f"{event}.error", model=model, error_type=type(exc).__name__, error=str(exc)[:500])
        raise


def upsert_source(root: Path, conn: sqlite3.Connection, cfg: dict, path: Path, source_type: str, *, force: bool, skip_embedding: bool, mode: str) -> None:
    raw = path.read_text(encoding="utf-8", errors="replace")
    digest = sha256_file(path)
    existing = conn.execute("SELECT sha256, status FROM paper_rag_sources WHERE source_path=?", (str(path),)).fetchone()
    if existing and existing["sha256"] == digest and existing["status"] == "ready" and not force:
        if mode == "enrich" and source_type == "paper_md":
            card = conn.execute("SELECT embedding_json FROM paper_rag_cards WHERE source_path=?", (str(path),)).fetchone()
            if not card or not card["embedding_json"]:
                title = title_from_markdown(path, raw)
                cleaned, _ = clean_markdown(raw, source_type=source_type)
                trace(root, "paper_card.start", path=str(path))
                create_paper_card(root, conn, cfg, path, title, cleaned)
                conn.commit()
                trace(root, "paper_card.end", path=str(path))
        print(f"skip {path.name}")
        return

    title = title_from_markdown(path, raw)
    cleaned, images = clean_markdown(raw, source_type=source_type)
    max_chars = 4500 if source_type == "paper_md" else 6000
    chunks = split_chunks(cleaned, source_type=source_type, max_chars=max_chars)
    trace(root, "source.start", path=str(path), source_type=source_type, count=len(chunks))

    conn.execute("DELETE FROM paper_rag_chunks WHERE source_path=?", (str(path),))
    try:
        conn.execute("DELETE FROM paper_rag_chunks_fts WHERE source_path=?", (str(path),))
    except sqlite3.Error:
        pass

    batch_size = 24
    embeddings: list[list[float] | None] = [None] * len(chunks)
    if not skip_embedding:
        for offset in range(0, len(chunks), batch_size):
            texts = [content for _, content in chunks[offset : offset + batch_size]]
            vectors = embed_texts(root, cfg, texts)
            for index, vector in enumerate(vectors, start=offset):
                embeddings[index] = vector

    for index, ((heading, content), vector) in enumerate(zip(chunks, embeddings)):
        chunk_id = sha1_text(f"{path.resolve()}\0{index}\0{content}")
        conn.execute(
            """
            INSERT OR REPLACE INTO paper_rag_chunks
            (chunk_id, source_path, source_type, chunk_index, heading, content, embedding_json, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chunk_id, str(path), source_type, index, heading, content, json.dumps(vector) if vector is not None else None, now_iso()),
        )
        try:
            conn.execute("INSERT INTO paper_rag_chunks_fts(chunk_id, source_path, heading, content) VALUES (?, ?, ?, ?)", (chunk_id, str(path), heading, content))
        except sqlite3.Error:
            pass

    conn.execute(
        """
        INSERT OR REPLACE INTO paper_rag_sources
        (source_path, source_type, sha256, title, status, chunk_count, indexed_at)
        VALUES (?, ?, ?, ?, 'ready', ?, ?)
        """,
        (str(path), source_type, digest, title, len(chunks), now_iso()),
    )

    if source_type == "paper_md":
        conn.execute("DELETE FROM paper_rag_images WHERE source_path=?", (str(path),))
        for raw_target, alt in images:
            if "://" in raw_target or raw_target.startswith("data:"):
                continue
            image_path = (path.parent / raw_target).resolve()
            if not image_path.exists():
                continue
            image_id = sha1_text(str(image_path))
            conn.execute(
                "INSERT OR REPLACE INTO paper_rag_images(image_id, source_path, image_path, alt, status, updated_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (image_id, str(path), str(image_path), alt, now_iso()),
            )
        if mode == "enrich":
            create_paper_card(root, conn, cfg, path, title, cleaned)

    conn.commit()
    trace(root, "source.end", path=str(path), source_type=source_type, count=len(chunks))


def create_paper_card(root: Path, conn: sqlite3.Connection, cfg: dict, path: Path, title: str, cleaned: str) -> None:
    excerpt = cleaned[:28000]
    prompt = (
        "Create a concise structured paper card for RAG. Return JSON with keys: "
        "problem, method, contributions, datasets, metrics, code_relevance, limitations, summary. "
        "Focus on paper and code retrieval value. Do not discuss figures unless essential.\n\n"
        f"Title: {title}\n\nPaper text:\n{excerpt}"
    )
    text = chat_complete(root, cfg, [{"role": "user", "content": prompt}], max_tokens=1500)
    card_json = None
    try:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        card_json = json.loads(match.group(0) if match else text)
    except Exception:
        pass
    embedding_json = None
    try:
        embedding_json = json.dumps(embed_texts(root, cfg, [text])[0])
    except Exception as exc:
        trace(root, "paper_card.embedding_skip", path=str(path), error_type=type(exc).__name__, error=str(exc)[:300])
    conn.execute(
        "INSERT OR REPLACE INTO paper_rag_cards(source_path, card_json, card_md, embedding_json, model, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (str(path), json.dumps(card_json, ensure_ascii=False) if card_json is not None else None, text, embedding_json, cfg["llm_model"], now_iso()),
    )


def sync(
    root: Path,
    mode: str,
    include_code: bool,
    include_pdf: bool,
    pdf: Path | None,
    force: bool,
    skip_embedding: bool,
    *,
    link: bool = False,
    link_skip_llm: bool = False,
    book: str | None = None,
) -> None:
    pdf_path = None
    if include_pdf or pdf is not None or book:
        if include_pdf or pdf is not None:
            pdf_path = resolve_pdf_path(RAG_BASE, pdf, book=book)
    root = resolve_rag_root(root, pdf=pdf_path, book=book)
    load_workspace_env(root)
    conn = connect(root)
    cfg = resolve_config(root)
    trace(
        root,
        "sync.start",
        mode=mode,
        include_code=include_code,
        include_pdf=include_pdf,
        pdf=str(pdf_path) if pdf_path else None,
        book=book or root.name,
    )
    if include_pdf:
        run_mineru_if_needed(root, pdf_path or resolve_pdf_path(RAG_BASE, pdf, book=book))
    sources = default_sources(root, include_code)
    for path, source_type in sources:
        upsert_source(root, conn, cfg, path, source_type, force=force, skip_embedding=skip_embedding, mode=mode)
    doc_links = ensure_official_repo_links(conn)
    if doc_links:
        print(f"paper-code document links created/verified: {doc_links}")
    if link and not skip_embedding:
        run_link(root, force=force, skip_llm=link_skip_llm)
    trace(root, "sync.end", count=len(sources))
    print(f"paper-rag sync complete sources={len(sources)} mode={mode}")


def run_mineru_if_needed(root: Path, pdf_path: Path) -> None:
    target = root / "md" / f"{pdf_path.stem}.md"
    if target.exists() and target.stat().st_size > 0:
        return
    helper = Path(__file__).resolve().with_name("mineru_pdf_to_md.py")
    if not helper.exists():
        raise RuntimeError(f"Missing MinerU helper: {helper}")
    completed = subprocess.run(
        [sys.executable, str(helper), "--root", str(root), "--pdf", str(pdf_path.name)],
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"MinerU helper failed with exit code {completed.returncode}")


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


CODE_QUERY_TERMS = {
    "code",
    "github",
    "repo",
    "repository",
    "run",
    "script",
    "command",
    "config",
    "configuration",
    "train",
    "training",
    "demo",
    "install",
    "requirements",
    "dataset path",
    "source code",
    "源码",
    "代码",
    "运行",
    "命令",
    "脚本",
    "安装",
    "配置",
}

COMMAND_QUERY_TERMS = {
    "how do i",
    "how to",
    "run",
    "command",
    "script",
    "execute",
    "launch",
    "start",
    "install",
    "setup",
    "train",
    "infer",
    "inference",
    "evaluate",
    "运行",
    "命令",
    "脚本",
    "执行",
    "启动",
    "安装",
    "训练",
    "推理",
}

CARD_QUERY_TERMS = {
    "contribution",
    "contributions",
    "summary",
    "summarize",
    "problem",
    "method",
    "approach",
    "dataset",
    "datasets",
    "metric",
    "metrics",
    "limitation",
    "limitations",
    "failure",
    "failures",
    "ablation",
    "result",
    "results",
    "评测",
    "评价",
    "指标",
    "数据集",
    "贡献",
    "方法",
    "局限",
    "失败",
}

EVAL_QUERY_TERMS = {
    "dataset",
    "datasets",
    "metric",
    "metrics",
    "evaluate",
    "evaluation",
    "benchmark",
    "benchmarks",
    "result",
    "results",
    "hota",
    "idf1",
    "mota",
    "assa",
    "mot17",
    "mot20",
    "dancetrack",
    "评测",
    "评价",
    "指标",
    "数据集",
    "结果",
}

EVAL_SECTION_TERMS = {
    "dataset",
    "datasets",
    "metric",
    "metrics",
    "experimental",
    "experiment",
    "experiments",
    "evaluation",
    "benchmark",
    "hota",
    "idf1",
    "mota",
    "assa",
    "detections",
    "mot17",
    "mot20",
    "dancetrack",
}


def tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-zA-Z0-9_./-]+", text.lower()) if len(token) >= 3}


def query_profile(question: str) -> dict[str, bool]:
    lower = question.lower()
    tokens = tokenize(question)
    wants_eval = any(term in lower for term in EVAL_QUERY_TERMS) or bool(tokens & EVAL_QUERY_TERMS)
    wants_code = any(term in lower for term in CODE_QUERY_TERMS) or bool(tokens & CODE_QUERY_TERMS)
    wants_card = any(term in lower for term in CARD_QUERY_TERMS) or bool(tokens & CARD_QUERY_TERMS)
    wants_command = wants_code and any(term in lower for term in COMMAND_QUERY_TERMS)
    return {"code": wants_code, "card": wants_card or (wants_eval and not wants_code), "command": wants_command, "eval": wants_eval}


def lexical_bonus(question_terms: set[str], *texts: str | None) -> float:
    haystack = tokenize(" ".join(text or "" for text in texts))
    if not haystack:
        return 0.0
    overlap = len(question_terms & haystack)
    return min(0.12, overlap * 0.015)


def evaluation_bonus(profile: dict[str, bool], row: sqlite3.Row) -> float:
    if not profile.get("eval") or row["source_type"] != "paper_md":
        return 0.0
    heading = (row["heading"] or "").lower()
    content = row["content"].lower()
    combined_terms = tokenize(heading) | tokenize(content[:5000])
    hits = len(combined_terms & EVAL_SECTION_TERMS)
    bonus = min(0.28, hits * 0.035)
    if any(term in heading for term in ("metric", "dataset", "experiment", "result", "evaluation")):
        bonus += 0.12
    if any(term in content for term in ("hota", "idf1", "mota", "assa")):
        bonus += 0.08
    return min(0.4, bonus)


def command_bonus(profile: dict[str, bool], question_terms: set[str], content: str, heading: str) -> float:
    if not profile.get("command"):
        return 0.0
    bonus = 0.0
    command_lines = COMMAND_LINE_RE.findall(content)
    if command_lines or has_command_text(content):
        bonus += 0.24
    combined_terms = tokenize(heading) | tokenize(content)
    overlap = question_terms & combined_terms
    if overlap:
        bonus += min(0.18, len(overlap) * 0.03)
    if any(term in combined_terms for term in {"install", "setup", "train", "eval", "test", "demo", "inference", "infer"}):
        bonus += 0.08
    return min(0.5, bonus)


def finalize_candidates(candidates: list[dict], profile: dict[str, bool], top_k: int) -> list[dict]:
    ranked = [item for item in sorted(candidates, key=lambda item: item["score"], reverse=True) if item["score"] > 0]
    if profile.get("code"):
        primary = [item for item in ranked if item["source_type"] == "code_md"]
        if not primary:
            return ranked[:top_k]
        secondary = [item for item in ranked if item["source_type"] != "code_md"]
        result = primary[:top_k]
        if len(result) < top_k and secondary:
            result.extend(secondary[: min(2, top_k - len(result))])
        return result[:top_k]
    primary = [item for item in ranked if item["source_type"] in {"paper_card", "paper_md"}]
    secondary = [item for item in ranked if item["source_type"] not in {"paper_card", "paper_md"}]
    result = primary[:top_k]
    if len(result) < top_k and secondary:
        result.extend(secondary[: min(1, top_k - len(result))])
    return result[:top_k]


def row_candidate(row: sqlite3.Row, score: float, kind: str = "chunk", *, link_meta: dict | None = None) -> dict:
    item = {
        "kind": kind,
        "score": score,
        "source_type": row["source_type"],
        "source_path": row["source_path"],
        "chunk_index": row["chunk_index"],
        "heading": row["heading"] or "",
        "content": row["content"],
        "chunk_id": row["chunk_id"],
    }
    if link_meta:
        item.update(link_meta)
    return item


def expand_linked_candidates(conn: sqlite3.Connection, candidates: list[dict], profile: dict[str, bool]) -> list[dict]:
    chunk_ids = {item["chunk_id"] for item in candidates if item.get("chunk_id")}
    if not chunk_ids:
        return candidates
    linked_rows = fetch_linked_chunks(conn, chunk_ids)
    if not linked_rows:
        return candidates
    existing = {item.get("chunk_id") for item in candidates}
    for row in linked_rows:
        if row["chunk_id"] in existing:
            continue
        base_score = float(row["link_confidence"]) if "link_confidence" in row.keys() else 0.65
        score = base_score + 0.12
        if profile.get("code") and row["source_type"] == "code_md":
            score += 0.1
        elif not profile.get("code") and row["source_type"] == "paper_md":
            score += 0.08
        candidates.append(
            row_candidate(
                row,
                score,
                link_meta={
                    "linked": True,
                    "relation_type": row["relation_type"] if "relation_type" in row.keys() else "",
                    "link_evidence": row["link_evidence"] if "link_evidence" in row.keys() else "",
                },
            )
        )
        existing.add(row["chunk_id"])
    return candidates


def card_candidate(row: sqlite3.Row, score: float) -> dict:
    return {
        "kind": "paper_card",
        "score": score,
        "source_type": "paper_card",
        "source_path": row["source_path"],
        "chunk_index": "card",
        "heading": "paper card",
        "content": row["card_md"],
    }


def retrieve(conn: sqlite3.Connection, root: Path, cfg: dict, question: str, top_k: int) -> list[dict]:
    profile = query_profile(question)
    question_terms = tokenize(question)
    rows = conn.execute("SELECT * FROM paper_rag_chunks WHERE embedding_json IS NOT NULL").fetchall()
    candidates: list[dict] = []
    if rows:
        qvec = embed_texts(root, cfg, [question])[0]
        for row in rows:
            try:
                score = cosine(qvec, json.loads(row["embedding_json"]))
            except Exception:
                score = 0.0
            if profile["code"] and row["source_type"] == "code_md":
                score *= 1.25
                content_lower = row["content"].lower()
                heading_lower = (row["heading"] or "").lower()
                score += command_bonus(profile, question_terms, content_lower, heading_lower)
                if "seqmap" in heading_lower or heading_lower.endswith(".txt`"):
                    score *= 0.75
            elif profile["code"] and row["source_type"] == "paper_md":
                score *= 0.82
            elif not profile["code"] and row["source_type"] == "code_md":
                score *= 0.45 if profile["card"] or profile["eval"] else 0.9
            elif not profile["code"] and row["source_type"] == "paper_md":
                score *= 1.08
            if not profile["code"]:
                score += evaluation_bonus(profile, row)
            score += lexical_bonus(question_terms, row["heading"], row["content"])
            candidates.append(row_candidate(row, score))

        card_rows = conn.execute("SELECT source_path, card_md, embedding_json FROM paper_rag_cards").fetchall()
        for card in card_rows:
            score = 0.0
            if card["embedding_json"]:
                try:
                    score = cosine(qvec, json.loads(card["embedding_json"]))
                except Exception:
                    score = 0.0
            score += lexical_bonus(question_terms, "paper card", card["card_md"])
            if profile["card"]:
                score = max(score * 1.35, 0.78 + lexical_bonus(question_terms, card["card_md"]))
            candidates.append(card_candidate(card, score))

        candidates = expand_linked_candidates(conn, candidates, profile)
        return finalize_candidates(candidates, profile, top_k)
    try:
        fts_rows = conn.execute(
            "SELECT c.* FROM paper_rag_chunks_fts f JOIN paper_rag_chunks c ON c.chunk_id=f.chunk_id WHERE paper_rag_chunks_fts MATCH ? LIMIT ?",
            (question, top_k),
        ).fetchall()
        return [row_candidate(row, 1.0) for row in fts_rows]
    except sqlite3.Error:
        pattern = f"%{question[:80]}%"
        like_rows = conn.execute("SELECT * FROM paper_rag_chunks WHERE content LIKE ? LIMIT ?", (pattern, top_k)).fetchall()
        return [row_candidate(row, 1.0) for row in like_rows]


def run_link(root: Path, force: bool, skip_llm: bool, *, pdf: Path | None = None, book: str | None = None) -> None:
    root, _ = open_workspace(root, pdf=pdf, book=book)
    conn = connect(root)
    cfg = resolve_config(root)
    ensure_official_repo_links(conn)
    total = sync_chunk_links(
        conn,
        root,
        cfg,
        force=force,
        skip_llm=skip_llm,
        chat_complete=chat_complete,
        embed_texts=embed_texts,
        trace=trace,
    )
    stats = link_stats(conn)
    print(f"paper-code chunk links stored: {total}")
    print(f"document_links={stats['document_links']} chunk_links={stats['chunk_links']}")


def query(root: Path, question: str, top_k: int, answer: bool, *, pdf: Path | None = None, book: str | None = None) -> None:
    root, _ = open_workspace(root, pdf=pdf, book=book)
    conn = connect(root)
    cfg = resolve_config(root)
    rows = retrieve(conn, root, cfg, question, top_k)
    print(f"retrieved={len(rows)}")
    for row in rows:
        link_note = ""
        if row.get("linked"):
            link_note = f" linked={row.get('relation_type', '')} {row.get('link_evidence', '')[:80]}"
        print(
            f"\n[{row['source_type']}] score={row['score']:.3f} {row['source_path']}#{row['chunk_index']} "
            f"{row['heading'] or ''}{link_note}"
        )
        print(row["content"][:700].replace("\n", " "))
    if answer and rows:
        blocks = []
        for row in rows:
            header = f"Source: {row['source_path']}#{row['chunk_index']} ({row['source_type']})"
            if row.get("linked"):
                header += f" [paper-code link: {row.get('relation_type', '')}]"
                if row.get("link_evidence"):
                    header += f" evidence={row['link_evidence'][:200]}"
            blocks.append(f"{header}\n{row['content']}")
        context = "\n\n".join(blocks)
        prompt = (
            "Answer the question using only the context. Cite source paths and chunk indexes. "
            "When a source is marked as a paper-code link, treat it as an aligned paper/code pair.\n\n"
            f"Question: {question}\n\nContext:\n{context}"
        )
        print("\nANSWER\n" + chat_complete(root, cfg, [{"role": "user", "content": prompt}], max_tokens=1400))


def vision(root: Path, selector: str, question: str, image_index: int, *, pdf: Path | None = None, book: str | None = None) -> None:
    root, _ = open_workspace(root, pdf=pdf, book=book)
    conn = connect(root)
    cfg = resolve_config(root)
    rows = conn.execute(
        "SELECT * FROM paper_rag_images WHERE source_path LIKE ? OR image_path LIKE ? ORDER BY image_path",
        (f"%{selector}%", f"%{selector}%"),
    ).fetchall()
    if not rows and Path(selector).exists():
        rows = [{"image_path": str(Path(selector).resolve()), "source_path": selector}]
    if not rows:
        raise RuntimeError(f"No image matched: {selector}")
    row = rows[min(image_index, len(rows) - 1)]
    image_path = Path(row["image_path"])
    cache_id = sha1_text(str(image_path.resolve()) + "\0" + question)
    cached = conn.execute("SELECT response FROM paper_rag_vision_cache WHERE cache_id=?", (cache_id,)).fetchone()
    if cached:
        print(cached["response"])
        return
    mime = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
            ],
        }
    ]
    response = chat_complete(root, cfg, messages, max_tokens=900, use_vlm=True)
    conn.execute(
        "INSERT OR REPLACE INTO paper_rag_vision_cache(cache_id, image_path, question, response, model, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (cache_id, str(image_path), question, response, cfg["vlm_model"], now_iso()),
    )
    conn.commit()
    print(response)


def status(root: Path, *, pdf: Path | None = None, book: str | None = None) -> None:
    root, resolved_pdf = open_workspace(root, pdf=pdf, book=book)
    conn = connect(root)
    sources = conn.execute("SELECT source_type, status, COUNT(*) count, SUM(chunk_count) chunks FROM paper_rag_sources GROUP BY source_type, status").fetchall()
    cards = conn.execute("SELECT COUNT(*) count FROM paper_rag_cards").fetchone()["count"]
    images = conn.execute("SELECT COUNT(*) count FROM paper_rag_images").fetchone()["count"]
    print(f"articles_root={pdf_dir_for(root).parent}")
    print(f"pdf_dir={pdf_dir_for(root)}")
    print(f"rag_base={rag_base_from(root)}")
    print(f"book={root.name}")
    print(f"book_root={root}")
    if resolved_pdf:
        print(f"pdf={resolved_pdf}")
    print(f"db={root / DB_NAME}")
    print(f"trace={root / TRACE_NAME}")
    for row in sources:
        print(f"{row['source_type']} {row['status']} sources={row['count']} chunks={row['chunks'] or 0}")
    print(f"paper_cards={cards}")
    print(f"images_registered={images}")
    stats = link_stats(conn)
    print(f"paper_code_links document={stats['document_links']} chunk={stats['chunk_links']}")
    for row in stats["breakdown"][:8]:
        print(f"  {row['relation_type']} via {row['match_method']}: {row['count']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Lightweight paper/code RAG with optional paper cards and on-demand vision.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="RAG base (articles/rag) or a book folder (articles/rag/<book>).")
    parser.add_argument(
        "--pdf",
        type=Path,
        help="PDF under articles/pdf; book folder defaults to the PDF stem.",
    )
    parser.add_argument("--book", help="Book folder name under articles/rag/ (overrides PDF stem).")
    sub = parser.add_subparsers(dest="command", required=True)

    sync_cmd = sub.add_parser("sync")
    sync_cmd.add_argument("--mode", choices=["fast", "enrich"], default="fast")
    sync_cmd.add_argument("--include-code", action=argparse.BooleanOptionalAction, default=True)
    sync_cmd.add_argument("--include-pdf", action="store_true")
    sync_cmd.add_argument("--force", action="store_true")
    sync_cmd.add_argument("--skip-embedding", action="store_true")
    sync_cmd.add_argument("--link", action="store_true", help="After indexing, build paper-code chunk links (embedding + LLM).")
    sync_cmd.add_argument("--link-skip-llm", action="store_true", help="With --link, use embedding/keyword only.")

    link_cmd = sub.add_parser("link", help="Build paper-code alignment links for registered official repos.")
    link_cmd.add_argument("--force", action="store_true")
    link_cmd.add_argument("--skip-llm", action="store_true", help="Embedding and keyword matching only.")

    query_cmd = sub.add_parser("query")
    query_cmd.add_argument("question")
    query_cmd.add_argument("--top-k", type=int, default=8)
    query_cmd.add_argument("--no-answer", action="store_true")

    vision_cmd = sub.add_parser("vision")
    vision_cmd.add_argument("selector", help="Image path or substring matched against source/image paths.")
    vision_cmd.add_argument("--question", default="Describe this figure for understanding the paper method.")
    vision_cmd.add_argument("--image-index", type=int, default=0)

    sub.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "sync":
            sync(
                args.root,
                args.mode,
                args.include_code,
                args.include_pdf,
                args.pdf,
                args.force,
                args.skip_embedding,
                link=args.link,
                link_skip_llm=args.link_skip_llm,
                book=args.book,
            )
        elif args.command == "link":
            run_link(args.root, args.force, args.skip_llm, pdf=args.pdf, book=args.book)
        elif args.command == "query":
            query(args.root, args.question, args.top_k, not args.no_answer, pdf=args.pdf, book=args.book)
        elif args.command == "vision":
            vision(args.root, args.selector, args.question, args.image_index, pdf=args.pdf, book=args.book)
        elif args.command == "status":
            status(args.root, pdf=args.pdf, book=args.book)
        return 0
    except Exception as exc:
        print(f"paper-rag error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
