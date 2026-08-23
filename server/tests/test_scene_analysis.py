from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
import random
from pathlib import Path
from unittest.mock import patch

from server.errors import ApiError
from server.scene_analysis import SceneAnalysisService
from server.storage import AssetStore
from server.tests.test_workflows import config


class SceneAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = config(Path(self.temp.name))
        self.settings.prepare()
        self.assets = AssetStore(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_asset(self, asset_id: str, kind: str = "video") -> Path:
        suffix = ".mp4" if kind == "video" else ".png"
        path = self.assets.upload_root / f"{asset_id}{suffix}"
        path.write_bytes(b"asset")
        self.assets.metadata.put(asset_id, {
            "id": asset_id, "kind": kind, "filename": path.name,
            "stored_name": path.name, "comfy_path": f"h3-studio/{path.name}",
            "created_at": time.time(),
        })
        return path

    def test_prefers_pyscenedetect_and_builds_bounded_scene_contract(self) -> None:
        asset_id = "a" * 32
        self.add_asset(asset_id)
        service = SceneAnalysisService(self.assets)
        with (
            patch("server.scene_analysis.importlib.util.find_spec", return_value=object()),
            patch.object(service, "_probe_video", return_value=(24.0, 240)),
            patch.object(service, "_detect_pyscenedetect", return_value=[48, 96, 144]),
            patch.object(service, "_detect_ffmpeg") as fallback,
        ):
            result = service.analyze({"asset_id": asset_id, "max_cuts": 2})
        self.assertEqual(result["detector"], "pyscenedetect")
        self.assertEqual(result["cut_frames"], [48, 96])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["frame_count"], 240)
        self.assertEqual(result["scenes"][1]["start_sec"], 2.0)
        self.assertEqual(result["scenes"][-1]["end_frame"], 240)
        fallback.assert_not_called()

    def test_falls_back_to_ffmpeg_when_pyscenedetect_is_unavailable(self) -> None:
        asset_id = "b" * 32
        self.add_asset(asset_id)
        service = SceneAnalysisService(self.assets)
        with (
            patch("server.scene_analysis.importlib.util.find_spec", return_value=None),
            patch.object(service, "_probe_video", return_value=(30.0, 300)),
            patch.object(service, "_detect_ffmpeg", return_value=[75, 225]) as fallback,
        ):
            result = service.analyze({"asset_id": asset_id})
        self.assertEqual(result["detector"], "ffmpeg")
        self.assertEqual(result["cut_frames"], [75, 225])
        fallback.assert_called_once()

    def test_rejects_non_video_and_unsafe_asset_path(self) -> None:
        image_id = "c" * 32
        self.add_asset(image_id, "image")
        service = SceneAnalysisService(self.assets)
        with self.assertRaises(ApiError) as raised:
            service.analyze({"asset_id": image_id})
        self.assertEqual(raised.exception.code, "asset_not_video")

        video_id = "d" * 32
        outside = Path(self.temp.name) / "outside.mp4"
        outside.write_bytes(b"outside")
        link = self.assets.upload_root / f"{video_id}.mp4"
        link.symlink_to(outside)
        self.assets.metadata.put(video_id, {
            "id": video_id, "kind": "video", "filename": link.name,
            "stored_name": link.name, "created_at": time.time(),
        })
        with self.assertRaises(ApiError) as raised:
            service.analyze({"asset_id": video_id})
        self.assertEqual(raised.exception.code, "unsafe_path")

    def test_timeout_and_concurrency_are_controlled(self) -> None:
        def timeout(command, **_kwargs):
            raise subprocess.TimeoutExpired(command, 1)

        service = SceneAnalysisService(self.assets, command_runner=timeout, timeout_seconds=1)
        with self.assertRaises(ApiError) as raised:
            service._run(["ffprobe"])
        self.assertEqual(raised.exception.status, 504)
        self.assertEqual(raised.exception.code, "scene_analysis_timeout")

        asset_id = "e" * 32
        self.add_asset(asset_id)
        self.assertTrue(service._slots.acquire(blocking=False))
        self.assertTrue(service._slots.acquire(blocking=False))
        try:
            with self.assertRaises(ApiError) as raised:
                service.analyze({"asset_id": asset_id})
            self.assertEqual(raised.exception.status, 429)
            self.assertEqual(raised.exception.code, "scene_analysis_busy")
        finally:
            service._slots.release()
            service._slots.release()

    def test_analysis_uses_one_deadline_and_ffmpeg_bounds_showinfo_output(self) -> None:
        commands: list[tuple[list[str], float]] = []

        def runner(command, **kwargs):
            commands.append((command, kwargs["timeout"]))
            return subprocess.CompletedProcess(
                command, 0, "", "[Parsed_showinfo] n:0 pts_time:1.0\n",
            )

        service = SceneAnalysisService(self.assets, command_runner=runner, timeout_seconds=7)
        cuts = service._detect_ffmpeg(
            Path("bounded.mp4"), fps=24.0, threshold=0.27,
            min_scene_frames=1, max_cuts=3,
            deadline=time.monotonic() + 2,
        )
        self.assertEqual(cuts, [24])
        command, timeout = commands[0]
        frame_limit = command.index("-frames:v")
        self.assertEqual(command[frame_limit + 1], "4")
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 2)

        with self.assertRaises(ApiError) as raised:
            service._remaining_timeout(time.monotonic() - 1)
        self.assertEqual(raised.exception.code, "scene_analysis_timeout")

    def test_probe_rejects_nonfinite_or_unbounded_stream_metadata(self) -> None:
        cases = (
            {"avg_frame_rate": "1/0", "nb_frames": "120", "duration": "5"},
            {"avg_frame_rate": "1000/1", "nb_frames": "120", "duration": "5"},
            {"avg_frame_rate": "24/1", "nb_frames": "10000001", "duration": "5"},
            {"avg_frame_rate": "24/1", "nb_frames": "120", "duration": "NaN"},
            {"avg_frame_rate": "24/1", "nb_frames": "0", "duration": "1e308"},
        )
        for stream in cases:
            with self.subTest(stream=stream):
                payload = json.dumps({
                    "streams": [stream],
                    "format": {"duration": stream["duration"]},
                })

                def runner(command, **_kwargs):
                    return subprocess.CompletedProcess(command, 0, payload, "")

                service = SceneAnalysisService(self.assets, command_runner=runner)
                with self.assertRaises(ApiError) as raised:
                    service._probe_video(Path("untrusted.mp4"))
                self.assertEqual(raised.exception.code, "scene_probe_failed")

    def test_tool_timeout_through_public_analysis_releases_capacity(self) -> None:
        asset_id = "1" * 32
        self.add_asset(asset_id)

        def timeout(command, **_kwargs):
            raise subprocess.TimeoutExpired(command, 1)

        service = SceneAnalysisService(
            self.assets, command_runner=timeout, timeout_seconds=1, slots=1,
        )
        with self.assertRaises(ApiError) as raised:
            service.analyze({"asset_id": asset_id})
        self.assertEqual((raised.exception.status, raised.exception.code), (504, "scene_analysis_timeout"))
        # Failure must never leak the bounded analysis slot.
        self.assertTrue(service._slots.acquire(blocking=False))
        service._slots.release()

    def test_detector_timeout_is_terminal_and_does_not_start_fallback(self) -> None:
        asset_id = "5" * 32
        self.add_asset(asset_id)

        def timeout(command, **_kwargs):
            raise subprocess.TimeoutExpired(command, 1)

        service = SceneAnalysisService(self.assets, command_runner=timeout, timeout_seconds=1)
        with (
            patch("server.scene_analysis.importlib.util.find_spec", return_value=object()),
            patch.object(service, "_probe_video", return_value=(24.0, 120)),
            patch.object(service, "_detect_ffmpeg") as fallback,
            self.assertRaises(ApiError) as raised,
        ):
            service.analyze({"asset_id": asset_id})
        self.assertEqual((raised.exception.status, raised.exception.code), (504, "scene_analysis_timeout"))
        fallback.assert_not_called()

    def test_ffprobe_failure_injection_rejects_untrusted_metadata(self) -> None:
        class Completed:
            def __init__(self, stdout: str, returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = ""
                self.returncode = returncode

        cases = (
            (Completed("not-json"), "invalid JSON"),
            (Completed(json.dumps({"streams": []})), "missing stream"),
            (Completed(json.dumps({
                "streams": [{"avg_frame_rate": "0/0", "nb_frames": "120"}],
                "format": {"duration": "5"},
            })), "zero fps"),
            (Completed(json.dumps({
                "streams": [{"avg_frame_rate": "24/1", "nb_frames": "0"}],
                "format": {"duration": "0"},
            })), "zero frames"),
            (Completed(json.dumps({
                "streams": [{"avg_frame_rate": "24/1", "nb_frames": "120"}],
            }), returncode=1), "nonzero exit"),
        )
        for completed, label in cases:
            service = SceneAnalysisService(self.assets, command_runner=lambda *_a, **_k: completed)
            with self.subTest(label=label), self.assertRaises(ApiError) as raised:
                service._probe_video(Path("/unused/video.mp4"))
            self.assertEqual(raised.exception.code, "scene_probe_failed")

    def test_cut_normalization_properties_are_bounded_contiguous_and_read_only(self) -> None:
        asset_id = "2" * 32
        self.add_asset(asset_id)
        service = SceneAnalysisService(self.assets)
        rng = random.Random(1302)
        before = {path.name: path.stat().st_size for path in self.assets.upload_root.iterdir()}
        with (
            patch("server.scene_analysis.importlib.util.find_spec", return_value=object()),
            patch.object(service, "_probe_video", return_value=(24.0, 480)),
        ):
            for maximum in (1, 2, 7, 25, 200):
                raw = [rng.randint(-200, 700) for _ in range(600)]
                raw.extend([0, 12, 12, 479, 480, 9999])
                with self.subTest(maximum=maximum), patch.object(
                    service, "_detect_pyscenedetect", return_value=raw,
                ):
                    result = service.analyze({
                        "asset_id": asset_id,
                        "max_cuts": maximum,
                        "min_scene_seconds": 0.5,
                    })
                cuts = result["cut_frames"]
                scenes = result["scenes"]
                self.assertLessEqual(len(cuts), maximum)
                self.assertEqual(cuts, sorted(set(cuts)))
                self.assertTrue(all(right - left >= 12 for left, right in zip([0, *cuts], [*cuts, 480])))
                self.assertEqual(scenes[0]["start_frame"], 0)
                self.assertEqual(scenes[-1]["end_frame"], 480)
                self.assertTrue(all(
                    scenes[index]["end_frame"] == scenes[index + 1]["start_frame"]
                    for index in range(len(scenes) - 1)
                ))
        after = {path.name: path.stat().st_size for path in self.assets.upload_root.iterdir()}
        self.assertEqual(after, before)

    def test_metadata_traversal_and_symlink_escape_never_reach_tools(self) -> None:
        service = SceneAnalysisService(self.assets)
        outside = Path(self.temp.name) / "outside-2.mp4"
        outside.write_bytes(b"outside")
        cases = {
            "3" * 32: "../../outside-2.mp4",
            "4" * 32: "nested-link.mp4",
        }
        (self.assets.upload_root / "nested-link.mp4").symlink_to(outside)
        for asset_id, stored_name in cases.items():
            self.assets.metadata.put(asset_id, {
                "id": asset_id, "kind": "video", "filename": "video.mp4",
                "stored_name": stored_name, "created_at": time.time(),
            })
        with patch.object(service, "_probe_video") as probe:
            for asset_id in cases:
                with self.subTest(asset_id=asset_id), self.assertRaises(ApiError) as raised:
                    service.analyze({"asset_id": asset_id})
                self.assertEqual(raised.exception.code, "unsafe_path")
        probe.assert_not_called()

    def test_rejects_unbounded_or_unknown_options(self) -> None:
        asset_id = "f" * 32
        self.add_asset(asset_id)
        service = SceneAnalysisService(self.assets)
        for body in (
            {"asset_id": asset_id, "max_cuts": 201},
            {"asset_id": asset_id, "threshold": 0},
            {"asset_id": asset_id, "unexpected": True},
        ):
            with self.subTest(body=body), self.assertRaises(ApiError) as raised:
                service.analyze(body)
            self.assertEqual(raised.exception.code, "invalid_scene_analysis")


if __name__ == "__main__":
    unittest.main()
