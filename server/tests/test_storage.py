from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server.errors import ApiError
from server.storage import AssetStore, JobStore
from server.tests.test_workflows import config


class AssetMetadataTests(unittest.TestCase):
    def test_library_duplicate_lookup_ignores_internal_and_missing_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = config(root)
            settings.prepare()
            store = AssetStore(settings)
            digest = "a" * 64
            for asset_id, visibility, stored_name in (
                ("1" * 32, "internal", "internal.png"),
                ("2" * 32, "library", "missing.png"),
                ("3" * 32, "library", "present.png"),
            ):
                store.metadata.put(asset_id, {
                    "id": asset_id, "kind": "image", "filename": stored_name,
                    "stored_name": stored_name, "sha256": digest,
                    "visibility": visibility, "created_at": 1,
                })
            (store.upload_root / "internal.png").write_bytes(b"internal")
            (store.upload_root / "present.png").write_bytes(b"present")

            found = store.find_library_duplicate(digest, requested_kind="image")

            self.assertIsNotNone(found)
            self.assertEqual(found["id"], "3" * 32)
            self.assertIsNone(store.find_library_duplicate(digest, requested_kind="video"))

    def test_legacy_cloned_asset_metadata_remains_visible_and_resolvable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = config(root)
            settings.prepare()
            store = AssetStore(settings)
            asset_id = "a" * 32
            source = store.upload_root / "legacy.png"
            source.write_bytes(b"legacy")
            store.metadata.put(asset_id, {
                "id": asset_id,
                "type": "image",
                "name": "old portrait.png",
                "comfy_path": "h3-studio/legacy.png",
                "content_url": "http://stale-machine.invalid/api/assets/old/content",
                "created_at": 1,
            })

            asset = store.get(asset_id)
            self.assertEqual(asset["kind"], "image")
            self.assertEqual(asset["filename"], "old portrait.png")
            self.assertEqual(asset["stored_name"], "legacy.png")
            self.assertEqual(store.content_path(asset).resolve(), source.resolve())
            public = store.list_public()[0]
            self.assertEqual(public["type"], "image")
            self.assertEqual(public["name"], "old portrait.png")
            self.assertEqual(public["content_url"], f"/api/assets/{asset_id}/content")
            self.assertEqual(public["thumbnail_url"], f"/api/assets/{asset_id}/thumbnail")

    def test_video_upload_is_probed_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = config(root)
            settings.prepare()
            content = b"\x00\x00\x00\x18ftypisom" + b"video-data"
            temporary = root / "upload.tmp"
            temporary.write_bytes(content)
            probe_json = '{"format":{"duration":"4.250"},"streams":[{"codec_type":"video","codec_name":"h264","width":1280,"height":720,"avg_frame_rate":"24/1","nb_frames":"102"},{"codec_type":"audio","codec_name":"aac"}]}'
            with patch("server.storage.subprocess.run", return_value=subprocess.CompletedProcess([], 0, probe_json, "")):
                asset = AssetStore(settings).import_file(
                    temporary, original_filename="clip.mp4", requested_kind="video", claimed_content_type="video/mp4"
                )
            self.assertEqual(asset["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(asset["media"]["duration"], 4.25)
            self.assertTrue(asset["media"]["has_audio"])
            self.assertEqual(asset["media"]["width"], 1280)
            self.assertEqual(asset["media"]["fps"], 24)
            self.assertEqual(asset["media"]["reference_fps"], 24)
            self.assertFalse(asset["media"]["normalized_to_24fps"])

    def test_image_signature_is_not_enough_when_decode_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = config(root)
            settings.prepare()
            temporary = root / "broken.png"
            temporary.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not-a-decodable-image")
            metadata = subprocess.CompletedProcess(
                [], 0, json.dumps({"streams": [{"codec_name": "png", "width": 1, "height": 1}]}), ""
            )
            decode_failure = subprocess.CompletedProcess([], 1, "", "decode failed")
            with patch("server.storage.subprocess.run", side_effect=[metadata, decode_failure]):
                with self.assertRaises(ApiError) as raised:
                    AssetStore(settings).import_file(
                        temporary,
                        original_filename="broken.png",
                        requested_kind="image",
                        claimed_content_type="image/png",
                    )
            self.assertEqual(raised.exception.code, "image_probe_failed")
            self.assertEqual(list((settings.comfy_input / "h3-studio").iterdir()), [])

    def test_non_24fps_video_is_normalized_and_records_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = config(root)
            settings.prepare()
            temporary = root / "upload.tmp"
            content = b"\x00\x00\x00\x18ftypisom" + b"video-data"
            temporary.write_bytes(content)

            def media_json(fps: int) -> str:
                return json.dumps({
                    "format": {"duration": "4.25"},
                    "streams": [{
                        "codec_type": "video", "codec_name": "h264", "width": 1280,
                        "height": 720, "avg_frame_rate": f"{fps}/1", "nb_frames": str(fps * 4),
                    }],
                })

            probe_results = [
                subprocess.CompletedProcess([], 0, media_json(30), ""),
                subprocess.CompletedProcess([], 0, media_json(24), ""),
            ]

            def normalize(_source: Path, destination: Path) -> None:
                destination.write_bytes(b"normalized-video")

            with patch("server.storage.subprocess.run", side_effect=probe_results), patch.object(
                AssetStore, "_normalize_video", side_effect=normalize
            ):
                asset = AssetStore(settings).import_file(
                    temporary,
                    original_filename="clip.mp4",
                    requested_kind="video",
                    claimed_content_type="video/mp4",
                )
            self.assertEqual(asset["media"]["source_fps"], 30)
            self.assertEqual(asset["media"]["reference_fps"], 24)
            self.assertTrue(asset["media"]["normalized_to_24fps"])
            self.assertTrue(asset["comfy_path"].endswith("-ref24.mp4"))


class JobStoreCacheTests(unittest.TestCase):
    def test_cache_is_isolated_from_nested_caller_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            value = {"id": "a" * 32, "created_at": 1, "outputs": [{"filename": "stable.mp4"}]}
            store.put(value["id"], value)
            cached = store.list()
            cached[0]["outputs"][0]["filename"] = "mutated.mp4"
            self.assertEqual(store.list()[0]["outputs"][0]["filename"], "stable.mp4")

    def test_concurrent_same_id_writes_leave_disk_and_cache_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            item_id = "b" * 32
            store.put(item_id, {"id": item_id, "created_at": 1, "revision": -1})
            barrier = threading.Barrier(3)

            def writer(start: int) -> None:
                barrier.wait()
                for revision in range(start, start + 40):
                    store.put(item_id, {"id": item_id, "created_at": revision, "revision": revision})

            threads = [threading.Thread(target=writer, args=(0,)), threading.Thread(target=writer, args=(100,))]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            self.assertEqual(store.get(item_id)["revision"], store.list()[0]["revision"])


if __name__ == "__main__":
    unittest.main()
