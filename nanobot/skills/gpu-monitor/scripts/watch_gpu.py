#!/usr/bin/env python3
"""Wait for an NVIDIA GPU to become available, then notify the user."""

from __future__ import annotations

import argparse
import csv
import ctypes
import datetime as dt
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path


QUERY = [
    "index",
    "name",
    "memory.total",
    "memory.used",
    "memory.free",
    "utilization.gpu",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll nvidia-smi until a GPU has enough free memory and low utilization."
    )
    parser.add_argument("--min-free-mb", type=int)
    parser.add_argument("--max-util", type=int)
    parser.add_argument("--max-used-mb", type=int)
    parser.add_argument("--min-total-mb", type=int)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--consecutive", type=int, default=2)
    parser.add_argument("--gpu", action="append", type=int, help="GPU index to consider; repeat for multiple GPUs.")
    parser.add_argument("--timeout-min", type=int, default=0, help="Stop after this many minutes; 0 means no timeout.")
    parser.add_argument("--command", help="Command to run once a GPU is available.")
    parser.add_argument("--once", action="store_true", help="Check once, print the result, and exit.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--condition-label", default="", help="Human-readable condition label included in output.")
    parser.add_argument("--background", action="store_true", help="Start a detached watcher and exit.")
    parser.add_argument("--log", help="Log file path for background mode.")
    parser.add_argument("--pid-file", help="PID file path for background mode.")
    parser.add_argument("--notify-local", action="store_true", help="Also show a local desktop notification when available.")
    parser.add_argument("--no-popup", action="store_true", help="Disable desktop popup alert.")
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def default_state_dir() -> Path:
    root = os.environ.get("TEMP") or os.environ.get("TMPDIR") or os.environ.get("TMP") or str(Path.home())
    return Path(root) / "gpu-monitor"


def launch_background(args: argparse.Namespace) -> int:
    state_dir = default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = Path(args.log) if args.log else state_dir / f"watch-{stamp}.log"
    pid_path = Path(args.pid_file) if args.pid_file else state_dir / f"watch-{stamp}.pid"

    child_args = [sys.executable, str(Path(__file__).resolve()), "--_child"]
    for name in ("interval", "consecutive", "timeout_min"):
        child_args.extend([f"--{name.replace('_', '-')}", str(getattr(args, name))])
    for name in ("min_free_mb", "max_util", "max_used_mb", "min_total_mb"):
        value = getattr(args, name)
        if value is not None:
            child_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.gpu:
        for gpu in args.gpu:
            child_args.extend(["--gpu", str(gpu)])
    if args.command:
        child_args.extend(["--command", args.command])
    if args.condition_label:
        child_args.extend(["--condition-label", args.condition_label])
    if args.notify_local:
        child_args.append("--notify-local")
    if args.no_popup:
        child_args.append("--no-popup")
    child_args.extend(["--log", str(log_path), "--pid-file", str(pid_path)])

    log_handle = open(log_path, "a", encoding="utf-8")
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        start_new_session = True
    process = subprocess.Popen(
        child_args,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=start_new_session,
        close_fds=os.name != "nt",
    )
    pid_path.write_text(str(process.pid), encoding="utf-8")
    print(f"Started GPU monitor PID {process.pid}")
    print(f"Log: {log_path}")
    print(f"PID file: {pid_path}")
    if os.name == "nt":
        print(f"Stop with: Stop-Process -Id {process.pid}")
    else:
        print(f"Stop with: kill {process.pid}")
    return 0


def query_gpus() -> list[dict[str, object]]:
    cmd = [
        "nvidia-smi",
        f"--query-gpu={','.join(QUERY)}",
        "--format=csv,noheader,nounits",
    ]
    result = subprocess.run(cmd, check=True, text=True, capture_output=True)
    rows: list[dict[str, object]] = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) != len(QUERY):
            continue
        clean = [part.strip() for part in row]
        rows.append(
            {
                "index": int(clean[0]),
                "name": clean[1],
                "memory_total": int(clean[2]),
                "memory_used": int(clean[3]),
                "memory_free": int(clean[4]),
                "utilization": int(clean[5]),
            }
        )
    return rows


def eligible_gpus(gpus: list[dict[str, object]], args: argparse.Namespace) -> list[dict[str, object]]:
    wanted = set(args.gpu or [])
    matches = []
    for gpu in gpus:
        if wanted and gpu["index"] not in wanted:
            continue
        checks = []
        if args.min_free_mb is not None:
            checks.append(gpu["memory_free"] >= args.min_free_mb)
        if args.max_util is not None:
            checks.append(gpu["utilization"] <= args.max_util)
        if args.max_used_mb is not None:
            checks.append(gpu["memory_used"] <= args.max_used_mb)
        if args.min_total_mb is not None:
            checks.append(gpu["memory_total"] >= args.min_total_mb)
        if checks and all(checks):
            matches.append(gpu)
    return matches


