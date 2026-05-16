---
name: paper-rag
description: Build and operate a lightweight paper-and-code RAG for research article workspaces. Use when Codex needs fast paper Markdown and official GitHub code indexing without default VLM processing, optional per-paper enrichment cards, on-demand figure/image VLM analysis, embedding/lexical retrieval, query answering, MinerU PDF-to-Markdown conversion, or trace-based diagnosis of slow RAG calls.
---

# Paper RAG

Use this skill instead of full multimodal RAGAnything when the task is paper/code understanding and VLM processing is too slow.

The design is intentionally lightweight:

- `fast`: default. Index paper Markdown and official code Markdown with embeddings. No VLM. No graph extraction.
- `enrich`: add one LLM-generated paper card per paper. Still no VLM.
- `vision`: call VLM only on selected figures/images, cache the result, and reuse it later.

## Scripts

Canonical scripts:

- `nanobot/skills/paper-rag/scripts/paper_rag.py`
- `nanobot/skills/paper-rag/scripts/workspace_paths.py`
- `nanobot/skills/paper-rag/scripts/paper_rag_links.py`
- `nanobot/skills/paper-rag/scripts/mineru_pdf_to_md.py`
- `nanobot/skills/paper-rag/scripts/github_sources.py`

The script stores data in the workspace `articles.sqlite3` and writes trace to `paper_rag_trace.jsonl`.

## Workspace

Input and output are split; **each book gets its own folder** under `rag/`:

```text
.nanobot/workspace/articles/
  pdf/                    # input: PDFs (one per book, or pass --pdf)
  .env                    # optional credentials
  rag/
    TrackTrack/           # one folder per book (name = PDF stem or --book)
      md/
      md/images/
      code/
      code_md/
      articles.sqlite3
      paper_rag_trace.jsonl
    HybridSORT/
      ...
```

`--root` defaults to `.nanobot/workspace/articles/rag` (the parent). The active book folder is `rag/<book>/`, where `<book>` defaults to the PDF filename stem (e.g. `TrackTrack.pdf` → `rag/TrackTrack/`).

PDFs are read from `articles/pdf/`. Pass `--pdf` or `--book` on every command when multiple books exist.

If `articles/pdf/` has exactly one PDF and you omit `--pdf`, that file (and its stem folder) is used.

Use an isolated demo when requested:

```powershell
.nanobot\workspace\articles_demo_<slug>\pdf\
.nanobot\workspace\articles_demo_<slug>\rag\<slug>\
```

## Fast Indexing

