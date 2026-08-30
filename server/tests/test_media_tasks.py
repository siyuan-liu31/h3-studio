from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from server.errors import ApiError
from server.media_tasks import MediaTaskManager


class FakeMedia:
    def __init__(self) -> None:
        self.receipts: dict[str, dict] = {}
        self.block = False
        self.reuse_and_cancel = False

    def derive(self, _source, _meta, _request, *, progress, cancel_event):
        progress(35)
        if self.block:
            while not cancel_event.wait(0.01):
                progress(50)
            raise ApiError(409, "cancelled", "cancelled")
        receipt = {
            "id": "a" * 32, "kind": "video", "display_name": "prepared.mp4",
            "filename": "prepared.mp4", "mime_type": "video/mp4", "size": 1,
            "sha256": "b" * 64, "media": {}, "created_at": time.time(),
        }
        self.receipts[receipt["id"]] = receipt
        if self.reuse_and_cancel:
            cancel_event.set()
            return {**receipt, "reused": True}
        return receipt

    def get(self, receipt_id):
        try:
            return self.receipts[receipt_id]
        except KeyError as error:
            raise ApiError(404, "not_found", "missing") from error

    @staticmethod
    def public(receipt):
        return dict(receipt)

    def delete(self, receipt_id):
        return self.receipts.pop(receipt_id)


class MediaTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = FakeMedia()
        self.manager = MediaTaskManager(self.root, self.media)  # type: ignore[arg-type]
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"source")

    def tearDown(self) -> None:
        self.manager.stop()
        self.temp.cleanup()

    def wait_terminal(self, task_id: str) -> dict:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            task = self.manager.get(task_id)
            if task["status"] in {"completed", "failed", "canceled"}:
                return task
            time.sleep(0.01)
        self.fail("media task did not become terminal")

    def test_background_task_reports_progress_and_durable_receipt(self) -> None:
        submitted = self.manager.submit(
            self.source, {"source_receipt": {"type": "asset", "asset_id": "c" * 32}},
            {"operation": "prepare_h3_reference", "preset": "h3-low-token"},
        )
        completed = self.wait_terminal(submitted["task_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["progress"], 100)
        self.assertEqual(completed["receipt_id"], "a" * 32)
        self.assertEqual(completed["receipt"]["id"], "a" * 32)

    def test_cancel_is_cooperative_terminal_and_leaves_no_result(self) -> None:
        self.media.block = True
        submitted = self.manager.submit(
            self.source, {"source_receipt": {"type": "asset", "asset_id": "c" * 32}},
            {"operation": "prepare_h3_reference", "preset": "h3-low-token"},
        )
        canceled = self.manager.cancel(submitted["task_id"])
        self.assertIn(canceled["status"], {"cancelling", "canceled"})
        terminal = self.wait_terminal(submitted["task_id"])
        self.assertEqual(terminal["status"], "canceled")
        self.assertEqual(terminal["error"]["code"], "cancelled")
        self.assertEqual(self.media.receipts, {})

    def test_restart_marks_inflight_task_retryable_instead_of_claiming_success(self) -> None:
        task_id = "d" * 32
        self.manager.store.put(task_id, {
            "id": task_id, "status": "running", "progress": 42,
            "operation": "prepare_h3_reference", "created_at": 1, "updated_at": 2,
        })
        recovered = MediaTaskManager(self.root, self.media)  # type: ignore[arg-type]
        value = recovered.get(task_id)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["error"]["code"], "media_task_interrupted")
        self.assertTrue(value["error"]["retryable"])

    def test_cancel_race_never_deletes_a_reused_shared_receipt(self) -> None:
        self.media.reuse_and_cancel = True
        submitted = self.manager.submit(
            self.source, {"source_receipt": {"type": "asset", "asset_id": "c" * 32}},
            {"operation": "prepare_h3_reference", "preset": "h3-low-token"},
        )
        terminal = self.wait_terminal(submitted["task_id"])
        self.assertEqual(terminal["status"], "canceled")
        self.assertIn("a" * 32, self.media.receipts)
