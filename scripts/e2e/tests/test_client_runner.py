from __future__ import annotations

import io
import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts.e2e.cli import main
from scripts.e2e.client import ApiClient, E2EError, JobTimeout
from scripts.e2e.runner import _local_expectations, assert_output, execute_run


PROFILE = {
    "id": "minimax-h3-fl2va",
    "version": "1.1",
    "manifest_sha256": "a" * 64,
    "compiler": "h3_fl",
    "output_type": "video",
    "sampling_mode": "turbo4",
    "available": True,
    "defaults": {"steps": 4, "lora_strength": 0.75},
}


class UploadHandler(BaseHTTPRequestHandler):
    body = b""
    api_key = ""

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        type(self).body = self.rfile.read(length)
        type(self).api_key = self.headers.get("X-API-Key", "")
        response = json.dumps({
            "id": "1" * 32, "kind": "image", "filename": "frame.png", "sha256": "b" * 64,
        }).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format, *_args):
        return


class RedirectSinkHandler(BaseHTTPRequestHandler):
    api_key = ""

    def do_GET(self):
        type(self).api_key = self.headers.get("X-API-Key", "")
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, _format, *_args):
        return


class ClientTests(unittest.TestCase):
    def test_local_expectations_cover_the_362_frame_grid_endpoint(self) -> None:
        request = {
            "parameters": {
                "aspect_ratio": "16:9", "duration": 362 / 24, "steps": 4,
                "lora_strength": 0.75, "denoise": 1,
            },
        }
        expected = _local_expectations(request, PROFILE, "video")
        self.assertEqual(expected["frames"], 362)
        self.assertEqual(expected["duration_actual"], 362 / 24)

    def test_upload_streams_multipart_and_auth_header(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), UploadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "frame.png")
                path.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
                asset = ApiClient(f"http://127.0.0.1:{server.server_address[1]}", "secret").upload(path, "image")
            self.assertEqual(asset["id"], "1" * 32)
            self.assertEqual(UploadHandler.api_key, "secret")
            self.assertIn(b'name="kind"', UploadHandler.body)
            self.assertIn(b"image", UploadHandler.body)
            self.assertIn(b'filename="frame.png"', UploadHandler.body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_ffprobe_assertions_require_24fps_audio_and_dimensions(self) -> None:
        value = {
            "format": {"duration": "5.166"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1344, "height": 768, "avg_frame_rate": "24/1", "nb_frames": "124"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(value), "")
        with patch("scripts.e2e.runner.subprocess.run", return_value=completed):
            evidence = assert_output(
                Path("clip.mp4"), output_type="video", expected_width=1344, expected_height=768,
                expected_duration=124 / 24, expected_frames=124,
            )
        self.assertEqual(evidence["fps"], 24)
        self.assertTrue(evidence["has_audio"])

        value["streams"][0]["avg_frame_rate"] = "30/1"
        with patch("scripts.e2e.runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(value), "")):
            with self.assertRaisesRegex(E2EError, "!= 24"):
                assert_output(Path("clip.mp4"), output_type="video")

    def test_download_refuses_a_different_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = ApiClient("http://127.0.0.1:3013")
            with self.assertRaisesRegex(E2EError, "different origin"):
                client.download("https://untrusted.invalid/output.mp4", Path(directory, "clip.mp4"))

    def test_json_redirect_is_rejected_without_forwarding_api_key(self) -> None:
        sink = ThreadingHTTPServer(("127.0.0.1", 0), RedirectSinkHandler)

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_address[1]}/stolen")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format, *_args):
                return

        source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (sink, source)]
        for thread in threads:
            thread.start()
        RedirectSinkHandler.api_key = ""
        try:
            client = ApiClient(f"http://127.0.0.1:{source.server_address[1]}", "secret")
            with self.assertRaisesRegex(E2EError, "refused HTTP redirect"):
                client.capabilities()
            self.assertEqual(RedirectSinkHandler.api_key, "")
        finally:
            for server in (source, sink):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


class FakeClient:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.payload = None
        self.canceled = []

    def capabilities(self):
        return {"profiles": [PROFILE]}

    def upload(self, _path, kind):
        return {"id": "1" * 32, "kind": kind, "filename": "frame.png", "sha256": "b" * 64}

    def submit(self, payload):
        self.payload = payload
        return {"job_id": "2" * 32}

    def wait(self, _job_id, **_kwargs):
        denoise = self.payload["parameters"].get("denoise", 1.0)
        return {
            "status": "completed", "parameters": {
                "width": 1344, "height": 768, "steps": 4,
                "profile_id": PROFILE["id"], "profile_version": PROFILE["version"],
                "profile_digest": PROFILE["manifest_sha256"], "frames": 124,
                "duration_requested": 124 / 24, "duration_actual": 124 / 24,
                "sampling_mode": "turbo4", "sampler": "sa_solver", "scheduler": "simple",
                "lora_strength": 0.75, "lora": "turbo.safetensors", "denoise": denoise,
            },
            "workflow_sha256": "c" * 64,
            "workflow_evidence": {
                "sha256": "c" * 64, "steps": 4, "sampler": "sa_solver", "scheduler": "simple",
                "lora": "turbo.safetensors", "lora_strength": 0.75, "denoise": denoise,
            },
            "download_url": "/api/download?id=x",
            "outputs": [{"filename": "clip.mp4", "sha256": "d" * 64}],
        }

    def download(self, _url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"video")
        return {"path": str(destination), "size": 5, "sha256": "d" * 64}

    def cancel(self, job_id):
        self.canceled.append(job_id)
        return {"id": job_id, "status": "canceled"}


class RunnerTests(unittest.TestCase):
    def test_execute_run_pins_profile_polls_downloads_and_probes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(root)
            with patch("scripts.e2e.runner.assert_output", return_value={"width": 1344, "height": 768, "fps": 24}):
                evidence = execute_run(
                    client,  # type: ignore[arg-type]
                    {"scenario": "t2v", "prompt": "ocean", "assets": [], "sampling_mode": "turbo4", "denoise": 0.65},
                    output_dir=root,
                )
        self.assertEqual(client.payload["profile_id"], PROFILE["id"])
        self.assertEqual(client.payload["profile_version"], PROFILE["version"])
        self.assertEqual(client.payload["profile_digest"], PROFILE["manifest_sha256"])
        self.assertEqual(evidence["workflow_sha256"], "c" * 64)
        self.assertEqual(evidence["resolved_parameters"]["steps"], 4)
        self.assertEqual(client.payload["parameters"]["denoise"], 0.65)
        self.assertEqual(evidence["workflow_evidence"]["denoise"], 0.65)

    def test_execute_run_rejects_path_like_job_id_before_download(self) -> None:
        class TraversalClient(FakeClient):
            def submit(self, payload):
                self.payload = payload
                return {"job_id": "../" + "a" * 29}

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(E2EError, "32 lowercase hexadecimal"):
                execute_run(
                    TraversalClient(Path(directory)),  # type: ignore[arg-type]
                    {"scenario": "t2v", "prompt": "ocean", "assets": []}, output_dir=Path(directory),
                )

    def test_timeout_best_effort_cancels_submitted_job(self) -> None:
        class TimeoutClient(FakeClient):
            def wait(self, job_id, **kwargs):
                raise JobTimeout("late")

        with tempfile.TemporaryDirectory() as directory:
            client = TimeoutClient(Path(directory))
            with self.assertRaises(JobTimeout):
                execute_run(
                    client,  # type: ignore[arg-type]
                    {"scenario": "t2v", "prompt": "ocean", "assets": []}, output_dir=Path(directory),
                )
            self.assertEqual(client.canceled, ["2" * 32])

    def test_wait_protocol_failure_cancels_and_records_partial_evidence(self) -> None:
        class BrokenWaitClient(FakeClient):
            def wait(self, job_id, **kwargs):
                raise E2EError("connection lost")

        with tempfile.TemporaryDirectory() as directory:
            client = BrokenWaitClient(Path(directory))
            with self.assertRaisesRegex(E2EError, "connection lost") as caught:
                execute_run(
                    client,  # type: ignore[arg-type]
                    {"scenario": "t2v", "prompt": "ocean", "assets": []}, output_dir=Path(directory),
                )
            self.assertTrue(caught.exception.partial_evidence["cancellation"]["ok"])
            self.assertEqual(caught.exception.partial_evidence["job_id"], "2" * 32)

    def test_result_must_match_pinned_profile_and_workflow_evidence(self) -> None:
        class WrongProfileClient(FakeClient):
            def wait(self, job_id, **kwargs):
                value = super().wait(job_id, **kwargs)
                value["parameters"]["profile_id"] = "wrong"
                return value

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(E2EError, "pinned profile"):
                execute_run(
                    WrongProfileClient(Path(directory)),  # type: ignore[arg-type]
                    {"scenario": "t2v", "prompt": "ocean", "assets": []}, output_dir=Path(directory),
                )

    def test_result_sampling_and_size_are_derived_locally_not_from_server_echo(self) -> None:
        class WrongSamplingClient(FakeClient):
            def wait(self, job_id, **kwargs):
                value = super().wait(job_id, **kwargs)
                value["parameters"].update({"width": 768, "sampler": "wrong", "frames": 22, "duration_actual": 1.0})
                return value

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(E2EError, "locally derived"):
                execute_run(
                    WrongSamplingClient(Path(directory)),  # type: ignore[arg-type]
                    {"scenario": "t2v", "prompt": "ocean", "assets": [], "aspect_ratio": "16:9", "duration": 124 / 24},
                    output_dir=Path(directory),
                )

    def test_actual_final_workflow_evidence_must_match_sampling_contract(self) -> None:
        class WrongWorkflowClient(FakeClient):
            def wait(self, job_id, **kwargs):
                value = super().wait(job_id, **kwargs)
                value["workflow_evidence"]["sampler"] = "wrong"
                return value

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(E2EError, "final workflow sampler"):
                execute_run(
                    WrongWorkflowClient(Path(directory)),  # type: ignore[arg-type]
                    {"scenario": "t2v", "prompt": "ocean", "assets": []}, output_dir=Path(directory),
                )

    def test_actual_final_workflow_denoise_must_match_nondefault_request(self) -> None:
        class WrongDenoiseClient(FakeClient):
            def wait(self, job_id, **kwargs):
                value = super().wait(job_id, **kwargs)
                value["workflow_evidence"]["denoise"] = 1.0
                return value

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(E2EError, "final workflow denoise"):
                execute_run(
                    WrongDenoiseClient(Path(directory)),  # type: ignore[arg-type]
                    {"scenario": "t2v", "prompt": "ocean", "assets": [], "denoise": 0.65}, output_dir=Path(directory),
                )
        class MalformedEvidenceClient(FakeClient):
            def wait(self, job_id, **kwargs):
                value = super().wait(job_id, **kwargs)
                value["workflow_evidence"]["lora_strength"] = []
                return value

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(E2EError, "invalid LoRA strength"):
                execute_run(
                    MalformedEvidenceClient(Path(directory)),  # type: ignore[arg-type]
                    {"scenario": "t2v", "prompt": "ocean", "assets": []}, output_dir=Path(directory),
                )

    def test_cli_dry_run_makes_no_network_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory, "manifest.json")
            manifest.write_text(json.dumps({
                "version": 1,
                "runs": [{"scenario": "t2v", "prompt": "ocean"}],
            }), encoding="utf-8")
            output = io.StringIO()
            with patch.object(ApiClient, "capabilities", side_effect=AssertionError("network used")), redirect_stdout(output):
                code = main(["--manifest", str(manifest), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["plans"][0]["scenario"], "t2v")


if __name__ == "__main__":
    unittest.main()
