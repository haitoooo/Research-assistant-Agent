---
name: rag-anything
description: Download and manage research papers, avoid duplicate PDFs with SQLite, queue SYSU/manual downloads, convert PDFs to Markdown with MinerU HTTP API, register official GitHub source code, index paper Markdown/images/source into RAGAnything/LightRAG, trace MinerU/embedding/VLM/LLM requests, query the RAG, and start the LightRAG WebUI for nanobot article workspaces.
---

# RAGAnything Paper Pipeline

Use this skill as the single replacement for the old `paper-ingest` workflow. The canonical scripts live here:

- `nanobot/skills/rag-anything/scripts/ingest_articles.py`
- `nanobot/skills/rag-anything/scripts/paper_db.py`
- `nanobot/skills/rag-anything/scripts/mineru_pdf_to_md.py`
- `nanobot/skills/rag-anything/scripts/github_sources.py`
- `nanobot/skills/rag-anything/scripts/raganything_rag.py`

Do not use `nanobot/skills/paper-ingest` as the main entrypoint for new work.

## Workspace Layout

Default root:

```powershell
.nanobot\workspace\articles
```

Important paths:

- `pdf/`: downloaded paper PDFs.
- `md/`: MinerU Markdown output.
- `md/images/`: local images referenced by MinerU Markdown.
- `code/`: official GitHub source repositories.
- `code_md/`: source Markdown extracted for RAG.
- `.env`: MinerU credentials such as `Access_Key` and `Secret_Key`.
- `need_sysu_download.md`: WeChat-ready manual/SYSU download queue.
- `articles.sqlite3`: duplicate detection and RAG status database.
- `raganything/`: RAGAnything/LightRAG storage.
- `raganything_output/`: parser output.
- `raganything_trace.jsonl`: per-request trace.

For demos, replace `--root` with the requested workspace, for example `.nanobot\workspace\articles_demo_tracktrack`.

## Full Pipeline

Prefer the single entrypoint for end-to-end ingestion:

```powershell
python nanobot\skills\rag-anything\scripts\ingest_articles.py --root .nanobot\workspace\articles
```

This sequence:

1. Syncs paper metadata for duplicate detection.
2. Detects PDFs in `pdf/` without same-stem Markdown in `md/`.
3. Calls MinerU for missing Markdown.
4. Syncs metadata again.
5. Clones/pulls registered official GitHub repositories and generates `code_md/`.
6. Indexes new or changed paper Markdown and code Markdown into RAGAnything.

Useful flags:

```powershell
python nanobot\skills\rag-anything\scripts\ingest_articles.py --root .nanobot\workspace\articles --skip-mineru
python nanobot\skills\rag-anything\scripts\ingest_articles.py --root .nanobot\workspace\articles --skip-raganything
python nanobot\skills\rag-anything\scripts\ingest_articles.py --root .nanobot\workspace\articles --include-pdf-in-rag
```

## Duplicate Database

Before downloading a candidate paper, check title, DOI, URL, or file hash:

```powershell
python nanobot\skills\rag-anything\scripts\paper_db.py --root .nanobot\workspace\articles check --title "Paper title" --doi "10.xxxx/xxxxx" --url "https://..."
```

If the result is `found`, do not download again unless the user explicitly wants a replacement.

After successful PDF downloads or Markdown conversion, sync:

```powershell
python nanobot\skills\rag-anything\scripts\paper_db.py --root .nanobot\workspace\articles sync
python nanobot\skills\rag-anything\scripts\paper_db.py --root .nanobot\workspace\articles stats
```

## Download Workflow

Create directories if missing:

```powershell
New-Item -ItemType Directory -Force -Path .nanobot\workspace\articles\pdf, .nanobot\workspace\articles\md | Out-Null
```

Search open PDFs first: arXiv, author pages, project pages, institutional repositories, conference proceedings, journal PDFs, DOI landing pages, and PubMed Central when relevant.

Save valid PDFs to `pdf/` with stable ASCII filenames:

```text
YYYY_FirstAuthor_Short_Title.pdf
```

Verify signatures before treating files as PDFs:

```powershell
Get-ChildItem .nanobot\workspace\articles\pdf -Filter *.pdf | ForEach-Object {
  $fs=[System.IO.File]::Open($_.FullName,[System.IO.FileMode]::Open,[System.IO.FileAccess]::Read,[System.IO.FileShare]::ReadWrite)
  try {
    $buf=New-Object byte[] 4
    [void]$fs.Read($buf,0,4)
    [PSCustomObject]@{ Name=$_.Name; MB=[math]::Round($_.Length/1MB,2); Signature=[System.Text.Encoding]::ASCII.GetString($buf) }
  } finally { $fs.Dispose() }
}
```

