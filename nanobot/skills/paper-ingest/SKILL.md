---
name: paper-ingest
description: Download research paper PDFs into the article workspace, avoid duplicate downloads with a SQLite paper database, keep a WeChat-ready SYSU/manual-download list, convert PDFs to Markdown with MinerU, and index/query Markdown with LightRAG.
metadata: {"nanobot":{"os":["darwin","linux","windows"],"requires":{"bins":["python"]}}}
---

# Paper Ingest

Use this skill when the user asks to collect papers, download PDFs, handle papers that require institutional access, or convert downloaded PDFs to Markdown.

Default workspace layout:

- PDF input/output: `.nanobot/workspace/articles/pdf/`
- Markdown output: `.nanobot/workspace/articles/md/`
- Markdown images/resources: `.nanobot/workspace/articles/md/images/` by default, matching MinerU links like `![](images/xxx.jpg)`
- GitHub source clones: `.nanobot/workspace/articles/code/`
- GitHub source Markdown for RAG: `.nanobot/workspace/articles/code_md/`
- MinerU credentials: `.nanobot/workspace/articles/.env`
- Manual download queue: `.nanobot/workspace/articles/need_sysu_download.md`
- Paper metadata database: `.nanobot/workspace/articles/articles.sqlite3`
- LightRAG storage: `.nanobot/workspace/articles/lightrag/`

## Database-first rule

Every time this skill is used for end-to-end ingestion, use the single entrypoint:

```powershell
python nanobot\skills\paper-ingest\scripts\ingest_articles.py --root .nanobot\workspace\articles
```

This does the full sequence:

1. Sync paper metadata for duplicate detection.
2. Detect PDFs in `pdf/` that do not have same-stem Markdown in `md/`.
3. Automatically call MinerU for missing Markdown.
4. Sync metadata again.
5. Clone/pull registered GitHub source repositories and generate code Markdown.
6. Insert new/changed paper Markdown and code Markdown into LightRAG.

If you only need duplicate metadata, start by syncing the local paper database:

```powershell
python nanobot\skills\paper-ingest\scripts\paper_db.py --root .nanobot\workspace\articles sync
```

Before downloading a candidate paper, check whether it is already known by title, DOI, URL, or an existing file hash:

```powershell
python nanobot\skills\paper-ingest\scripts\paper_db.py --root .nanobot\workspace\articles check --title "Paper title" --doi "10.xxxx/xxxxx" --url "https://..."
```

If the check prints `found`, do not download it again unless the user explicitly wants a replacement. If it prints `not-found`, proceed with the download attempt.

After any successful PDF download, run metadata `sync` so new PDFs and Markdown paths are stored for duplicate detection.

After Markdown conversion, `ingest_articles.py` runs LightRAG sync so new or changed Markdown files are inserted into the LightRAG working directory. Manual command:

```powershell
python nanobot\skills\paper-ingest\scripts\lightrag_rag.py --root .nanobot\workspace\articles sync
```

For RAG questions over the indexed papers:

```powershell
python nanobot\skills\paper-ingest\scripts\lightrag_rag.py --root .nanobot\workspace\articles query "加密域多目标跟踪有哪些主要方法？" --mode hybrid
```

## Download workflow

1. Create the directories if missing:

```powershell
New-Item -ItemType Directory -Force -Path .nanobot\workspace\articles\pdf, .nanobot\workspace\articles\md | Out-Null
```

2. Search for open PDFs first: arXiv, author pages, institutional repositories, journal PDF endpoints, PubMed Central, conference proceedings, and DOI landing pages.

3. For each candidate, run the database `check` command before downloading.

4. Save successful PDF downloads to `.nanobot/workspace/articles/pdf/` with stable ASCII filenames:

```text
YYYY_FirstAuthor_Short_Title.pdf
```

5. Verify every downloaded file before counting it as done:

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

Only treat `Signature=%PDF` files as valid PDFs.

