"""Durable background task receipts for cancellable media preprocessing."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import ApiError
from .media import MediaService
from .security import validate_id
from .storage import JsonStore


class MediaTaskManager:
    def __init__(self, data_root: Path, media: MediaService) -> None:
        self.media = media
        self.store = JsonStore(data_root / "metadata" / "media-tasks")
        self._lock = threading.RLock()
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._recover_interrupted()

    def _recover_interrupted(self) -> None:
        for task in self.store.list():
            if task.get("status") not in {"queued", "running", "cancelling"}:
                continue
            task.update({
                "status": "failed",
                "progress": int(task.get("progress", 0) or 0),
                "error": {
                    "code": "media_task_interrupted",
                    "message": "server restarted before media preprocessing completed; retry the operation",
                    "retryable": True,
                },
                "updated_at": time.time(),
            })
            self.store.put(str(task["id"]), task)

    def submit(
        self,
        source: Path,
        source_meta: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        now = time.time()
        task = {
            "id": task_id,
            "status": "queued",
            "progress": 0,
            "operation": request.get("operation"),
            "source": source_meta.get("source_receipt", {}),
            "created_at": now,
            "updated_at": now,
        }
        cancel_event = threading.Event()
        with self._lock:
            self.store.put(task_id, task)
            self._cancel[task_id] = cancel_event
            worker = threading.Thread(
                target=self._run,
                args=(task_id, source, dict(source_meta), dict(request), cancel_event),
                name=f"h3-media-task-{task_id[:8]}",
                daemon=True,
            )
            self._threads[task_id] = worker
            worker.start()
        return self.public(task)

    def _run(
        self,
        task_id: str,
        source: Path,
        source_meta: dict[str, Any],
        request: dict[str, Any],
        cancel_event: threading.Event,
    ) -> None:
        self._update(task_id, status="running", progress=1)

        def on_progress(value: int) -> None:
            self._update(task_id, progress=max(1, min(99, int(value))))

        try:
            receipt = self.media.derive(
                source, source_meta, request,
                progress=on_progress, cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                # A reused receipt predates this task and may already be bound
                # elsewhere; cancellation must never delete shared output.
                if receipt.get("reused") is not True:
                    try:
                        self.media.delete(str(receipt["id"]))
                    except ApiError:
                        pass
                raise ApiError(409, "cancelled", "reference preprocessing was cancelled")
            self._update(
                task_id,
                status="completed",
                progress=100,
                receipt_id=receipt["id"],
            )
        except ApiError as error:
            canceled = error.code in {"cancelled", "canceled"} or cancel_event.is_set()
            self._update(
                task_id,
                status="canceled" if canceled else "failed",
                error={
                    "code": "cancelled" if canceled else error.code,
                    "message": "reference preprocessing was cancelled" if canceled else error.message,
                    "retryable": not canceled and error.status >= 500,
                    **({"details": error.details} if error.details is not None else {}),
                },
            )
        except Exception:
            self._update(
                task_id,
                status="failed",
                error={
                    "code": "media_processing_failed",
                    "message": "reference preprocessing failed",
                    "retryable": True,
                },
            )
        finally:
            with self._lock:
                self._cancel.pop(task_id, None)
                self._threads.pop(task_id, None)

    def _update(self, task_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            task = self.store.get(task_id)
            task.update(changes)
            task["updated_at"] = time.time()
            self.store.put(task_id, task)
            return task

    def get(self, task_id: str) -> dict[str, Any]:
        return self.public(self.store.get(validate_id(task_id, "media task id")))

    def cancel(self, task_id: str) -> dict[str, Any]:
        task_id = validate_id(task_id, "media task id")
        with self._lock:
            task = self.store.get(task_id)
            if task.get("status") in {"completed", "failed", "canceled"}:
                return self.public(task)
            event = self._cancel.get(task_id)
            if event is not None:
                event.set()
            task.update({"status": "cancelling", "updated_at": time.time()})
            self.store.put(task_id, task)
            return self.public(task)

    def stop(self) -> None:
        with self._lock:
            events = list(self._cancel.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + 3
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))

    def public(self, task: dict[str, Any]) -> dict[str, Any]:
        value = {
            key: task[key]
            for key in (
                "id", "status", "progress", "operation", "source", "created_at",
                "updated_at", "receipt_id", "error",
            )
            if key in task
        }
        task_id = str(task["id"])
        value.update({
            "task_id": task_id,
            "status_url": f"/api/media-tasks/{task_id}",
            "cancel_url": f"/api/media-tasks/{task_id}/cancel",
        })
        if task.get("status") == "completed" and isinstance(task.get("receipt_id"), str):
            try:
                value["receipt"] = self.media.public(self.media.get(str(task["receipt_id"])))
            except ApiError:
                value.update({
                    "status": "failed",
                    "error": {
                        "code": "media_result_missing",
                        "message": "completed media task result is unavailable",
                        "retryable": True,
                    },
                })
        return value
