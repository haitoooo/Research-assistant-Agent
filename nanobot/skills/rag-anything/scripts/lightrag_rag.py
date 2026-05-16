from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ROOT = Path(".nanobot/workspace/articles")
DB_NAME = "articles.sqlite3"


def find_nanobot_config(root: Path) -> Path | None:
    candidates = [
        root.parent.parent / "config.json",
        Path(".nanobot/config.json"),
        Path("config.json"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_json(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_lightrag_config(root: Path) -> dict[str, str | int]:
    config = load_json(find_nanobot_config(root))
    defaults = config.get("agents", {}).get("defaults", {})
    llm_provider_name = defaults.get("provider") or "openai"
    embed_provider_name = defaults.get("embedProvider") or defaults.get("embed_provider") or llm_provider_name
    llm_provider = config.get("providers", {}).get(llm_provider_name, {})
    embed_provider = config.get("providers", {}).get(embed_provider_name, {})

    llm_model = (
        os.getenv("LIGHTRAG_LLM_MODEL")
        or defaults.get("visual_model")
        or defaults.get("visualModel")
        or defaults.get("model")
        or "gpt-4o-mini"
    )
    embedding_model = os.getenv("LIGHTRAG_EMBEDDING_MODEL") or defaults.get("embed_model") or defaults.get("embedModel") or "text-embedding-3-small"
    embedding_dim = int(os.getenv("LIGHTRAG_EMBEDDING_DIM", "1536"))
    embedding_max_tokens = int(os.getenv("LIGHTRAG_EMBEDDING_MAX_TOKENS", "8192"))

    llm_api_key = os.getenv("LIGHTRAG_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or llm_provider.get("apiKey") or llm_provider.get("api_key") or ""
    llm_api_base = os.getenv("LIGHTRAG_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or llm_provider.get("apiBase") or llm_provider.get("api_base") or ""
    embedding_api_key = os.getenv("LIGHTRAG_EMBEDDING_API_KEY") or embed_provider.get("apiKey") or embed_provider.get("api_key") or llm_api_key
    embedding_api_base = os.getenv("LIGHTRAG_EMBEDDING_BASE_URL") or embed_provider.get("apiBase") or embed_provider.get("api_base") or llm_api_base

    return {
        "llm_model": str(llm_model),
        "embedding_model": str(embedding_model),
        "embedding_dim": embedding_dim,
        "embedding_max_tokens": embedding_max_tokens,
        "llm_api_key": str(llm_api_key),
        "llm_api_base": str(llm_api_base),
        "embedding_api_key": str(embedding_api_key),
        "embedding_api_base": str(embedding_api_base),
        "llm_provider": str(llm_provider_name),
        "embedding_provider": str(embed_provider_name),
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def sha256_text(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def doc_id_for(path: Path, content_hash: str) -> str:
    stable = hashlib.sha1(str(path.as_posix()).encode("utf-8")).hexdigest()[:16]
    return f"paper-{stable}-{content_hash[:16]}"


def title_from_markdown(path: Path) -> str:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            return text.lstrip("#").strip()[:240]
        if len(text) > 20:
            return text[:240]
    return path.stem


def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lightrag_docs (
            id INTEGER PRIMARY KEY,
            md_path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            lightrag_doc_id TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS visual_captions (
            id INTEGER PRIMARY KEY,
            image_path TEXT NOT NULL,
            image_sha256 TEXT NOT NULL,
            model TEXT NOT NULL,
            caption TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(image_sha256, model)
        );
        """
    )
    conn.commit()
    return conn


async def initialize_rag(root: Path):
    try:
        from lightrag import LightRAG, QueryParam  # noqa: F401
        from lightrag.llm.openai import openai_complete_if_cache, openai_embed
        from lightrag.utils import EmbeddingFunc
    except ModuleNotFoundError as exc:
        raise RuntimeError("LightRAG is not installed. Install it with: python -m pip install lightrag-hku") from exc

    cfg = resolve_lightrag_config(root)
    if not cfg["llm_api_key"]:
        raise RuntimeError(
            "OpenAI-compatible LLM API key is not set. Put OPENAI_API_KEY/LIGHTRAG_LLM_API_KEY in the environment/.env "
            "or configure providers.<provider>.apiKey in .nanobot/config.json"
        )
    if not cfg["embedding_api_key"]:
        raise RuntimeError(
            "OpenAI-compatible embedding API key is not set. Configure providers.<embedProvider>.apiKey "
            "or set LIGHTRAG_EMBEDDING_API_KEY."
        )

    async def visual_model_complete(
        prompt,
        system_prompt=None,
        history_messages=None,
        enable_cot: bool = False,
        keyword_extraction=False,
        **kwargs,
    ) -> str:
        if history_messages is None:
            history_messages = []
        return await openai_complete_if_cache(
            str(cfg["llm_model"]),
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages,
            enable_cot=enable_cot,
            keyword_extraction=keyword_extraction,
            base_url=str(cfg["llm_api_base"]) or None,
            api_key=str(cfg["llm_api_key"]),
            **kwargs,
        )

    async def configured_embed(texts: list[str], **kwargs):
        return await openai_embed.func(
            texts,
            model=str(cfg["embedding_model"]),
            base_url=str(cfg["embedding_api_base"]) or None,
            api_key=str(cfg["embedding_api_key"]),
            **kwargs,
        )

    embedding_func = EmbeddingFunc(
        embedding_dim=int(cfg["embedding_dim"]),
        func=configured_embed,
        max_token_size=int(cfg["embedding_max_tokens"]),
        model_name=str(cfg["embedding_model"]),
        supports_asymmetric=True,
    )

    workdir = root / "lightrag"
    workdir.mkdir(parents=True, exist_ok=True)
    print(
        f"LightRAG LLM model={cfg['llm_model']} provider={cfg['llm_provider']} "
        f"embedding_model={cfg['embedding_model']} embedding_provider={cfg['embedding_provider']}",
        flush=True,
    )
    rag = LightRAG(
        working_dir=str(workdir),
        embedding_func=embedding_func,
        llm_model_func=visual_model_complete,
        llm_model_name=str(cfg["llm_model"]),
        addon_params={"language": "Simplified Chinese"},
    )
    await rag.initialize_storages()
    return rag


def markdown_documents(root: Path) -> list[Path]:
    paths: list[Path] = []
    for dirname in ("md", "code_md"):
        directory = root / dirname
        paths.extend(path for path in directory.glob("*.md") if path.is_file() and path.stat().st_size > 0)
    return sorted(paths)


def markdown_image_refs(markdown: str) -> list[str]:
    refs: list[str] = []
    for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
        ref = match.group(1).strip().strip('"').strip("'")
        if ref and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", ref):
            refs.append(ref)
    return refs


def resolve_image_path(md_path: Path, ref: str) -> Path | None:
    ref = ref.split("#", 1)[0].split("?", 1)[0]
    candidate = (md_path.parent / ref).resolve()
    try:
        candidate.relative_to(md_path.parent.resolve())
    except ValueError:
        return None
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def caption_image(path: Path, cfg: dict[str, str | int], title: str) -> str:
    from openai import AsyncOpenAI

    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    client = AsyncOpenAI(api_key=str(cfg["llm_api_key"]), base_url=str(cfg["llm_api_base"]) or None)
    prompt = (
        "You are preparing a research-paper image for RAG retrieval. "
        "Describe the figure/table/diagram concisely in Chinese, preserving technical terms, "
        "axes, labels, algorithms, metrics, and any visible comparative results. "
        f"Paper title/context: {title}"
    )
    response = await client.chat.completions.create(
        model=str(cfg["llm_model"]),
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ],
            }
        ],
        temperature=0.1,
        max_tokens=500,
    )
    return (response.choices[0].message.content or "").strip()


async def caption_markdown_images(conn: sqlite3.Connection, md_path: Path, markdown: str, title: str, cfg: dict[str, str | int]) -> list[tuple[str, str]]:
    if os.getenv("LIGHTRAG_SKIP_VISUAL_CAPTIONS", "").lower() in {"1", "true", "yes", "on"}:
        return []
    refs = markdown_image_refs(markdown)
    if not refs:
        return []
    max_images = int(os.getenv("LIGHTRAG_VISUAL_CAPTION_MAX_IMAGES_PER_DOC", "0"))
    if max_images > 0:
        refs = refs[:max_images]

    captions: list[tuple[str, str]] = []
    for ref in refs:
        image_path = resolve_image_path(md_path, ref)
        if image_path is None:
            continue
        image_hash = sha256_file(image_path)
        row = conn.execute(
            "SELECT caption FROM visual_captions WHERE image_sha256=? AND model=?",
            (image_hash, str(cfg["llm_model"])),
        ).fetchone()
        if row:
            captions.append((ref, row["caption"]))
            continue
        try:
            caption = await caption_image(image_path, cfg, title)
        except Exception as exc:
            print(f"visual caption failed {ref}: {exc}", flush=True)
            continue
        if not caption:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO visual_captions (image_path, image_sha256, model, caption, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(image_path), image_hash, str(cfg["llm_model"]), caption, now_iso()),
        )
        conn.commit()
        captions.append((ref, caption))
        print(f"captioned {ref}", flush=True)
    return captions


async def markdown_for_lightrag(conn: sqlite3.Connection, md_path: Path, cfg: dict[str, str | int]) -> tuple[str, str]:
    title = title_from_markdown(md_path)
    text = md_path.read_text(encoding="utf-8", errors="replace")
    captions = await caption_markdown_images(conn, md_path, text, title, cfg)
    if captions:
        lines = ["", "## Visual Asset Descriptions"]
        for ref, caption in captions:
            lines.append(f"- `{ref}`: {caption}")
        text += "\n".join(lines) + "\n"
    return title, text


async def sync_lightrag(root: Path, force: bool) -> None:
    load_env(root / ".env")
    conn = connect(root)
    documents = markdown_documents(root)
    if not documents:
        print(f"no Markdown files found in {root / 'md'}")
        return
    rag = await initialize_rag(root)
    inserted = 0
    skipped = 0
    try:
        for path in documents:
            content_hash = sha256_text(path)
            row = conn.execute("SELECT * FROM lightrag_docs WHERE md_path=?", (str(path),)).fetchone()
            if row and row["content_sha256"] == content_hash and not force:
                skipped += 1
                print(f"skip indexed {path.name}")
                continue

            cfg = resolve_lightrag_config(root)
            title, text = await markdown_for_lightrag(conn, path, cfg)
            doc_id = doc_id_for(path, content_hash)
            await rag.ainsert(text, ids=[doc_id], file_paths=[str(path)])
            conn.execute(
                """
                INSERT INTO lightrag_docs (md_path, title, content_sha256, lightrag_doc_id, indexed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(md_path) DO UPDATE SET
                    title=excluded.title,
                    content_sha256=excluded.content_sha256,
                    lightrag_doc_id=excluded.lightrag_doc_id,
                    indexed_at=excluded.indexed_at
                """,
                (str(path), title, content_hash, doc_id, now_iso()),
            )
            conn.commit()
            inserted += 1
            print(f"indexed-lightrag {path.name}")
    finally:
        await rag.finalize_storages()

    print(f"lightrag sync complete inserted={inserted} skipped={skipped} dir={root / 'lightrag'}")


async def query_lightrag(root: Path, question: str, mode: str, only_context: bool) -> None:
    load_env(root / ".env")
    from lightrag import QueryParam

    rag = await initialize_rag(root)
    try:
        result = await rag.aquery(
            question,
            param=QueryParam(
                mode=mode,
                only_need_context=only_context,
                response_type="Multiple Paragraphs",
            ),
        )
        print(result)
    finally:
        await rag.finalize_storages()


def status(root: Path) -> None:
    conn = connect(root)
    rows = conn.execute("SELECT COUNT(*) FROM lightrag_docs").fetchone()[0]
    print(f"lightrag_dir={root / 'lightrag'}")
    print(f"indexed_markdown={rows}")
    for row in conn.execute("SELECT title, md_path, indexed_at FROM lightrag_docs ORDER BY indexed_at DESC LIMIT 20"):
        print(f"- {row['title']} | {row['md_path']} | {row['indexed_at']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Index and query article Markdown with LightRAG.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Insert new/changed Markdown files into LightRAG.")
    sync.add_argument("--force", action="store_true", help="Reinsert Markdown even when the content hash is unchanged.")

    query = sub.add_parser("query", help="Ask a question against the LightRAG index.")
    query.add_argument("question")
    query.add_argument("--mode", choices=["local", "global", "hybrid", "naive", "mix"], default="hybrid")
    query.add_argument("--context", action="store_true", help="Return retrieved context only.")

    sub.add_parser("status", help="Show LightRAG indexing status.")

    args = parser.parse_args()
    try:
        if args.command == "sync":
            asyncio.run(sync_lightrag(args.root, args.force))
            return 0
        if args.command == "query":
            asyncio.run(query_lightrag(args.root, args.question, args.mode, args.context))
            return 0
        if args.command == "status":
            status(args.root)
            return 0
    except Exception as exc:
        print(f"lightrag error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
