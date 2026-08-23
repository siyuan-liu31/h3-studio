from __future__ import annotations

import base64
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server.app import H3StudioServer, Handler, Runtime
from server.storage import AssetStore, JobStore
from server.tests.test_app import FakeComfy, make_config


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ResultImportSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = make_config(root)
        self.config.prepare()
        self.runtime = Runtime(
            self.config,
            AssetStore(self.config),
            JobStore(self.config.data_root / "metadata" / "jobs"),
            FakeComfy(),  # type: ignore[arg-type]
        )
        self.server = H3StudioServer(("127.0.0.1", 0), Handler, self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, job_id: str) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            f"/api/jobs/{job_id}/assets",
            body=json.dumps({"index": 0}).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": "test-key"},
        )
        response = connection.getresponse()
        content = response.read()
        connection.close()
        return response.status, json.loads(content)

    def completed_job(self, token: str = "a") -> str:
        job_id = token * 32
        output = self.config.comfy_output / f"generated-{token}.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(PNG_1X1)
        self.runtime.jobs.put(job_id, {
            "id": job_id,
            "job_id": job_id,
            "status": "completed",
            "output_type": "image",
            "outputs": [{
                "filename": output.name,
                "subfolder": "",
                "type": "output",
                "mime_type": "image/png",
            }],
            "created_at": 1.0,
            "updated_at": 2.0,
        })
        return job_id

    def test_slow_media_import_does_not_hold_the_global_mutation_lock(self) -> None:
        job_id = self.completed_job("b")
        entered = threading.Event()
        release = threading.Event()
        original_import = self.runtime.assets.import_file

        def slow_import(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError("test did not release blocked import")
            return original_import(*args, **kwargs)

        result: list[tuple[int, dict]] = []
        with patch.object(self.runtime.assets, "import_file", side_effect=slow_import):
            worker = threading.Thread(target=lambda: result.append(self.request(job_id)), daemon=True)
            worker.start()
            self.assertTrue(entered.wait(timeout=2), "request never reached media import")
            acquired = self.runtime.mutation_lock.acquire(timeout=0.25)
            if acquired:
                self.runtime.mutation_lock.release()
            release.set()
            worker.join(timeout=4)

        self.assertTrue(acquired, "media copy/probe/import must occur outside the global mutation lock")
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][0], 201)

    def test_job_metadata_commit_failure_rolls_back_imported_asset(self) -> None:
        job_id = self.completed_job("c")
        original_put = self.runtime.jobs.put

        def fail_asset_commit(key, value):
            outputs = value.get("outputs", []) if isinstance(value, dict) else []
            if key == job_id and any(isinstance(item, dict) and item.get("asset_id") for item in outputs):
                raise OSError("injected job metadata failure")
            return original_put(key, value)

        with patch.object(self.runtime.jobs, "put", side_effect=fail_asset_commit):
            status, body = self.request(job_id)

        self.assertEqual(status, 500, body)
        self.assertEqual(self.runtime.assets.list(), [], "failed metadata commit must not leave an orphan asset")
        self.assertFalse(any(self.runtime.assets.upload_root.iterdir()), "failed metadata commit must remove imported files")
        stored_outputs = self.runtime.jobs.get(job_id)["outputs"]
        self.assertNotIn("asset_id", stored_outputs[0])

    def test_concurrent_and_repeated_imports_reuse_one_durable_asset(self) -> None:
        job_id = self.completed_job("d")
        barrier = threading.Barrier(3)
        results: list[tuple[int, dict]] = []

        def import_result() -> None:
            barrier.wait(timeout=2)
            results.append(self.request(job_id))

        workers = [threading.Thread(target=import_result, daemon=True) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)
        for worker in workers:
            worker.join(timeout=5)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        asset_ids = {body["asset_id"] for _, body in results}
        self.assertEqual(len(asset_ids), 1)
        self.assertEqual(len(self.runtime.assets.list()), 1)

        repeated_status, repeated = self.request(job_id)
        self.assertEqual(repeated_status, 200)
        self.assertEqual(repeated["asset_id"], next(iter(asset_ids)))
        self.assertTrue(repeated["reused"])
        self.assertEqual(len(self.runtime.assets.list()), 1)


if __name__ == "__main__":
    unittest.main()
