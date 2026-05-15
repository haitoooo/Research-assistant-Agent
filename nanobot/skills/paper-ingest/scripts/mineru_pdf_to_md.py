from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

import requests


BASE_URL = "https://mineru.net/api/v4"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}


class MinerUTransientError(RuntimeError):
    pass


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def token_candidates(env: dict[str, str]) -> list[str]:
    candidates: list[str] = []
    access = env.get("Access_Key") or env.get("ACCESS_KEY")
    secret = env.get("Secret_Key") or env.get("SECRET_KEY")
    if access and secret:
        try:
            from openxlab.xlab.handler.user_token import get_jwt

            candidates.append(get_jwt(access, secret))
        except Exception as exc:
            print(f"OpenXLab JWT exchange failed: {exc}", flush=True)
    for key in ("MINERU_API_TOKEN", "API_TOKEN", "TOKEN", "Secret_Key", "SECRET_KEY", "Access_Key", "ACCESS_KEY"):
        value = env.get(key)
        if value:
            candidates.append(value)
    if access and secret:
        candidates.extend([f"{access}:{secret}", f"{access},{secret}", f"{access}.{secret}"])

    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return deduped


def request_json(method: str, url: str, token: str, **kwargs) -> dict:
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    response = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    try:
        payload = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError(f"Expected JSON from {url}, got {response.text[:200]!r}")
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {payload}")
    code = payload.get("code")
    if code == -60012:
        raise MinerUTransientError(f"MinerU task not ready: {payload}")
    if code not in (0, 200, None):
        raise RuntimeError(f"API error: {payload}")
    return payload


