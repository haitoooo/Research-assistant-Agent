"""Event-driven GPU monitor service.

The service polls ``nvidia-smi`` in the background, but it only calls the
configured event callback when a state transition occurs: a matching GPU becomes
available, a watched GPU process leaves the GPU, or a launched command exits.
"""

from __future__ import annotations

import asyncio
import csv
import getpass
import json
import os
import signal
import subprocess
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

GPU_QUERY = [
    "index",
    "uuid",
    "name",
    "memory.total",
    "memory.used",
    "memory.free",
    "utilization.gpu",
]

COMPUTE_QUERY = [
    "pid",
    "process_name",
    "used_gpu_memory",
    "gpu_uuid",
]


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class GpuMonitorState:
    """Mutable monitor state persisted with each job."""

    first_check_done: bool = False
    availability_streaks: dict[str, int] = field(default_factory=dict)
    available_indices: list[int] = field(default_factory=list)
    live_pids: list[int] = field(default_factory=list)
    process_snapshot_done: bool = False
    command_started: bool = False
    command_pid: int | None = None
    command_returncode: int | None = None
    command_log: str = ""
    last_error: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GpuMonitorState":
        data = data or {}
        return cls(
            first_check_done=bool(data.get("first_check_done", False)),
            availability_streaks={str(k): int(v) for k, v in data.get("availability_streaks", {}).items()},
            available_indices=[int(x) for x in data.get("available_indices", [])],
            live_pids=[int(x) for x in data.get("live_pids", [])],
            process_snapshot_done=bool(data.get("process_snapshot_done", False)),
            command_started=bool(data.get("command_started", False)),
            command_pid=data.get("command_pid"),
            command_returncode=data.get("command_returncode"),
            command_log=str(data.get("command_log") or ""),
            last_error=str(data.get("last_error") or ""),
        )


@dataclass
class GpuMonitorJob:
    """A GPU monitor job registered by the agent."""

    id: str
    name: str
    watch_pids: list[int] = field(default_factory=list)
    watch_my_processes: bool = False
    command: str = ""
    interval_s: int = 60
    consecutive: int = 2
    notify_initial_available: bool = True
    enabled: bool = True
    channel: str = ""
    chat_id: str = ""
    channel_meta: dict[str, Any] = field(default_factory=dict)
    session_key: str | None = None
    created_at_ms: int = field(default_factory=_now_ms)
    updated_at_ms: int = field(default_factory=_now_ms)
    next_check_ms: int = field(default_factory=_now_ms)
    state: GpuMonitorState = field(default_factory=GpuMonitorState)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GpuMonitorJob":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            watch_pids=[int(x) for x in data.get("watch_pids", [])],
            watch_my_processes=bool(data.get("watch_my_processes", False)),
            command=str(data.get("command") or ""),
            interval_s=max(5, int(data.get("interval_s", 60))),
            consecutive=max(1, int(data.get("consecutive", 2))),
            notify_initial_available=bool(data.get("notify_initial_available", True)),
            enabled=bool(data.get("enabled", True)),
            channel=str(data.get("channel") or ""),
            chat_id=str(data.get("chat_id") or ""),
            channel_meta=dict(data.get("channel_meta") or {}),
            session_key=data.get("session_key"),
            created_at_ms=int(data.get("created_at_ms") or _now_ms()),
            updated_at_ms=int(data.get("updated_at_ms") or _now_ms()),
            next_check_ms=int(data.get("next_check_ms") or _now_ms()),
            state=GpuMonitorState.from_dict(data.get("state")),
        )


@dataclass
class GpuMonitorEvent:
    """State transition emitted by :class:`GpuMonitorService`."""

    kind: str
    job: GpuMonitorJob
    gpu: dict[str, Any] | None = None
    pid: int | None = None
    process: dict[str, Any] | None = None
    returncode: int | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "job": {
                "id": self.job.id,
                "name": self.job.name,
                "watch_pids": self.job.watch_pids,
                "watch_my_processes": self.job.watch_my_processes,
                "command": self.job.command,
                "command_pid": self.job.state.command_pid,
                "command_log": self.job.state.command_log,
            },
            "gpu": self.gpu,
            "pid": self.pid,
            "process": self.process,
            "returncode": self.returncode,
            "error": self.error,
        }

    def default_message(self) -> str:
        if self.kind == "gpu_available" and self.gpu:
            return f"GPU {self.gpu['index']} is available: {format_gpu(self.gpu)}"
        if self.kind == "command_started":
            return f"GPU is available and training command started for {self.job.name}."
        if self.kind == "command_finished":
            return f"Training command finished for {self.job.name} with exit code {self.returncode}."
        if self.kind == "process_finished" and self.pid:
            return f"GPU process {self.pid} finished for {self.job.name}."
        if self.kind == "error":
            return f"GPU monitor error for {self.job.name}: {self.error}"
        return f"GPU monitor event for {self.job.name}: {self.kind}"