def criteria(args: argparse.Namespace) -> dict[str, object]:
    data: dict[str, object] = {}
    if args.gpu:
        data["gpu"] = args.gpu
    if args.min_free_mb is not None:
        data["min_free_mb"] = args.min_free_mb
    if args.max_util is not None:
        data["max_util"] = args.max_util
    if args.max_used_mb is not None:
        data["max_used_mb"] = args.max_used_mb
    if args.min_total_mb is not None:
        data["min_total_mb"] = args.min_total_mb
    if args.condition_label:
        data["label"] = args.condition_label
    return data


def has_criteria(args: argparse.Namespace) -> bool:
    return any(
        getattr(args, name) is not None
        for name in ("min_free_mb", "max_util", "max_used_mb", "min_total_mb")
    )


def format_gpu(gpu: dict[str, object]) -> str:
    return (
        f"GPU {gpu['index']} {gpu['name']}: "
        f"free={gpu['memory_free']} MiB, used={gpu['memory_used']} MiB, "
        f"total={gpu['memory_total']} MiB, util={gpu['utilization']}%"
    )


def desktop_notify(message: str, popup: bool = True) -> None:
    print("\a", end="", flush=True)
    system = platform.system()
    if system == "Windows":
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass
        if popup:
            try:
                ctypes.windll.user32.MessageBoxW(0, message, "GPU monitor", 0x00001000)
            except Exception:
                pass
    elif popup and system == "Darwin":
        escaped = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        script = f'display notification "{escaped}" with title "GPU monitor"'
        try:
            subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass
    elif popup:
        try:
            subprocess.run(["notify-send", "GPU monitor", message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass


def run_command(command: str) -> int:
    print(f"Running command: {command}", flush=True)
    if os.name == "nt":
        completed = subprocess.run(command, shell=True)
    else:
        completed = subprocess.run(shlex.split(command))
    return completed.returncode


def check_once(args: argparse.Namespace) -> int:
    if not has_criteria(args):
        payload = {
            "status": "error",
            "error": "no GPU availability criteria supplied",
            "hint": "pass at least one of --min-free-mb, --max-util, --max-used-mb, or --min-total-mb",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"Error: {payload['error']}. {payload['hint']}")
        return 3
    try:
        gpus = query_gpus()
        matches = eligible_gpus(gpus, args)
        payload = {
            "status": "available" if matches else "waiting",
            "criteria": criteria(args),
            "matches": matches,
            "gpus": gpus,
            "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        elif matches:
            print("GPU available:")
            for gpu in matches:
                print(format_gpu(gpu))
        else:
            print("GPU not available yet.")
            for gpu in gpus:
                print(format_gpu(gpu))
        return 0 if matches else 1
    except FileNotFoundError:
        payload = {"status": "error", "error": "nvidia-smi was not found on PATH"}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(payload["error"])
        return 127
    except subprocess.CalledProcessError as exc:
        error = (exc.stderr or exc.stdout or str(exc)).strip()
        payload = {"status": "error", "error": error}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"nvidia-smi failed: {error}")
        return 2


def write_child_pid(args: argparse.Namespace) -> None:
    if args.pid_file:
        Path(args.pid_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.pid_file).write_text(str(os.getpid()), encoding="utf-8")


def monitor(args: argparse.Namespace) -> int:
    if not has_criteria(args):
        print(
            "Error: no GPU availability criteria supplied. "
            "Pass at least one of --min-free-mb, --max-util, --max-used-mb, or --min-total-mb.",
            flush=True,
        )
        return 3
    write_child_pid(args)
    deadline = time.monotonic() + args.timeout_min * 60 if args.timeout_min else None
    streak = 0
    last_error = ""
    print(
        "GPU monitor started: "
        f"criteria={criteria(args)}, "
        f"interval={args.interval}s, consecutive={args.consecutive}",
        flush=True,
    )
    while True:
        if deadline and time.monotonic() >= deadline:
            print("Timed out before a matching GPU became available.", flush=True)
            return 2
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            gpus = query_gpus()
            matches = eligible_gpus(gpus, args)
            if matches:
                streak += 1
                best = max(matches, key=lambda gpu: int(gpu["memory_free"]))
                print(f"[{now}] match {streak}/{args.consecutive}: {format_gpu(best)}", flush=True)
                if streak >= args.consecutive:
                    message = f"GPU available for experiment.\n{format_gpu(best)}"
                    print(message, flush=True)
                    if args.notify_local:
                        desktop_notify(message, popup=not args.no_popup)
                    if args.command:
                        return run_command(args.command)
                    return 0
            else:
                streak = 0
                summary = "; ".join(format_gpu(gpu) for gpu in gpus) if gpus else "no GPUs returned"
                print(f"[{now}] waiting: {summary}", flush=True)
        except FileNotFoundError:
            print("nvidia-smi was not found on PATH.", flush=True)
            return 127
        except subprocess.CalledProcessError as exc:
            error = (exc.stderr or exc.stdout or str(exc)).strip()
            if error != last_error:
                print(f"[{now}] nvidia-smi failed: {error}", flush=True)
                last_error = error
            streak = 0
        time.sleep(max(5, args.interval))


def main() -> int:
    args = parse_args()
    if args.once:
        return check_once(args)
    if args.background and not args._child:
        return launch_background(args)
    return monitor(args)


if __name__ == "__main__":
    raise SystemExit(main())
