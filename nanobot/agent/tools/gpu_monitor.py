"""Tool for registering event-driven GPU monitor jobs."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.gpu_monitor.service import GpuMonitorService


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema("Action to perform", enum=["start", "list", "remove"]),
        name=StringSchema("Short label for the monitor job."),
        job_id=StringSchema("Required when action='remove'."),
        watch_pids=StringSchema("Optional comma-separated process IDs to watch until they leave the GPU."),
        watch_my_processes=BooleanSchema(
            description=(
                "Snapshot current GPU processes owned by the current OS user from gpu_status, "
                "then notify when they leave the GPU."
            ),
            default=False,
        ),
        command=StringSchema(
            "Optional training command to start once a GPU changes from busy to idle."
        ),
        interval_seconds=IntegerSchema(60, description="Polling interval in seconds."),
        consecutive=IntegerSchema(
            2,
            description="Reserved compatibility field; GPU-free notifications are based on busy-to-idle state transitions.",
        ),
        notify_initial_available=BooleanSchema(
            description="Reserved compatibility field; already-idle GPUs are treated as baseline, not new events.",
            default=True,
        ),
        required=["action"],
    )
)
class GpuMonitorTool(Tool, ContextAware):
    """Register GPU availability/process-completion monitors without periodic model calls."""

    def __init__(self, service: GpuMonitorService):
        self._service = service
        self._channel: ContextVar[str] = ContextVar("gpu_monitor_channel", default="")
        self._chat_id: ContextVar[str] = ContextVar("gpu_monitor_chat_id", default="")
        self._metadata: ContextVar[dict[str, Any]] = ContextVar("gpu_monitor_metadata", default={})
        self._session_key: ContextVar[str] = ContextVar("gpu_monitor_session_key", default="")

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return isinstance(getattr(ctx, "gpu_monitor_service", None), GpuMonitorService)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(ctx.gpu_monitor_service)

    def set_context(self, ctx: RequestContext) -> None:
        self._channel.set(ctx.channel)
        self._chat_id.set(ctx.chat_id)
        self._metadata.set(dict(ctx.metadata or {}))
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")

    @property
    def name(self) -> str:
        return "gpu_monitor"

    @property
    def description(self) -> str:
        return (
            "Start/list/remove event-driven GPU monitors. The monitor polls nvidia-smi "
            "in Python, maintains gpu_status, and only wakes the agent when a GPU "
            "changes from busy to idle, a watched GPU process finishes, or a launched "
            "training command exits."
        )

    async def execute(
        self,
        action: str,
        name: str | None = None,
        job_id: str | None = None,
        watch_pids: str | None = None,
        watch_my_processes: bool = False,
        command: str | None = None,
        interval_seconds: int = 60,
        consecutive: int = 2,
        notify_initial_available: bool = True,
        **kwargs: Any,
    ) -> str:
        if action == "list":
            return self._list_jobs()
        if action == "remove":
            if not job_id:
                return "Error: job_id is required for remove"
            return f"Removed GPU monitor job {job_id}" if self._service.remove_job(job_id) else f"Job {job_id} not found"
        if action != "start":
            return f"Unknown action: {action}"

        try:
            pids = _parse_int_list(watch_pids)
        except ValueError as exc:
            return f"Error: invalid integer list ({exc})"
        try:
            job = self._service.add_job(
                name=(name or "gpu-monitor").strip(),
                watch_pids=pids,
                watch_my_processes=watch_my_processes,
                command=(command or "").strip(),
                interval_s=interval_seconds,
                consecutive=consecutive,
                notify_initial_available=notify_initial_available,
                channel=self._channel.get(),
                chat_id=self._chat_id.get(),
                channel_meta=self._metadata.get(),
                session_key=self._session_key.get() or None,
            )
        except ValueError as exc:
            return f"Error: {exc}"

        parts = [f"Started GPU monitor '{job.name}' (id: {job.id})"]
        if pids:
            parts.append(f"watching GPU pid(s): {', '.join(str(pid) for pid in pids)}")
        if job.watch_my_processes:
            parts.append("will snapshot current-user GPU processes from gpu_status")
        if job.command:
            parts.append("will start command when available; log will be stored under gpu-monitor/logs")
        return "\n".join(parts)

    def _list_jobs(self) -> str:
        jobs = self._service.list_jobs()
        if not jobs:
            return "No GPU monitor jobs."
        lines = []
        for job in jobs:
            state = "enabled" if job.enabled else "disabled"
            parts = [f"- {job.name} (id: {job.id}, {state}, every {job.interval_s}s)"]
            if job.state.available_indices:
                parts.append(f"  Available GPUs already notified: {job.state.available_indices}")
            if job.watch_pids:
                live = job.state.live_pids or []
                parts.append(f"  Watching pids: {job.watch_pids}; still on GPU: {live}")
            if job.watch_my_processes:
                parts.append(f"  Watching current-user GPU processes; still on GPU: {job.state.live_pids}")
            if job.state.command_pid:
                parts.append(f"  Command pid: {job.state.command_pid}; log: {job.state.command_log}")
            lines.append("\n".join(parts))
        return "GPU monitor jobs:\n" + "\n".join(lines)


def _parse_int_list(value: str | None) -> list[int]:
    if not value:
        return []
    parsed = []
    for part in value.replace(";", ",").split(","):
        text = part.strip()
        if not text:
            continue
        parsed.append(int(text))
    return parsed
