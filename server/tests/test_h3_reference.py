from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from server.app import H3StudioServer, Handler, Runtime
from server.errors import ApiError
from server.h3_reference import (
    ReferenceParameters,
    calculate_reference_plan,
    estimate_packed_tokens,
    idempotency_key,
    risk_assessment,
)
from server.media import MediaService
from server.profiles import DEFAULT_REGISTRY
from server.storage import AssetStore
from server.storage import JobStore
from server.tests.test_app import FakeComfy, make_config


class ReferencePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parameters = ReferenceParameters(audio="remove")

    def test_common_ratios_preserve_orientation_and_use_aligned_canvases(self) -> None:
        cases = {
            (1080, 1920): (480, 864, "portrait"),
            (1920, 1080): (864, 480, "landscape"),
            (1080, 1080): (480, 480, "square"),
            (1440, 1080): (640, 480, "landscape"),
            (1080, 1440): (480, 640, "portrait"),
            (2560, 1080): (864, 384, "landscape"),
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                plan = calculate_reference_plan(*source, 0, self.parameters)
                self.assertEqual((plan.canvas_width, plan.canvas_height, plan.orientation), expected)
                self.assertEqual(plan.canvas_width % 32, 0)
                self.assertEqual(plan.canvas_height % 32, 0)
                self.assertLessEqual(plan.content_width, plan.canvas_width)
                self.assertLessEqual(plan.content_height, plan.canvas_height)

    def test_rotation_is_applied_before_orientation_and_small_content_is_not_upscaled(self) -> None:
        rotated = calculate_reference_plan(1920, 1080, 90, self.parameters)
        self.assertEqual((rotated.display_width, rotated.display_height), (1080, 1920))
        self.assertEqual((rotated.canvas_width, rotated.canvas_height), (480, 864))
        small = calculate_reference_plan(320, 180, 0, self.parameters)
        self.assertEqual((small.content_width, small.content_height), (320, 180))
        self.assertFalse(small.scaled)
        self.assertEqual((small.canvas_width, small.canvas_height), (320, 192))

    def test_parameters_require_explicit_audio_and_control_unsafe_values(self) -> None:
        with self.assertRaises(ApiError) as missing:
            ReferenceParameters.parse({"operation": "prepare_h3_reference"})
        self.assertEqual(missing.exception.code, "invalid_parameter")
        preset = ReferenceParameters.parse({"operation": "prepare_h3_reference", "preset": "h3-low-token"})
        self.assertEqual(preset.audio, "remove")
        for value in ({"audio": "guess"}, {"audio": "remove", "alignment": 16}, {"audio": "remove", "fps": 30}):
            with self.subTest(value=value), self.assertRaises(ApiError):
                ReferenceParameters.parse(value)

    def test_idempotency_and_sm120_sage_risk_are_stable_and_versioned(self) -> None:
        first = idempotency_key("a" * 64, self.parameters)
        self.assertEqual(first, idempotency_key("a" * 64, self.parameters))
        self.assertNotEqual(first, idempotency_key("b" * 64, self.parameters))
        estimate = estimate_packed_tokens(
            [{"width": 720, "height": 1280, "frames": 360}],
            target_width=768, target_height=1344, target_frames=362,
        )
        self.assertGreaterEqual(estimate["total_tokens"], 150_000)
        risky = risk_assessment(estimate["total_tokens"], gpu_architecture="sm_120 Blackwell", attention_backend="SageAttention")
        self.assertTrue(risky["requires_reference_optimization"])
        self.assertFalse(risk_assessment(estimate["total_tokens"], gpu_architecture="sm_90", attention_backend="SageAttention")["requires_reference_optimization"])
        self.assertFalse(risk_assessment(149_999, gpu_architecture="sm120", attention_backend="SageAttention")["requires_reference_optimization"])


class ReferenceMediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = make_config(self.root)
        self.config.prepare()
        self.assets = AssetStore(self.config)
        self.service = MediaService(self.config, self.assets)
        self.source = self.root / "portrait.mp4"
        self.source.write_bytes(b"source")
        self.meta = {
            "kind": "video", "sha256": hashlib.sha256(b"source").hexdigest(),
            "filename": "portrait.mp4", "display_name": "Portrait source",
            "media": {"width": 1080, "height": 1920, "rotation": 0, "duration": 15.1, "fps": 30, "frame_count": 453, "has_audio": True},
            "source_receipt": {"type": "asset", "asset_id": "a" * 32},
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_receipt_is_auditable_idempotent_and_does_not_modify_source(self) -> None:
        commands: list[list[str]] = []

        def create(command, destination, *_args, **_kwargs):
            commands.append(command)
            destination.write_bytes(b"prepared")

        output_probe = {"width": 480, "height": 864, "duration": 15.0, "fps": 24, "frame_count": 360, "has_audio": False, "video_codec": "h264", "pixel_format": "yuv420p"}
        with patch.object(self.service, "_run_reference", side_effect=create), patch.object(AssetStore, "_probe_media", return_value=output_probe):
            first = self.service.derive(self.source, self.meta, {"operation": "prepare_h3_reference", "preset": "h3-low-token"})
            second = self.service.derive(self.source, self.meta, {"operation": "prepare_h3_reference", "preset": "h3-low-token"})
        self.assertEqual(self.source.read_bytes(), b"source")
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(commands), 1)
        self.assertIn("fillborders=left=0:right=0:top=5:bottom=5:mode=smear", " ".join(commands[0]))
        receipt = first["preprocessing"]
        self.assertEqual(receipt["source"]["sha256"], self.meta["sha256"])
        self.assertEqual((receipt["output"]["canvas_width"], receipt["output"]["canvas_height"]), (480, 864))
        self.assertTrue(receipt["output"]["truncated"])
        self.assertFalse(receipt["output"]["has_audio"])
        self.assertRegex(first["sha256"], r"^[0-9a-f]{64}$")

    def test_failure_cleans_staging_destination_and_receipt(self) -> None:
        with patch.object(self.service, "_run_reference", side_effect=ApiError(422, "media_processing_failed", "failed")):
            with self.assertRaises(ApiError):
                self.service.derive(self.source, self.meta, {"operation": "prepare_h3_reference", "preset": "h3-low-token"})
        self.assertEqual(self.service.metadata.list(), [])
        self.assertEqual(list(self.service.root.iterdir()), [])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
    def test_real_ffmpeg_output_is_h264_yuv420p_24fps_aligned_and_audio_controlled(self) -> None:
        source = self.root / "real.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=s=540x960:r=30:d=1.1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.1",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
        ], check=True, capture_output=True)
        meta = {
            "kind": "video", "sha256": AssetStore.hash_file(source), "filename": source.name,
            "media": AssetStore._probe_media(source, "video"),
            "source_receipt": {"type": "asset", "asset_id": "b" * 32},
        }
        removed = self.service.derive(source, meta, {
            "operation": "prepare_h3_reference", "audio": "remove", "max_duration": 1,
        })
        output = removed["media"]
        self.assertEqual((output["width"], output["height"]), (480, 864))
        self.assertAlmostEqual(output["fps"], 24, delta=0.01)
        self.assertLessEqual(output["duration"], 1.05)
        self.assertEqual(output["video_codec"], "h264")
        self.assertEqual(output["pixel_format"], "yuv420p")
        self.assertFalse(output["has_audio"])
        kept = self.service.derive(source, meta, {
            "operation": "prepare_h3_reference", "audio": "keep", "max_duration": 1,
        })
        self.assertTrue(kept["media"]["has_audio"])


class ReferenceRiskApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = replace(make_config(root), gpu_architecture="sm120 Blackwell", attention_backend="SageAttention")
        config.prepare()
        config.comfy_output.mkdir(parents=True, exist_ok=True)
        self.assets = AssetStore(config)
        self.fake = FakeComfy()
        runtime = Runtime(config, self.assets, JobStore(config.data_root / "metadata" / "jobs"), self.fake)  # type: ignore[arg-type]
        self.server = H3StudioServer(("127.0.0.1", 0), Handler, runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.source_id = "7" * 32
        self.derived_id = "8" * 32
        stored = f"{self.source_id}.mp4"
        (self.assets.upload_root / stored).write_bytes(b"source-video")
        self.assets.metadata.put(self.source_id, {
            "id": self.source_id, "kind": "video", "filename": "large.mp4", "display_name": "large.mp4",
            "stored_name": stored, "comfy_path": f"h3-studio/{stored}", "sha256": "a" * 64,
            "visibility": "library", "created_at": time.time(),
            "media": {"width": 720, "height": 1280, "duration": 15, "fps": 24, "reference_fps": 24,
                      "frame_count": 360, "has_audio": False, "rotation": 0},
        })

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, request_id: str) -> tuple[int, dict]:
        profile = DEFAULT_REGISTRY.get("minimax-h3-ref2va")
        body = json.dumps({
            "request_id": request_id,
            "output_type": "video", "director_mode": "r2v", "prompt": "Use the motion reference.",
            "profile_id": profile.id, "profile_version": profile.version, "profile_digest": profile.digest(),
            "references": [{"asset_id": self.source_id, "role": "motion"}],
            "parameters": {"duration": 15, "aspect_ratio": "9:16", "steps": 4},
        }).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("POST", "/api/generate", body=body, headers={
            "Content-Type": "application/json", "X-API-Key": "test-key",
        })
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw)

    def api_request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        body = None if payload is None else json.dumps(payload).encode()
        connection.request(method, path, body=body, headers={
            "Content-Type": "application/json", "X-API-Key": "test-key",
        })
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw)

    def _save_derived(self, _receipt_id: str, **_kwargs) -> dict:
        stored = f"{self.derived_id}.mp4"
        self.assets.metadata.put(self.derived_id, {
            "id": self.derived_id, "kind": "video", "filename": "optimized.mp4", "stored_name": stored,
            "comfy_path": f"h3-studio/{stored}", "sha256": "b" * 64, "visibility": "internal",
            "media": {"width": 480, "height": 864, "duration": 15, "fps": 24, "reference_fps": 24,
                      "frame_count": 360, "has_audio": False},
        })
        return self.assets.get(self.derived_id)

    def test_sm120_sage_long_sequence_automatically_binds_auditable_derived_reference(self) -> None:
        receipt = {
            "id": "9" * 32, "reused": False,
            "preprocessing": {
                "source": {"width": 720, "height": 1280},
                "output": {"canvas_width": 480, "canvas_height": 864},
            },
        }
        with patch.object(self.server.runtime.media, "derive", return_value=receipt), patch.object(
            self.server.runtime.media, "save_as_asset", side_effect=self._save_derived,
        ):
            status, body = self.request("1" * 32)
        self.assertEqual(status, 202, body)
        self.assertTrue(body["reference_preflight"]["optimized"])
        self.assertEqual(body["references"][0]["asset_id"], self.derived_id)
        self.assertEqual(body["reference_preflight"]["derivations"][0]["source_asset_id"], self.source_id)
        load_video = next(node for node in self.fake.workflow.values() if node.get("class_type") == "LoadVideo")
        self.assertIn(self.derived_id, json.dumps(load_video["inputs"]))
        self.assertTrue((self.assets.upload_root / f"{self.source_id}.mp4").exists())

    def test_preprocessing_failure_is_not_submitted_and_returns_retry_evidence(self) -> None:
        with patch.object(self.server.runtime.media, "derive", side_effect=ApiError(507, "insufficient_storage", "full")):
            status, body = self.request("2" * 32)
        self.assertEqual(status, 507)
        self.assertEqual(self.fake.submit_count, 0)
        details = body["error"]["details"]
        self.assertEqual(details["stage"], "reference_preprocessing")
        self.assertEqual(details["request_id"], "2" * 32)
        self.assertEqual(details["materialized_locators"], [])

    def test_background_reference_api_reports_durable_progress_and_validates_mode(self) -> None:
        receipt_id = "6" * 32

        def derive(_path, _meta, _request, *, progress, cancel_event):
            self.assertFalse(cancel_event.is_set())
            progress(55)
            destination = self.server.runtime.media.root / f"{receipt_id}.mp4"
            destination.write_bytes(b"prepared")
            value = {
                "id": receipt_id, "kind": "video", "display_name": "prepared.mp4",
                "filename": "prepared.mp4", "stored_name": destination.name,
                "mime_type": "video/mp4", "size": destination.stat().st_size,
                "sha256": hashlib.sha256(b"prepared").hexdigest(), "media": {},
                "source": {"type": "asset", "asset_id": self.source_id},
                "operation": "prepare_h3_reference", "created_at": time.time(),
            }
            self.server.runtime.media.metadata.put(receipt_id, value)
            return self.server.runtime.media.public(value)

        payload = {
            "source": {"type": "asset", "asset_id": self.source_id},
            "operation": "prepare_h3_reference", "preset": "h3-low-token", "background": True,
        }
        with patch.object(self.server.runtime.media, "derive", side_effect=derive):
            status, submitted = self.api_request("POST", "/api/media/derive", payload)
            self.assertEqual(status, 202, submitted)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status, task = self.api_request("GET", submitted["status_url"])
                if task["status"] == "completed":
                    break
                time.sleep(0.01)
            else:
                self.fail("background reference task did not complete")
        self.assertEqual(status, 200)
        self.assertEqual(task["progress"], 100)
        self.assertEqual(task["receipt"]["id"], receipt_id)

        invalid = {**payload, "background": "yes"}
        invalid_status, invalid_body = self.api_request("POST", "/api/media/derive", invalid)
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid_body["error"]["code"], "invalid_parameter")


if __name__ == "__main__":
    unittest.main()