Use fast mode by default. It is optimized for batches of papers and code.

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py sync --mode fast
```

What fast mode does:

- Reads existing `md/*.md`.
- Reads existing `code_md/*.md`.
- Removes image links from text chunks but registers image paths.
- Skips references/acknowledgements for paper Markdown.
- Chunks paper and code text by Markdown headings, then splits only oversized sections.
- Keeps fenced code blocks intact when detecting Markdown headings, so README shell/Python command blocks are not split by comment-like lines.
- Keeps short command chunks when they match common command-line patterns such as `python`, `python -m`, `bash`, `pip`, `conda`, `npm`, `docker`, `torchrun`, or similar executable lines.
- Embeds chunks using `.nanobot/config.json`.
- Stores chunks, embeddings, sources, and image references in SQLite.
- Does not call VLM.
- Does not run LightRAG graph/entity extraction.

If PDFs need Markdown first:

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py sync --mode fast --include-pdf --pdf HybridSORT.pdf
```

PDF is read from `articles/pdf/`. Markdown and the DB are written to `articles/rag/HybridSORT/`. Bulk PDF directory processing is disabled; use one PDF per run or pass `--pdf`.

Lexical-only fallback when embedding credentials are unavailable:

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py sync --mode fast --skip-embedding
```

## Paper-Code Alignment Links

After fast indexing, build a paper-code alignment graph in `paper_rag_links`:

- **Document level** (`official_repo`): created automatically when an official GitHub repo is registered and matched to a paper Markdown by title.
- **Chunk level** (`implements_method`, `eval_script`, `config`, `dataset_pipeline`): built with embedding similarity, keyword/symbol overlap (e.g. `HMIoU`, class names, datasets), and LLM verification.

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py sync --mode fast --link
python nanobot\skills\paper-rag\scripts\paper_rag.py link
python nanobot\skills\paper-rag\scripts\paper_rag.py link --skip-llm
```

`sync --link` requires embeddings. Use standalone `link` after re-indexing without re-embedding everything.

Link tuning env vars:

- `PAPER_RAG_LINK_EMBED_TOP_K` (default `4`)
- `PAPER_RAG_LINK_EMBED_THRESHOLD` (default `0.50`)
- `PAPER_RAG_LINK_LLM_THRESHOLD` (default `0.55`)
- `PAPER_RAG_LINK_MAX_LLM_PAIRS` (default `40`)

Query retrieval expands linked counterpart chunks (paper chunk → linked code chunk, and vice versa). `status` reports document/chunk link counts.

Example questions that benefit from links:

- "Where is HMIoU implemented in the official code?"
- "Which script/config matches the benchmark table in the paper?"

## Official GitHub Source

Register only clearly official repositories linked by the paper, project page, arXiv, publisher, author page, or PDF.

```powershell
python nanobot\skills\paper-rag\scripts\github_sources.py add https://github.com/owner/repo --paper-title "Paper title"
python nanobot\skills\paper-rag\scripts\github_sources.py sync
python nanobot\skills\paper-rag\scripts\paper_rag.py sync --mode fast
```

Do not register third-party reimplementations by default. The helper writes extracted code Markdown to `code_md/`.

If no official repository is found, skip this step. The RAG must still index paper Markdown and answer from `paper_md` / `paper_rag_cards`; code-oriented questions will fall back to paper implementation details instead of failing.

Official-source rule:

- Register a repo when the paper/arXiv/publisher/project page links to it, or the repo itself clearly claims to be the official code for the exact paper.
- Do not register similarly named repos, forks, Papers With Code mirrors, or third-party reimplementations unless the user explicitly asks.
- If uncertain, proceed paper-only and report that no official code was registered.

## Enrich Mode

Use enrich mode when the user wants better paper-level synthesis but still wants to avoid VLM.

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py sync --mode enrich
```

For each paper Markdown, enrich mode creates one paper card with:

- problem
- method
- contributions
- datasets
- metrics
- code relevance
- limitations
- summary

This is one LLM call per paper, not per chunk.

## Query

Retrieve chunks and synthesize an answer:

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py query "How does TrackTrack use object permanence?"
```

Retrieve only, without LLM answer synthesis:

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py query "training config" --no-answer
```

Control retrieval count:

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py query "dataset and metrics" --top-k 12
```

Query routing is automatic:

- Questions with code/run/config/train/install terms boost `code_md` and cap non-code context.
- If no `code_md` exists, code-oriented questions fall back to the best paper/card chunks instead of returning an empty result.
- Questions about paper evaluation, datasets, metrics, benchmarks, or results prioritize `paper_md` and avoid mistaking generic "evaluate" for code execution.
- Questions about contributions, methods, limitations, summaries, or results include `paper_rag_cards` as high-priority candidates.
- Run/command questions boost generic executable command chunks rather than repository-specific filenames.
- Candidate scores are printed to make recall debugging easier.

Run a small retrieval check after indexing:

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py --root <root> query "What are the main contributions of <paper>?" --top-k 6 --no-answer
python nanobot\skills\paper-rag\scripts\paper_rag.py --root <root> query "Which datasets and metrics are used to evaluate <paper>?" --top-k 6 --no-answer
python nanobot\skills\paper-rag\scripts\paper_rag.py --root <root> query "How do I run <repo or method>?" --top-k 8 --no-answer
```

Expected recall shape:

- Contributions/limitations should return `paper_card` first after `enrich`, otherwise Method/Conclusion paper chunks.
- Dataset/metric questions should return `paper_md` chunks such as Experimental Setting, Metrics, Benchmark Results, or tables.
- Code/run questions should return `code_md` command chunks when official source exists; if not, they should fall back to paper implementation details.

## On-Demand Vision

Do not call VLM during normal indexing. Use `vision` only when the user asks about a figure, architecture diagram, visual comparison, or specific image.

By source/image substring:

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py vision TrackTrack --question "Describe the method overview figure."
```

By direct image path:

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py vision .nanobot\workspace\articles\rag\md\images\figure1.jpg --question "What does this figure show?"
```

Choose a later matched image:

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py vision TrackTrack --image-index 1
```

Vision responses are cached in SQLite by image path and question.

## Status And Trace

Check indexed sources, chunk counts, paper cards, and image registry:

```powershell
python nanobot\skills\paper-rag\scripts\paper_rag.py status
```

Inspect latest trace:

```powershell
Get-Content .nanobot\workspace\articles\paper_rag_trace.jsonl -Tail 80
```

Trace events include:

- `sync.start/end`
- `source.start/end`
- `embedding.start/end/error`
- `llm.start/end/error` (text: enrich, link, query answer)
- `vlm.start/end/error` (vision only)
- `link.start/end`
- `link.llm.start/end`

Disable trace:

```powershell
$env:PAPER_RAG_TRACE = "0"
```

## Config

The script reads `.nanobot/config.json`.

Expected fields:

```json
{
  "agents": {
    "defaults": {
      "model": "DeepSeek-V4-Flash",
      "visual_model": "Qwen3.5-397B-A17B",
      "provider": "openai",
      "embed_model": "text-embedding-3-small",
      "embedProvider": "apiyi"
    }
  }
}
```

Model routing:

- `model` → default LLM for enrich, link verification, and query answers (`chat_complete`).
- `visual_model` → VLM only for on-demand `vision` (figures/images).
- `embed_model` + `embedProvider` → chunk indexing and retrieval embeddings.

Embedding calls use `providers.<embedProvider>.apiBase/apiKey`.
LLM and VLM calls use `providers.<provider>.apiBase/apiKey`.

Environment overrides:

- `PAPER_RAG_EMBEDDING_MODEL`
- `PAPER_RAG_EMBEDDING_API_KEY`
- `PAPER_RAG_EMBEDDING_BASE_URL`
- `PAPER_RAG_LLM_MODEL` (text LLM; overrides `model`)
- `PAPER_RAG_VLM_MODEL` (vision only; overrides `visual_model`)
- `PAPER_RAG_LLM_API_KEY`
- `PAPER_RAG_LLM_BASE_URL`
- `PAPER_RAG_TRACE`

Never print API keys, tokens, SYSU credentials, cookies, or signed temporary URLs.

## MinerU

Direct PDF-to-Markdown conversion:

```powershell
python nanobot\skills\paper-rag\scripts\mineru_pdf_to_md.py --pdf HybridSORT.pdf --interval 20 --timeout 3600
```

Put credentials in `articles/.env` or `articles/rag/.env` (`MINERU_API_TOKEN` or OpenXLab `Access_Key` / `Secret_Key`).

The helper reads PDF from `articles/pdf/` and writes Markdown to `articles/rag/md/` and `articles/rag/md/images/`.
It requires exactly one `--pdf` (or a single PDF already in `articles/pdf/`). It does not bulk-scan the PDF folder, and it does not run downstream sync unless `--sync-db` is set.

## Recommended Flow

For a single paper/code RAG run:

1. Put the target PDF in `.nanobot\workspace\articles\pdf\` (one file, or pass `--pdf`).
2. Put `.env` in `articles\` or `articles\rag\` if needed.
3. Convert the PDF (output → `articles\rag\md\`):
   `python nanobot\skills\paper-rag\scripts\mineru_pdf_to_md.py --pdf <paper.pdf>`
   or `python nanobot\skills\paper-rag\scripts\paper_rag.py sync --mode fast --include-pdf --pdf <paper.pdf>`.
4. If an official GitHub repo is found, register and sync it:
   `python nanobot\skills\paper-rag\scripts\github_sources.py add <url> --paper-title "<title>"`
   then `python nanobot\skills\paper-rag\scripts\github_sources.py sync`.
5. Run fast indexing:
   `python nanobot\skills\paper-rag\scripts\paper_rag.py sync --mode fast`.
6. When official code exists, build alignment links:
   `python nanobot\skills\paper-rag\scripts\paper_rag.py link` (or `sync --mode fast --link`).
7. Run `status` and at least two retrieval checks, one paper-focused and one code-focused if code exists.
8. Run `sync --mode enrich` when paper-level questions need better synthesis; this is one LLM call per paper.
9. Run `vision` only when the user asks about a specific figure.

If Markdown already exists, skip MinerU and go directly to official GitHub sync and fast indexing. If no official GitHub exists, skip GitHub and still run paper-only indexing.

## Validated Examples

The current workflow was validated on two MOT papers and the lessons should generalize:

- TrackTrack: existing Markdown plus official GitHub source. The key fix was preserving fenced code blocks and generic command chunks so README commands are not dropped or split by comment-style lines.
- HybridSORT: single PDF converted through MinerU plus official GitHub source. The key guardrail was single-PDF MinerU processing; no shared `pdf/` directory sweep.

Do not hard-code paper names, repo filenames, datasets, or command names from these examples. Use them only as regression patterns:

- one-paper MinerU must process only the specified PDF;
- official code is optional and must be verified;
- indexing must work with paper-only and paper+code roots;
- query routing must separate paper evaluation questions from code execution questions.

## Completion Checklist

Before reporting done:

- Confirm the root used.
- Confirm `paper_rag.py status`.
- Confirm whether indexing was `fast` or `enrich`.
- Report whether embeddings were used or lexical-only fallback was used.
- Report `paper_rag_links` counts from `status` when official code was indexed.
- Mention whether VLM was skipped or called on demand.
- If slow, cite the trace event and model/provider that caused it.
