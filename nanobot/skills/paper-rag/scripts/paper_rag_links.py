from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

RELATION_TYPES = (
    "official_repo",
    "implements_method",
    "eval_script",
    "config",
    "dataset_pipeline",
)
MATCH_METHODS = ("official_repo", "embedding", "keyword", "llm")

ACRONYM_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*\b|\b[A-Z]{2,}\b")
CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)")
DEF_RE = re.compile(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)")
CONFIG_RE = re.compile(r"[`'\"]?([\w./-]+\.(?:yaml|yml|json|toml|cfg|ini|py))[`'\"]?", re.I)
DATASET_RE = re.compile(
    r"\b(MOT17|MOT20|DanceTrack|COCO|KITTI|BDD100K|Waymo|nuScenes|ImageNet|DAVIS|YouTube-?VIS)\b",
    re.I,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def link_id(*parts: str | None) -> str:
    payload = "\0".join(part or "" for part in parts)
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_links_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_rag_links (
            link_id TEXT PRIMARY KEY,
            paper_source_path TEXT,
            paper_chunk_id TEXT,
            code_source_path TEXT,
            code_chunk_id TEXT,
            relation_type TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence TEXT,
            match_method TEXT NOT NULL DEFAULT 'unknown',
            created_at TEXT NOT NULL
        );
        """
    )
    _ensure_column(conn, "paper_rag_links", "match_method", "TEXT NOT NULL DEFAULT 'unknown'")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_rag_links_paper_chunk ON paper_rag_links(paper_chunk_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_rag_links_code_chunk ON paper_rag_links(code_chunk_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_rag_links_paper_source ON paper_rag_links(paper_source_path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_rag_links_code_source ON paper_rag_links(code_source_path)"
    )


def normalize_title(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def find_paper_source(conn: sqlite3.Connection, paper_title: str | None) -> str | None:
    if not paper_title:
        return None
    target = normalize_title(paper_title)
    if not target:
        return None
    best_path: str | None = None
    best_score = 0.0
    for row in conn.execute(
        "SELECT source_path, title FROM paper_rag_sources WHERE source_type='paper_md'"
    ).fetchall():
        title = row["title"] or Path(row["source_path"]).stem
        candidate = normalize_title(title)
        if not candidate:
            continue
        if target == candidate or target in candidate or candidate in target:
            return row["source_path"]
        overlap = len(set(target.split()) & set(candidate.split()))
        score = overlap / max(len(set(target.split())), 1)
        if score > best_score:
            best_score = score
            best_path = row["source_path"]
    if best_score >= 0.45:
        return best_path
    slug = re.sub(r"[^a-z0-9]+", "", target)
    for row in conn.execute(
        "SELECT source_path FROM paper_rag_sources WHERE source_type='paper_md'"
    ).fetchall():
        stem = re.sub(r"[^a-z0-9]+", "", Path(row["source_path"]).stem.lower())
        if slug and (slug in stem or stem in slug):
            return row["source_path"]
    return best_path


def extract_symbols(text: str) -> set[str]:
    symbols: set[str] = set()
    for match in ACRONYM_RE.findall(text):
        if len(match) >= 2 and match not in {"PDF", "URL", "RGB", "GPU", "CPU", "API"}:
            symbols.add(match)
    for pattern in (CLASS_RE, DEF_RE):
        symbols.update(pattern.findall(text))
    symbols.update(m.group(1) if m.lastindex else m.group(0) for m in CONFIG_RE.finditer(text))
    symbols.update(m.group(0) for m in DATASET_RE.finditer(text))
    return {s for s in symbols if len(s) >= 2}


def delete_links_for_pair(
    conn: sqlite3.Connection,
    paper_source: str,
    code_source: str,
    *,
    chunk_level_only: bool = False,
) -> None:
    if chunk_level_only:
        conn.execute(
            """
            DELETE FROM paper_rag_links
            WHERE paper_source_path=? AND code_source_path=?
              AND paper_chunk_id IS NOT NULL AND code_chunk_id IS NOT NULL
            """,
            (paper_source, code_source),
        )
    else:
        conn.execute(
            "DELETE FROM paper_rag_links WHERE paper_source_path=? AND code_source_path=?",
            (paper_source, code_source),
        )


def upsert_link(
    conn: sqlite3.Connection,
    *,
    paper_source_path: str | None,
    paper_chunk_id: str | None,
    code_source_path: str | None,
    code_chunk_id: str | None,
    relation_type: str,
    confidence: float,
    evidence: str,
    match_method: str,
) -> str:
    lid = link_id(paper_source_path, paper_chunk_id, code_source_path, code_chunk_id, relation_type)
    conn.execute(
        """
        INSERT OR REPLACE INTO paper_rag_links
        (link_id, paper_source_path, paper_chunk_id, code_source_path, code_chunk_id,
         relation_type, confidence, evidence, match_method, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lid,
            paper_source_path,
            paper_chunk_id,
            code_source_path,
            code_chunk_id,
            relation_type,
            confidence,
            evidence[:2000] if evidence else None,
            match_method,
            now_iso(),
        ),
    )
    return lid