def download_bytes(url: str, timeout: int = 180) -> bytes:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            return response.content
        except Exception as exc:
            last_error = exc
            time.sleep(2 + attempt * 3)

    curl = "curl.exe" if os.name == "nt" else "curl"
    completed = subprocess.run(
        [curl, "-L", "--fail", "--retry", "3", "--max-time", str(timeout), "--silent", "--show-error", url],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode == 0:
        return completed.stdout
    raise RuntimeError(f"download failed: {last_error}; curl: {completed.stderr.decode(errors='replace')[:300]}")


def create_batch(pdf_paths: list[Path], token: str) -> tuple[str, list[dict]]:
    files = [{"name": path.name, "is_ocr": True, "data_id": path.stem} for path in pdf_paths]
    payload = request_json(
        "POST",
        f"{BASE_URL}/file-urls/batch",
        token,
        json={"enable_formula": True, "enable_table": True, "language": "en", "files": files},
    )
    data = payload.get("data") or {}
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls") or []
    if not batch_id or len(file_urls) != len(pdf_paths):
        raise RuntimeError(f"Unexpected create-batch response: {payload}")
    return batch_id, file_urls


def upload_files(pdf_paths: list[Path], file_urls: list[dict]) -> None:
    for path, entry in zip(pdf_paths, file_urls):
        upload_url = entry if isinstance(entry, str) else entry.get("url")
        if not upload_url:
            raise RuntimeError(f"Missing upload URL for {path.name}: {entry}")
        print(f"upload {path.name}", flush=True)
        with path.open("rb") as handle:
            response = requests.put(upload_url, data=handle, headers={"Content-Type": "application/pdf"}, timeout=300)
        if response.status_code >= 400:
            raise RuntimeError(f"upload failed HTTP {response.status_code}: {response.text[:500]}")


def poll_results(batch_id: str, token: str, interval: int, timeout_seconds: int) -> list[dict]:
    deadline = time.time() + timeout_seconds
    last_summary = ""
    while time.time() < deadline:
        try:
            payload = request_json("GET", f"{BASE_URL}/extract-results/batch/{batch_id}", token)
        except MinerUTransientError as exc:
            summary = "task-not-ready"
            if summary != last_summary:
                print(f"batch {batch_id}: {summary} ({exc})", flush=True)
                last_summary = summary
            time.sleep(interval)
            continue
        data = payload.get("data") or {}
        results = data.get("extract_result") or data.get("extract_results") or []
        counts: dict[str, int] = {}
        for item in results:
            state = str(item.get("state") or item.get("status") or "unknown")
            counts[state] = counts.get(state, 0) + 1
        summary = ", ".join(f"{key}:{value}" for key, value in sorted(counts.items())) or "waiting"
        if summary != last_summary:
            print(f"batch {batch_id}: {summary}", flush=True)
            last_summary = summary
        if results and all(str(item.get("state") or item.get("status")).lower() in {"done", "finished", "success"} for item in results):
            return results
        failed = [item for item in results if str(item.get("state") or item.get("status")).lower() in {"failed", "error"}]
        if failed:
            raise RuntimeError(f"MinerU failed: {failed}")
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for batch {batch_id}")


def output_asset_path(member_name: str, output_dir: Path, stem: str) -> Path | None:
    posix = PurePosixPath(member_name)
    if posix.is_absolute() or ".." in posix.parts:
        return None
    if posix.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    parts = list(posix.parts)
    if "images" in parts:
        relative = Path(*parts[parts.index("images") :])
    else:
        relative = Path("assets") / stem / posix.name
    return output_dir / relative


def extract_markdown_and_assets(zip_path: Path, output_dir: Path, stem: str, target: Path) -> None:
    asset_count = 0
    with zipfile.ZipFile(zip_path) as archive:
        member = next((m for m in archive.namelist() if m.endswith("/full.md") or m == "full.md"), None)
        if member is None:
            member = next((m for m in archive.namelist() if m.lower().endswith(".md")), None)
        if member is None:
            raise RuntimeError(f"No markdown file inside {zip_path}")

        if target.exists() and target.stat().st_size > 0:
            print(f"skip existing {target}", flush=True)
        else:
            target.write_bytes(archive.read(member))
            print(f"write {target}", flush=True)

        for name in archive.namelist():
            asset_path = output_asset_path(name, output_dir, stem)
            if asset_path is None:
                continue
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            asset_path.write_bytes(archive.read(name))
            asset_count += 1
    if asset_count:
        print(f"write assets {stem}: {asset_count}", flush=True)


def download_markdown(results: list[dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for item in results:
        name = item.get("file_name") or item.get("name") or item.get("data_id") or "result"
        stem = Path(name).stem
        zip_url = item.get("full_zip_url") or item.get("zip_url") or item.get("result_url")
        md_url = item.get("md_url") or item.get("markdown_url")
        target = output_dir / f"{stem}.md"
        if md_url and (not target.exists() or target.stat().st_size == 0):
            target.write_bytes(download_bytes(md_url, timeout=120))
            print(f"write {target}", flush=True)
            continue
        if md_url and not zip_url:
            print(f"skip existing {target}", flush=True)
            continue
        if not zip_url:
            raise RuntimeError(f"No markdown or zip URL for {name}: {item}")
        zip_path = output_dir / f"{stem}.zip"
        zip_path.write_bytes(download_bytes(zip_url, timeout=180))
        extract_markdown_and_assets(zip_path, output_dir, stem, target)
        zip_path.unlink(missing_ok=True)


def sync_db(root: Path) -> None:
    paper_db = Path(__file__).with_name("paper_db.py")
    if paper_db.exists():
        completed = subprocess.run([sys.executable, str(paper_db), "--root", str(root), "sync"], check=False)
        if completed.returncode != 0:
            print(f"paper DB sync failed with exit code {completed.returncode}", flush=True)

    lightrag = Path(__file__).with_name("lightrag_rag.py")
    if not lightrag.exists():
        return
    completed = subprocess.run([sys.executable, str(lightrag), "--root", str(root), "sync"], check=False)
    if completed.returncode != 0:
        print(f"LightRAG sync skipped/failed with exit code {completed.returncode}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(".nanobot/workspace/articles"))
    parser.add_argument("--interval", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--batch-id")
    args = parser.parse_args()

    root = args.root
    pdf_dir = root / "pdf"
    md_dir = root / "md"
    env = load_env(root / ".env")
    pdf_paths = sorted(pdf_dir.glob("*.pdf"))
    if not pdf_paths:
        print(f"No PDF files found in {pdf_dir}", file=sys.stderr)
        return 1

    errors: list[str] = []
    for token in token_candidates(env):
        try:
            if args.batch_id:
                batch_id = args.batch_id
            else:
                batch_id, file_urls = create_batch(pdf_paths, token)
                print(f"created batch {batch_id} for {len(pdf_paths)} PDFs", flush=True)
                upload_files(pdf_paths, file_urls)
            results = poll_results(batch_id, token, args.interval, args.timeout)
            download_markdown(results, md_dir)
            sync_db(root)
            return 0
        except Exception as exc:
            errors.append(str(exc))
            print(f"token candidate failed: {exc}", flush=True)
    print("All token candidates failed:", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
