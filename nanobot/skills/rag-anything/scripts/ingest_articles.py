from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


DEFAULT_ROOT = Path(".nanobot/workspace/articles")


def run(command: list[str]) -> int:
    print("run " + " ".join(command), flush=True)
    return subprocess.run(command, check=False).returncode


def pdfs_missing_markdown(root: Path) -> list[Path]:
    pdf_dir = root / "pdf"
    md_dir = root / "md"
    missing: list[Path] = []
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        md = md_dir / f"{pdf.stem}.md"
        if not md.exists() or md.stat().st_size == 0:
            missing.append(pdf)
    return missing


def resolve_pdf_path(root: Path, raw_path: Path) -> Path:
    pdf_dir = root / "pdf"
    candidates = []
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.extend([raw_path, pdf_dir / raw_path, pdf_dir / f"{raw_path}.pdf"])
    found = next((candidate for candidate in candidates if candidate.exists() and candidate.is_file()), None)
    if not found:
        raise FileNotFoundError(f"PDF not found: {raw_path}")
    if found.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF path: {found}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full paper ingest pipeline: metadata, MinerU, GitHub source, RAGAnything.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--interval", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--skip-mineru", action="store_true")
    parser.add_argument("--skip-raganything", action="store_true")
    parser.add_argument("--skip-lightrag", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--include-pdf-in-rag", action="store_true", help="Let RAGAnything also process PDFs directly.")
    parser.add_argument("--pdf", type=Path, help="Single PDF path, filename, or stem to process when MinerU/PDF indexing is needed.")
    args = parser.parse_args()

    root = args.root
    script_dir = Path(__file__).resolve().parent
    paper_db = script_dir / "paper_db.py"
    mineru = script_dir / "mineru_pdf_to_md.py"
    raganything = script_dir / "raganything_rag.py"
    github_sources = script_dir / "github_sources.py"

    rc = run([sys.executable, str(paper_db), "--root", str(root), "sync"])
    if rc != 0:
        return rc

    target_pdf = resolve_pdf_path(root, args.pdf) if args.pdf else None
    missing = pdfs_missing_markdown(root)
    target_missing = []
    if target_pdf:
        target_md = root / "md" / f"{target_pdf.stem}.md"
        if not target_md.exists() or target_md.stat().st_size == 0:
            target_missing = [target_pdf]
    if missing and not target_pdf and not args.skip_mineru:
        print("MinerU is single-file only. Re-run with --pdf <one-pdf>.", flush=True)
        return 2
    if target_missing and not args.skip_mineru:
        print(f"MinerU needed for {target_pdf.name}", flush=True)
        rc = run(
            [
                sys.executable,
                str(mineru),
                "--root",
                str(root),
                "--pdf",
                str(target_pdf),
                "--interval",
                str(args.interval),
                "--timeout",
                str(args.timeout),
            ]
        )
        if rc != 0:
            return rc
    elif missing:
        print(f"skip MinerU; {len(missing)} PDF(s) still missing Markdown", flush=True)
    else:
        print("all PDFs already have same-stem Markdown", flush=True)

    rc = run([sys.executable, str(paper_db), "--root", str(root), "sync"])
    if rc != 0:
        return rc

    if github_sources.exists():
        rc = run([sys.executable, str(github_sources), "--root", str(root), "sync"])
        if rc != 0:
            return rc

    if args.skip_lightrag:
        args.skip_raganything = True

    if not args.skip_raganything:
        command = [sys.executable, str(raganything), "--root", str(root), "sync"]
        if args.include_pdf_in_rag:
            command.append("--include-pdf")
            if not target_pdf:
                print("--include-pdf-in-rag now requires --pdf <one-pdf>", flush=True)
                return 2
            command.extend(["--pdf", str(target_pdf)])
        rc = run(command)
        if rc != 0:
            print(f"RAGAnything sync failed or skipped with exit code {rc}", flush=True)
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