6. Run database `sync` after valid new PDFs are saved. The database uses SHA-256 for hard duplicate detection and title/DOI/URL normalization for soft duplicate detection.

## SYSU / manual download queue

Do not ask for the user's SYSU password or try to store institutional credentials.

If a PDF cannot be downloaded publicly because it needs SYSU/CARSI/Shibboleth/institutional login, append a WeChat-ready entry to `.nanobot/workspace/articles/need_sysu_download.md`:

```markdown
## Need SYSU Download

1. Paper title
   Link: https://...
   DOI: https://doi.org/...
   Reason: requires SYSU federated login / institutional access
```

Keep entries concise so the user can copy them into WeChat. Prefer the DOI or publisher landing page over expired signed PDF URLs. If the user later places the downloaded PDF into `pdf/`, remove or mark the item as done only when they ask for cleanup.

Prefer the database helper, which also records the item with `manual_needed` status and avoids duplicate queue entries:

```powershell
python nanobot\skills\paper-ingest\scripts\paper_db.py --root .nanobot\workspace\articles add-manual --title "Paper title" --url "https://..." --doi "10.xxxx/xxxxx"
```

## MinerU PDF to Markdown

The normal route is `ingest_articles.py`, which calls MinerU automatically when a PDF is missing Markdown. Use the MinerU script directly only when you want to bypass the full pipeline:

```powershell
python nanobot\skills\paper-ingest\scripts\mineru_pdf_to_md.py --root .nanobot\workspace\articles --interval 20 --timeout 3600
```

The script expects `.nanobot/workspace/articles/.env` to contain either:

```text
MINERU_API_TOKEN=...
```

or OpenXLab/MinerU keys:

```text
Access_Key=...
Secret_Key=...
```

It creates a MinerU batch, uploads all PDFs, polls until complete, downloads the result zip files, and writes one `.md` per PDF into `md/`. It also extracts images from MinerU zip files into `md/images/`, so Markdown links such as `![](images/xxx.jpg)` resolve locally. It skips existing non-empty Markdown files during result download but still refreshes image assets when a result zip is available. On success it runs `paper_db.py sync` for metadata and `lightrag_rag.py sync` for LightRAG indexing.

## LightRAG processing

Install the LightRAG package when first needed:

```powershell
python -m pip install lightrag-hku
```

LightRAG also needs an LLM/embedding provider key. The default helper uses OpenAI-compatible functions, so set `OPENAI_API_KEY` in the environment or in `.nanobot/workspace/articles/.env`.

The LightRAG helper reads `.nanobot/config.json` and uses `agents.defaults.visual_model` for LLM calls when present. With this config:

```json
{
  "agents": {
    "defaults": {
      "visual_model": "Qwen3.5-397B-A17B",
      "provider": "openai"
    }
  }
}
```

LightRAG entity extraction, summarization, and query answering call `Qwen3.5-397B-A17B` through the configured OpenAI-compatible provider. During `sync`, local Markdown image links such as `![](images/xxx.jpg)` are also sent to this visual model for concise figure/table captions; those captions are appended to the text inserted into LightRAG and cached in SQLite so unchanged images are not captioned repeatedly.

Embeddings stay separate and read these fields from `.nanobot/config.json`:

```json
{
  "agents": {
    "defaults": {
      "embed_model": "text-embedding-3-small",
      "embedProvider": "apiyi"
    }
  }
}
```

The helper calls `providers.apiyi.apiBase/apiKey` for embeddings while continuing to call `providers.<provider>` for the visual/LLM model. Environment overrides are available: `LIGHTRAG_EMBEDDING_MODEL`, `LIGHTRAG_EMBEDDING_API_KEY`, `LIGHTRAG_EMBEDDING_BASE_URL`, `LIGHTRAG_EMBEDDING_DIM`, and `LIGHTRAG_EMBEDDING_MAX_TOKENS`. If a paper has too many images and you want to cap visual-model calls, set `LIGHTRAG_VISUAL_CAPTION_MAX_IMAGES_PER_DOC` to a positive integer; `0` means no cap.