Only count `Signature=%PDF` files as valid.

## SYSU / Manual Queue

Do not ask for, store, or handle the user's SYSU password.

If a PDF requires SYSU/CARSI/Shibboleth/institutional access, add a concise WeChat-ready item:

```powershell
python nanobot\skills\rag-anything\scripts\paper_db.py --root .nanobot\workspace\articles add-manual --title "Paper title" --url "https://..." --doi "10.xxxx/xxxxx"
```

Prefer DOI or publisher landing pages over signed temporary PDF URLs. When the user later places the PDF into `pdf/`, run the full pipeline again.

## MinerU PDF To Markdown

Normal route:

```powershell
python nanobot\skills\rag-anything\scripts\ingest_articles.py --root .nanobot\workspace\articles --pdf HybridSORT.pdf
```

Direct MinerU route:

```powershell
python nanobot\skills\rag-anything\scripts\mineru_pdf_to_md.py --root .nanobot\workspace\articles --pdf HybridSORT.pdf --interval 20 --timeout 3600
```

The root `.env` should contain either:

```text
MINERU_API_TOKEN=...
```

or:

```text
Access_Key=...
Secret_Key=...
```

The script uses MinerU HTTP API to create a one-file batch, upload the named PDF, poll completion, download the result zip, write one `.md` into `md/`, and extract images into `md/images/` so links like `![](images/xxx.jpg)` resolve locally. Bulk PDF directory processing is disabled; always pass exactly one `--pdf`.

If result download fails after MinerU completed, rerun with the printed batch id:

```powershell
python nanobot\skills\rag-anything\scripts\mineru_pdf_to_md.py --root .nanobot\workspace\articles --pdf HybridSORT.pdf --batch-id <batch_id> --interval 10 --timeout 600
```

## RAGAnything Indexing

RAGAnything uses LightRAG storage internally. This skill is API-first:

- PDF conversion uses MinerU HTTP API.
- Markdown and code insertion use RAGAnything/LightRAG Python APIs.
- Embeddings use the configured embedding API.
- Image understanding uses the configured VLM API.

Config is read from `.nanobot/config.json`:

```json
{
  "agents": {
    "defaults": {
      "visual_model": "Qwen3.5-397B-A17B",
      "provider": "openai",
      "embed_model": "text-embedding-3-small",
      "embedProvider": "apiyi"
    }
  }
}
```

Index existing `md/` and `code_md/` without rerunning MinerU:

```powershell
python nanobot\skills\rag-anything\scripts\raganything_rag.py --root .nanobot\workspace\articles sync
```

Convert missing PDFs through MinerU HTTP API first, then index Markdown:

```powershell
python nanobot\skills\rag-anything\scripts\raganything_rag.py --root .nanobot\workspace\articles sync --include-pdf --pdf HybridSORT.pdf
```

Force re-index:

```powershell
python nanobot\skills\rag-anything\scripts\raganything_rag.py --root .nanobot\workspace\articles sync --force
```

Query:

```powershell
python nanobot\skills\rag-anything\scripts\raganything_rag.py --root .nanobot\workspace\articles query "What are the main methods in encrypted-domain multi-object tracking?" --mode mix
```

Status:

```powershell
python nanobot\skills\rag-anything\scripts\raganything_rag.py --root .nanobot\workspace\articles status
```

Use SQLite for duplicate detection and status tracking. Do not fall back to old SQLite FTS chunks unless the user explicitly asks.

## Official GitHub Source

Register GitHub source only when it is clearly official:

- linked from the paper, arXiv page, project page, publisher page, author page, or PDF;
- under an author/lab/org account explicitly named by the paper/project.

Do not register uncertain code:

- third-party reimplementations;
- forks without official upstream links;
- repos that only share the title;
- ambiguous search results.

Commands:

```powershell
python nanobot\skills\rag-anything\scripts\github_sources.py --root .nanobot\workspace\articles add https://github.com/owner/repo --paper-title "Paper title"
python nanobot\skills\rag-anything\scripts\github_sources.py --root .nanobot\workspace\articles sync
python nanobot\skills\rag-anything\scripts\raganything_rag.py --root .nanobot\workspace\articles sync
```

