"""Exclusive GPU task scheduling and resident-model lifecycle management.

The scheduler deliberately owns *task lifetime*, not merely submission time.
ComfyUI prompts and external voice workers therefore hold the same FIFO lease
until their heavy GPU work reaches a terminal state.  This prevents a second
backend from observing an apparently idle submitter while the GPU is still
busy in another process.
"""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


TaskRunner = Callable[[threading.Event, Callable[[str, float | None], None]], Any]
ReleaseResident = Callable[[], None]


@dataclass(slots=True)
class _Task:
    id: str
    owner: str
    backend: str
    model_key: str
    runner: TaskRunner
    created_at: float
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    status: str = "queued"
    stage: str = "waiting_for_gpu"
    progress: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: str | None = None


class GpuResourceManager:
    """FIFO scheduler for one physical GPU and every heavy backend using it."""

    def __init__(
        self,
        device_index: int = 0,
        *,
        idle_release_seconds: int = 180,
        clock: Callable[[], float] = time.monotonic,
        memory_probe: Callable[[int], dict[str, Any]] | None = None,
    ) -> None:
        self.device_index = device_index
        self.idle_release_seconds = max(0, idle_release_seconds)
        self._clock = clock
        self._memory_probe = memory_probe or self._probe_nvidia_smi
        self._condition = threading.Condition(threading.RLock())
        self._queue: deque[_Task] = deque()
        self._tasks: dict[str, _Task] = {}
        self._active: _Task | None = None
        self._resident_backend: str | None = None
        self._resident_model: str | None = None
        self._resident_since: float | None = None
        self._last_used_at: float | None = None
        self._release_hooks: dict[str, ReleaseResident] = {}
        self._stop = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"h3-gpu-resource-{device_index}",
            daemon=True,
        )
        self._thread.start()

    def register_backend(self, backend: str, release: ReleaseResident) -> None:
        if not backend or not callable(release):
            raise ValueError("backend and release callback are required")
        with self._condition:
            self._release_hooks[backend] = release

    def submit(self, owner: str, backend: str, model_key: str, runner: TaskRunner) -> str:
        if not owner or not backend or not model_key:
            raise ValueError("owner, backend, and model_key are required")
        task = _Task(
            id=uuid.uuid4().hex,
            owner=owner,
            backend=backend,
            model_key=model_key,
            runner=runner,
            created_at=time.time(),
        )
        with self._condition:
            if self._stop:
                raise RuntimeError("GPU resource manager is stopped")
            self._tasks[task.id] = task
            self._queue.append(task)
            self._condition.notify_all()
        return task.id

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status in {"completed", "failed", "canceled"}:
                return self._public_locked(task)
            task.cancel_event.set()
            if task.status == "queued":
                try:
                    self._queue.remove(task)
                except ValueError:
                    pass
                task.status = "canceled"
                task.stage = "canceled_before_start"
                task.finished_at = time.time()
                task.done_event.set()
            else:
                task.stage = "cancelling"
            self._condition.notify_all()
            return self._public_locked(task)

    def get(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            queue_position = None
            if task.status == "queued":
                try:
                    queue_position = list(self._queue).index(task) + 1
                except ValueError:
                    # The scheduler may have dequeued the task immediately
                    # before changing it to running.  The next read resolves
                    # the transient state without inventing a queue position.
                    pass
            return self._public_locked(task, queue_position=queue_position)

    def wait(self, task_id: str, timeout: float | None = None) -> dict[str, Any]:
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
        task.done_event.wait(timeout)
        return self.get(task_id)

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            active = self._public_locked(self._active) if self._active else None
            queued = [self._public_locked(task, queue_position=index + 1) for index, task in enumerate(self._queue)]
            value = {
                "device_index": self.device_index,
                "exclusive": True,
                "policy": "fifo_no_preemption",
                "resident": {
                    "backend": self._resident_backend,
                    "model_key": self._resident_model,
                    "since": self._resident_since,
                    "last_used_at": self._last_used_at,
                } if self._resident_backend else None,
                "active": active,
                "queue": queued,
                "queue_length": len(queued),
            }
        try:
            value["memory"] = self._memory_probe(self.device_index)
        except Exception as error:  # memory telemetry must never break scheduling
            value["memory"] = {"available": False, "reason": str(error)}
        return value

    def stop(self, timeout: float = 5) -> None:
        with self._condition:
            if self._stop:
                return
            self._stop = True
            for task in self._queue:
                task.cancel_event.set()
                task.status = "canceled"
                task.stage = "server_stopping"
                task.finished_at = time.time()
                task.done_event.set()
            self._queue.clear()
            if self._active:
                self._active.cancel_event.set()
                self._active.stage = "server_stopping"
            self._condition.notify_all()
        self._thread.join(timeout=max(0, timeout))
        self.release_resident()

    def release_resident(self) -> bool:
        with self._condition:
            if self._active is not None or self._resident_backend is None:
                return False
            backend = self._resident_backend
        self._release_backend(backend)
        with self._condition:
            if self._active is None and self._resident_backend == backend:
                self._clear_resident_locked()
        return True

    def _run(self) -> None:
        while True:
            task: _Task | None = None
            release_backend: str | None = None
            with self._condition:
                while not self._stop and not self._queue:
                    wait_seconds: float | None = None
                    if self._resident_backend and self.idle_release_seconds > 0 and self._last_used_at is not None:
                        wait_seconds = max(0.01, self.idle_release_seconds - (self._clock() - self._last_used_at))
                        if wait_seconds <= 0.01:
                            release_backend = self._resident_backend
                            break
                    self._condition.wait(timeout=wait_seconds)
                if self._stop:
                    break
                if release_backend is None and self._queue:
                    task = self._queue.popleft()
                    if task.cancel_event.is_set():
                        task.status = "canceled"
                        task.stage = "canceled_before_start"
                        task.finished_at = time.time()
                        task.done_event.set()
                        continue
                    if self._resident_backend and (
                        self._resident_backend != task.backend or self._resident_model != task.model_key
                    ):
                        release_backend = self._resident_backend
                    self._active = task
                    task.status = "running"
                    task.stage = "switching_model" if release_backend else "starting"
                    task.started_at = time.time()

            if release_backend is not None:
                try:
                    self._release_backend(release_backend)
                except Exception as error:
                    if task is not None:
                        self._finish_failed(task, f"failed to release resident backend {release_backend}: {error}")
                    continue
                with self._condition:
                    if self._resident_backend == release_backend:
                        self._clear_resident_locked()
                if task is None:
                    continue

            if task is None:
                continue
            self._execute(task)

        with self._condition:
            backend = self._resident_backend
        if backend:
            try:
                self._release_backend(backend)
            finally:
                with self._condition:
                    self._clear_resident_locked()

    def _execute(self, task: _Task) -> None:
        def update(stage: str, progress: float | None = None) -> None:
            with self._condition:
                if task.status != "running":
                    return
                task.stage = str(stage)[:128]
                if progress is not None:
                    task.progress = max(0.0, min(1.0, float(progress)))

        try:
            if task.cancel_event.is_set():
                raise _Canceled()
            result = task.runner(task.cancel_event, update)
            if task.cancel_event.is_set():
                raise _Canceled()
        except _Canceled:
            self._finish(task, "canceled", "canceled", result=None)
            self._release_after_abnormal(task.backend)
        except Exception as error:
            if task.cancel_event.is_set():
                self._finish(task, "canceled", "canceled", result=None)
            else:
                self._finish_failed(task, str(error))
            self._release_after_abnormal(task.backend)
        else:
            with self._condition:
                now = self._clock()
                if self._resident_backend != task.backend or self._resident_model != task.model_key:
                    self._resident_backend = task.backend
                    self._resident_model = task.model_key
                    self._resident_since = time.time()
                self._last_used_at = now
            self._finish(task, "completed", "completed", result=result)

    def _finish_failed(self, task: _Task, error: str) -> None:
        self._finish(task, "failed", "failed", error=error)

    def _finish(self, task: _Task, status: str, stage: str, *, result: Any = None, error: str | None = None) -> None:
        with self._condition:
            task.status = status
            task.stage = stage
            task.progress = 1.0 if status == "completed" else task.progress
            task.result = result
            task.error = error
            task.finished_at = time.time()
            if self._active is task:
                self._active = None
            task.done_event.set()
            self._condition.notify_all()

    def _release_after_abnormal(self, backend: str) -> None:
        try:
            self._release_backend(backend)
        except Exception:
            pass
        with self._condition:
            if self._resident_backend == backend:
                self._clear_resident_locked()

    def _release_backend(self, backend: str) -> None:
        callback = self._release_hooks.get(backend)
        if callback is not None:
            callback()

    def _clear_resident_locked(self) -> None:
        self._resident_backend = None
        self._resident_model = None
        self._resident_since = None
        self._last_used_at = None

    def _public_locked(self, task: _Task | None, *, queue_position: int | None = None) -> dict[str, Any]:
        if task is None:
            return {}
        reason = None
        if task.status == "queued":
            if queue_position and queue_position > 1:
                reason = "waiting_behind_queued_task"
            elif self._active:
                reason = f"waiting_for_{self._active.backend}_task"
            elif self._resident_backend and (
                self._resident_backend != task.backend or self._resident_model != task.model_key
            ):
                reason = "waiting_for_model_release"
            else:
                reason = "waiting_for_gpu"
        return {
            "id": task.id,
            "owner": task.owner,
            "backend": task.backend,
            "model_key": task.model_key,
            "status": task.status,
            "stage": task.stage,
            "progress": task.progress,
            "queue_position": queue_position,
            "queue_reason": reason,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "error": task.error,
            "result": task.result if task.status == "completed" else None,
        }

    @staticmethod
    def _probe_nvidia_smi(device_index: int) -> dict[str, Any]:
        command = [
            "nvidia-smi",
            f"--id={device_index}",
            "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=3, check=True)
        fields = [value.strip() for value in completed.stdout.strip().split(",")]
        if len(fields) != 5:
            raise RuntimeError("nvidia-smi returned an unexpected response")
        return {
            "available": True,
            "name": fields[0],
            "total_mib": int(fields[1]),
            "used_mib": int(fields[2]),
            "free_mib": int(fields[3]),
            "utilization_percent": int(fields[4]),
        }


class _Canceled(Exception):
    pass