def ensure_official_repo_links(conn: sqlite3.Connection) -> int:
    count = 0
    try:
        rows = conn.execute(
            "SELECT paper_title, rag_md_path FROM github_sources WHERE status='ready' AND rag_md_path IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return 0
    for row in rows:
        code_path = row["rag_md_path"]
        if not code_path or not Path(code_path).exists():
            continue
        paper_path = find_paper_source(conn, row["paper_title"])
        if not paper_path:
            continue
        existing = conn.execute(
            """
            SELECT 1 FROM paper_rag_links
            WHERE paper_source_path=? AND code_source_path=? AND relation_type='official_repo'
              AND paper_chunk_id IS NULL AND code_chunk_id IS NULL
            """,
            (paper_path, code_path),
        ).fetchone()
        if existing:
            continue
        upsert_link(
            conn,
            paper_source_path=paper_path,
            paper_chunk_id=None,
            code_source_path=code_path,
            code_chunk_id=None,
            relation_type="official_repo",
            confidence=1.0,
            evidence=f"Registered official repo for paper: {row['paper_title'] or paper_path}",
            match_method="official_repo",
        )
        count += 1
    conn.commit()
    return count


def load_chunk_pairs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    rows = conn.execute(
        """
        SELECT DISTINCT paper_source_path, code_source_path
        FROM paper_rag_links
        WHERE relation_type='official_repo'
          AND paper_source_path IS NOT NULL AND code_source_path IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        pairs.append((row["paper_source_path"], row["code_source_path"]))
    return pairs


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def embedding_candidates(
    paper_chunks: list[sqlite3.Row],
    code_chunks: list[sqlite3.Row],
    *,
    top_k: int,
    threshold: float,
) -> list[tuple[sqlite3.Row, sqlite3.Row, float, str]]:
    candidates: list[tuple[sqlite3.Row, sqlite3.Row, float, str]] = []
    code_with_emb = []
    for code in code_chunks:
        if not code["embedding_json"]:
            continue
        try:
            code_with_emb.append((code, json.loads(code["embedding_json"])))
        except Exception:
            continue
    for paper in paper_chunks:
        if not paper["embedding_json"]:
            continue
        try:
            pvec = json.loads(paper["embedding_json"])
        except Exception:
            continue
        scored: list[tuple[sqlite3.Row, float]] = []
        for code, cvec in code_with_emb:
            score = cosine(pvec, cvec)
            if score >= threshold:
                scored.append((code, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        for code, score in scored[:top_k]:
            candidates.append((paper, code, score, "embedding"))
    return candidates


def keyword_candidates(
    paper_chunks: list[sqlite3.Row],
    code_chunks: list[sqlite3.Row],
) -> list[tuple[sqlite3.Row, sqlite3.Row, float, str]]:
    code_symbols = {row["chunk_id"]: extract_symbols(row["content"] + " " + (row["heading"] or "")) for row in code_chunks}
    candidates: list[tuple[sqlite3.Row, sqlite3.Row, float, str]] = []
    seen: set[tuple[str, str]] = set()
    for paper in paper_chunks:
        paper_text = paper["content"] + " " + (paper["heading"] or "")
        symbols = extract_symbols(paper_text)
        if not symbols:
            continue
        for code in code_chunks:
            overlap = symbols & code_symbols.get(code["chunk_id"], set())
            if not overlap:
                continue
            key = (paper["chunk_id"], code["chunk_id"])
            if key in seen:
                continue
            seen.add(key)
            score = min(0.95, 0.45 + 0.08 * len(overlap))
            evidence = ", ".join(sorted(overlap)[:8])
            candidates.append((paper, code, score, f"keyword:{evidence}"))
    return candidates


def merge_candidates(
    *groups: list[tuple[sqlite3.Row, sqlite3.Row, float, str]],
) -> dict[tuple[str, str], tuple[sqlite3.Row, sqlite3.Row, float, str]]:
    merged: dict[tuple[str, str], tuple[sqlite3.Row, sqlite3.Row, float, str]] = {}
    for group in groups:
        for paper, code, score, hint in group:
            key = (paper["chunk_id"], code["chunk_id"])
            current = merged.get(key)
            if current is None or score > current[2]:
                merged[key] = (paper, code, score, hint)
            elif current and hint.startswith("keyword:") and not current[3].startswith("keyword:"):
                merged[key] = (paper, code, max(score, current[2]), f"{current[3]};{hint}")
    return merged


def llm_classify_batch(
    root: Path,
    cfg: dict,
    pairs: list[tuple[sqlite3.Row, sqlite3.Row, float, str]],
    chat_complete: Callable[..., str],
) -> list[dict]:
    if not pairs:
        return []
    blocks = []
    for index, (paper, code, embed_score, hint) in enumerate(pairs, start=1):
        paper_excerpt = (paper["heading"] or "") + "\n" + paper["content"][:1200]
        code_excerpt = (code["heading"] or "") + "\n" + code["content"][:1200]
        blocks.append(
            f"Pair {index}:\n"
            f"embedding_score={embed_score:.3f} hint={hint}\n"
            f"PAPER:\n{paper_excerpt}\n"
            f"CODE:\n{code_excerpt}\n"
        )
    prompt = (
        "You align paper excerpts with official repository code excerpts.\n"
        "For each pair, decide if the code chunk implements, configures, evaluates, or pipelines data for the paper chunk.\n"
        "Return JSON array with one object per pair in order. Each object keys:\n"
        "linked (bool), relation_type (implements_method|eval_script|config|dataset_pipeline|none), "
        "confidence (0-1 float), evidence (short string, max 120 chars).\n"
        "Be conservative: linked=true only when there is a concrete semantic match (module name, method, config, dataset, metric table, command).\n\n"
        + "\n---\n".join(blocks)
    )
    text = chat_complete(root, cfg, [{"role": "user", "content": prompt}], max_tokens=1800, temperature=0.0)
    match = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def build_chunk_links_for_pair(
    conn: sqlite3.Connection,
    root: Path,
    cfg: dict,
    paper_source: str,
    code_source: str,
    *,
    embed_top_k: int,
    embed_threshold: float,
    llm_threshold: float,
    max_llm_pairs: int,
    skip_llm: bool,
    chat_complete: Callable[..., str],
    embed_texts: Callable[..., list[list[float]]] | None,
    trace: Callable[..., None] | None,
) -> int:
    paper_chunks = conn.execute(
        "SELECT * FROM paper_rag_chunks WHERE source_path=? AND source_type='paper_md' ORDER BY chunk_index",
        (paper_source,),
    ).fetchall()
    code_chunks = conn.execute(
        "SELECT * FROM paper_rag_chunks WHERE source_path=? AND source_type='code_md' ORDER BY chunk_index",
        (code_source,),
    ).fetchall()
    if not paper_chunks or not code_chunks:
        return 0

    delete_links_for_pair(conn, paper_source, code_source, chunk_level_only=True)

    embed_cands = embedding_candidates(
        paper_chunks, code_chunks, top_k=embed_top_k, threshold=embed_threshold
    )
    keyword_cands = keyword_candidates(paper_chunks, code_chunks)
    merged = merge_candidates(embed_cands, keyword_cands)
    if not merged:
        conn.commit()
        return 0

    ranked = sorted(merged.values(), key=lambda item: item[2], reverse=True)
    to_verify = ranked[:max_llm_pairs]

    stored = 0
    if skip_llm or not cfg.get("llm_api_key"):
        for paper, code, score, hint in ranked:
            if score < embed_threshold and not hint.startswith("keyword:"):
                continue
            relation = infer_relation_type(paper, code, hint)
            upsert_link(
                conn,
                paper_source_path=paper_source,
                paper_chunk_id=paper["chunk_id"],
                code_source_path=code_source,
                code_chunk_id=code["chunk_id"],
                relation_type=relation,
                confidence=score,
                evidence=hint,
                match_method="embedding" if not hint.startswith("keyword:") else "keyword",
            )
            stored += 1
        conn.commit()
        return stored

    if trace:
        trace(root, "link.llm.start", paper=paper_source, code=code_source, pairs=len(to_verify))
    llm_results = llm_classify_batch(root, cfg, to_verify, chat_complete)
    if trace:
        trace(root, "link.llm.end", pairs=len(llm_results))

    for index, (paper, code, embed_score, hint) in enumerate(to_verify):
        result = llm_results[index] if index < len(llm_results) else {}
        if not isinstance(result, dict):
            continue
        if not result.get("linked"):
            continue
        confidence = float(result.get("confidence") or 0.0)
        if confidence < llm_threshold:
            continue
        relation = result.get("relation_type") or "implements_method"
        if relation not in RELATION_TYPES or relation == "official_repo":
            relation = infer_relation_type(paper, code, hint)
        evidence = str(result.get("evidence") or hint)[:500]
        upsert_link(
            conn,
            paper_source_path=paper_source,
            paper_chunk_id=paper["chunk_id"],
            code_source_path=code_source,
            code_chunk_id=code["chunk_id"],
            relation_type=relation,
            confidence=confidence,
            evidence=evidence,
            match_method="llm",
        )
        stored += 1

    for paper, code, score, hint in ranked[max_llm_pairs:]:
        if score < max(embed_threshold + 0.08, 0.62) and not hint.startswith("keyword:"):
            continue
        if hint.startswith("keyword:") and score >= 0.55:
            relation = infer_relation_type(paper, code, hint)
            upsert_link(
                conn,
                paper_source_path=paper_source,
                paper_chunk_id=paper["chunk_id"],
                code_source_path=code_source,
                code_chunk_id=code["chunk_id"],
                relation_type=relation,
                confidence=score,
                evidence=hint,
                match_method="keyword",
            )
            stored += 1
    conn.commit()
    return stored


def infer_relation_type(paper: sqlite3.Row, code: sqlite3.Row, hint: str) -> str:
    combined = f"{paper['heading']} {paper['content'][:800]} {code['heading']} {code['content'][:800]}".lower()
    code_text = code["content"].lower()
    if any(term in combined for term in ("dataset", "mot17", "mot20", "dancetrack", "benchmark")):
        if "data" in code_text or "dataset" in code_text:
            return "dataset_pipeline"
    if any(term in combined for term in ("config", ".yaml", ".yml", ".json", "hyperparameter")):
        return "config"
    if any(term in combined for term in ("eval", "test", "metric", "benchmark", "hota", "mota")):
        if any(term in code_text for term in ("eval", "test", "metric")):
            return "eval_script"
    if hint.startswith("keyword:"):
        return "implements_method"
    return "implements_method"


def sync_chunk_links(
    conn: sqlite3.Connection,
    root: Path,
    cfg: dict,
    *,
    force: bool,
    skip_llm: bool,
    chat_complete: Callable[..., str],
    embed_texts: Callable[..., list[list[float]]] | None,
    trace: Callable[..., None] | None,
) -> int:
    ensure_official_repo_links(conn)
    pairs = load_chunk_pairs(conn)
    if not pairs:
        return 0

    embed_top_k = int(os.getenv("PAPER_RAG_LINK_EMBED_TOP_K", "4"))
    embed_threshold = float(os.getenv("PAPER_RAG_LINK_EMBED_THRESHOLD", "0.50"))
    llm_threshold = float(os.getenv("PAPER_RAG_LINK_LLM_THRESHOLD", "0.55"))
    max_llm_pairs = int(os.getenv("PAPER_RAG_LINK_MAX_LLM_PAIRS", "40"))

    total = 0
    start = time.perf_counter()
    if trace:
        trace(root, "link.start", pairs=len(pairs), skip_llm=skip_llm)
    for paper_source, code_source in pairs:
        if force:
            delete_links_for_pair(conn, paper_source, code_source, chunk_level_only=True)
        count = build_chunk_links_for_pair(
            conn,
            root,
            cfg,
            paper_source,
            code_source,
            embed_top_k=embed_top_k,
            embed_threshold=embed_threshold,
            llm_threshold=llm_threshold,
            max_llm_pairs=max_llm_pairs,
            skip_llm=skip_llm,
            chat_complete=chat_complete,
            embed_texts=embed_texts,
            trace=trace,
        )
        total += count
        print(f"linked {Path(paper_source).name} <-> {Path(code_source).name}: {count} chunk pairs")
    if trace:
        trace(root, "link.end", count=total, duration_ms=int((time.perf_counter() - start) * 1000))
    return total


def fetch_linked_chunks(
    conn: sqlite3.Connection,
    chunk_ids: set[str],
    *,
    min_confidence: float = 0.5,
) -> list[sqlite3.Row]:
    if not chunk_ids:
        return []
    placeholders = ",".join("?" for _ in chunk_ids)
    ids = list(chunk_ids)
    try:
        paper_to_code = conn.execute(
            f"""
            SELECT c.*, l.relation_type, l.confidence AS link_confidence, l.evidence AS link_evidence
            FROM paper_rag_links l
            JOIN paper_rag_chunks c ON c.chunk_id = l.code_chunk_id
            WHERE l.paper_chunk_id IN ({placeholders})
              AND l.code_chunk_id IS NOT NULL
              AND l.confidence >= ?
            """,
            ids + [min_confidence],
        ).fetchall()
        code_to_paper = conn.execute(
            f"""
            SELECT c.*, l.relation_type, l.confidence AS link_confidence, l.evidence AS link_evidence
            FROM paper_rag_links l
            JOIN paper_rag_chunks c ON c.chunk_id = l.paper_chunk_id
            WHERE l.code_chunk_id IN ({placeholders})
              AND l.paper_chunk_id IS NOT NULL
              AND l.confidence >= ?
            """,
            ids + [min_confidence],
        ).fetchall()
        seen: set[str] = set()
        merged: list[sqlite3.Row] = []
        for row in paper_to_code + code_to_paper:
            if row["chunk_id"] in seen:
                continue
            seen.add(row["chunk_id"])
            merged.append(row)
        return merged
    except sqlite3.Error:
        return []


def link_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT relation_type, match_method, COUNT(*) count
        FROM paper_rag_links
        GROUP BY relation_type, match_method
        ORDER BY count DESC
        """
    ).fetchall()
    doc = conn.execute(
        "SELECT COUNT(*) count FROM paper_rag_links WHERE paper_chunk_id IS NULL AND code_chunk_id IS NULL"
    ).fetchone()["count"]
    chunk = conn.execute(
        "SELECT COUNT(*) count FROM paper_rag_links WHERE paper_chunk_id IS NOT NULL AND code_chunk_id IS NOT NULL"
    ).fetchone()["count"]
    return {"document_links": doc, "chunk_links": chunk, "breakdown": [dict(row) for row in rows]}
