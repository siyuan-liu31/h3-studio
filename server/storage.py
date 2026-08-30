"""Durable asset and job metadata stores."""

from __future__ import annotations

import json
import copy
import hashlib
import os
import shutil
import subprocess
import threading
import time
import uuid
import math
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ApiError
from .security import safe_filename, secure_join, validate_id, validate_media


class JsonStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, item_id: str) -> Path:
        return secure_join(self.root, validate_id(item_id) + ".json")

    def put(self, item_id: str, value: dict[str, Any]) -> None:
        path = self._path(item_id)
        temporary = path.with_suffix(f".json.tmp-{uuid.uuid4().hex}")
        encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        with self._lock:
            with temporary.open("xb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)

    def get(self, item_id: str) -> dict[str, Any]:
        path = self._path(item_id)
        try:
            with path.open("r", encoding="utf-8") as source:
                value = json.load(source)
        except FileNotFoundError as error:
            raise ApiError(404, "not_found", "item does not exist") from error
        except (OSError, json.JSONDecodeError) as error:
            raise ApiError(500, "metadata_corrupt", "stored metadata cannot be read") from error
        if not isinstance(value, dict):
            raise ApiError(500, "metadata_corrupt", "stored metadata is invalid")
        return value

    def list(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                values.append(value)
        return sorted(
            values,
            key=lambda value: float(value.get("created_at", 0)),
            reverse=True,
        )

    def delete(self, item_id: str) -> None:
        path = self._path(item_id)
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError as error:
                raise ApiError(404, "not_found", "item does not exist") from error


class AssetStore:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.metadata = JsonStore(config.data_root / "metadata" / "assets")
        self.upload_root = config.comfy_input / "h3-studio"
        self.upload_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def find_library_duplicate(self, sha256: str, *, requested_kind: str = "auto") -> dict[str, Any] | None:
        """Return a reusable library asset with the same exact bytes, if one exists.

        Internal task materializations are deliberately excluded: sharing those
        ids with user uploads would couple their deletion and retention rules.
        """
        for asset in self.list():
            if asset.get("visibility", "library") != "library" or asset.get("sha256") != sha256:
                continue
            if requested_kind != "auto" and asset.get("kind") != requested_kind:
                continue
            try:
                self.content_path(asset)
            except ApiError:
                continue
            return asset
        return None

    def import_file(
        self,
        temp_path: Path,
        *,
        original_filename: str,
        requested_kind: str = "auto",
        claimed_content_type: str = "application/octet-stream",
        visibility: str = "library",
    ) -> dict[str, Any]:
        if visibility not in {"library", "internal"}:
            raise ApiError(400, "invalid_visibility", "visibility must be library or internal")
        cleaned = safe_filename(original_filename)
        size = temp_path.stat().st_size
        with temp_path.open("rb") as source:
            head = source.read(64)
        signature = validate_media(
            head=head,
            filename=cleaned,
            requested_kind=requested_kind,
            size=size,
            limits={
                "image": self.config.max_image_bytes,
                "video": self.config.max_video_bytes,
                "audio": self.config.max_audio_bytes,
            },
        )
        asset_id = uuid.uuid4().hex
        stored_name = f"{asset_id}{signature.extension}"
        destination = secure_join(self.upload_root, stored_name)
        staging = destination.with_suffix(destination.suffix + f".tmp-{uuid.uuid4().hex}")
        sha256 = hashlib.sha256()
        try:
            with temp_path.open("rb") as source, staging.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    sha256.update(chunk)
                    output.write(chunk)
            os.replace(staging, destination)
        finally:
            staging.unlink(missing_ok=True)
            temp_path.unlink(missing_ok=True)
        probe: dict[str, Any] = {}
        comfy_destination = destination
        if signature.kind == "image":
            try:
                probe = self._probe_image(destination)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
        elif signature.kind in {"video", "audio"}:
            try:
                probe = self._probe_media(destination, signature.kind)
                if signature.kind == "video" and not math.isclose(float(probe.get("fps", 0)), 24.0, abs_tol=0.01):
                    comfy_destination = destination.with_name(f"{asset_id}-ref24.mp4")
                    self._normalize_video(destination, comfy_destination)
                    normalized_probe = self._probe_media(comfy_destination, "video")
                    if not math.isclose(float(normalized_probe.get("fps", 0)), 24.0, abs_tol=0.01):
                        raise ApiError(422, "media_normalization_failed", "reference video could not be normalized to 24 fps")
                    normalized_probe["source_fps"] = probe.get("fps")
                    normalized_probe["reference_fps"] = normalized_probe.get("fps")
                    normalized_probe["normalized_to_24fps"] = True
                    probe = normalized_probe
                elif signature.kind == "video":
                    probe["source_fps"] = probe.get("fps")
                    probe["reference_fps"] = probe.get("fps")
                    probe["normalized_to_24fps"] = False
            except Exception:
                comfy_destination.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
                raise
        now = time.time()
        value: dict[str, Any] = {
            "id": asset_id,
            "kind": signature.kind,
            "filename": cleaned,
            "stored_name": stored_name,
            "comfy_path": f"h3-studio/{comfy_destination.name}",
            "mime_type": signature.mime_type,
            "claimed_content_type": claimed_content_type,
            "size": size,
            "storage_size": size + (comfy_destination.stat().st_size if comfy_destination != destination else 0),
            "sha256": sha256.hexdigest(),
            "media": probe,
            "visibility": visibility,
            "created_at": now,
            "content_url": f"/api/assets/{asset_id}/content",
        }
        try:
            self.metadata.put(asset_id, value)
        except Exception:
            if comfy_destination != destination:
                comfy_destination.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise
        return value

    @staticmethod
    def _probe_image(path: Path) -> dict[str, Any]:
        probe = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height", "-of", "json", str(path),
        ]
        decode = ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "null", "-"]
        try:
            inspected = subprocess.run(probe, capture_output=True, text=True, timeout=20, check=False)
            decoded = subprocess.run(decode, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ApiError(422, "image_probe_failed", "ffmpeg could not inspect the uploaded image") from error
        try:
            value = json.loads(inspected.stdout)
            streams = value.get("streams", []) if isinstance(value, dict) else []
            stream = streams[0] if isinstance(streams, list) and streams and isinstance(streams[0], dict) else None
        except (json.JSONDecodeError, IndexError) as error:
            raise ApiError(422, "image_probe_failed", "uploaded image metadata is invalid") from error
        if inspected.returncode or decoded.returncode or not stream:
            raise ApiError(422, "image_probe_failed", "uploaded image is corrupt or unsupported")
        width, height = int(stream.get("width", 0) or 0), int(stream.get("height", 0) or 0)
        if width <= 0 or height <= 0 or width > 16384 or height > 16384 or width * height > 50_000_000:
            raise ApiError(422, "image_dimensions", "uploaded image dimensions exceed the safe limit")
        return {"width": width, "height": height, "codec": stream.get("codec_name")}

    @staticmethod
    def _normalize_video(source: Path, destination: Path) -> None:
        command = [
            "ffmpeg", "-y", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
            "-vf", "fps=24", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(destination),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ApiError(422, "media_normalization_failed", "ffmpeg could not normalize the reference video") from error
        if completed.returncode or not destination.is_file() or destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise ApiError(422, "media_normalization_failed", "reference video normalization failed")

    @staticmethod
    def _probe_media(path: Path, expected_kind: str) -> dict[str, Any]:
        command = [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ApiError(422, "media_probe_failed", "ffprobe could not inspect uploaded media") from error
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ApiError(422, "media_probe_failed", "ffprobe returned invalid media metadata") from error
        if completed.returncode or not isinstance(value, dict):
            raise ApiError(422, "media_probe_failed", "uploaded media is corrupt or unsupported")
        streams = value.get("streams", [])
        if not isinstance(streams, list):
            streams = []
        video = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"), None)
        audio = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"), None)
        if expected_kind == "video" and video is None:
            raise ApiError(422, "media_probe_failed", "uploaded video has no decodable video stream")
        if expected_kind == "audio" and audio is None:
            raise ApiError(422, "media_probe_failed", "uploaded audio has no decodable audio stream")
        raw_duration = (value.get("format") or {}).get("duration") if isinstance(value.get("format"), dict) else None
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            raise ApiError(422, "media_probe_failed", "uploaded media has no positive duration")
        def rate(stream: dict[str, Any] | None) -> float:
            raw = stream.get("avg_frame_rate", stream.get("r_frame_rate", "0/1")) if stream else "0/1"
            try:
                numerator, denominator = str(raw).split("/", 1)
                return float(numerator) / float(denominator) if float(denominator) else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                return 0.0

        try:
            video_duration = float(video.get("duration", 0) or 0) if video else 0.0
        except (TypeError, ValueError):
            video_duration = 0.0
        rotation = 0
        if video:
            tags = video.get("tags") if isinstance(video.get("tags"), dict) else {}
            raw_rotation: Any = tags.get("rotate", 0)
            side_data = video.get("side_data_list")
            if isinstance(side_data, list):
                rotated = next(
                    (item.get("rotation") for item in side_data if isinstance(item, dict) and item.get("rotation") is not None),
                    None,
                )
                if rotated is not None:
                    raw_rotation = rotated
            try:
                rotation = int(round(float(raw_rotation))) % 360
            except (TypeError, ValueError, OverflowError):
                rotation = 0
        return {
            "duration": round(duration, 3),
            "video_duration": round(video_duration, 3) if video_duration > 0 else None,
            "has_video": video is not None,
            "has_audio": audio is not None,
            "video_codec": video.get("codec_name") if video else None,
            "pixel_format": video.get("pix_fmt") if video else None,
            "audio_codec": audio.get("codec_name") if audio else None,
            "sample_rate": int(audio.get("sample_rate", 0) or 0) if audio else None,
            "channels": int(audio.get("channels", 0) or 0) if audio else None,
            "width": int(video.get("width", 0)) if video else None,
            "height": int(video.get("height", 0)) if video else None,
            "rotation": rotation if video else None,
            "fps": round(rate(video), 6) if video else None,
            "frame_count": int(video.get("nb_frames", 0) or 0) if video else None,
        }

    @staticmethod
    def _compatible_metadata(asset: dict[str, Any]) -> dict[str, Any]:
        """Normalize durable records written by older H3 Studio releases.

        Cloned AutoDL machines often keep the shared metadata directory while
        the application release changes.  Early releases exposed ``type`` /
        ``name`` and some records only retained the ComfyUI-relative path.  Do
        not make those valid records disappear from the library or become
        unusable references after an upgrade.
        """

        value = dict(asset)
        kind = value.get("kind", value.get("type"))
        if isinstance(kind, str) and kind in {"image", "video", "audio"}:
            value["kind"] = kind
        filename = value.get("filename")
        if not isinstance(filename, str) or not filename:
            legacy_name = value.get("display_name", value.get("name"))
            if isinstance(legacy_name, str) and legacy_name:
                value["filename"] = legacy_name
        stored_name = value.get("stored_name")
        comfy_path = value.get("comfy_path")
        if (not isinstance(stored_name, str) or not stored_name) and isinstance(comfy_path, str):
            prefix = "h3-studio/"
            relative = comfy_path[len(prefix):] if comfy_path.startswith(prefix) else ""
            if relative and "/" not in relative and "\\" not in relative:
                value["stored_name"] = relative
                stored_name = relative
        if (not isinstance(comfy_path, str) or not comfy_path) and isinstance(stored_name, str) and stored_name:
            value["comfy_path"] = f"h3-studio/{stored_name}"
        if value.get("visibility") not in {"library", "internal"}:
            # All records written before private canvas materialization were
            # user-library assets.
            value["visibility"] = "library"
        return value

    def get(self, asset_id: str) -> dict[str, Any]:
        return self._compatible_metadata(self.metadata.get(asset_id))

    @staticmethod
    def _display_name(value: Any) -> str:
        if not isinstance(value, str):
            raise ApiError(400, "invalid_display_name", "display_name must be a string")
        value = value.strip()
        if not value or len(value) > 120 or any(ord(char) < 32 for char in value):
            raise ApiError(400, "invalid_display_name", "display_name must be 1..120 printable characters")
        return value

    def update_library_metadata(
        self, asset_id: str, *, display_name: Any = ...,
        folder_id: Any = ..., pinned: Any = ..., folder_exists: Any = None,
    ) -> dict[str, Any]:
        """Atomically update user-facing library metadata without renaming media files."""
        asset_id = validate_id(asset_id, "asset id")
        asset = self.get(asset_id)
        # Updating/saving an internal materialization is the explicit user
        # action that promotes it into the reusable asset library. The binary
        # and stable asset id stay unchanged.
        asset["visibility"] = "library"
        if display_name is not ...:
            asset["display_name"] = self._display_name(display_name)
        if folder_id is not ...:
            if folder_id is not None:
                folder_id = validate_id(folder_id, "folder id")
                if folder_exists is not None:
                    folder_exists(folder_id)
            asset["folder_id"] = folder_id
        if pinned is not ...:
            if not isinstance(pinned, bool):
                raise ApiError(400, "invalid_pinned", "pinned must be a boolean")
            asset["pinned"] = pinned
        asset["updated_at"] = time.time()
        self.metadata.put(asset_id, asset)
        return asset

    def list(self) -> list[dict[str, Any]]:
        return [self._compatible_metadata(asset) for asset in self.metadata.list()]

    @staticmethod
    def public_metadata(asset: dict[str, Any]) -> dict[str, Any]:
        """Return the stable, reusable asset-library contract.

        Storage paths and implementation filenames are intentionally excluded;
        clients only need the durable id, display metadata and authenticated
        content route to reconnect an existing upload to a new canvas.
        """

        asset_id = str(asset.get("id", ""))
        asset = AssetStore._compatible_metadata(asset)
        kind = str(asset.get("kind", ""))
        filename = str(asset.get("filename", asset.get("name", "")))
        display_name = str(asset.get("display_name") or filename)
        media = asset.get("media", {})
        if not isinstance(media, dict):
            media = {}
        # Never retain a host from an old/cloned receipt.  Media must resolve
        # through the currently-open Studio gateway and its authentication.
        content_url = f"/api/assets/{asset_id}/content"
        return {
            "id": asset_id,
            "asset_id": asset_id,
            "name": display_name,
            "display_name": display_name,
            "filename": filename,
            "type": kind,
            "kind": kind,
            "content_url": content_url,
            "media": dict(media),
            "mime_type": str(asset.get("mime_type", "application/octet-stream")),
            "size": int(asset.get("size", 0) or 0),
            "content_hash": str(asset.get("sha256", "")),
            "created_at": float(asset.get("created_at", 0) or 0),
            "updated_at": float(asset.get("updated_at", asset.get("created_at", 0)) or 0),
            "folder_id": asset.get("folder_id") if isinstance(asset.get("folder_id"), str) else None,
            "pinned": asset.get("pinned") is True,
            "visibility": str(asset.get("visibility", "library")),
            "thumbnail_url": f"/api/assets/{asset_id}/thumbnail" if kind in {"image", "video"} else None,
        }

    def list_public(self, *, query: str = "", folder_id: str | None | object = ...) -> list[dict[str, Any]]:
        needle = query.strip().casefold()
        result = []
        for asset in self.list():
            if asset.get("visibility", "library") != "library":
                continue
            derived = asset.get("derived")
            if isinstance(derived, dict) and derived.get("kind") in {"continuation", "source_range"}:
                # Long-video continuity/source-range helpers are implementation artifacts,
                # not user-saved library items.
                continue
            if folder_id is not ... and asset.get("folder_id") != folder_id:
                continue
            public = self.public_metadata(asset)
            if needle and needle not in f"{public['display_name']} {public['filename']} {public['kind']}".casefold():
                continue
            result.append(public)
        return sorted(result, key=lambda item: item.get("pinned") is True, reverse=True)

    def used_bytes(self) -> int:
        return sum(int(asset.get("storage_size", asset.get("size", 0)) or 0) for asset in self.list())

    def delete(self, asset_id: str) -> dict[str, Any]:
        asset = self.get(asset_id)
        paths: set[Path] = set()
        stored_name = asset.get("stored_name")
        comfy_path = asset.get("comfy_path")
        if isinstance(stored_name, str):
            paths.add(secure_join(self.upload_root, stored_name))
        if isinstance(comfy_path, str):
            relative = comfy_path.removeprefix("h3-studio/")
            paths.add(secure_join(self.upload_root, relative))
        self.metadata.delete(asset_id)
        for path in paths:
            path.unlink(missing_ok=True)
        return asset

    def content_path(self, asset: dict[str, Any]) -> Path:
        stored_name = asset.get("stored_name")
        if not isinstance(stored_name, str):
            raise ApiError(500, "metadata_corrupt", "asset filename is missing")
        path = secure_join(self.upload_root, stored_name)
        if not path.is_file():
            raise ApiError(404, "asset_file_missing", "asset content is missing")
        return path


class JobStore(JsonStore):
    """Job metadata index cached for the lifetime of the API process.

    Job receipts are only mutated through this store, so rebuilding and sorting
    every JSON file on every paginated Results request is unnecessary work.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._list_cache: list[dict[str, Any]] | None = None

    def put(self, item_id: str, value: dict[str, Any]) -> None:
        with self._lock:
            super().put(item_id, value)
            if self._list_cache is not None:
                replacement = copy.deepcopy(value)
                replacement.setdefault("id", item_id)
                self._list_cache = [replacement, *(item for item in self._list_cache if item.get("id") != item_id)]
                self._list_cache.sort(key=lambda item: float(item.get("created_at", 0) or 0), reverse=True)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._list_cache is None:
                self._list_cache = super().list()
            return copy.deepcopy(self._list_cache)

    def delete(self, item_id: str) -> None:
        with self._lock:
            super().delete(item_id)
            if self._list_cache is not None:
                self._list_cache = [item for item in self._list_cache if item.get("id") != item_id]


class AssetFolderStore(JsonStore):
    """Small durable virtual-folder tree; media files remain content-addressed."""

    @staticmethod
    def _name(value: Any) -> str:
        if not isinstance(value, str):
            raise ApiError(400, "invalid_folder_name", "folder name must be a string")
        value = value.strip()
        if not value or len(value) > 80 or any(ord(char) < 32 for char in value) or "/" in value or "\\" in value:
            raise ApiError(400, "invalid_folder_name", "folder name must be 1..80 characters and cannot contain path separators")
        return value

    def create(self, name: Any, parent_id: Any = None) -> dict[str, Any]:
        name = self._name(name)
        if parent_id is not None:
            parent_id = validate_id(parent_id, "parent folder id")
            self.get(parent_id)
        if any(item.get("parent_id") == parent_id and str(item.get("name", "")).casefold() == name.casefold() for item in self.list()):
            raise ApiError(409, "folder_exists", "a folder with that name already exists here")
        now = time.time()
        value = {"id": uuid.uuid4().hex, "name": name, "parent_id": parent_id, "created_at": now, "updated_at": now}
        self.put(value["id"], value)
        return value

    def update(self, folder_id: str, *, name: Any = None, parent_id: Any = ...) -> dict[str, Any]:
        folder_id = validate_id(folder_id, "folder id")
        value = self.get(folder_id)
        new_name = self._name(name) if name is not None else str(value["name"])
        new_parent = value.get("parent_id") if parent_id is ... else parent_id
        if new_parent is not None:
            new_parent = validate_id(new_parent, "parent folder id")
            if new_parent == folder_id:
                raise ApiError(400, "folder_cycle", "a folder cannot contain itself")
            cursor = self.get(new_parent)
            seen = {folder_id}
            while cursor.get("parent_id") is not None:
                current = str(cursor["parent_id"])
                if current in seen:
                    raise ApiError(400, "folder_cycle", "folder move would create a cycle")
                seen.add(current)
                cursor = self.get(current)
        if any(item.get("id") != folder_id and item.get("parent_id") == new_parent and str(item.get("name", "")).casefold() == new_name.casefold() for item in self.list()):
            raise ApiError(409, "folder_exists", "a folder with that name already exists here")
        value.update({"name": new_name, "parent_id": new_parent, "updated_at": time.time()})
        self.put(folder_id, value)
        return value

    def search(self, query: str = "") -> list[dict[str, Any]]:
        needle = query.strip().casefold()
        return [item for item in self.list() if not needle or needle in str(item.get("name", "")).casefold()]
