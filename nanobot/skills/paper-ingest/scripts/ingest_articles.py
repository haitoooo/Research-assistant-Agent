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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full paper ingest pipeline: metadata, MinerU, GitHub source, RAGAnything.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--interval", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--skip-mineru", action="store_true")
    parser.add_argument("--skip-raganything", action="store_true")
    parser.add_argument("--skip-lightrag", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--include-pdf-in-rag", action="store_true", help="Let RAGAnything also process PDFs directly.")
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

    missing = pdfs_missing_markdown(root)
    if missing and not args.skip_mineru:
        print(f"MinerU needed for {len(missing)} PDF(s): " + ", ".join(path.name for path in missing), flush=True)
        rc = run(
            [
                sys.executable,
                str(mineru),
                "--root",
                str(root),
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
        rc = run(command)
        if rc != 0:
            print(f"RAGAnything sync failed or skipped with exit code {rc}", flush=True)
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