LightRAG commands:

```powershell
# Insert new/changed Markdown files into LightRAG
python nanobot\skills\paper-ingest\scripts\lightrag_rag.py --root .nanobot\workspace\articles sync

# Ask questions
python nanobot\skills\paper-ingest\scripts\lightrag_rag.py --root .nanobot\workspace\articles query "question" --mode hybrid

# Inspect indexed Markdown records
python nanobot\skills\paper-ingest\scripts\lightrag_rag.py --root .nanobot\workspace\articles status
```

Use SQLite only for duplicate detection and manual-download tracking. Do not use the old SQLite FTS chunks path for RAG unless the user explicitly asks for a fallback.

## GitHub Source Code

During paper discovery/download, register GitHub source only when it is clearly official:

- official repository linked from the paper, arXiv page, project page, publisher page, or author page;
- repository under an author/lab/organization account that is explicitly named by the paper/project;
- repository URL stated in the PDF itself.

Do not register uncertain code:

- third-party reimplementations;
- forks without an official upstream link;
- repos that merely share a paper title but are not linked by the authors;
- ambiguous search results.

If no clearly official GitHub repository is found, treat the paper as having no source code and continue without asking the user.

When a paper has an official or relevant GitHub repository, register it before running the full ingest:

```powershell
python nanobot\skills\paper-ingest\scripts\github_sources.py --root .nanobot\workspace\articles add https://github.com/owner/repo --paper-title "Paper title"
```

Then run the full pipeline:

```powershell
python nanobot\skills\paper-ingest\scripts\ingest_articles.py --root .nanobot\workspace\articles
```

The GitHub step clones or pulls repos into `code/`, extracts README/config/training/eval/demo/source files into one Markdown file per repo under `code_md/`, and LightRAG indexes those files together with paper Markdown. This lets questions retrieve both the paper text and implementation details.

Manual GitHub commands:

```powershell
python nanobot\skills\paper-ingest\scripts\github_sources.py --root .nanobot\workspace\articles list
python nanobot\skills\paper-ingest\scripts\github_sources.py --root .nanobot\workspace\articles sync
```

Avoid indexing datasets, checkpoints, generated outputs, and large binary files. The helper skips common large directories and caps included files/bytes.

If result download fails after MinerU has already completed, rerun with the printed batch id:

```powershell
python nanobot\skills\paper-ingest\scripts\mineru_pdf_to_md.py --root .nanobot\workspace\articles --batch-id <batch_id> --interval 10 --timeout 600
```

## Dependency notes

The script uses `requests`. If `.env` uses `Access_Key` and `Secret_Key`, it also needs `openxlab` to exchange them for a JWT:

```powershell
python -m pip install openxlab
```

Be aware that `openxlab` may pin older versions of common packages. If installing it downgrades the project environment, restore the project dependencies after the conversion.

## Completion checklist

Before reporting done:

- Prefer `ingest_articles.py` for the full workflow unless the user asked for a single sub-step.
- Confirm `paper_db.py sync` was run at the start and after any new PDF/Markdown output.
- Confirm `github_sources.py sync` was run when GitHub repos are registered.
- Confirm `lightrag_rag.py sync` was run after Markdown output, or report why it was skipped.
- List valid PDFs in `pdf/` and confirm each starts with `%PDF`.
- List generated Markdown files in `md/` and confirm non-zero sizes.
- Confirm Markdown image links resolve under `md/images/` when the converted Markdown contains `![](images/...)`.
- Show `paper_db.py stats` and `lightrag_rag.py status` counts when available.
- Mention any papers added to `need_sysu_download.md`.
- Do not print API keys, cookies, SYSU credentials, or signed temporary PDF URLs.
