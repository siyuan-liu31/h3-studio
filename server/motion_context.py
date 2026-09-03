"""Durable, bounded Motion Context latent storage for long-video projects."""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ApiError
from .security import secure_join, validate_id
from .storage import JsonStore


MOTION_CONTEXT_FORMAT = "h3-motion-context/v1"
MOTION_CONTEXT_NODE_ID = "19"


def _latent_output(record: dict[str, Any]) -> dict[str, str] | None:
    outputs = record.get("outputs")
    node = outputs.get(MOTION_CONTEXT_NODE_ID) if isinstance(outputs, dict) else None
    found: list[dict[str, str]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            filename = value.get("filename")
            if isinstance(filename, str) and filename.lower().endswith(".latent"):
                found.append({
                    "filename": filename,
                    "subfolder": str(value.get("subfolder", "")),
                    "type": str(value.get("type", "output")),
                })
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(node)
    return found[-1] if found else None


class MotionContextStore:
    """Keeps one integrity-checked context latent per successful source job."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.metadata = JsonStore(config.data_root / "metadata" / "motion-context")
        self.root = config.data_root / "motion-context"
        self.input_root = config.comfy_input / "h3-studio-motion-context"
        self.root.mkdir(parents=True, exist_ok=True)
        self.input_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def capture(
        self,
        job: dict[str, Any],
        record: dict[str, Any],
        *,
        project_id: str,
        segment_id: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        job_id = validate_id(str(job.get("id")), "job id")
        project_id = validate_id(project_id, "project id")
        segment_id = validate_id(segment_id, "segment id")
        attempt_id = validate_id(attempt_id, "attempt id")
        with self._lock:
            try:
                current = self.metadata.get(job_id)
            except ApiError as error:
                if error.status != 404:
                    raise
                current = None
            if current is not None:
                self._validated_path(current)
                return current
            output = _latent_output(record)
            if output is None:
                raise ApiError(
                    409, "motion_context_missing",
                    "video completed but ComfyUI did not return the required Motion Context latent",
                )
            if output.get("type") != "output":
                raise ApiError(409, "motion_context_invalid", "ComfyUI returned an unsupported Motion Context location")
            source = secure_join(self.config.comfy_output, output.get("subfolder", ""), output["filename"])
            if not source.is_file() or source.stat().st_size <= 0:
                raise ApiError(409, "motion_context_missing", "the Motion Context latent is unavailable")
            context_id = uuid.uuid4().hex
            destination = secure_join(self.root, context_id + ".latent")
            temporary = destination.with_name(f"{context_id}.tmp-{uuid.uuid4().hex}.latent")
            try:
                shutil.copy2(source, temporary)
                size = temporary.stat().st_size
                used = sum(
                    int(item.get("size", 0) or 0)
                    for item in self.metadata.list()
                    if item.get("job_id") != job_id
                )
                if used + size > self.config.max_motion_context_storage_bytes:
                    raise ApiError(
                        507, "motion_context_storage_full",
                        "Motion Context storage quota is full; delete or rerun old long-video projects",
                    )
                with temporary.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                os.replace(temporary, destination)
                manifest = {
                    "id": job_id,
                    "format": MOTION_CONTEXT_FORMAT,
                    "context_id": context_id,
                    "stored_name": destination.name,
                    "sha256": digest,
                    "size": size,
                    "job_id": job_id,
                    "project_id": project_id,
                    "segment_id": segment_id,
                    "attempt_id": attempt_id,
                    "created_at": time.time(),
                }
                self.metadata.put(job_id, manifest)
                return manifest
            except ApiError:
                destination.unlink(missing_ok=True)
                raise
            except OSError as error:
                destination.unlink(missing_ok=True)
                code = "motion_context_storage_full" if getattr(error, "errno", None) == 28 else "motion_context_write_failed"
                status = 507 if code.endswith("storage_full") else 500
                raise ApiError(status, code, "Motion Context latent could not be stored atomically") from error
            finally:
                temporary.unlink(missing_ok=True)

    def get(self, job_id: str) -> dict[str, Any]:
        job_id = validate_id(job_id, "source job id")
        try:
            manifest = self.metadata.get(job_id)
        except ApiError as error:
            if error.status == 404:
                raise ApiError(
                    409, "motion_context_unavailable",
                    "the previous segment has no usable Motion Context latent; rerun it with Motion Context enabled",
                ) from error
            raise
        self._validated_path(manifest)
        return manifest

    def stage(self, manifest: dict[str, Any], consumer_job_id: str) -> tuple[str, Path]:
        consumer_job_id = validate_id(consumer_job_id, "consumer job id")
        context_id = validate_id(str(manifest.get("context_id")), "context id")
        source = self._validated_path(manifest)
        filename = f"{consumer_job_id}-{context_id}.latent"
        destination = secure_join(self.input_root, filename)
        temporary = destination.with_name(f"{filename}.tmp-{uuid.uuid4().hex}")
        try:
            shutil.copy2(source, temporary)
            with temporary.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != manifest.get("sha256"):
                raise ApiError(409, "motion_context_corrupt", "staged Motion Context latent failed its integrity check")
            os.replace(temporary, destination)
        except ApiError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise ApiError(
                507 if getattr(error, "errno", None) == 28 else 500,
                "motion_context_stage_failed",
                "Motion Context latent could not be staged for ComfyUI",
            ) from error
        finally:
            temporary.unlink(missing_ok=True)
        return f"h3-studio-motion-context/{filename}", destination

    @staticmethod
    def cleanup_staged(path: Path | None) -> None:
        if path is not None:
            path.unlink(missing_ok=True)

    def prune_project(self, project_id: str, keep_job_ids: set[str]) -> int:
        project_id = validate_id(project_id, "project id")
        keep = {validate_id(item, "job id") for item in keep_job_ids}
        removed = 0
        with self._lock:
            for manifest in self.metadata.list():
                if manifest.get("project_id") != project_id or manifest.get("job_id") in keep:
                    continue
                self._delete_manifest(manifest)
                removed += 1
        return removed

    def delete_project(self, project_id: str) -> int:
        return self.prune_project(project_id, set())

    def _validated_path(self, manifest: dict[str, Any]) -> Path:
        if manifest.get("format") != MOTION_CONTEXT_FORMAT:
            raise ApiError(409, "motion_context_corrupt", "Motion Context metadata format is unsupported")
        path = secure_join(self.root, str(manifest.get("stored_name", "")))
        if not path.is_file() or path.stat().st_size != int(manifest.get("size", -1)):
            raise ApiError(409, "motion_context_corrupt", "Motion Context latent is missing or damaged")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != manifest.get("sha256"):
            raise ApiError(409, "motion_context_corrupt", "Motion Context latent failed its integrity check")
        return path

    def _delete_manifest(self, manifest: dict[str, Any]) -> None:
        job_id = validate_id(str(manifest.get("job_id")), "job id")
        stored = manifest.get("stored_name")
        if isinstance(stored, str):
            secure_join(self.root, stored).unlink(missing_ok=True)
        try:
            self.metadata.delete(job_id)
        except ApiError as error:
            if error.status != 404:
                raise
