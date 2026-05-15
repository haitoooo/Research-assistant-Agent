---
name: gpu-monitor
description: Start event-driven GPU monitors for ML/deep-learning work. Use when the user asks to wait for a GPU to become idle, says a GPU is free and wants you to train, asks to start a training command once a GPU changes from busy to idle, or wants a WeChat reminder when their GPU task/training process finishes. Prefer the gpu_monitor tool over cron so the model is called only when a GPU becomes idle or a watched task finishes.
metadata: {"nanobot":{"os":["darwin","linux","windows"],"requires":{"bins":["python"]}}}
---

# GPU Monitor

Use this skill for GPU availability and training completion workflows.

The important rule: do not create a recurring cron job for GPU checks. Use the `gpu_monitor` tool. It runs a Python background service that periodically polls `nvidia-smi`, writes a local `gpu_status` JSON state file, and only wakes the agent when there is a real event:

- a GPU changes from `has_program=true` to `has_program=false`;
- a watched GPU process leaves the GPU;
- my GPU task changes from present to finished;
- a training command launched by the monitor exits;
- `nvidia-smi` fails in a way the user needs to fix.

This avoids periodic model calls. Polling is done by Python; the model is called only by the gateway event callback when notification text needs to be delivered.

## Local GPU Status

The monitor maintains `<workspace>/gpu-monitor/gpu_status`. Treat this as the source of truth for GPU decisions. Each GPU entry records:

- whether the GPU has any program: `has_program`;
- whether it has one of my programs: `has_my_program`;
- process details: PID, process name, owner when available, used GPU memory, and whether it is tracked by nanobot.

The trigger condition is a state transition, not a static resource rule. A GPU-free notification happens only when a GPU's previous `gpu_status` entry had `has_program=true` and the current entry has `has_program=false`. A GPU that was already idle at baseline should not trigger a notification.

## GPU Selection

Do not use filters. Do not pass GPU index, memory, or utilization filters. GPU-free monitoring always watches all GPUs and triggers only when any GPU changes from busy to idle.

## Start Training When GPU Is Free

When the user wants you to start a training command once resources are available, call:

```text
gpu_monitor(
  action="start",
  name="train-<short-label>",
  command="<training command>",
  interval_seconds=60,
  consecutive=2
)
```

The monitor starts the command only after any GPU changes from busy to idle. It sets `CUDA_VISIBLE_DEVICES` to that GPU if the command does not already set it. It notifies the original chat when the command starts and again when it exits.

## Remind When My Task Finishes

When the user says their task is running and wants a reminder after it finishes, monitor the GPU process ID if available:

```text
gpu_monitor(
  action="start",
  name="task-finish-<short-label>",
  watch_pids="12345",
  interval_seconds=60
)
```

If the PID is unknown but the user means all currently running tasks owned by the current OS user, snapshot them from `gpu_status`:

```text
gpu_monitor(
  action="start",
  name="my-task-finish-<short-label>",
  watch_my_processes=true,
  interval_seconds=60
)
```

If ownership cannot be determined and multiple candidates exist, inspect `nvidia-smi` with the exec tool and ask for the PID instead of guessing.

## Wait For GPU Only

When the user only wants a reminder that a GPU is free:

```text
gpu_monitor(
  action="start",
  name="gpu-free-<short-label>",
  interval_seconds=60
)
```

The monitor will not repeatedly notify for the same still-free GPU; it only reports a busy-to-idle transition.

## Manage Monitors

List monitors:

```text
gpu_monitor(action="list")
```

Remove a monitor:

```text
gpu_monitor(action="remove", job_id="<id>")
```

Use `scripts/watch_gpu.py` only for manual/local terminal checks. For nanobot user notification, prefer the `gpu_monitor` tool because it preserves the original WeChat/session target and avoids periodic agent turns.
