from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from server.errors import ApiError
from server.motion_context import MotionContextStore
from server.tests.test_workflows import config


class MotionContextStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = config(self.root)
        self.settings.prepare()
        self.store = MotionContextStore(self.settings)
        self.job_id = "a" * 32
        self.project_id = "b" * 32
        self.segment_id = "c" * 32
        self.attempt_id = "d" * 32

    def tearDown(self) -> None:
        self.temp.cleanup()

    def record(self, payload: bytes = b"motion-context") -> dict:
        path = self.settings.comfy_output / "chain.latent"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {"outputs": {"19": {"latents": [{"filename": path.name, "subfolder": "", "type": "output"}]}}}

    def capture(self, payload: bytes = b"motion-context") -> dict:
        return self.store.capture(
            {"id": self.job_id}, self.record(payload), project_id=self.project_id,
            segment_id=self.segment_id, attempt_id=self.attempt_id,
        )

    def test_capture_stage_integrity_and_project_pruning(self) -> None:
        manifest = self.capture()
        locator, staged = self.store.stage(manifest, "e" * 32)
        self.assertEqual(locator, f"h3-studio-motion-context/{staged.name}")
        self.assertEqual(staged.read_bytes(), b"motion-context")
        self.store.cleanup_staged(staged)
        self.assertFalse(staged.exists())
        self.assertEqual(self.store.prune_project(self.project_id, {self.job_id}), 0)
        self.assertEqual(self.store.delete_project(self.project_id), 1)
        self.assertFalse((self.store.root / str(manifest["stored_name"])).exists())

    def test_missing_wrong_node_and_corruption_fail_closed(self) -> None:
        with self.assertRaises(ApiError) as missing:
            self.store.capture(
                {"id": self.job_id}, {"outputs": {"18": {"latents": [{"filename": "x.latent"}]}}},
                project_id=self.project_id, segment_id=self.segment_id, attempt_id=self.attempt_id,
            )
        self.assertEqual(missing.exception.code, "motion_context_missing")
        manifest = self.capture()
        (self.store.root / str(manifest["stored_name"])).write_bytes(b"changed")
        with self.assertRaises(ApiError) as corrupt:
            self.store.get(self.job_id)
        self.assertEqual(corrupt.exception.code, "motion_context_corrupt")

    def test_quota_failure_leaves_no_partial_file_or_metadata(self) -> None:
        settings = replace(self.settings, max_motion_context_storage_bytes=2)
        store = MotionContextStore(settings)
        with self.assertRaises(ApiError) as raised:
            store.capture(
                {"id": self.job_id}, self.record(b"too-large"), project_id=self.project_id,
                segment_id=self.segment_id, attempt_id=self.attempt_id,
            )
        self.assertEqual(raised.exception.code, "motion_context_storage_full")
        self.assertEqual(list(store.root.glob("*.latent")), [])
        self.assertEqual(store.metadata.list(), [])


if __name__ == "__main__":
    unittest.main()