The GitHub step clones/pulls repos into `code/`, extracts README/config/training/eval/demo/source files into `code_md/`, and RAGAnything indexes those files with the paper Markdown. Avoid datasets, checkpoints, generated outputs, and large binaries.

## Per-Request Trace

Trace is enabled by default and written to:

```powershell
<root>\raganything_trace.jsonl
```

Events include:

- `mineru.http.*`, `mineru.upload_http.*`, `mineru.download_http.*`
- `embedding.start/end/error`
- `llm.start/end/error`
- `vlm.start/end/error`
- `source.text_insert.*`, `source.multimodal.*`, `sync.*`

Trace logs model/provider/host, durations, input sizes, counts, and errors. It never logs API keys or full prompts.

Inspect:

```powershell
Get-Content .nanobot\workspace\articles\raganything_trace.jsonl -Tail 80
```

Disable:

```powershell
$env:RAGANYTHING_TRACE = "0"
```

Redirect:

```powershell
$env:RAGANYTHING_TRACE_FILE = ".nanobot\workspace\articles\custom_trace.jsonl"
```

## Diagnosing Slow Runs

Check status and the final trace lines:

```powershell
python nanobot\skills\rag-anything\scripts\raganything_rag.py --root <root> status
Get-Content <root>\raganything_trace.jsonl -Tail 30
```

Interpretation:

- Trace ending with `sync.end` means indexing completed even if the outer shell timed out.
- `embedding.start` without matching `embedding.end/error` means embedding provider is stuck.
- `llm.start` without matching `llm.end/error` means LLM/VLM provider is stuck.
- `mineru.http.start` without matching end/error means MinerU API/network is stuck.
- `indexed_sources=0` means SQLite was not marked ready yet.

Observed TrackTrack behavior:

- `apiyi` embedding usually completed in 1-5 seconds.
- `Qwen3.5-397B-A17B` graph/entity extraction often took 90-140 seconds, with one observed call around 303 seconds.
- Completed demo status was `indexed_sources=2`: paper Markdown plus official GitHub source Markdown.

## WebUI

Install missing server dependencies if needed:

```powershell
python -m pip install bcrypt
```

Start from repo root. This reads API keys from `.nanobot/config.json`; do not print them.

```powershell
$cfg = Get-Content .nanobot\config.json -Raw | ConvertFrom-Json
$env:LLM_BINDING = "openai"
$env:LLM_MODEL = $cfg.agents.defaults.visual_model
$env:LLM_BINDING_HOST = $cfg.providers.openai.apiBase
$env:LLM_BINDING_API_KEY = $cfg.providers.openai.apiKey
$env:EMBEDDING_BINDING = "openai"
$env:EMBEDDING_MODEL = $cfg.agents.defaults.embed_model
$env:EMBEDDING_DIM = "1536"
$env:EMBEDDING_BINDING_HOST = $cfg.providers.apiyi.apiBase
$env:EMBEDDING_BINDING_API_KEY = $cfg.providers.apiyi.apiKey

lightrag-server --working-dir .nanobot\workspace\articles\raganything --host 127.0.0.1 --port 9621 --llm-binding openai --embedding-binding openai --timeout 300
```

Open:

```text
http://127.0.0.1:9621
```

For TrackTrack:

```powershell
lightrag-server --working-dir .nanobot\workspace\articles_demo_tracktrack\raganything --host 127.0.0.1 --port 9621 --llm-binding openai --embedding-binding openai --timeout 300
```

If starting in the background, write helper scripts inside the workspace; `D:\tmp` may deny writes.

## Completion Checklist

Before reporting done:

- Confirm the root used.
- Confirm `paper_db.py sync` ran after new PDFs/Markdown.
- Confirm `github_sources.py sync` ran when official GitHub repos were registered.
- Confirm `raganything_rag.py sync` ran, or explain why it was skipped.
- List valid PDFs and confirm `%PDF` signatures when downloads occurred.
- List generated Markdown and confirm non-zero sizes.
- Confirm image links resolve under `md/images/` when Markdown contains `![](images/...)`.
- Show `paper_db.py stats` and `raganything_rag.py status` when available.
- Show whether trace ends with `sync.end` or an error.
- Mention any entries added to `need_sysu_download.md`.
- Do not print API keys, cookies, SYSU credentials, or signed temporary PDF URLs.
