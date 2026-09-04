"""Safe, temporary media derivatives and lazy thumbnail generation."""

from __future__ import annotations

import hashlib
import math
import mimetypes
import os
import queue
import signal
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .errors import ApiError
from .h3_reference import (
    ALGORITHM_VERSION,
    ReferenceParameters,
    calculate_reference_plan,
    estimate_video_tokens,
    idempotency_key,
)
from .security import safe_filename, secure_join, validate_id
from .storage import AssetStore, JsonStore


def _number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ApiError(400, "invalid_parameter", f"{label} must be a finite number")
    result = float(value)
    if result < minimum:
        raise ApiError(400, "invalid_parameter", f"{label} must be >= {minimum}")
    return result


class MediaService:
    def __init__(
        self,
        config: Config,
        assets: AssetStore,
        mutation_lock: threading.RLock | None = None,
    ) -> None:
        self.config = config
        self.assets = assets
        self.metadata = JsonStore(config.data_root / "metadata" / "derivations")
        self.root = config.data_root / "derivations"
        self.thumbnails = config.data_root / "thumbnails"
        self.root.mkdir(parents=True, exist_ok=True)
        self.thumbnails.mkdir(parents=True, exist_ok=True)
        self._slots = threading.BoundedSemaphore(2)
        self._lock = threading.RLock()
        self._thumbnail_locks_guard = threading.Lock()
        self._thumbnail_locks: dict[str, threading.Lock] = {}
        self._thumbnail_lock_users: dict[str, int] = {}
        self._mutation_lock = mutation_lock or threading.RLock()
        self._reserved_bytes = 0
        # Derivatives are intentionally temporary results.  Reconcile crash
        # remnants and expire old receipts at startup so an abandoned browser
        # workflow cannot leak storage forever.
        self.garbage_collect(older_than_seconds=config.asset_ttl_days * 86400)

    def used_bytes(self) -> int:
        return sum(int(item.get("size", 0) or 0) for item in self.metadata.list())

    def quota_bytes(self) -> int:
        """Temporary storage already committed or reserved by active ffmpeg."""

        with self._lock:
            return self.used_bytes() + self._reserved_bytes

    def _stored_bytes(self) -> int:
        thumbnails = 0
        for path in self.thumbnails.glob("*.jpg"):
            try:
                thumbnails += path.stat().st_size
            except OSError:
                continue
        return self.assets.used_bytes() + self.used_bytes() + thumbnails

    @contextmanager
    def _reserve_output(self, budget: int):
        budget = max(1, int(budget))
        with self._mutation_lock, self._lock:
            if self._stored_bytes() + self._reserved_bytes + budget > self.config.max_asset_storage_bytes:
                raise ApiError(507, "media_quota", "media storage quota would be exceeded")
            try:
                free = shutil.disk_usage(self.root).free
            except OSError as error:
                raise ApiError(507, "disk_full", "free disk space could not be verified") from error
            if free < budget:
                raise ApiError(507, "disk_full", "insufficient free disk space for derived media")
            self._reserved_bytes += budget
        try:
            yield
        finally:
            with self._mutation_lock, self._lock:
                self._reserved_bytes -= budget

    def _output_budget(
        self,
        output_kind: str,
        operation: str,
        source: Path,
        duration: float,
    ) -> int:
        limit = {
            "image": self.config.max_image_bytes,
            "video": self.config.max_video_bytes,
            "audio": self.config.max_audio_bytes,
        }[output_kind]
        # Reserve the hard output ceiling, not an average codec estimate.  WAV
        # channel count/rate and pathological re-encodes can otherwise race the
        # shared quota even when their normal-case estimate is small.
        return limit

    def garbage_collect(self, *, older_than_seconds: float) -> dict[str, int]:
        cutoff = time.time() - max(0.0, float(older_than_seconds))
        removed_receipts = 0
        removed_files = 0
        removed_thumbnails = 0
        with self._mutation_lock, self._lock:
            live_names: set[str] = set()
            for value in self.metadata.list():
                receipt_id = value.get("id")
                stored_name = value.get("stored_name")
                if not isinstance(receipt_id, str) or not isinstance(stored_name, str):
                    continue
                created_at = float(value.get("created_at", 0) or 0)
                path: Path | None = None
                try:
                    path = secure_join(self.root, stored_name)
                except ApiError:
                    pass
                expired = created_at <= cutoff
                missing = path is None or not path.is_file()
                if expired or missing:
                    try:
                        self.metadata.delete(receipt_id)
                        removed_receipts += 1
                    except ApiError:
                        pass
                    if path is not None and path.is_file():
                        path.unlink(missing_ok=True)
                        removed_files += 1
                    continue
                live_names.add(stored_name)
            for path in self.root.iterdir():
                if not path.is_file() or path.name in live_names:
                    continue
                try:
                    stale = path.stat().st_mtime <= cutoff
                except FileNotFoundError:
                    continue
                if stale:
                    path.unlink(missing_ok=True)
                    removed_files += 1
            for path in self.thumbnails.glob("*.jpg"):
                try:
                    stale = path.stat().st_mtime <= cutoff
                except FileNotFoundError:
                    continue
                if stale:
                    path.unlink(missing_ok=True)
                    removed_thumbnails += 1
        return {
            "derivation_receipts": removed_receipts,
            "derivation_files": removed_files,
            "thumbnails": removed_thumbnails,
        }

    @staticmethod
    def _run(command: list[str], destination: Path, *, timeout: int = 600) -> None:
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError as error:
            destination.unlink(missing_ok=True)
            raise ApiError(503, "ffmpeg_unavailable", "ffmpeg is unavailable on the MiniMax H3 Video Studio server") from error
        except (OSError, subprocess.TimeoutExpired) as error:
            destination.unlink(missing_ok=True)
            raise ApiError(422, "media_processing_failed", "ffmpeg could not process the media") from error
        if completed.returncode or not destination.is_file() or destination.stat().st_size <= 0:
            destination.unlink(missing_ok=True)
            raise ApiError(422, "media_processing_failed", "ffmpeg failed to create the requested derivative")

    def thumbnail(self, source: Path, *, cache_key: str, kind: str) -> Path:
        if kind not in {"image", "video"}:
            raise ApiError(400, "thumbnail_unsupported", "only image and video media have thumbnails")
        # Include the thumbnail recipe version so improvements invalidate stale
        # derivatives without deleting user media or mutating existing files.
        digest = hashlib.sha256(f"v2:{kind}:{cache_key}".encode("utf-8")).hexdigest()
        destination = secure_join(self.thumbnails, f"{digest}.jpg")
        if destination.is_file() and destination.stat().st_size:
            return destination
        with self._thumbnail_locks_guard:
            thumbnail_lock = self._thumbnail_locks.setdefault(digest, threading.Lock())
            self._thumbnail_lock_users[digest] = self._thumbnail_lock_users.get(digest, 0) + 1
        thumbnail_lock.acquire()
        try:
            if destination.is_file() and destination.stat().st_size:
                return destination
            return self._create_thumbnail(source, destination, kind=kind)
        finally:
            thumbnail_lock.release()
            with self._thumbnail_locks_guard:
                users = self._thumbnail_lock_users.get(digest, 1) - 1
                if users <= 0 and self._thumbnail_locks.get(digest) is thumbnail_lock:
                    self._thumbnail_locks.pop(digest, None)
                    self._thumbnail_lock_users.pop(digest, None)
                else:
                    self._thumbnail_lock_users[digest] = users

    def _create_thumbnail(self, source: Path, destination: Path, *, kind: str) -> Path:
        staging = destination.with_name(f"{destination.stem}.tmp-{uuid.uuid4().hex}.jpg")
        command = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
        command += ["-i", str(source), "-frames:v", "1"]
        if kind == "video":
            # Generated clips commonly begin on a black transition frame.
            # Pick the most representative frame from the opening sample
            # window instead of permanently caching frame zero.
            command += ["-vf", "thumbnail=48,scale='min(480,iw)':-2"]
        else:
            command += ["-vf", "scale='min(480,iw)':-2"]
        command += ["-q:v", "4", "-fs", str(self.config.max_image_bytes + 1), str(staging)]
        if not self._slots.acquire(timeout=5):
            raise ApiError(429, "media_busy", "two media operations are already running")
        try:
            with self._reserve_output(self.config.max_image_bytes):
                try:
                    self._run(command, staging, timeout=60)
                    os.replace(staging, destination)
                    if destination.stat().st_size > self.config.max_image_bytes:
                        destination.unlink(missing_ok=True)
                        raise ApiError(413, "thumbnail_too_large", "thumbnail exceeds the configured image size limit")
                finally:
                    staging.unlink(missing_ok=True)
        finally:
            self._slots.release()
        return destination

    def derive(
        self,
        source: Path,
        source_meta: dict[str, Any],
        data: dict[str, Any],
        *,
        progress: Callable[[int], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        operation = data.get("operation")
        allowed = {"video_trim", "frame", "audio_trim", "extract_audio", "remove_audio", "prepare_h3_reference"}
        if operation not in allowed:
            raise ApiError(400, "invalid_operation", f"operation must be one of {', '.join(sorted(allowed))}")
        kind = str(source_meta.get("kind", ""))
        media = source_meta.get("media", {}) if isinstance(source_meta.get("media"), dict) else {}
        if operation == "prepare_h3_reference":
            if kind != "video":
                raise ApiError(400, "media_kind_mismatch", "prepare_h3_reference requires video input")
            return self._prepare_h3_reference(
                source, source_meta, data, progress=progress, cancel_event=cancel_event,
            )
        duration = float(media.get("duration", 0) or 0)
        raw_video_duration = media.get("video_duration")
        if not raw_video_duration:
            frame_count = float(media.get("frame_count", 0) or 0)
            fps = float(media.get("fps", 0) or 0)
            raw_video_duration = frame_count / fps if frame_count > 0 and fps > 0 else duration
        video_duration = float(raw_video_duration)
        if operation in {"video_trim", "frame", "extract_audio", "remove_audio"} and kind != "video":
            raise ApiError(400, "media_kind_mismatch", f"{operation} requires video input")
        if operation == "audio_trim" and kind != "audio":
            raise ApiError(400, "media_kind_mismatch", "audio_trim requires audio input")
        if operation == "extract_audio" and not media.get("has_audio"):
            raise ApiError(422, "audio_stream_missing", "source video has no audio stream")

        receipt_id = uuid.uuid4().hex
        output_kind = "image" if operation == "frame" else "audio" if operation in {"audio_trim", "extract_audio"} else "video"
        # WAV has an unambiguous RIFF/WAVE signature understood by the same
        # upload validator used by AssetStore.  MP4-family .m4a files use an
        # ftyp header and cannot be classified as audio safely from a short
        # signature alone.
        extension = ".jpg" if output_kind == "image" else ".wav" if output_kind == "audio" else ".mp4"
        destination = secure_join(self.root, receipt_id + extension)
        staging = destination.with_name(f"{receipt_id}.tmp-{uuid.uuid4().hex}{extension}")
        command = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
        label = str(operation)
        requested_duration = duration
        if operation in {"video_trim", "audio_trim"}:
            start = _number(data.get("start", 0), "start")
            end = _number(data.get("end"), "end")
            if end <= start or (duration > 0 and end > duration + 0.05):
                raise ApiError(400, "invalid_time_range", "end must be after start and within source duration")
            command += ["-ss", f"{start:.6f}", "-i", str(source), "-t", f"{end-start:.6f}"]
            requested_duration = end - start
            if operation == "video_trim":
                command += ["-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
            else:
                command += ["-vn", "-c:a", "pcm_s16le"]
            label = f"trim-{start:g}-{end:g}"
        elif operation == "frame":
            position = data.get("position", "current")
            if position not in {"first", "last", "current"}:
                raise ApiError(400, "invalid_parameter", "position must be first, last, or current")
            if position == "first":
                at = 0.0
            elif position == "last":
                # Decode the final *video-stream* tail and repeatedly replace
                # one image. Container duration may be extended by a longer
                # audio stream, so EOF-relative seeking is incorrect here.
                tail = max(0.001, min(2.0, video_duration if video_duration > 0 else 2.0))
                tail_start = max(0.0, video_duration - tail)
                command += [
                    "-ss", f"{tail_start:.6f}", "-i", str(source),
                    "-map", "0:v:0", "-update", "1", "-q:v", "2",
                ]
                label = f"{position}-frame"
                at = None
            else:
                at = _number(data.get("time"), "time")
                if video_duration > 0 and at > video_duration:
                    raise ApiError(400, "invalid_parameter", "time must be within source duration")
            if at is not None:
                command += ["-ss", f"{at:.6f}", "-i", str(source), "-frames:v", "1", "-q:v", "2"]
                label = f"{position}-frame"
        elif operation == "extract_audio":
            command += ["-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le"]
        elif operation == "remove_audio":
            command += [
                "-i", str(source), "-map", "0:v:0", "-an", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
            ]
        limit = {"image": self.config.max_image_bytes, "video": self.config.max_video_bytes, "audio": self.config.max_audio_bytes}[output_kind]
        # ffmpeg stops muxing at this boundary; the postcondition below rejects
        # the one-byte-over sentinel instead of accepting a truncated result.
        command += ["-fs", str(limit + 1), str(staging)]
        budget = self._output_budget(output_kind, str(operation), source, requested_duration)

        if not self._slots.acquire(blocking=False):
            raise ApiError(429, "media_busy", "two media operations are already running")
        try:
            with self._reserve_output(budget):
                self._run(command, staging)
                os.replace(staging, destination)
                probe = AssetStore._probe_image(destination) if output_kind == "image" else AssetStore._probe_media(destination, output_kind)
                size = destination.stat().st_size
                if size > limit:
                    raise ApiError(413, "derived_too_large", "derived media exceeds the configured size limit")
                digest = hashlib.sha256()
                with destination.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                sha256 = digest.hexdigest()
                display_name = safe_filename(str(data.get("display_name") or f"{source.stem}-{label}{extension}"))
                value = {
                    "id": receipt_id, "kind": output_kind, "display_name": display_name,
                    "filename": display_name, "stored_name": destination.name,
                    "mime_type": mimetypes.guess_type(destination.name)[0] or "application/octet-stream",
                    "size": size, "sha256": sha256, "media": probe,
                    "created_at": time.time(), "source": source_meta.get("source_receipt", {}),
                    "operation": operation,
                    "parameters": {key: data[key] for key in ("start", "end", "time", "position") if key in data},
                }
                with self._mutation_lock, self._lock:
                    if self._stored_bytes() + size > self.config.max_asset_storage_bytes:
                        raise ApiError(507, "media_quota", "derived media storage quota would be exceeded")
                    self.metadata.put(receipt_id, value)
                return self.public(value)
        except Exception:
            destination.unlink(missing_ok=True)
            try:
                self.metadata.delete(receipt_id)
            except ApiError:
                pass
            raise
        finally:
            staging.unlink(missing_ok=True)
            self._slots.release()

    def mux_audio(
        self,
        video: Path,
        video_meta: dict[str, Any],
        audio: Path,
        audio_meta: dict[str, Any],
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically replace a video's audio, padding/trimming to exact duration."""
        if video_meta.get("kind") != "video":
            raise ApiError(400, "media_kind_mismatch", "video must identify a video input")
        audio_kind = audio_meta.get("kind")
        audio_media = audio_meta.get("media") if isinstance(audio_meta.get("media"), dict) else {}
        if audio_kind not in {"video", "audio"} or (audio_kind == "video" and audio_media.get("has_audio") is not True):
            raise ApiError(422, "audio_stream_missing", "audio must identify an audio file or a video with a usable audio stream")
        video_media = video_meta.get("media") if isinstance(video_meta.get("media"), dict) else {}
        duration = _number(data.get("duration", video_media.get("video_duration") or video_media.get("duration")), "duration", minimum=0.001)
        known_duration = float(video_media.get("video_duration") or video_media.get("duration") or 0)
        if known_duration > 0 and duration > known_duration + 0.05:
            raise ApiError(400, "invalid_time_range", "duration cannot exceed the video stream duration")
        receipt_id = uuid.uuid4().hex
        destination = secure_join(self.root, receipt_id + ".mp4")
        staging = destination.with_name(f"{receipt_id}.tmp-{uuid.uuid4().hex}.mp4")
        command = [
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-i", str(video), "-i", str(audio),
            "-filter_complex",
            f"[1:a:0]asetpts=PTS-STARTPTS,aresample=48000,apad=pad_dur={duration:.9f},atrim=duration={duration:.9f}[a]",
            "-map", "0:v:0", "-map", "[a]", "-t", f"{duration:.9f}",
            "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", "-fs", str(self.config.max_video_bytes + 1), str(staging),
        ]
        if not self._slots.acquire(blocking=False):
            raise ApiError(429, "media_busy", "two media operations are already running")
        try:
            with self._reserve_output(self.config.max_video_bytes):
                self._run(command, staging)
                os.replace(staging, destination)
                probe = AssetStore._probe_media(destination, "video")
                if probe.get("has_audio") is not True:
                    raise ApiError(422, "media_processing_failed", "muxed output has no audio stream")
                size = destination.stat().st_size
                if size > self.config.max_video_bytes:
                    raise ApiError(413, "derived_too_large", "muxed media exceeds the configured video size limit")
                sha256 = AssetStore.hash_file(destination)
                display_name = safe_filename(str(data.get("display_name") or f"{video.stem}-muxed.mp4"))
                value = {
                    "id": receipt_id, "kind": "video", "display_name": display_name,
                    "filename": display_name, "stored_name": destination.name,
                    "mime_type": "video/mp4", "size": size, "sha256": sha256,
                    "media": probe, "created_at": time.time(),
                    "source": {
                        "video": video_meta.get("source_receipt", {}),
                        "audio": audio_meta.get("source_receipt", {}),
                    },
                    "operation": "mux_audio",
                    "parameters": {"duration": duration, "audio_end_behavior": "pad_or_trim_to_video"},
                }
                with self._mutation_lock, self._lock:
                    if self._stored_bytes() + size > self.config.max_asset_storage_bytes:
                        raise ApiError(507, "media_quota", "derived media storage quota would be exceeded")
                    self.metadata.put(receipt_id, value)
                return self.public(value)
        except Exception:
            destination.unlink(missing_ok=True)
            try:
                self.metadata.delete(receipt_id)
            except ApiError:
                pass
            raise
        finally:
            staging.unlink(missing_ok=True)
            self._slots.release()

    def _prepare_h3_reference(
        self,
        source: Path,
        source_meta: dict[str, Any],
        data: dict[str, Any],
        *,
        progress: Callable[[int], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        parameters = ReferenceParameters.parse(data)
        media = source_meta.get("media") if isinstance(source_meta.get("media"), dict) else {}
        if not media.get("width") or not media.get("height") or not media.get("duration"):
            try:
                media = AssetStore._probe_media(source, "video")
            except ApiError as error:
                raise ApiError(error.status, "media_probe_failed", "source video could not be inspected") from error
        if float(media.get("duration", 0) or 0) <= 0:
            raise ApiError(422, "invalid_duration", "source video must have a positive duration")
        plan = calculate_reference_plan(
            media.get("width"), media.get("height"), media.get("rotation", 0), parameters,
        )
        source_sha256 = str(source_meta.get("sha256") or "")
        if len(source_sha256) != 64:
            source_sha256 = AssetStore.hash_file(source)
        reuse_key = idempotency_key(source_sha256, parameters)
        with self._lock:
            reusable = next(
                (
                    receipt for receipt in self.metadata.list()
                    if receipt.get("operation") == "prepare_h3_reference"
                    and receipt.get("idempotency_key") == reuse_key
                ),
                None,
            )
            if reusable is not None:
                try:
                    reusable_path = self.path(reusable)
                except ApiError:
                    reusable_path = None
                expected_size = int(reusable.get("size", -1) or -1)
                expected_sha256 = reusable.get("sha256")
                if (
                    reusable_path is not None and reusable_path.is_file()
                    and reusable_path.stat().st_size == expected_size
                    and isinstance(expected_sha256, str)
                    and AssetStore.hash_file(reusable_path) == expected_sha256
                ):
                    public = self.public(reusable)
                    public["reused"] = True
                    return public

        receipt_id = uuid.uuid4().hex
        destination = secure_join(self.root, receipt_id + ".mp4")
        staging = destination.with_name(f"{receipt_id}.tmp-{uuid.uuid4().hex}.mp4")
        pads = (plan.pad_left, plan.pad_right, plan.pad_top, plan.pad_bottom)
        filters = [
            f"scale={plan.content_width}:{plan.content_height}:flags=lanczos",
            "setsar=1",
            f"pad={plan.canvas_width}:{plan.canvas_height}:{plan.pad_left}:{plan.pad_top}:color=black",
        ]
        if any(pads):
            filters.append(
                "fillborders="
                f"left={plan.pad_left}:right={plan.pad_right}:top={plan.pad_top}:bottom={plan.pad_bottom}:mode=smear"
            )
        filters.append(f"fps={parameters.fps}")
        requested_duration = min(float(media.get("duration", 0) or 0), parameters.max_duration)
        command = [
            "ffmpeg", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats", "-i", str(source),
            "-t", f"{requested_duration:.6f}", "-map", "0:v:0",
        ]
        if parameters.audio == "keep":
            command += ["-map", "0:a?", "-c:a", "aac", "-b:a", "192k"]
        else:
            command += ["-an"]
        command += [
            "-vf", ",".join(filters), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-metadata:s:v:0", "rotate=0",
            "-movflags", "+faststart", "-fs", str(self.config.max_video_bytes + 1), str(staging),
        ]
        if not self._slots.acquire(blocking=False):
            raise ApiError(429, "media_busy", "two media operations are already running")
        try:
            with self._reserve_output(self.config.max_video_bytes):
                self._run_reference(
                    command, staging, requested_duration,
                    progress=progress, cancel_event=cancel_event,
                )
                probe = AssetStore._probe_media(staging, "video")
                if int(probe.get("width", 0) or 0) != plan.canvas_width or int(probe.get("height", 0) or 0) != plan.canvas_height:
                    raise ApiError(422, "media_processing_failed", "prepared reference dimensions failed verification")
                if float(probe.get("duration", 0) or 0) > parameters.max_duration + 0.05:
                    raise ApiError(422, "media_processing_failed", "prepared reference duration failed verification")
                if abs(float(probe.get("fps", 0) or 0) - parameters.fps) > 0.01:
                    raise ApiError(422, "media_processing_failed", "prepared reference frame rate failed verification")
                if probe.get("video_codec") != "h264" or probe.get("pixel_format") != "yuv420p":
                    raise ApiError(422, "media_processing_failed", "prepared reference codec failed verification")
                if parameters.audio == "remove" and probe.get("has_audio"):
                    raise ApiError(422, "media_processing_failed", "prepared reference unexpectedly contains audio")
                size = staging.stat().st_size
                if size > self.config.max_video_bytes:
                    raise ApiError(413, "derived_too_large", "prepared reference exceeds the configured video size limit")
                derived_sha256 = AssetStore.hash_file(staging)
                os.replace(staging, destination)
                output_frames = int(probe.get("frame_count", 0) or 0)
                if output_frames <= 0:
                    output_frames = max(1, int(round(float(probe.get("duration", 0) or 0) * parameters.fps)))
                source_frames = int(media.get("frame_count", 0) or 0)
                source_receipt = source_meta.get("source_receipt", {})
                value = {
                    "id": receipt_id,
                    "kind": "video",
                    "display_name": safe_filename(str(data.get("display_name") or f"{source.stem}-h3-reference.mp4")),
                    "filename": safe_filename(str(data.get("display_name") or f"{source.stem}-h3-reference.mp4")),
                    "stored_name": destination.name,
                    "mime_type": "video/mp4",
                    "size": size,
                    "sha256": derived_sha256,
                    "media": probe,
                    "created_at": time.time(),
                    "source": source_receipt,
                    "operation": "prepare_h3_reference",
                    "parameters": parameters.public(),
                    "algorithm_version": ALGORITHM_VERSION,
                    "idempotency_key": reuse_key,
                    "preprocessing": {
                        "algorithm_version": ALGORITHM_VERSION,
                        "source": {
                            **(source_receipt if isinstance(source_receipt, dict) else {}),
                            "sha256": source_sha256,
                            "display_name": str(source_meta.get("display_name") or source_meta.get("filename") or source.name),
                            "width": int(media.get("width", 0) or 0),
                            "height": int(media.get("height", 0) or 0),
                            "rotation": int(media.get("rotation", 0) or 0),
                            "fps": float(media.get("fps", 0) or 0),
                            "frame_count": source_frames,
                            "duration": float(media.get("duration", 0) or 0),
                            "has_audio": media.get("has_audio") is True,
                        },
                        "plan": plan.public(),
                        "output": {
                            "content_width": plan.content_width,
                            "content_height": plan.content_height,
                            "canvas_width": plan.canvas_width,
                            "canvas_height": plan.canvas_height,
                            "fps": float(probe.get("fps", 0) or 0),
                            "frame_count": output_frames,
                            "duration": float(probe.get("duration", 0) or 0),
                            "has_audio": probe.get("has_audio") is True,
                            "truncated": float(media.get("duration", 0) or 0) > parameters.max_duration,
                            "trim_start": 0.0,
                            "trim_end": float(probe.get("duration", 0) or 0),
                        },
                        "token_estimate": {
                            "reference_video_tokens": estimate_video_tokens(plan.canvas_width, plan.canvas_height, output_frames),
                        },
                    },
                }
                with self._mutation_lock, self._lock:
                    if self._stored_bytes() + size > self.config.max_asset_storage_bytes:
                        raise ApiError(507, "media_quota", "derived media storage quota would be exceeded")
                    self.metadata.put(receipt_id, value)
                public = self.public(value)
                public["reused"] = False
                return public
        except ApiError as error:
            destination.unlink(missing_ok=True)
            if error.code in {"media_quota", "disk_full"}:
                raise ApiError(507, "insufficient_storage", "insufficient storage for prepared reference") from error
            raise
        except OSError as error:
            destination.unlink(missing_ok=True)
            if getattr(error, "errno", None) == 28:
                raise ApiError(507, "insufficient_storage", "insufficient storage for prepared reference") from error
            raise
        except Exception:
            destination.unlink(missing_ok=True)
            try:
                self.metadata.delete(receipt_id)
            except ApiError:
                pass
            raise
        finally:
            staging.unlink(missing_ok=True)
            self._slots.release()

    @staticmethod
    def _run_reference(
        command: list[str],
        destination: Path,
        duration: float,
        *,
        progress: Callable[[int], None] | None,
        cancel_event: threading.Event | None,
    ) -> None:
        """Run ffmpeg with cooperative cancellation and bounded progress events."""
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            raise ApiError(503, "ffmpeg_unavailable", "ffmpeg is unavailable") from error

        lines: queue.Queue[str | None] = queue.Queue()
        stderr_lines: deque[str] = deque(maxlen=20)

        def read_progress() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line.rstrip("\r\n"))
            lines.put(None)

        reader = threading.Thread(target=read_progress, name="h3-reference-progress", daemon=True)
        def read_errors() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line.rstrip("\r\n"))

        error_reader = threading.Thread(target=read_errors, name="h3-reference-errors", daemon=True)
        reader.start()
        error_reader.start()
        started = time.monotonic()
        last_progress = -1

        def stop_process() -> None:
            if process.poll() is not None:
                return
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, AttributeError):
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, AttributeError):
                    process.kill()

        try:
            if progress is not None:
                progress(1)
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    stop_process()
                    raise ApiError(409, "cancelled", "reference preprocessing was cancelled")
                if time.monotonic() - started > 300:
                    stop_process()
                    raise ApiError(422, "media_processing_failed", "ffmpeg timed out while preparing the reference")
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line and line.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        processed = int(line.split("=", 1)[1]) / 1_000_000
                    except (TypeError, ValueError):
                        continue
                    current = max(1, min(99, int(processed / max(duration, 0.001) * 100)))
                    if current > last_progress:
                        last_progress = current
                        if progress is not None:
                            progress(current)
            reader.join(timeout=1)
            error_reader.join(timeout=1)
            if cancel_event is not None and cancel_event.is_set():
                raise ApiError(409, "cancelled", "reference preprocessing was cancelled")
            if process.returncode or not destination.is_file() or destination.stat().st_size == 0:
                destination.unlink(missing_ok=True)
                message = stderr_lines[-1][:300] if stderr_lines else "ffmpeg failed"
                raise ApiError(422, "media_processing_failed", message)
            if progress is not None:
                progress(99)
        finally:
            if process.poll() is None:
                stop_process()
            reader.join(timeout=1)
            error_reader.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    @staticmethod
    def public(value: dict[str, Any]) -> dict[str, Any]:
        receipt_id = str(value["id"])
        return {
            "id": receipt_id, "receipt_id": receipt_id, "kind": value["kind"],
            "display_name": value["display_name"], "filename": value["filename"],
            "mime_type": value["mime_type"], "size": value["size"], "sha256": value["sha256"],
            "media": value.get("media", {}), "created_at": value["created_at"],
            "source": value.get("source", {}), "operation": value.get("operation"),
            "parameters": value.get("parameters", {}),
            **({"preprocessing": value["preprocessing"]} if isinstance(value.get("preprocessing"), dict) else {}),
            **({"algorithm_version": value["algorithm_version"]} if isinstance(value.get("algorithm_version"), str) else {}),
            "pinned": value.get("pinned") is True,
            "content_url": f"/api/derivations/{receipt_id}/content",
            "preview_url": f"/api/derivations/{receipt_id}/content",
            "download_url": f"/api/derivations/{receipt_id}/download",
            "thumbnail_url": f"/api/derivations/{receipt_id}/thumbnail" if value["kind"] in {"image", "video"} else None,
            **({"asset_id": value["asset_id"]} if isinstance(value.get("asset_id"), str) else {}),
        }

    def list_public(self) -> list[dict[str, Any]]:
        """Return durable derivation receipts without exposing server paths."""
        return [self.public(value) for value in sorted(
            self.metadata.list(), key=lambda item: item.get("pinned") is True, reverse=True,
        )]

    def update_metadata(self, receipt_id: str, *, pinned: Any) -> dict[str, Any]:
        if not isinstance(pinned, bool):
            raise ApiError(400, "invalid_pinned", "pinned must be a boolean")
        with self._mutation_lock, self._lock:
            value = self.get(receipt_id)
            value["pinned"] = pinned
            value["metadata_updated_at"] = time.time()
            self.metadata.put(receipt_id, value)
            return self.public(value)

    def get(self, receipt_id: str) -> dict[str, Any]:
        return self.metadata.get(validate_id(receipt_id, "receipt id"))

    def path(self, value: dict[str, Any]) -> Path:
        path = secure_join(self.root, str(value.get("stored_name", "")))
        if not path.is_file():
            raise ApiError(404, "derivation_file_missing", "derived media file is missing")
        return path

    def delete(self, receipt_id: str) -> dict[str, Any]:
        with self._mutation_lock, self._lock:
            value = self.get(receipt_id)
            path = self.path(value)
            self.metadata.delete(receipt_id)
            path.unlink(missing_ok=True)
            return value

    def save_as_asset(
        self, receipt_id: str, *, display_name: Any = ..., folder_id: Any = ...,
        folder_exists: Callable[[str], Any] | None = None, visibility: str = "library",
    ) -> dict[str, Any]:
        if visibility not in {"library", "internal"}:
            raise ApiError(400, "invalid_visibility", "visibility must be library or internal")
        with self._mutation_lock, self._lock:
            value = self.get(receipt_id)
            existing = value.get("asset_id")
            if isinstance(existing, str):
                try:
                    asset = self.assets.get(existing)
                except ApiError as error:
                    if error.status != 404:
                        raise
                else:
                    if visibility == "library" and (
                        asset.get("visibility", "library") == "internal"
                        or display_name is not ... or folder_id is not ...
                    ):
                        asset = self.assets.update_library_metadata(
                            existing,
                            display_name=display_name,
                            folder_id=folder_id,
                            folder_exists=folder_exists,
                        )
                    return self.assets.public_metadata(asset)
            source = self.path(value)
            if self._stored_bytes() + source.stat().st_size > self.config.max_asset_storage_bytes:
                raise ApiError(507, "asset_quota", "asset storage quota would be exceeded; delete unused assets first")
            if shutil.disk_usage(self.root).free < source.stat().st_size:
                raise ApiError(507, "disk_full", "insufficient free disk space to save derived media")
            temporary = secure_join(self.config.data_root / "tmp", f"derive-{uuid.uuid4().hex}{source.suffix}")
            asset: dict[str, Any] | None = None
            try:
                shutil.copy2(source, temporary)
                original_name = value["display_name"] if display_name is ... else display_name
                asset = self.assets.import_file(
                    temporary,
                    original_filename=str(original_name),
                    requested_kind=str(value["kind"]),
                    claimed_content_type=str(value["mime_type"]),
                    visibility=visibility,
                )
                if self._stored_bytes() > self.config.max_asset_storage_bytes:
                    raise ApiError(507, "asset_quota", "normalized media would exceed the asset storage quota")
                if visibility == "library" and (display_name is not ... or folder_id is not ...):
                    asset = self.assets.update_library_metadata(
                        str(asset["id"]),
                        display_name=display_name,
                        folder_id=folder_id,
                        folder_exists=folder_exists,
                    )
                value["asset_id"] = asset["id"]
                value["asset_visibility"] = visibility
                value["saved_at"] = time.time()
                self.metadata.put(receipt_id, value)
            except Exception:
                if asset is not None:
                    self.assets.delete(str(asset["id"]))
                raise
            finally:
                temporary.unlink(missing_ok=True)
            return self.assets.public_metadata(asset)
