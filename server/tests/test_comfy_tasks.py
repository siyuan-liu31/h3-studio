from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from server.comfy_tasks import ComfyTaskCoordinator
from server.gpu_resources import GpuResourceManager
from server.storage import JobStore


class FakeComfy:
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.canceled: list[str] = []
        self.state = "completed"

    @staticmethod
    def workflow_resource_key(_workflow):
        return "comfy:test-model"

    def submit(self, _workflow, client_id):
        self.submitted.append(client_id)
        return f"prompt-{client_id}"

    def status(self, _prompt_id):
        return {"status": self.state}

    def cancel(self, prompt_id):
        self.canceled.append(prompt_id)


class ComfyTaskCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.jobs = JobStore(Path(self.temp.name) / "jobs")
        self.resources = GpuResourceManager(idle_release_seconds=0, memory_probe=lambda _index: {})
        self.comfy = FakeComfy()
        self.coordinator = ComfyTaskCoordinator(self.jobs, self.comfy, self.resources, poll_seconds=1)

    def tearDown(self) -> None:
        self.resources.stop()
        self.temp.cleanup()

    def add_job(self, job_id: str) -> None:
        self.jobs.put(job_id, {
            "id": job_id,
            "client_id": f"client-{job_id}",
            "status": "submitting",
            "created_at": time.time(),
            "updated_at": time.time(),
        })

    def test_fast_submission_preserves_prompt_and_resource_receipt(self) -> None:
        job_id = "a" * 32
        self.add_job(job_id)
        value = self.coordinator.schedule(job_id, {"1": {"class_type": "Loader"}})
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            value = self.jobs.get(job_id)
            if value.get("prompt_id") and value.get("resource_task_id"):
                break
            time.sleep(0.01)
        self.assertEqual(value["prompt_id"], f"prompt-client-{job_id}")
        self.assertTrue(value["resource_task_id"])
        self.assertEqual(value["gpu_queue_policy"], "fifo_no_preemption")
        self.assertEqual(self.resources.wait(value["resource_task_id"], 1)["status"], "completed")

    def test_comfy_waits_behind_voice_lease_then_runs(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def voice_task(_cancel, _update):
            started.set()
            release.wait(2)

        voice_id = self.resources.submit("voice-a", "voice", "vevo2-fm", voice_task)
        self.assertTrue(started.wait(1))
        job_id = "b" * 32
        self.add_job(job_id)
        value = self.coordinator.schedule(job_id, {"1": {"class_type": "Loader"}})
        self.assertIsNone(value.get("prompt_id"))
        queued = self.coordinator.queued_status(value)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["queue_position"], 1)
        self.assertEqual(queued["queue_reason"], "waiting_for_voice_task")

        release.set()
        self.assertEqual(self.resources.wait(voice_id, 1)["status"], "completed")
        resource_id = self.jobs.get(job_id)["resource_task_id"]
        self.assertEqual(self.resources.wait(resource_id, 1)["status"], "completed")
        self.assertEqual(self.jobs.get(job_id)["prompt_id"], f"prompt-client-{job_id}")

    def test_observed_terminal_state_releases_lease_before_next_submission(self) -> None:
        self.comfy.state = "running"
        job_id = "c" * 32
        self.add_job(job_id)
        job = self.coordinator.schedule(job_id, {"1": {"class_type": "Loader"}})
        self.assertTrue(job.get("prompt_id"))

        self.coordinator.notify_terminal(job, "completed")

        resource = self.resources.get(job["resource_task_id"])
        self.assertEqual(resource["status"], "completed")
        self.assertIsNone(self.resources.snapshot()["active"])


if __name__ == "__main__":
    unittest.main()
