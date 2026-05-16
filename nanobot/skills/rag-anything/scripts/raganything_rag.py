from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_ROOT = Path(".nanobot/workspace/articles")
DB_NAME = "articles.sqlite3"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def trace_enabled() -> bool:
    return os.getenv("RAGANYTHING_TRACE", "1").lower() not in {"0", "false", "no"}


def safe_host(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(str(url))
    return parsed.netloc or str(url).split("/", 1)[0]


def text_size(value) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return len(str(value))


def trace_event(root: Path, event: str, **fields) -> None:
    if not trace_enabled():
        return
    safe_fields = {
        key: value
        for key, value in fields.items()
        if key.lower() not in {"api_key", "authorization", "token", "secret", "access_key", "secret_key"}
    }
    payload = {"ts": now_iso(), "event": event, **safe_fields}
    line = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / "raganything_trace.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception as exc:
        try:
            print(f"trace write failed: {exc}", flush=True)
        except Exception:
            pass
    try:
        summary_keys = (
            "request_id",
            "provider",
            "model",
            "base_host",
            "source_type",
            "path",
            "duration_ms",
            "error_type",
            "text_count",
            "total_chars",
            "image_count",
        )
        summary = {key: safe_fields[key] for key in summary_keys if key in safe_fields}
        line = f"trace {event}: {json.dumps(summary, ensure_ascii=True, default=str)}"
        if len(line) > 1000:
            line = line[:1000] + "...<truncated>"
        print(line, flush=True)
    except Exception:
        pass


def find_nanobot_config(root: Path) -> Path | None:
    for candidate in (root.parent.parent / "config.json", Path(".nanobot/config.json"), Path("config.json")):
        if candidate.exists():
            return candidate
    return None


def load_json(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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


def resolve_config(root: Path) -> dict[str, str | int]:
    config = load_json(find_nanobot_config(root))
    defaults = config.get("agents", {}).get("defaults", {})
    providers = config.get("providers", {})
    llm_provider_name = defaults.get("provider") or "openai"
    embed_provider_name = defaults.get("embedProvider") or defaults.get("embed_provider") or llm_provider_name
    llm_provider = providers.get(llm_provider_name, {})
    embed_provider = providers.get(embed_provider_name, {})

    return {
        "llm_model": str(
            os.getenv("RAGANYTHING_LLM_MODEL")
            or os.getenv("LIGHTRAG_LLM_MODEL")
            or defaults.get("visual_model")
            or defaults.get("visualModel")
            or defaults.get("model")
            or "gpt-4o-mini"
        ),
        "llm_api_key": str(
            os.getenv("RAGANYTHING_LLM_API_KEY")
            or os.getenv("LIGHTRAG_LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or llm_provider.get("apiKey")
            or llm_provider.get("api_key")
            or ""
        ),
        "llm_api_base": str(
            os.getenv("RAGANYTHING_LLM_BASE_URL")
            or os.getenv("LIGHTRAG_LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or llm_provider.get("apiBase")
            or llm_provider.get("api_base")
            or ""
        ),
        "embedding_model": str(
            os.getenv("RAGANYTHING_EMBEDDING_MODEL")
            or os.getenv("LIGHTRAG_EMBEDDING_MODEL")
            or defaults.get("embed_model")
            or defaults.get("embedModel")
            or "text-embedding-3-small"
        ),
        "embedding_api_key": str(
            os.getenv("RAGANYTHING_EMBEDDING_API_KEY")
            or os.getenv("LIGHTRAG_EMBEDDING_API_KEY")
            or embed_provider.get("apiKey")
            or embed_provider.get("api_key")
            or ""
        ),
        "embedding_api_base": str(
            os.getenv("RAGANYTHING_EMBEDDING_BASE_URL")
            or os.getenv("LIGHTRAG_EMBEDDING_BASE_URL")
            or embed_provider.get("apiBase")
            or embed_provider.get("api_base")
            or ""
        ),
        "embedding_dim": int(os.getenv("RAGANYTHING_EMBEDDING_DIM") or os.getenv("LIGHTRAG_EMBEDDING_DIM") or "1536"),
        "embedding_max_tokens": int(
            os.getenv("RAGANYTHING_EMBEDDING_MAX_TOKENS") or os.getenv("LIGHTRAG_EMBEDDING_MAX_TOKENS") or "8192"
        ),
        "llm_provider": str(llm_provider_name),
        "embedding_provider": str(embed_provider_name),
    }


def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raganything_docs (
            id INTEGER PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            source_type TEXT NOT NULL,
            status TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def stable_doc_id(path: Path, content: str) -> str:
    digest = hashlib.sha256()
    digest.update(str(path.resolve()).encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(content.encode("utf-8", errors="replace"))
    return "doc-" + digest.hexdigest()[:32]


def markdown_image_items(markdown_path: Path, content: str) -> list[dict]:
    items: list[dict] = []
    pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
    for index, match in enumerate(pattern.finditer(content)):
        raw_target = match.group(2).strip().strip('"').strip("'")
        if not raw_target or "://" in raw_target or raw_target.startswith("data:"):
            continue
        image_path = (markdown_path.parent / raw_target).resolve()
        if not image_path.exists():
            continue
        items.append(
            {
                "type": "image",
                "img_path": str(image_path),
                "image_caption": match.group(1).strip(),
                "page_idx": 0,
                "index": index,
            }
        )
    return items


async def process_markdown_source_api(rag, path: Path, source_type: str) -> str:
    from raganything.utils import insert_text_content

    content = path.read_text(encoding="utf-8", errors="replace")
    doc_id = stable_doc_id(path, content)
    file_name = str(path)
    root = Path(rag.config.working_dir).parent
    trace_event(root, "source.process.start", source_type=source_type, path=str(path), chars=len(content), doc_id=doc_id)
    init_result = await rag._ensure_lightrag_initialized()
    if not init_result or not init_result.get("success"):
        raise RuntimeError(f"LightRAG initialization failed: {(init_result or {}).get('error', 'unknown error')}")

    print(f"source-api insert text: {path.name} chars={len(content)}", flush=True)
    start = time.perf_counter()
    trace_event(root, "source.text_insert.start", path=str(path), doc_id=doc_id, chars=len(content))
    await insert_text_content(rag.lightrag, input=content, file_paths=file_name, ids=doc_id)
    trace_event(root, "source.text_insert.end", path=str(path), doc_id=doc_id, duration_ms=int((time.perf_counter() - start) * 1000))

    multimodal_items = markdown_image_items(path, content)
    if multimodal_items and rag.config.enable_image_processing:
        if hasattr(rag, "set_content_source_for_context"):
            content_list = [{"type": "text", "text": content}, *multimodal_items]
            rag.set_content_source_for_context(content_list, rag.config.content_format)
        print(f"source-api process images with VLM: {path.name} images={len(multimodal_items)}", flush=True)
        start = time.perf_counter()
        trace_event(root, "source.multimodal.start", path=str(path), doc_id=doc_id, image_count=len(multimodal_items))
        await rag._process_multimodal_content(multimodal_items, file_name, doc_id)
        trace_event(root, "source.multimodal.end", path=str(path), doc_id=doc_id, duration_ms=int((time.perf_counter() - start) * 1000))
    else:
        await rag._mark_multimodal_processing_complete(doc_id)
        trace_event(root, "source.multimodal.skip", path=str(path), doc_id=doc_id, image_count=len(multimodal_items), enabled=rag.config.enable_image_processing)
    trace_event(root, "source.process.end", source_type=source_type, path=str(path), doc_id=doc_id)
    return doc_id


def mineru_api_pdf_to_markdown(root: Path, pdf_paths: list[Path], interval: int = 20, timeout: int = 3600) -> None:
    if not pdf_paths:
        return
    if len(pdf_paths) != 1:
        raise RuntimeError("MinerU PDF conversion is single-file only. Pass exactly one PDF.")
    os.environ.setdefault("RAGANYTHING_TRACE_FILE", str(root / "raganything_trace.jsonl"))
    try:
        from mineru_pdf_to_md import (
            create_batch,
            download_markdown,
            load_env as load_mineru_env,
            poll_results,
            token_candidates,
            upload_files,
        )
    except ModuleNotFoundError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from mineru_pdf_to_md import (
            create_batch,
            download_markdown,
            load_env as load_mineru_env,
            poll_results,
            token_candidates,
            upload_files,
        )

    env_path = root / ".env"
    if not env_path.exists():
        raise RuntimeError(f"Missing MinerU credentials file: {env_path}")
    env = load_mineru_env(env_path)
    errors: list[str] = []
    candidates = token_candidates(env)
    trace_event(root, "mineru.token_candidates", count=len(candidates), pdf_count=len(pdf_paths))
    for token_index, token in enumerate(candidates, start=1):
        try:
            start = time.perf_counter()
            trace_event(root, "mineru.create_batch.start", token_index=token_index, pdf_count=len(pdf_paths))
            batch_id, file_urls = create_batch(pdf_paths, token)
            trace_event(
                root,
                "mineru.create_batch.end",
                token_index=token_index,
                batch_id=batch_id,
                upload_url_count=len(file_urls),
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
            print(f"mineru api created batch {batch_id} for {len(pdf_paths)} PDF(s)", flush=True)
            start = time.perf_counter()
            trace_event(root, "mineru.upload.start", batch_id=batch_id, pdf_count=len(pdf_paths))
            upload_files(pdf_paths, file_urls)
            trace_event(root, "mineru.upload.end", batch_id=batch_id, duration_ms=int((time.perf_counter() - start) * 1000))
            start = time.perf_counter()
            trace_event(root, "mineru.poll.start", batch_id=batch_id, interval=interval, timeout=timeout)
            results = poll_results(batch_id, token, interval, timeout)
            trace_event(
                root,
                "mineru.poll.end",
                batch_id=batch_id,
                result_count=len(results),
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
            start = time.perf_counter()
            trace_event(root, "mineru.download.start", batch_id=batch_id, result_count=len(results))
            download_markdown(results, root / "md", pdf_paths[0].stem)
            trace_event(root, "mineru.download.end", batch_id=batch_id, duration_ms=int((time.perf_counter() - start) * 1000))
            return
        except Exception as exc:
            errors.append(str(exc))
            trace_event(root, "mineru.error", token_index=token_index, error_type=type(exc).__name__, error=str(exc)[:500])
            print(f"mineru api token candidate failed: {exc}", flush=True)
    raise RuntimeError("All MinerU API token candidates failed: " + " | ".join(errors))


async def build_raganything(root: Path):
    try:
        from lightrag.llm.openai import openai_complete_if_cache, openai_embed
        from lightrag.utils import EmbeddingFunc
        from raganything import RAGAnything, RAGAnythingConfig
    except ModuleNotFoundError as exc:
        raise RuntimeError("RAGAnything is not installed. Install it with: python -m pip install raganything") from exc

    cfg = resolve_config(root)
    if not cfg["llm_api_key"]:
        raise RuntimeError("Missing LLM API key for RAGAnything.")
    if not cfg["embedding_api_key"]:
        raise RuntimeError("Missing embedding API key for RAGAnything.")

    async def llm_complete(prompt, system_prompt=None, history_messages=None, enable_cot=False, keyword_extraction=False, **kwargs):
        request_id = hashlib.sha1(f"{time.time_ns()}:{id(prompt)}".encode()).hexdigest()[:12]
        start = time.perf_counter()
        trace_event(
            root,
            "llm.start",
            request_id=request_id,
            provider=cfg["llm_provider"],
            model=cfg["llm_model"],
            base_host=safe_host(str(cfg["llm_api_base"])),
            prompt_chars=text_size(prompt),
            system_chars=text_size(system_prompt),
            history_count=len(history_messages or []),
            enable_cot=enable_cot,
            keyword_extraction=keyword_extraction,
        )
        try:
            result = await openai_complete_if_cache(
                str(cfg["llm_model"]),
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages or [],
                enable_cot=enable_cot,
                keyword_extraction=keyword_extraction,
                base_url=str(cfg["llm_api_base"]) or None,
                api_key=str(cfg["llm_api_key"]),
                **kwargs,
            )
            trace_event(
                root,
                "llm.end",
                request_id=request_id,
                duration_ms=int((time.perf_counter() - start) * 1000),
                response_chars=text_size(result),
            )
            return result
        except Exception as exc:
            trace_event(
                root,
                "llm.error",
                request_id=request_id,
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_type=type(exc).__name__,
                error=str(exc)[:500],
            )
            raise

    async def vision_complete(prompt, image_data=None, system_prompt=None, history_messages=None, **kwargs):
        if not image_data:
            return await llm_complete(prompt, system_prompt=system_prompt, history_messages=history_messages, **kwargs)
        from openai import AsyncOpenAI

        request_id = hashlib.sha1(f"{time.time_ns()}:{id(prompt)}:vision".encode()).hexdigest()[:12]
        start = time.perf_counter()
        client = AsyncOpenAI(api_key=str(cfg["llm_api_key"]), base_url=str(cfg["llm_api_base"]) or None)
        image_url = image_data if str(image_data).startswith("data:") else f"data:image/jpeg;base64,{image_data}"
        trace_event(
            root,
            "vlm.start",
            request_id=request_id,
            provider=cfg["llm_provider"],
            model=cfg["llm_model"],
            base_host=safe_host(str(cfg["llm_api_base"])),
            prompt_chars=text_size(prompt),
            system_chars=text_size(system_prompt),
            history_count=len(history_messages or []),
            image_data_chars=text_size(image_data),
            max_tokens=700,
        )
        try:
            response = await client.chat.completions.create(
                model=str(cfg["llm_model"]),
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                temperature=0.1,
                max_tokens=700,
            )
            result = (response.choices[0].message.content or "").strip()
            trace_event(
                root,
                "vlm.end",
                request_id=request_id,
                duration_ms=int((time.perf_counter() - start) * 1000),
                response_chars=text_size(result),
            )
            return result
        except Exception as exc:
            trace_event(
                root,
                "vlm.error",
                request_id=request_id,
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_type=type(exc).__name__,
                error=str(exc)[:500],
            )
            raise

    async def embed(texts: list[str], **kwargs):
        request_id = hashlib.sha1(f"{time.time_ns()}:{len(texts)}:embed".encode()).hexdigest()[:12]
        start = time.perf_counter()
        total_chars = sum(text_size(text) for text in texts)
        trace_event(
            root,
            "embedding.start",
            request_id=request_id,
            provider=cfg["embedding_provider"],
            model=cfg["embedding_model"],
            base_host=safe_host(str(cfg["embedding_api_base"])),
            text_count=len(texts),
            total_chars=total_chars,
        )
        try:
            result = await openai_embed.func(
                texts,
                model=str(cfg["embedding_model"]),
                base_url=str(cfg["embedding_api_base"]) or None,
                api_key=str(cfg["embedding_api_key"]),
                **kwargs,
            )
            vector_count = len(result) if hasattr(result, "__len__") else None
            trace_event(
                root,
                "embedding.end",
                request_id=request_id,
                duration_ms=int((time.perf_counter() - start) * 1000),
                vector_count=vector_count,
            )
            return result
        except Exception as exc:
            trace_event(
                root,
                "embedding.error",
                request_id=request_id,
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_type=type(exc).__name__,
                error=str(exc)[:500],
            )
            raise

    embedding_func = EmbeddingFunc(
        embedding_dim=int(cfg["embedding_dim"]),
        func=embed,
        max_token_size=int(cfg["embedding_max_tokens"]),
        model_name=str(cfg["embedding_model"]),
        supports_asymmetric=True,
    )

    print(
        f"RAGAnything LLM={cfg['llm_model']}({cfg['llm_provider']}) "
        f"embedding={cfg['embedding_model']}({cfg['embedding_provider']})",
        flush=True,
    )
    config = RAGAnythingConfig(
        working_dir=str(root / "raganything"),
        parser_output_dir=str(root / "raganything_output"),
        parser="mineru",
        parse_method="auto",
        enable_image_processing=os.getenv("RAGANYTHING_ENABLE_IMAGE_PROCESSING", "1").lower() not in {"0", "false", "no"},
        enable_table_processing=True,
        enable_equation_processing=True,
        context_mode="page",
        context_window=1,
        max_context_tokens=2000,
        content_format="minerU",
    )
    return RAGAnything(
        config=config,
        llm_model_func=llm_complete,
        vision_model_func=vision_complete,
        embedding_func=embedding_func,
        lightrag_kwargs={
            "llm_model_name": str(cfg["llm_model"]),
            "embedding_func": embedding_func,
            "llm_model_func": llm_complete,
            "working_dir": str(root / "raganything"),
        },
    )


def resolve_pdf_path(root: Path, pdf: Path) -> Path:
    candidates = []
    if pdf.is_absolute():
        candidates.append(pdf)
    else:
        candidates.extend([pdf, root / "pdf" / pdf, root / "pdf" / f"{pdf}.pdf"])
    found = next((candidate for candidate in candidates if candidate.exists() and candidate.is_file()), None)
    if found is None:
        raise FileNotFoundError(f"PDF not found: {pdf}")
    if found.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF path: {found}")
    return found


def default_sources(root: Path, include_pdf: bool, pdf: Path | None) -> list[tuple[Path, str]]:
    sources: list[tuple[Path, str]] = []
    for directory, source_type in ((root / "md", "paper_md"), (root / "code_md", "code_md")):
        sources.extend((path, source_type) for path in sorted(directory.glob("*.md")) if path.stat().st_size > 0)
    if include_pdf:
        if pdf is None:
            raise RuntimeError("--include-pdf now requires --pdf <single-pdf>; bulk PDF processing is disabled.")
        pdf_path = resolve_pdf_path(root, pdf)
        if pdf_path.stat().st_size > 0:
            sources.append((pdf_path, "pdf"))
    return sources


async def sync(root: Path, include_pdf: bool, pdf: Path | None, force: bool, mineru_interval: int, mineru_timeout: int) -> None:
    load_env(root / ".env")
    trace_event(root, "sync.start", include_pdf=include_pdf, pdf=str(pdf) if pdf else None, force=force, mineru_interval=mineru_interval, mineru_timeout=mineru_timeout)
    conn = connect(root)
    if include_pdf:
        if pdf is None:
            raise RuntimeError("--include-pdf now requires --pdf <single-pdf>; bulk PDF processing is disabled.")
        pdf_path = resolve_pdf_path(root, pdf)
        md_path = root / "md" / f"{pdf_path.stem}.md"
        if not md_path.exists() or md_path.stat().st_size == 0:
            mineru_api_pdf_to_markdown(root, [pdf_path], interval=mineru_interval, timeout=mineru_timeout)
    sources = default_sources(root, include_pdf, pdf)
    trace_event(root, "sync.sources", count=len(sources), sources=[{"path": str(path), "type": source_type} for path, source_type in sources])
    if not sources:
        print("no sources found for RAGAnything")
        trace_event(root, "sync.end", inserted=0, skipped=0)
        return
    rag = await build_raganything(root)
    inserted = 0
    skipped = 0
    try:
        for path, source_type in sources:
            existing = conn.execute("SELECT status FROM raganything_docs WHERE source_path=?", (str(path),)).fetchone()
            if existing and existing["status"] == "ready" and not force:
                skipped += 1
                print(f"skip raganything {path.name}")
                continue
            print(f"raganything process {source_type}: {path}", flush=True)
            source_start = time.perf_counter()
            trace_event(root, "sync.source.start", path=str(path), source_type=source_type)
            try:
                if path.suffix.lower() == ".md":
                    await process_markdown_source_api(rag, path, source_type)
                elif path.suffix.lower() == ".pdf":
                    md_path = root / "md" / f"{path.stem}.md"
                    if not md_path.exists() or md_path.stat().st_size == 0:
                        mineru_api_pdf_to_markdown(root, [path], interval=mineru_interval, timeout=mineru_timeout)
                    await process_markdown_source_api(rag, md_path, "paper_md")
                else:
                    await rag.process_document_complete(str(path), output_dir=str(root / "raganything_output"))
                trace_event(root, "sync.source.end", path=str(path), source_type=source_type, duration_ms=int((time.perf_counter() - source_start) * 1000))
            except Exception as exc:
                trace_event(
                    root,
                    "sync.source.error",
                    path=str(path),
                    source_type=source_type,
                    duration_ms=int((time.perf_counter() - source_start) * 1000),
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                    traceback=traceback.format_exc(limit=12),
                )
                raise
            conn.execute(
                """
                INSERT INTO raganything_docs (source_path, source_type, status, indexed_at)
                VALUES (?, ?, 'ready', ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    source_type=excluded.source_type,
                    status='ready',
                    indexed_at=excluded.indexed_at
                """,
                (str(path), source_type, now_iso()),
            )
            conn.commit()
            inserted += 1
    finally:
        await rag.finalize_storages()
        trace_event(root, "sync.finalize")
    trace_event(root, "sync.end", inserted=inserted, skipped=skipped, rag_dir=str(root / "raganything"))
    print(f"raganything sync complete inserted={inserted} skipped={skipped} dir={root / 'raganything'}")


async def query(root: Path, question: str, mode: str) -> None:
    load_env(root / ".env")
    trace_event(root, "query.start", mode=mode, question_chars=len(question))
    rag = await build_raganything(root)
    try:
        start = time.perf_counter()
        result = await rag.aquery(question, mode=mode)
        trace_event(root, "query.end", mode=mode, duration_ms=int((time.perf_counter() - start) * 1000), response_chars=text_size(result))
        print(result)
    finally:
        await rag.finalize_storages()


def status(root: Path) -> None:
    conn = connect(root)
    rows = conn.execute("SELECT source_path, source_type, status, indexed_at FROM raganything_docs ORDER BY indexed_at DESC").fetchall()
    print(f"raganything_dir={root / 'raganything'}")
    print(f"trace_file={root / 'raganything_trace.jsonl'}")
    print(f"indexed_sources={len(rows)}")
    for row in rows[:30]:
        print(f"- {row['status']} {row['source_type']} {row['source_path']} {row['indexed_at']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Index and query article sources with RAGAnything.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    sync_cmd = sub.add_parser("sync")
    sync_cmd.add_argument("--include-pdf", action="store_true", help="Also process PDFs directly. Default uses existing md/code_md first.")
    sync_cmd.add_argument("--pdf", type=Path, help="Single PDF path, filename, or stem to process when --include-pdf is used.")
    sync_cmd.add_argument("--force", action="store_true")
    sync_cmd.add_argument("--mineru-interval", type=int, default=20)
    sync_cmd.add_argument("--mineru-timeout", type=int, default=3600)
    query_cmd = sub.add_parser("query")
    query_cmd.add_argument("question")
    query_cmd.add_argument("--mode", default="mix", choices=["local", "global", "hybrid", "naive", "mix"])
    sub.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "sync":
            asyncio.run(sync(args.root, args.include_pdf, args.pdf, args.force, args.mineru_interval, args.mineru_timeout))
            return 0
        if args.command == "query":
            asyncio.run(query(args.root, args.question, args.mode))
            return 0
        if args.command == "status":
            status(args.root)
            return 0
    except Exception as exc:
        print(f"raganything error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
