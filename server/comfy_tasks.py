"""Bridge durable H3 jobs to the exclusive GPU scheduler."""

from __future__ import annotations

import threading
import time
import hashlib
import json
from typing import Any

from .comfy import ComfyClient
from .errors import ApiError
from .gpu_resources import GpuResourceManager
from .storage import JobStore


class ComfyTaskCoordinator:
    def __init__(self, jobs: JobStore, comfy: ComfyClient, resources: GpuResourceManager, *, poll_seconds: int = 2) -> None:
        self.jobs = jobs
        self.comfy = comfy
        self.resources = resources
        self.poll_seconds = max(1, poll_seconds)
        self._lock = threading.RLock()
        self._canceled_prompts: set[str] = set()
        self._wake_events: dict[str, threading.Event] = {}
        self._observed_terminal: dict[str, str] = {}

    def _cancel_prompt(self, prompt_id: str) -> None:
        with self._lock:
            if prompt_id in self._canceled_prompts:
                return
            # Keep the lock through the short control request: concurrent API
            # and scheduler cancellation must not both reach ComfyUI.  Add the
            # receipt only after success so a transient failure remains
            # retryable by long-project recovery.
            self.comfy.cancel(prompt_id)
            self._canceled_prompts.add(prompt_id)

    def schedule(self, job_id: str, workflow: dict[str, Any]) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        key_builder = getattr(self.comfy, "workflow_resource_key", None)
        model_key = key_builder(workflow) if callable(key_builder) else "comfy:" + hashlib.sha256(
            json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        def run(cancel: threading.Event, update) -> dict[str, Any]:
            prompt_id: str | None = None
            try:
                if cancel.is_set():
                    raise ApiError(409, "generation_canceled", "generation was canceled before submission")
                update("submitting_to_comfy", 0.02)
                prompt_id = self.comfy.submit(workflow, str(job["client_id"]))
                with self._lock:
                    current = self.jobs.get(job_id)
                    if current.get("status") == "canceled" or cancel.is_set():
                        self._cancel_prompt(prompt_id)
                        raise ApiError(409, "generation_canceled", "generation was canceled during submission")
                    current.update({"prompt_id": prompt_id, "status": "queued", "updated_at": time.time()})
                    self.jobs.put(job_id, current)
                while True:
                    with self._lock:
                        observed = self._observed_terminal.get(job_id)
                    if observed:
                        return {"prompt_id": prompt_id, "status": observed}
                    status = self.comfy.status(prompt_id)
                    state = str(status.get("status", "queued"))
                    update("comfy_running" if state == "running" else f"comfy_{state}", 0.5 if state == "running" else 0.05)
                    if state in {"completed", "error", "failed", "not_found", "canceled"}:
                        return {"prompt_id": prompt_id, "status": state}
                    wake = self._wake_events[job_id]
                    wake.wait(self.poll_seconds)
                    wake.clear()
                    if cancel.is_set():
                        self._cancel_prompt(prompt_id)
                        raise ApiError(409, "generation_canceled", "generation was canceled")
            except ApiError as error:
                with self._lock:
                    current = self.jobs.get(job_id)
                    if current.get("status") not in {"completed", "canceled"}:
                        status = "canceled" if cancel.is_set() or error.code == "generation_canceled" else "failed"
                        current.update({
                            "status": status, "message": error.message,
                            "error_code": error.code, "updated_at": time.time(),
                        })
                        self.jobs.put(job_id, current)
                raise
            except Exception as error:
                with self._lock:
                    current = self.jobs.get(job_id)
                    if current.get("status") not in {"completed", "canceled"}:
                        current.update({
                            "status": "failed", "message": "generation execution failed",
                            "error_code": "internal_error", "updated_at": time.time(),
                        })
                        self.jobs.put(job_id, current)
                raise RuntimeError("generation execution failed") from error
            finally:
                with self._lock:
                    self._wake_events.pop(job_id, None)
                    self._observed_terminal.pop(job_id, None)

        # Submission and the durable resource receipt form one critical
        # section with the runner's prompt receipt.  This closes the fast-GPU
        # race where either side could otherwise overwrite the other.
        with self._lock:
            self._wake_events[job_id] = threading.Event()
            resource_task_id = self.resources.submit(job_id, "comfy", model_key, run)
            current = self.jobs.get(job_id)
            current["resource_task_id"] = resource_task_id
            current["gpu_queue_policy"] = "fifo_no_preemption"
            self.jobs.put(job_id, current)
        # Preserve the historical first-job response when submission is fast,
        # while never holding a control request behind an existing GPU task.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            current = self.jobs.get(job_id)
            if current.get("prompt_id") or current.get("status") in {"failed", "canceled"}:
                return current
            resource = self.resources.get(resource_task_id)
            if resource.get("status") == "queued":
                break
            time.sleep(0.01)
        return self.jobs.get(job_id)

    def notify_terminal(self, job: dict[str, Any], state: str, *, wait_timeout: float = 0.5) -> None:
        """Reconcile an API-observed Comfy terminal state with the GPU lease.

        Status requests also query ComfyUI directly.  Without this signal, a
        request can report completion while the scheduler is still sleeping
        between polls, causing an immediately submitted follow-up to queue
        behind work that is already finished.
        """
        if state not in {"completed", "error", "failed", "not_found", "canceled"}:
            return
        job_id = str(job.get("id", ""))
        resource_id = job.get("resource_task_id")
        if not job_id or not isinstance(resource_id, str):
            return
        with self._lock:
            wake = self._wake_events.get(job_id)
            if wake is None:
                return
            self._observed_terminal[job_id] = state
            wake.set()
        # A terminal API response should not race the next submission.  This
        # is bounded; a wedged Comfy status request cannot block the API
        # indefinitely and will still be recovered by the normal poll loop.
        try:
            self.resources.wait(resource_id, max(0.0, wait_timeout))
        except KeyError:
            pass

    def cancel(self, job: dict[str, Any]) -> None:
        resource_id = job.get("resource_task_id")
        if isinstance(resource_id, str):
            try:
                self.resources.cancel(resource_id)
            except KeyError:
                pass
        job_id = str(job.get("id", ""))
        with self._lock:
            wake = self._wake_events.get(job_id)
            if wake is not None:
                wake.set()
        prompt_id = job.get("prompt_id")
        if isinstance(prompt_id, str) and prompt_id:
            self._cancel_prompt(prompt_id)

    def queued_status(self, job: dict[str, Any]) -> dict[str, Any] | None:
        resource_id = job.get("resource_task_id")
        if not isinstance(resource_id, str):
            return None
        try:
            return self.resources.get(resource_id)
        except KeyError:
            return None
