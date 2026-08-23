"""Bounded, read-only scene analysis for durable video assets."""

from __future__ import annotations

import importlib.util
import json
import math
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .errors import ApiError
from .security import validate_id
from .storage import AssetStore


MAX_SCENE_CUTS = 200
SCENE_ANALYSIS_TIMEOUT_SECONDS = 45
MAX_ANALYSIS_FPS = 240.0
MAX_ANALYSIS_FRAMES = 10_000_000
_PTS_TIME = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


_PYSCENEDETECT_SCRIPT = r"""
import json
import sys
from scenedetect import SceneManager, open_video
from scenedetect.detectors import ContentDetector

path, threshold, min_scene_len, maximum = sys.argv[1:]
video = open_video(path)
manager = SceneManager()
manager.add_detector(ContentDetector(
    threshold=float(threshold), min_scene_len=int(min_scene_len),
))
manager.detect_scenes(video, show_progress=False)
scenes = manager.get_scene_list(start_in_scene=True)
cuts = [int(start.get_frames()) for start, _ in scenes[1:]][:int(maximum) + 1]
print(json.dumps(cuts, separators=(",", ":")))
"""


class SceneAnalysisService:
    """Analyze only AssetStore-owned videos without writing derived media."""

    def __init__(
        self,
        assets: AssetStore,
        *,
        command_runner: Callable[..., Any] = subprocess.run,
        timeout_seconds: int = SCENE_ANALYSIS_TIMEOUT_SECONDS,
        slots: int = 2,
    ) -> None:
        self.assets = assets
        self.command_runner = command_runner
        self.timeout_seconds = max(1, int(timeout_seconds))
        self._slots = threading.BoundedSemaphore(max(1, int(slots)))

    def analyze(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_scene_analysis", "scene analysis body must be an object")
        extra = set(body) - {"asset_id", "threshold", "min_scene_seconds", "max_cuts"}
        if extra:
            raise ApiError(400, "invalid_scene_analysis", f"unsupported fields: {', '.join(sorted(extra))}")
        asset_id = validate_id(body.get("asset_id"), "asset id")
        threshold = self._number(body.get("threshold", 27.0), "threshold", 1.0, 99.0)
        min_scene_seconds = self._number(
            body.get("min_scene_seconds", 0.5), "min_scene_seconds", 0.1, 30.0,
        )
        max_cuts = self._integer(body.get("max_cuts", MAX_SCENE_CUTS), "max_cuts", 1, MAX_SCENE_CUTS)

        if not self._slots.acquire(blocking=False):
            raise ApiError(429, "scene_analysis_busy", "scene analysis capacity is busy; retry shortly")
        try:
            # One wall-clock budget covers probe, preferred detector and
            # fallback. A failed detector must not receive a fresh timeout.
            deadline = time.monotonic() + self.timeout_seconds
            asset = self.assets.get(asset_id)
            if asset.get("kind") != "video":
                raise ApiError(400, "asset_not_video", "scene analysis requires a video asset")
            path = self.assets.content_path(asset)
            root = self.assets.upload_root.resolve()
            try:
                path = path.resolve(strict=True)
                path.relative_to(root)
            except (OSError, ValueError) as error:
                raise ApiError(403, "unsafe_path", "asset path escapes the configured storage root") from error

            fps, frame_count = self._probe_video(path, deadline=deadline)
            min_scene_frames = max(1, int(round(min_scene_seconds * fps)))
            detector = "ffmpeg"
            cuts: list[int] | None = None
            if importlib.util.find_spec("scenedetect") is not None:
                cuts = self._detect_pyscenedetect(
                    path, threshold=threshold, min_scene_frames=min_scene_frames,
                    max_cuts=max_cuts, deadline=deadline,
                )
                if cuts is not None:
                    detector = "pyscenedetect"
            if cuts is None:
                cuts = self._detect_ffmpeg(
                    path, fps=fps, threshold=threshold / 100.0,
                    min_scene_frames=min_scene_frames, max_cuts=max_cuts,
                    deadline=deadline,
                )

            candidates = sorted({
                int(frame) for frame in cuts
                if min_scene_frames <= int(frame) <= frame_count - min_scene_frames
            })
            valid: list[int] = []
            for frame in candidates:
                if not valid or frame - valid[-1] >= min_scene_frames:
                    valid.append(frame)
            truncated = len(valid) > max_cuts
            valid = valid[:max_cuts]
            boundaries = [0, *valid, frame_count]
            scenes = [
                {
                    "index": index,
                    "start_frame": start,
                    "end_frame": end,
                    "start_sec": round(start / fps, 6),
                    "end_sec": round(end / fps, 6),
                    "duration_sec": round((end - start) / fps, 6),
                }
                for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]))
                if end > start
            ]
            return {
                "asset_id": asset_id,
                "detector": detector,
                "fps": round(fps, 6),
                "frame_count": frame_count,
                "cut_frames": valid,
                "scenes": scenes,
                "truncated": truncated,
                "max_cuts": max_cuts,
            }
        finally:
            self._slots.release()

    @staticmethod
    def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
        if isinstance(value, bool):
            raise ApiError(400, "invalid_scene_analysis", f"{name} must be a number")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as error:
            raise ApiError(400, "invalid_scene_analysis", f"{name} must be a number") from error
        if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
            raise ApiError(400, "invalid_scene_analysis", f"{name} must be between {minimum:g} and {maximum:g}")
        return parsed

    @staticmethod
    def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ApiError(400, "invalid_scene_analysis", f"{name} must be an integer between {minimum} and {maximum}")
        return value

    @staticmethod
    def _rate(value: Any) -> float:
        try:
            numerator, denominator = str(value).split("/", 1)
            return float(numerator) / float(denominator) if float(denominator) else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    @staticmethod
    def _remaining_timeout(deadline: float, cap: float | None = None) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ApiError(504, "scene_analysis_timeout", "scene analysis exceeded its time limit")
        return min(remaining, cap) if cap is not None else remaining

    def _run(self, command: list[str], *, timeout: float | None = None) -> Any:
        try:
            return self.command_runner(
                command, capture_output=True, text=True,
                timeout=self.timeout_seconds if timeout is None else timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ApiError(504, "scene_analysis_timeout", "scene analysis exceeded its time limit") from error
        except OSError as error:
            raise ApiError(503, "scene_analysis_unavailable", "required media analysis executable is unavailable") from error

    def _probe_video(self, path: Path, *, deadline: float | None = None) -> tuple[float, int]:
        timeout = (
            min(self.timeout_seconds, 15)
            if deadline is None
            else self._remaining_timeout(deadline, 15)
        )
        completed = self._run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate,r_frame_rate,nb_frames,duration:format=duration",
            "-of", "json", str(path),
        ], timeout=timeout)
        try:
            payload = json.loads(completed.stdout)
            stream = payload["streams"][0]
            fps = self._rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
            duration = float(stream.get("duration") or (payload.get("format") or {}).get("duration") or 0)
            frame_count = int(stream.get("nb_frames") or 0)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ApiError(422, "scene_probe_failed", "video stream metadata is invalid") from error
        if (
            completed.returncode
            or not math.isfinite(fps)
            or fps <= 0
            or fps > MAX_ANALYSIS_FPS
            or not math.isfinite(duration)
            or duration < 0
            or (duration > 0 and duration * fps > MAX_ANALYSIS_FRAMES)
        ):
            raise ApiError(422, "scene_probe_failed", "video stream could not be inspected")
        if frame_count <= 0 and duration > 0:
            frame_count = int(round(duration * fps))
        if frame_count <= 0 or frame_count > MAX_ANALYSIS_FRAMES:
            raise ApiError(422, "scene_probe_failed", "video frame count is unavailable or exceeds the safe analysis limit")
        return fps, frame_count

    def _detect_pyscenedetect(
        self, path: Path, *, threshold: float, min_scene_frames: int,
        max_cuts: int, deadline: float | None = None,
    ) -> list[int] | None:
        timeout = (
            self.timeout_seconds
            if deadline is None
            else self._remaining_timeout(deadline)
        )
        completed = self._run([
            sys.executable, "-c", _PYSCENEDETECT_SCRIPT, str(path), str(threshold),
            str(min_scene_frames), str(max_cuts),
        ], timeout=timeout)
        if completed.returncode:
            return None
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, list) or any(not isinstance(frame, int) for frame in value):
            return None
        return value

    def _detect_ffmpeg(
        self, path: Path, *, fps: float, threshold: float,
        min_scene_frames: int, max_cuts: int, deadline: float | None = None,
    ) -> list[int]:
        timeout = (
            self.timeout_seconds
            if deadline is None
            else self._remaining_timeout(deadline)
        )
        completed = self._run([
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", str(path),
            "-an", "-filter:v", f"select=gt(scene\\,{threshold:.6f}),showinfo",
            # Stop once one more selected frame than the response budget was
            # emitted. This bounds captured showinfo stderr for low-threshold
            # or adversarial high-cut videos.
            "-frames:v", str(max_cuts + 1),
            "-f", "null", "-",
        ], timeout=timeout)
        if completed.returncode:
            raise ApiError(422, "scene_detection_failed", "ffmpeg could not analyze video scenes")
        # Parse at most one extra valid item so the response can attest
        # truncation without returning an unbounded list to callers.
        frames: list[int] = []
        for match in _PTS_TIME.finditer(completed.stderr or ""):
            frame = int(round(float(match.group(1)) * fps))
            if frame <= 0 or (frames and frame - frames[-1] < min_scene_frames):
                continue
            frames.append(frame)
            if len(frames) > max_cuts:
                break
        return frames