def query_gpus() -> list[dict[str, Any]]:
    """Return current GPU state from ``nvidia-smi``."""

    result = subprocess.run(
        [
            "nvidia-smi",
            f"--query-gpu={','.join(GPU_QUERY)}",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    rows: list[dict[str, Any]] = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) != len(GPU_QUERY):
            continue
        clean = [part.strip() for part in row]
        rows.append(
            {
                "index": int(clean[0]),
                "uuid": clean[1],
                "name": clean[2],
                "memory_total": int(clean[3]),
                "memory_used": int(clean[4]),
                "memory_free": int(clean[5]),
                "utilization": int(clean[6]),
            }
        )
    return rows


def query_compute_processes() -> list[dict[str, Any]]:
    """Return current GPU compute processes from ``nvidia-smi``."""

    result = subprocess.run(
        [
            "nvidia-smi",
            f"--query-compute-apps={','.join(COMPUTE_QUERY)}",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    rows: list[dict[str, Any]] = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) != len(COMPUTE_QUERY):
            continue
        clean = [part.strip() for part in row]
        rows.append(
            {
                "pid": int(clean[0]),
                "process_name": clean[1],
                "used_gpu_memory": _parse_used_gpu_memory(clean[2]),
                "gpu_uuid": clean[3],
            }
        )
    return annotate_process_owners(rows)


def _parse_used_gpu_memory(value: str) -> int | None:
    text = value.strip()
    if not text or text.upper() == "[N/A]":
        return None
    with suppress(ValueError):
        return int(text)
    return None


def idle_gpus(gpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return GPUs with no running program in gpu_status."""

    return [gpu for gpu in gpus if not bool(gpu.get("has_program"))]


def build_gpu_status(
    gpus: list[dict[str, Any]],
    processes: list[dict[str, Any]],
    tracked_pids: set[int] | None = None,
) -> dict[str, Any]:
    """Build the persisted per-GPU status snapshot used for decisions."""

    tracked_pids = tracked_pids or set()
    current_user = _normalize_user(getpass.getuser())
    by_uuid: dict[str, list[dict[str, Any]]] = {}
    for process in processes:
        pid = int(process["pid"])
        owner = str(process.get("owner") or "")
        is_current_user = bool(owner) and _normalize_user(owner) == current_user
        is_tracked = pid in tracked_pids
        enriched = {
            **process,
            "owner": owner,
            "is_current_user": is_current_user,
            "is_tracked": is_tracked,
            "is_my_program": bool(is_current_user or is_tracked),
        }
        by_uuid.setdefault(str(process.get("gpu_uuid") or ""), []).append(enriched)

    gpu_items = []
    for gpu in gpus:
        gpu_processes = by_uuid.get(str(gpu.get("uuid") or ""), [])
        gpu_items.append(
            {
                **gpu,
                "has_program": bool(gpu_processes),
                "has_my_program": any(bool(proc.get("is_my_program")) for proc in gpu_processes),
                "processes": gpu_processes,
            }
        )

    return {
        "checked_at_ms": _now_ms(),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "current_user": getpass.getuser(),
        "gpus": gpu_items,
    }


def _gpu_status_by_index(gpu_status: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    if not gpu_status:
        return {}
    indexed: dict[int, dict[str, Any]] = {}
    for gpu in gpu_status.get("gpus", []):
        with suppress(ValueError, TypeError):
            indexed[int(gpu["index"])] = gpu
    return indexed


def annotate_process_owners(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    owners = query_process_owners([int(proc["pid"]) for proc in processes])
    for process in processes:
        process["owner"] = owners.get(int(process["pid"]), "")
    return processes


def query_process_owners(pids: list[int]) -> dict[int, str]:
    if not pids:
        return {}
    if os.name == "nt":
        return _query_process_owners_windows(pids)
    return _query_process_owners_posix(pids)


def _query_process_owners_posix(pids: list[int]) -> dict[int, str]:
    try:
        completed = subprocess.run(
            ["ps", "-o", "pid=", "-o", "user=", "-p", ",".join(str(pid) for pid in pids)],
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception:
        return {}
    owners: dict[int, str] = {}
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            with suppress(ValueError):
                owners[int(parts[0])] = parts[1].strip()
    return owners


def _query_process_owners_windows(pids: list[int]) -> dict[int, str]:
    owners: dict[int, str] = {}
    for pid in pids:
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\";"
            "if ($p) { $o=$p.GetOwner(); if ($o.User) { Write-Output \"$($p.ProcessId) $($o.User)\" } }"
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                text=True,
                capture_output=True,
                check=False,
            )
        except Exception:
            continue
        parts = completed.stdout.strip().split(None, 1)
        if len(parts) == 2:
            with suppress(ValueError):
                owners[int(parts[0])] = parts[1].strip()
    return owners


def _normalize_user(value: str) -> str:
    return value.replace("\\", "/").split("/")[-1].lower()


def format_gpu(gpu: dict[str, Any]) -> str:
    return (
        f"GPU {gpu['index']} {gpu['name']}: "
        f"free={gpu['memory_free']} MiB, used={gpu['memory_used']} MiB, "
        f"total={gpu['memory_total']} MiB, util={gpu['utilization']}%"
    )


class GpuMonitorService:
    """Background GPU monitor that emits events only on relevant transitions."""

    def __init__(
        self,
        store_path: Path,
        on_event: Callable[[GpuMonitorEvent], Awaitable[None]] | None = None,
    ):
        self.store_path = store_path
        self.status_path = store_path.parent / "gpu_status"
        self.on_event = on_event
        self._jobs: dict[str, GpuMonitorJob] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._processes: dict[str, subprocess.Popen] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._load()
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("GPU monitor started with {} job(s)", len(self._jobs))

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def add_job(
        self,
        *,
        name: str,
        watch_pids: list[int] | None = None,
        watch_my_processes: bool = False,
        command: str = "",
        interval_s: int = 60,
        consecutive: int = 2,
        notify_initial_available: bool = True,
        channel: str = "",
        chat_id: str = "",
        channel_meta: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> GpuMonitorJob:
        watch_pids = watch_pids or []
        monitor_availability = command or not watch_pids and not watch_my_processes
        if not monitor_availability and not watch_pids and not watch_my_processes:
            raise ValueError("pass watch_pids and/or watch_my_processes")
        now = _now_ms()
        job = GpuMonitorJob(
            id=uuid.uuid4().hex[:8],
            name=name[:80] or "gpu-monitor",
            watch_pids=watch_pids,
            watch_my_processes=watch_my_processes,
            command=command,
            interval_s=max(5, interval_s),
            consecutive=max(1, consecutive),
            notify_initial_available=notify_initial_available,
            channel=channel,
            chat_id=chat_id,
            channel_meta=channel_meta or {},
            session_key=session_key,
            created_at_ms=now,
            updated_at_ms=now,
            next_check_ms=now,
            state=GpuMonitorState(live_pids=list(watch_pids)),
        )
        self._jobs[job.id] = job
        self._save()
        logger.info("GPU monitor added job '{}' ({})", job.name, job.id)
        return job

    def remove_job(self, job_id: str) -> bool:
        job = self._jobs.pop(job_id, None)
        if not job:
            return False
        self._processes.pop(job_id, None)
        self._save()
        logger.info("GPU monitor removed job {}", job_id)
        return True

    def list_jobs(self) -> list[GpuMonitorJob]:
        if not self._running:
            self._load()
        return sorted(self._jobs.values(), key=lambda job: job.created_at_ms)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("GPU monitor tick failed")
            await asyncio.sleep(5)

    async def _tick(self) -> None:
        now = _now_ms()
        due = [job for job in self._jobs.values() if job.enabled and job.next_check_ms <= now]
        if not due:
            return

        gpus: list[dict[str, Any]] = []
        processes: list[dict[str, Any]] = []
        previous_gpu_status = self._read_gpu_status()
        gpu_status: dict[str, Any] = {"gpus": []}
        gpu_error = ""

        try:
            gpus = query_gpus()
        except FileNotFoundError:
            gpu_error = "nvidia-smi was not found on PATH"
        except subprocess.CalledProcessError as exc:
            gpu_error = (exc.stderr or exc.stdout or str(exc)).strip()

        if not gpu_error:
            try:
                processes = query_compute_processes()
            except FileNotFoundError:
                gpu_error = "nvidia-smi was not found on PATH"
            except subprocess.CalledProcessError as exc:
                if any(job.watch_pids or job.watch_my_processes for job in due):
                    gpu_error = (exc.stderr or exc.stdout or str(exc)).strip()
                else:
                    processes = []

        if not gpu_error:
            gpu_status = build_gpu_status(gpus, processes, self._tracked_pids())
            self._write_gpu_status(gpu_status)
            gpus = list(gpu_status.get("gpus", []))

        changed = False
        for job in due:
            events = self._check_job(job, previous_gpu_status, gpu_status, gpu_error)
            job.next_check_ms = _now_ms() + job.interval_s * 1000
            job.updated_at_ms = _now_ms()
            changed = True
            for event in events:
                await self._emit(event)
        if changed:
            self._save()

    def _check_job(
        self,
        job: GpuMonitorJob,
        previous_gpu_status: dict[str, Any] | None,
        gpu_status: dict[str, Any],
        gpu_error: str,
    ) -> list[GpuMonitorEvent]:
        events: list[GpuMonitorEvent] = []

        if gpu_error:
            if gpu_error != job.state.last_error:
                job.state.last_error = gpu_error
                events.append(GpuMonitorEvent(kind="error", job=job, error=gpu_error))
            return events
        if not gpu_error:
            job.state.last_error = ""

        gpus = list(gpu_status.get("gpus", []))
        if self._monitors_availability(job) and not (job.command and job.state.command_started):
            events.extend(self._check_availability(job, previous_gpu_status, gpus))

        if job.watch_pids or job.watch_my_processes:
            events.extend(self._check_watched_processes(job, gpu_status))

        if job.state.command_started:
            events.extend(self._check_command(job))

        if job.state.command_started and job.state.command_returncode is not None:
            job.enabled = False
        elif (job.watch_pids or job.watch_my_processes) and not job.state.live_pids and not job.command:
            job.enabled = False

        return events

    @staticmethod
    def _monitors_availability(job: GpuMonitorJob) -> bool:
        return bool(job.command or not (job.watch_pids or job.watch_my_processes))

    def _check_availability(
        self,
        job: GpuMonitorJob,
        previous_gpu_status: dict[str, Any] | None,
        gpus: list[dict[str, Any]],
    ) -> list[GpuMonitorEvent]:
        events: list[GpuMonitorEvent] = []
        previous_by_index = _gpu_status_by_index(previous_gpu_status)
        current_matches = {
            int(gpu["index"]): gpu
            for gpu in idle_gpus(gpus)
        }

        if not job.state.first_check_done:
            job.state.first_check_done = True
            job.state.available_indices = sorted(current_matches)
            if previous_gpu_status is None:
                return events

        transitioned_indices = []
        for index, gpu in current_matches.items():
            previous_gpu = previous_by_index.get(index)
            if previous_gpu and bool(previous_gpu.get("has_program")) and not bool(gpu.get("has_program")):
                transitioned_indices.append(index)

        if not transitioned_indices:
            job.state.available_indices = sorted(current_matches)
            return events

        job.state.available_indices = sorted(current_matches)
        best = max(
            (current_matches[index] for index in transitioned_indices),
            key=lambda gpu: int(gpu["memory_free"]),
        )
        if job.command and not job.state.command_started:
            self._start_command(job, best)
            events.append(GpuMonitorEvent(kind="command_started", job=job, gpu=best))
        else:
            events.append(GpuMonitorEvent(kind="gpu_available", job=job, gpu=best))
        return events

    def _check_watched_processes(
        self,
        job: GpuMonitorJob,
        gpu_status: dict[str, Any],
    ) -> list[GpuMonitorEvent]:
        events: list[GpuMonitorEvent] = []
        gpu_pids = {
            int(proc["pid"]): proc
            for gpu in gpu_status.get("gpus", [])
            for proc in gpu.get("processes", [])
        }
        if job.watch_my_processes and not job.state.process_snapshot_done:
            job.state.process_snapshot_done = True
            job.state.live_pids = sorted(
                int(pid) for pid, proc in gpu_pids.items() if proc.get("is_my_program")
            )
            if not job.state.live_pids:
                events.append(
                    GpuMonitorEvent(
                        kind="error",
                        job=job,
                        error="no current-user GPU process found in gpu_status",
                    )
                )
            return events

        live: list[int] = []
        for pid in job.state.live_pids or job.watch_pids:
            if pid in gpu_pids:
                live.append(pid)
            else:
                events.append(
                    GpuMonitorEvent(
                        kind="process_finished",
                        job=job,
                        pid=pid,
                        process=gpu_pids.get(pid),
                    )
                )
        job.state.live_pids = live
        return events

    def _tracked_pids(self) -> set[int]:
        tracked: set[int] = set()
        for job in self._jobs.values():
            tracked.update(int(pid) for pid in job.watch_pids)
            tracked.update(int(pid) for pid in job.state.live_pids)
            if job.state.command_pid:
                tracked.add(int(job.state.command_pid))
        for process in self._processes.values():
            if process.pid:
                tracked.add(int(process.pid))
        return tracked

    def _start_command(self, job: GpuMonitorJob, gpu: dict[str, Any]) -> None:
        log_dir = self.store_path.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job.id}.log"
        env = os.environ.copy()
        env.setdefault("CUDA_VISIBLE_DEVICES", str(gpu["index"]))
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            start_new_session = True

        with open(log_path, "a", encoding="utf-8") as log:
            if os.name == "nt":
                process = subprocess.Popen(
                    job.command,
                    shell=True,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                    creationflags=creationflags,
                )
            else:
                process = subprocess.Popen(
                    job.command,
                    shell=True,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=start_new_session,
                )

        self._processes[job.id] = process
        job.state.command_started = True
        job.state.command_pid = process.pid
        job.state.command_log = str(log_path)
        job.state.command_returncode = None

    def _check_command(self, job: GpuMonitorJob) -> list[GpuMonitorEvent]:
        process = self._processes.get(job.id)
        if process is not None:
            returncode = process.poll()
            if returncode is None:
                return []
            self._processes.pop(job.id, None)
            job.state.command_returncode = returncode
            return [GpuMonitorEvent(kind="command_finished", job=job, returncode=returncode)]

        pid = job.state.command_pid
        if pid and not _pid_exists(pid):
            job.state.command_returncode = -1
            return [GpuMonitorEvent(kind="command_finished", job=job, returncode=None)]
        return []

    async def _emit(self, event: GpuMonitorEvent) -> None:
        logger.info("GPU monitor event: {} job={}", event.kind, event.job.id)
        if self.on_event:
            await self.on_event(event)

    def _load(self) -> None:
        if not self.store_path.exists():
            self._jobs = {}
            return
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            jobs = [GpuMonitorJob.from_dict(item) for item in data.get("jobs", [])]
            self._jobs = {job.id: job for job in jobs}
        except Exception:
            logger.exception("Failed to load GPU monitor store {}", self.store_path)
            self._jobs = {}

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "jobs": [asdict(job) for job in self._jobs.values()],
        }
        tmp_path = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, self.store_path)

    def _write_gpu_status(self, gpu_status: dict[str, Any]) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.status_path.with_suffix(self.status_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(gpu_status, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, self.status_path)

    def _read_gpu_status(self) -> dict[str, Any] | None:
        if not self.status_path.exists():
            return None
        try:
            data = json.loads(self.status_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            logger.debug("Failed to read previous gpu_status", exc_info=True)
        return None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        if os.name == "nt":
            return _pid_exists_windows(pid)
        return False
    return True


def _pid_exists_windows(pid: int) -> bool:
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception:
        return False
    return str(pid) in completed.stdout


def terminate_process_group(pid: int) -> None:
    """Best-effort helper for callers that need to stop a launched command."""

    if os.name == "nt":
        with suppress(Exception):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        return
    with suppress(Exception):
        os.killpg(pid, signal.SIGTERM)
