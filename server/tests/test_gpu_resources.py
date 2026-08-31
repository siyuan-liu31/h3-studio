from __future__ import annotations

import threading
import time
import unittest

from server.gpu_resources import GpuResourceManager


class GpuResourceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = GpuResourceManager(
            idle_release_seconds=0,
            memory_probe=lambda _index: {"available": True, "free_mib": 1234},
        )

    def tearDown(self) -> None:
        self.manager.stop()

    def test_tasks_are_exclusive_fifo_and_report_queue_reason(self) -> None:
        first_started = threading.Event()
        allow_first = threading.Event()
        order: list[str] = []

        def first(_cancel, update):
            order.append("first-start")
            update("inference", 0.5)
            first_started.set()
            allow_first.wait(2)
            order.append("first-end")

        def second(_cancel, _update):
            order.append("second")

        first_id = self.manager.submit("job-1", "comfy", "h3-a", first)
        self.assertTrue(first_started.wait(1))
        second_id = self.manager.submit("voice-1", "vevo2", "vevo2-fm", second)
        queued = self.manager.get(second_id)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["queue_position"], 1)
        self.assertEqual(queued["queue_reason"], "waiting_for_comfy_task")
        self.assertEqual(self.manager.get(first_id)["stage"], "inference")
        allow_first.set()
        self.assertEqual(self.manager.wait(second_id, 2)["status"], "completed")
        self.assertEqual(order, ["first-start", "first-end", "second"])

    def test_same_model_reuses_resident_and_switch_releases_once(self) -> None:
        releases: list[str] = []
        self.manager.register_backend("voice", lambda: releases.append("voice"))
        self.manager.register_backend("comfy", lambda: releases.append("comfy"))
        for owner in ("one", "two"):
            task_id = self.manager.submit(owner, "voice", "vevo2-fm", lambda _c, _u: owner)
            self.assertEqual(self.manager.wait(task_id, 1)["status"], "completed")
        self.assertEqual(releases, [])
        switched = self.manager.submit("three", "comfy", "h3", lambda _c, _u: None)
        self.assertEqual(self.manager.wait(switched, 1)["status"], "completed")
        self.assertEqual(releases, ["voice"])

    def test_queued_cancel_never_runs_and_active_cancel_cleans_backend(self) -> None:
        started = threading.Event()
        releases: list[str] = []
        self.manager.register_backend("voice", lambda: releases.append("voice"))

        def active(cancel, _update):
            started.set()
            while not cancel.wait(0.01):
                pass

        active_id = self.manager.submit("active", "voice", "model", active)
        self.assertTrue(started.wait(1))
        ran = threading.Event()
        queued_id = self.manager.submit("queued", "voice", "model", lambda _c, _u: ran.set())
        self.assertEqual(self.manager.cancel(queued_id)["status"], "canceled")
        self.manager.cancel(active_id)
        self.assertEqual(self.manager.wait(active_id, 1)["status"], "canceled")
        self.assertFalse(ran.is_set())
        self.assertEqual(releases, ["voice"])

    def test_crash_releases_backend_and_next_task_runs(self) -> None:
        released = threading.Event()
        self.manager.register_backend("voice", released.set)
        failed = self.manager.submit("bad", "voice", "model", lambda _c, _u: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(self.manager.wait(failed, 1)["status"], "failed")
        self.assertTrue(released.is_set())
        good = self.manager.submit("good", "voice", "model", lambda _c, _u: "ok")
        self.assertEqual(self.manager.wait(good, 1)["result"], "ok")

    def test_idle_timeout_releases_resident(self) -> None:
        self.manager.stop()
        released = threading.Event()
        self.manager = GpuResourceManager(
            idle_release_seconds=1,
            memory_probe=lambda _index: {"available": True},
        )
        self.manager.register_backend("voice", released.set)
        task_id = self.manager.submit("one", "voice", "model", lambda _c, _u: None)
        self.assertEqual(self.manager.wait(task_id, 1)["status"], "completed")
        self.assertTrue(released.wait(2))
        self.assertIsNone(self.manager.snapshot()["resident"])

    def test_snapshot_includes_memory_and_policy(self) -> None:
        value = self.manager.snapshot()
        self.assertEqual(value["policy"], "fifo_no_preemption")
        self.assertTrue(value["exclusive"])
        self.assertEqual(value["memory"]["free_mib"], 1234)


if __name__ == "__main__":
    unittest.main()
