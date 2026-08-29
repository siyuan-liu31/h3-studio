"""Safe, temporary media derivatives and lazy thumbnail generation."""

from __future__ import annotations

import hashlib
import math
import mimetypes
import os
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .errors import ApiError
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

    def derive(self, source: Path, source_meta: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        operation = data.get("operation")
        allowed = {"video_trim", "frame", "audio_trim", "extract_audio", "remove_audio"}
        if operation not in allowed:
            raise ApiError(400, "invalid_operation", f"operation must be one of {', '.join(sorted(allowed))}")
        kind = str(source_meta.get("kind", ""))
        media = source_meta.get("media", {}) if isinstance(source_meta.get("media"), dict) else {}
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
