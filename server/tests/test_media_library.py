from __future__ import annotations

import hashlib
import base64
import http.client
import json
import os
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
from server.media import MediaService
from server.storage import AssetStore, JobStore
from server.tests.test_app import FakeComfy, make_config

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class MediaServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = make_config(self.root)
        self.config.prepare()
        self.assets = AssetStore(self.config)
        self.service = MediaService(self.config, self.assets)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"source-video")
        self.meta = {
            "kind": "video",
            "media": {"duration": 10.0, "has_audio": True},
            "source_receipt": {"type": "asset", "asset_id": "a" * 32},
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def fake_run(_command, destination, **_kwargs):
        destination.write_bytes(b"derived-content")

    def test_frame_receipt_is_hashed_and_does_not_auto_create_asset(self) -> None:
        with patch.object(self.service, "_run", side_effect=self.fake_run), patch.object(
            AssetStore, "_probe_image", return_value={"width": 320, "height": 180, "codec": "mjpeg"}
        ):
            receipt = self.service.derive(self.source, self.meta, {"operation": "frame", "position": "last"})
        self.assertEqual(receipt["kind"], "image")
        self.assertEqual(receipt["sha256"], hashlib.sha256(b"derived-content").hexdigest())
        self.assertIn(receipt["receipt_id"], receipt["content_url"])
        self.assertEqual(self.assets.list(), [], "derivatives must remain results until explicitly saved")

    def test_trim_rejects_out_of_bounds_but_is_not_limited_by_h3_generation_duration(self) -> None:
        with self.assertRaises(ApiError) as outside:
            self.service.derive(self.source, self.meta, {"operation": "video_trim", "start": 9, "end": 11})
        self.assertEqual(outside.exception.code, "invalid_time_range")
        long_meta = {**self.meta, "media": {"duration": 30, "has_audio": True}}
        with patch.object(self.service, "_run", side_effect=self.fake_run), patch.object(
            AssetStore, "_probe_media", return_value={"duration": 20, "has_audio": True, "fps": 24}
        ):
            receipt = self.service.derive(
                self.source, long_meta,
                {"operation": "video_trim", "start": 0, "end": 20},
            )
        self.assertEqual(receipt["media"]["duration"], 20)

    def test_failed_metadata_commit_rolls_back_file(self) -> None:
        with patch.object(self.service, "_run", side_effect=self.fake_run), patch.object(
            AssetStore, "_probe_image", return_value={"width": 1, "height": 1}
        ), patch.object(self.service.metadata, "put", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.derive(self.source, self.meta, {"operation": "frame", "position": "first"})
        self.assertEqual(list(self.service.root.iterdir()), [])

    def test_command_parameters_cannot_be_shell_injected(self) -> None:
        commands = []

        def capture(command, destination, **_kwargs):
            commands.append(command)
            destination.write_bytes(b"frame")

        with patch.object(self.service, "_run", side_effect=capture), patch.object(
            AssetStore, "_probe_image", return_value={"width": 1, "height": 1}
        ):
            self.service.derive(self.source, self.meta, {
                "operation": "frame", "position": "current", "time": 1,
                "display_name": "frame;touch pwned.jpg",
            })
        self.assertIsInstance(commands[0], list)
        self.assertNotIn("frame;touch pwned.jpg", commands[0])
        self.assertFalse((self.root / "pwned.jpg").exists())

    def test_first_and_last_frame_build_exact_tail_decode_commands(self) -> None:
        commands = []

        def capture(command, destination, **_kwargs):
            commands.append(command)
            destination.write_bytes(b"frame")

        with patch.object(self.service, "_run", side_effect=capture), patch.object(
            AssetStore, "_probe_image", return_value={"width": 1, "height": 1}
        ):
            self.service.derive(self.source, self.meta, {"operation": "frame", "position": "first"})
            self.service.derive(self.source, self.meta, {"operation": "frame", "position": "last"})
        first_seek = commands[0][commands[0].index("-ss") + 1]
        self.assertEqual(first_seek, "0.000000")
        self.assertEqual(commands[1][commands[1].index("-ss") + 1], "8.000000")
        self.assertNotIn("-sseof", commands[1])
        self.assertIn("-update", commands[1])
        self.assertNotIn("-fps_mode", commands[1])
        self.assertNotIn("-frames:v", commands[1])
        self.assertIn("-fs", commands[1])

    def test_atomic_reservation_includes_parallel_derivations(self) -> None:
        limited = replace(self.config, max_asset_storage_bytes=1536 * 1024)
        service = MediaService(limited, self.assets)
        entered = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        def blocked(_command, destination, **_kwargs):
            entered.set()
            release.wait(2)
            destination.write_bytes(b"frame")

        def first():
            try:
                service.derive(self.source, self.meta, {"operation": "frame", "position": "first"})
            except Exception as error:  # pragma: no cover - assertion reports it
                errors.append(error)

        with patch.object(service, "_run", side_effect=blocked), patch.object(
            AssetStore, "_probe_image", return_value={"width": 1, "height": 1}
        ):
            worker = threading.Thread(target=first)
            worker.start()
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ApiError) as raised:
                service.derive(self.source, self.meta, {"operation": "frame", "position": "first"})
            self.assertEqual(raised.exception.code, "media_quota")
            release.set()
            worker.join(2)
        self.assertEqual(errors, [])

        asset_id = "7" * 32
        self.assets.metadata.put(asset_id, {
            "id": asset_id, "kind": "image", "size": 600 * 1024,
            "storage_size": 600 * 1024, "created_at": 1,
        })
        with patch.object(service, "_run") as run:
            with self.assertRaises(ApiError) as raised:
                service.derive(self.source, self.meta, {"operation": "frame", "position": "first"})
        self.assertEqual(raised.exception.code, "media_quota")
        run.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
    def test_audio_derivatives_can_be_saved_as_real_audio_assets(self) -> None:
        source_video = self.root / "source-with-audio.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=black:s=64x64:r=24:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source_video),
        ], check=True, capture_output=True)
        video_meta = {
            "kind": "video",
            "media": AssetStore._probe_media(source_video, "video"),
            "source_receipt": {"type": "job", "job_id": "a" * 32, "index": 0},
        }
        extracted = self.service.derive(source_video, video_meta, {"operation": "extract_audio"})
        self.assertEqual(extracted["kind"], "audio")
        self.assertEqual(self.service.path(self.service.get(extracted["id"])).read_bytes()[:4], b"RIFF")
        saved_extracted = self.service.save_as_asset(extracted["id"], display_name="extracted.wav")
        self.assertEqual(saved_extracted["kind"], "audio")

        source_audio = self.root / "source.wav"
        subprocess.run([
            "ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
            "-i", "sine=frequency=880:duration=2", "-c:a", "pcm_s16le", str(source_audio),
        ], check=True, capture_output=True)
        audio_meta = {
            "kind": "audio",
            "media": AssetStore._probe_media(source_audio, "audio"),
            "source_receipt": {"type": "asset", "asset_id": "b" * 32},
        }
        trimmed = self.service.derive(
            source_audio, audio_meta, {"operation": "audio_trim", "start": 0.25, "end": 1.75},
        )
        saved_trimmed = self.service.save_as_asset(trimmed["id"], display_name="trimmed.wav")
        self.assertEqual(saved_trimmed["kind"], "audio")
        self.assertAlmostEqual(float(saved_trimmed["media"]["duration"]), 1.5, delta=0.05)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
    def test_last_frame_is_the_true_last_decodable_pixel(self) -> None:
        source = self.root / "red-then-blue.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=red:s=64x64:r=24:d=0.5",
            "-f", "lavfi", "-i", "color=blue:s=64x64:r=24:d=0.5",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ], check=True, capture_output=True)
        meta = {
            "kind": "video",
            "media": AssetStore._probe_media(source, "video"),
            "source_receipt": {"type": "asset", "asset_id": "f" * 32},
        }
        receipt = self.service.derive(source, meta, {"operation": "frame", "position": "last"})
        frame = self.service.path(self.service.get(receipt["id"]))
        rgb = subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(frame),
            "-vf", "scale=1:1", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ], check=True, capture_output=True).stdout
        self.assertGreater(rgb[2], rgb[0], "the exact last frame should be blue, not an estimated earlier red frame")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
    def test_last_frame_uses_video_stream_duration_when_audio_is_longer(self) -> None:
        source = self.root / "short-video-long-audio.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=red:s=64x64:r=24:d=0.5",
            "-f", "lavfi", "-i", "color=blue:s=64x64:r=24:d=0.5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map", "[v]", "-map", "2:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(source),
        ], check=True, capture_output=True)
        media = AssetStore._probe_media(source, "video")
        self.assertGreater(float(media["duration"]), 4.5)
        self.assertLess(float(media["video_duration"]), 1.1)
        receipt = self.service.derive(source, {
            "kind": "video", "media": media,
            "source_receipt": {"type": "asset", "asset_id": "9" * 32},
        }, {"operation": "frame", "position": "last"})
        frame = self.service.path(self.service.get(receipt["id"]))
        rgb = subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(frame),
            "-vf", "scale=1:1", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ], check=True, capture_output=True).stdout
        self.assertGreater(rgb[2], rgb[0])

    def test_gc_expires_receipts_orphans_and_thumbnail_cache(self) -> None:
        receipt_id = "e" * 32
        derivative = self.service.root / f"{receipt_id}.jpg"
        derivative.write_bytes(b"jpeg")
        self.service.metadata.put(receipt_id, {
            "id": receipt_id, "stored_name": derivative.name, "size": 4,
            "created_at": time.time() - 3600,
        })
        orphan = self.service.root / "orphan.jpg"
        orphan.write_bytes(b"orphan")
        thumbnail = self.service.thumbnails / "cached.jpg"
        thumbnail.write_bytes(b"thumb")
        old = time.time() - 3600
        os.utime(orphan, (old, old))
        os.utime(thumbnail, (old, old))
        result = self.service.garbage_collect(older_than_seconds=60)
        self.assertEqual(result, {"derivation_receipts": 1, "derivation_files": 2, "thumbnails": 1})
        self.assertFalse(derivative.exists())
        self.assertFalse(orphan.exists())
        self.assertFalse(thumbnail.exists())

    def test_thumbnail_is_cached_by_safe_digest(self) -> None:
        calls = []

        def generate(_command, destination, **_kwargs):
            calls.append(destination)
            destination.write_bytes(b"jpeg")

        with patch.object(self.service, "_run", side_effect=generate):
            first = self.service.thumbnail(self.source, cache_key="../../escape", kind="video")
            first_mtime = first.stat().st_mtime_ns
            second = self.service.thumbnail(self.source, cache_key="../../escape", kind="video")
        self.assertEqual(first, second)
        self.assertEqual(second.stat().st_mtime_ns, first_mtime, "cache hits must preserve stable ETags")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.service._thumbnail_locks, {})
        first.resolve().relative_to(self.service.thumbnails.resolve())

    def test_public_asset_metadata_includes_content_hash_for_duplicate_detection(self) -> None:
        asset = {
            "id": "a" * 32, "kind": "image", "filename": "same.png",
            "mime_type": "image/png", "size": 3, "sha256": "b" * 64,
            "media": {"width": 1, "height": 1}, "visibility": "library",
        }
        public = self.assets.public_metadata(asset)
        self.assertEqual(public["content_hash"], asset["sha256"])
        self.assertEqual(len(public["content_hash"]), 64)

    def test_video_thumbnail_uses_representative_frame_recipe(self) -> None:
        commands = []

        def generate(command, destination, **_kwargs):
            commands.append(command)
            destination.write_bytes(b"jpeg")

        with patch.object(self.service, "_run", side_effect=generate):
            self.service.thumbnail(self.source, cache_key="opening-black-frame", kind="video")

        self.assertEqual(len(commands), 1)
        self.assertIn("thumbnail=48,scale='min(480,iw)':-2", commands[0])
        self.assertNotIn("-ss", commands[0])

    def test_concurrent_thumbnail_requests_share_one_generation(self) -> None:
        calls = []
        barrier = threading.Barrier(4)
        results = []

        def generate(_command, destination, **_kwargs):
            calls.append(destination)
            time.sleep(0.05)
            destination.write_bytes(b"jpeg")

        def request_thumbnail() -> None:
            barrier.wait(timeout=2)
            results.append(self.service.thumbnail(self.source, cache_key="same-video", kind="video"))

        with patch.object(self.service, "_run", side_effect=generate):
            threads = [threading.Thread(target=request_thumbnail) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(len(results), 4)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.service._thumbnail_locks, {})

    def test_corrupt_receipt_cannot_escape_derivation_root(self) -> None:
        receipt_id = "d" * 32
        self.service.metadata.put(receipt_id, {"id": receipt_id, "stored_name": "../../secret"})
        with self.assertRaises(ApiError) as raised:
            self.service.path(self.service.get(receipt_id))
        self.assertEqual(raised.exception.code, "unsafe_path")


class LibraryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(Path(self.temp.name))
        self.config.prepare()
        self.runtime = Runtime(
            self.config, AssetStore(self.config),
            JobStore(self.config.data_root / "metadata" / "jobs"),
            FakeComfy(),  # type: ignore[arg-type]
        )
        self.server = H3StudioServer(("127.0.0.1", 0), Handler, self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.asset_id = "a" * 32
        self.runtime.assets.metadata.put(self.asset_id, {
            "id": self.asset_id, "kind": "image", "filename": "old.png",
            "stored_name": f"{self.asset_id}.png", "mime_type": "image/png",
            "size": 1, "created_at": 1,
        })

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None, *, auth: bool = True):
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        if auth:
            headers["X-API-Key"] = "test-key"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read()
        connection.close()
        return response.status, json.loads(content) if content else {}

    def test_folder_create_move_rename_and_search_preserve_old_asset(self) -> None:
        status, created = self.request("POST", "/api/asset-folders", {"name": "Characters"})
        self.assertEqual(status, 201)
        folder_id = created["id"]
        status, updated = self.request("PATCH", f"/api/assets/{self.asset_id}", {
            "display_name": "Hero Reference", "folder_id": folder_id,
        })
        self.assertEqual(status, 200)
        self.assertEqual(updated["display_name"], "Hero Reference")
        self.assertEqual(updated["filename"], "old.png")
        status, found = self.request("GET", f"/api/assets?q=hero&folder_id={folder_id}")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in found["assets"]], [self.asset_id])
        status, deleted = self.request("DELETE", f"/api/asset-folders/{folder_id}")
        self.assertEqual(status, 200)
        self.assertEqual(deleted["assets_moved"], 1)
        self.assertEqual(deleted["subfolders_moved"], 0)
        status, found = self.request("GET", "/api/assets?q=hero")
        self.assertEqual(status, 200)
        self.assertIsNone(found["assets"][0]["folder_id"])

    def test_folder_delete_reparents_assets_and_subfolders_without_deleting_media(self) -> None:
        asset_path = self.runtime.assets.upload_root / f"{self.asset_id}.png"
        asset_path.write_bytes(PNG_1X1)
        status, parent = self.request("POST", "/api/asset-folders", {"name": "Parent"})
        self.assertEqual(status, 201)
        status, child = self.request("POST", "/api/asset-folders", {"name": "Child", "parent_id": parent["id"]})
        self.assertEqual(status, 201)
        status, _ = self.request("PATCH", f"/api/assets/{self.asset_id}", {"folder_id": parent["id"]})
        self.assertEqual(status, 200)

        status, deleted = self.request("DELETE", f"/api/asset-folders/{parent['id']}")
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted["assets_moved"], 1)
        self.assertEqual(deleted["subfolders_moved"], 1)
        self.assertIsNone(deleted["destination_folder_id"])
        self.assertIsNone(self.runtime.assets.get(self.asset_id).get("folder_id"))
        self.assertIsNone(self.runtime.folders.get(child["id"]).get("parent_id"))
        self.assertTrue(asset_path.exists())

    def test_folder_delete_conflict_keeps_the_tree_unchanged(self) -> None:
        status, existing = self.request("POST", "/api/asset-folders", {"name": "Child"})
        self.assertEqual(status, 201)
        status, parent = self.request("POST", "/api/asset-folders", {"name": "Parent"})
        self.assertEqual(status, 201)
        status, child = self.request("POST", "/api/asset-folders", {"name": "Child", "parent_id": parent["id"]})
        self.assertEqual(status, 201)
        status, _ = self.request("PATCH", f"/api/assets/{self.asset_id}", {"folder_id": parent["id"]})
        self.assertEqual(status, 200)

        status, body = self.request("DELETE", f"/api/asset-folders/{parent['id']}")
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "folder_name_conflict")
        self.assertEqual(self.runtime.assets.get(self.asset_id)["folder_id"], parent["id"])
        self.assertEqual(self.runtime.folders.get(child["id"])["parent_id"], parent["id"])
        self.assertEqual(self.runtime.folders.get(existing["id"])["name"], "Child")

    def test_asset_pin_is_persisted_and_sorted_first(self) -> None:
        other_id = "1" * 32
        other_path = self.runtime.assets.upload_root / f"{other_id}.png"
        other_path.write_bytes(PNG_1X1)
        self.runtime.assets.metadata.put(other_id, {
            "id": other_id, "kind": "image", "filename": "newer.png", "stored_name": other_path.name,
            "mime_type": "image/png", "size": len(PNG_1X1), "sha256": hashlib.sha256(PNG_1X1).hexdigest(),
            "media": {"width": 1, "height": 1}, "created_at": 999,
        })
        status, pinned = self.request("PATCH", f"/api/assets/{self.asset_id}", {"pinned": True})
        self.assertEqual(status, 200, pinned)
        self.assertTrue(pinned["pinned"])
        status, listed = self.request("GET", "/api/assets")
        self.assertEqual(status, 200)
        self.assertEqual(listed["assets"][0]["id"], self.asset_id)
        self.assertTrue(self.runtime.assets.get(self.asset_id)["pinned"])

    def test_folder_names_and_ids_reject_path_traversal(self) -> None:
        status, body = self.request("POST", "/api/asset-folders", {"name": "../../escape"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_folder_name")
        status, body = self.request("PATCH", f"/api/assets/{self.asset_id}", {"folder_id": "../../escape"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_id")

    def test_folder_move_rejects_cycles(self) -> None:
        status, parent = self.request("POST", "/api/asset-folders", {"name": "Parent"})
        self.assertEqual(status, 201)
        status, child = self.request("POST", "/api/asset-folders", {"name": "Child", "parent_id": parent["id"]})
        self.assertEqual(status, 201)
        status, body = self.request("PATCH", f"/api/asset-folders/{parent['id']}", {"parent_id": child["id"]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "folder_cycle")

    def test_thumbnail_requires_auth(self) -> None:
        status, body = self.request("GET", f"/api/assets/{self.asset_id}/thumbnail", auth=False)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_completed_job_list_exposes_cached_thumbnail_endpoint(self) -> None:
        job_id = "c" * 32
        output = self.config.comfy_output / "result.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video-result")
        self.runtime.jobs.put(job_id, {
            "id": job_id, "status": "completed", "output_type": "video",
            "outputs": [{"filename": output.name, "subfolder": "", "type": "output", "sha256": "abc"}],
            "created_at": 1, "updated_at": 2,
        })
        status, listing = self.request("GET", "/api/jobs")
        self.assertEqual(status, 200)
        listed = listing["jobs"][0]
        self.assertEqual(listed["thumbnail_url"], f"/api/jobs/{job_id}/thumbnail?index=0")
        self.assertEqual(listed["preview_url"], f"/api/preview?id={job_id}&index=0")
        self.assertEqual(listed["download_url"], f"/api/download?id={job_id}&index=0")
        self.assertEqual(listed["outputs"][0]["download_url"], f"/api/download?id={job_id}&index=0")
        self.assertNotIn(
            "download_url", self.runtime.jobs.get(job_id)["outputs"][0],
            "listing must not mutate durable job metadata",
        )

        def create_jpeg(_command, destination, **_kwargs):
            destination.write_bytes(b"jpeg-thumbnail")

        with patch.object(self.runtime.media, "_run", side_effect=create_jpeg):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            connection.request("GET", f"/api/jobs/{job_id}/thumbnail?index=0", headers={"X-API-Key": "test-key"})
            response = connection.getresponse()
            content = response.read()
            content_type = response.getheader("Content-Type")
            connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(content, b"jpeg-thumbnail")
        self.assertEqual(content_type, "image/jpeg")

    def test_job_listing_is_bounded_paginated_and_omits_workflow_graph(self) -> None:
        for index in range(25):
            job_id = f"{index:032x}"
            self.runtime.jobs.put(job_id, {
                "id": job_id, "status": "failed", "output_type": "video",
                "created_at": index, "updated_at": index,
                "workflow": {"huge": "x" * 1000}, "graph": {"nodes": [1]},
            })
        status, first = self.request("GET", "/api/jobs?limit=20")
        self.assertEqual(status, 200)
        self.assertEqual(len(first["jobs"]), 20)
        self.assertEqual(first["next_cursor"], "5.0:00000000000000000000000000000005")
        self.assertEqual(first["jobs"][0]["id"], f"{24:032x}")
        self.assertNotIn("workflow", first["jobs"][0])
        self.assertNotIn("graph", first["jobs"][0])
        status, second = self.request("GET", f"/api/jobs?limit=20&cursor={first['next_cursor']}")
        self.assertEqual(status, 200)
        self.assertEqual(len(second["jobs"]), 5)
        self.assertIsNone(second["next_cursor"])
        status, invalid = self.request("GET", "/api/jobs?limit=1000")
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "invalid_pagination")

    def test_derivation_is_a_result_until_explicit_save_to_asset(self) -> None:
        source_id = "b" * 32
        source_path = self.runtime.assets.upload_root / f"{source_id}.mp4"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"fake-video")
        self.runtime.assets.metadata.put(source_id, {
            "id": source_id, "kind": "video", "filename": "source.mp4",
            "stored_name": source_path.name, "mime_type": "video/mp4", "size": 10,
            "sha256": hashlib.sha256(b"fake-video").hexdigest(),
            "media": {"duration": 2.0, "fps": 24, "has_audio": True}, "created_at": 2,
        })

        def create_png(_command, destination, **_kwargs):
            destination.write_bytes(PNG_1X1)

        with patch.object(self.runtime.media, "_run", side_effect=create_png), patch.object(
            AssetStore, "_probe_image", return_value={"width": 1, "height": 1, "codec": "png"}
        ):
            status, receipt = self.request("POST", "/api/media/derive", {
                "source": {"type": "asset", "asset_id": source_id},
                "operation": "frame", "position": "current", "time": 1,
            })
            self.assertEqual(status, 201, receipt)
            receipt_id = receipt["receipt_id"]
            self.assertEqual(len(self.runtime.assets.list()), 2, "only the two seeded assets exist before explicit save")
            # The setup asset plus source are pre-existing; the receipt is not.
            self.assertNotIn(receipt_id, {item["id"] for item in self.runtime.assets.list()})
            status, listed = self.request("GET", "/api/derivations")
            self.assertEqual(status, 200, listed)
            self.assertEqual(listed["derivations"][0]["id"], receipt_id)
            status, pinned = self.request("PATCH", f"/api/derivations/{receipt_id}", {"pinned": True})
            self.assertEqual(status, 200, pinned)
            self.assertTrue(pinned["pinned"])
            self.assertNotIn("stored_name", listed["derivations"][0])
            self.assertNotIn("asset_id", listed["derivations"][0])
            status, saved = self.request("POST", f"/api/derivations/{receipt_id}/assets", {
                "display_name": "Saved frame",
            })
        self.assertEqual(status, 201, saved)
        self.assertEqual(saved["display_name"], "Saved frame")
        self.assertIn(saved["asset_id"], {item["id"] for item in self.runtime.assets.list()})
        status, listed = self.request("GET", "/api/derivations")
        self.assertEqual(status, 200, listed)
        self.assertEqual(listed["derivations"][0]["asset_id"], saved["asset_id"])
        status, folder = self.request("POST", "/api/asset-folders", {"name": "Derived"})
        self.assertEqual(status, 201, folder)
        status, reused = self.request("POST", f"/api/derivations/{receipt_id}/assets", {
            "display_name": "Renamed frame", "folder_id": folder["id"],
        })
        self.assertEqual(status, 201, reused)
        self.assertEqual(reused["asset_id"], saved["asset_id"])
        self.assertEqual(reused["display_name"], "Renamed frame")
        self.assertEqual(reused["folder_id"], folder["id"])

    def test_derivation_receipt_can_be_used_as_a_chain_source_and_deleted(self) -> None:
        receipt_id = "d" * 32
        source_path = self.runtime.media.root / f"{receipt_id}.mp4"
        source_path.write_bytes(b"derived-video")
        self.runtime.media.metadata.put(receipt_id, {
            "id": receipt_id, "kind": "video", "display_name": "derived.mp4",
            "filename": "derived.mp4", "stored_name": source_path.name,
            "mime_type": "video/mp4", "size": source_path.stat().st_size,
            "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "media": {"duration": 1.0, "fps": 24, "has_audio": False},
            "created_at": 1,
        })
        chained = {
            "id": "e" * 32, "receipt_id": "e" * 32, "kind": "image",
            "display_name": "frame.jpg", "content_url": "/api/derivations/" + "e" * 32 + "/content",
        }
        with patch.object(self.runtime.media, "derive", return_value=chained) as derive:
            status, body = self.request("POST", "/api/media/derive", {
                "source": {"type": "derivation", "receipt_id": receipt_id},
                "operation": "frame", "time": 0.5,
            })
        self.assertEqual(status, 201, body)
        source, metadata, request = derive.call_args.args
        self.assertEqual(source, source_path.resolve())
        self.assertEqual(metadata["source_receipt"], {"type": "derivation", "receipt_id": receipt_id})
        self.assertEqual(request["operation"], "frame")
        status, deleted = self.request("DELETE", f"/api/derivations/{receipt_id}")
        self.assertEqual(status, 200, deleted)
        self.assertFalse(source_path.exists())


if __name__ == "__main__":
    unittest.main()
