"""Sequential long-video API orchestration and merged-media acceptance checks."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.parse
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Protocol

from scripts.e2e.client import ApiClient, E2EError, validate_opaque_id
from scripts.e2e.runner import assert_output
from scripts.long_video.manifest import H3_FPS, H3_MAX_FRAMES


class LongVideoError(E2EError):
    """A long-video protocol, orchestration, or acceptance failure."""


class Client(Protocol):
    def json_request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def download(self, path_or_url: str, destination: Path) -> dict[str, Any]: ...


PROJECT_STATES = {"draft", "running", "partial", "stopping", "stopped", "completed", "failed", "merging"}
SEGMENT_STATES = {"pending", "running", "completed", "failed", "stopped", "stale"}
ACTIVE_SEGMENT_STATES = {"running"}
STARTED_SEGMENT_STATES = ACTIVE_SEGMENT_STATES | {"completed", "failed", "stopped", "stale"}
ATTEMPT_STATES = {"preparing", "submitting", "queued", "running", "completed", "failed", "canceled"}


def _project_path(project_id: str, suffix: str = "") -> str:
    safe_id = validate_opaque_id(project_id, label="project id")
    return f"/api/video-projects/{safe_id}{suffix}"


def _segment_path(project_id: str, segment_id: str) -> str:
    safe_segment = validate_opaque_id(segment_id, label="segment id")
    return _project_path(project_id, f"/segments/{safe_segment}/run")


def _effective_duration(requested: float) -> float:
    frames = min(5 + 17 * max(0, math.ceil((requested * H3_FPS - 5) / 17)), H3_MAX_FRAMES)
    return frames / H3_FPS


def expected_total_duration(manifest: dict[str, Any]) -> float:
    return sum(_effective_duration(float(item["request"]["parameters"]["duration"])) for item in manifest["project"]["segments"])


def _receipt_snapshot(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": receipt.get("status"),
        "current_index": receipt.get("current_index"),
        "stop_requested": receipt.get("stop_requested"),
        "segments": [
            {
                "id": item.get("id"), "index": item.get("index"), "continuation": item.get("continuation"),
                "status": item.get("status"), "job_id": item.get("job_id"),
                "attempt_count": len(item.get("attempts", [])) if isinstance(item.get("attempts"), list) else None,
            }
            for item in receipt.get("segments", []) if isinstance(item, dict)
        ],
        "merged": receipt.get("merged"),
    }


def validate_project_receipt(
    receipt: Any,
    manifest: dict[str, Any],
    *,
    project_id: str | None = None,
    enforce_order: bool = False,
    expected_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise LongVideoError("project response must be an object")
    actual_id = validate_opaque_id(receipt.get("id"), label="project id")
    if project_id is not None and actual_id != project_id:
        raise LongVideoError("project response id changed")
    if receipt.get("title") != manifest["project"]["title"]:
        raise LongVideoError("project response title differs from the manifest")
    state = receipt.get("status")
    if state not in PROJECT_STATES:
        raise LongVideoError(f"project response has invalid status {state!r}")
    if type(receipt.get("current_index")) is not int or not -1 <= receipt["current_index"] < len(manifest["project"]["segments"]):
        raise LongVideoError("project response has invalid current_index")
    if not isinstance(receipt.get("stop_requested"), bool):
        raise LongVideoError("project response has invalid stop_requested")
    for key in ("created_at", "updated_at"):
        if not isinstance(receipt.get(key), (int, float)) or isinstance(receipt.get(key), bool):
            raise LongVideoError(f"project response has invalid {key}")
    segments = receipt.get("segments")
    expected_segments = manifest["project"]["segments"]
    expected_requests = expected_requests or [item["request"] for item in expected_segments]
    if not isinstance(segments, list) or len(segments) != len(expected_segments):
        raise LongVideoError("project response segment count differs from the manifest")
    started: list[bool] = []
    for index, (segment, expected) in enumerate(zip(segments, expected_segments, strict=True)):
        if not isinstance(segment, dict):
            raise LongVideoError(f"segment receipt {index} must be an object")
        validate_opaque_id(segment.get("id"), label=f"segment {index} id")
        if segment.get("index") != index:
            raise LongVideoError(f"segment receipt {index} has the wrong index")
        if segment.get("continuation") != expected["continuation"]:
            raise LongVideoError(f"segment receipt {index} changed continuation mode")
        if segment.get("status") not in SEGMENT_STATES:
            raise LongVideoError(f"segment receipt {index} has invalid status")
        if not isinstance(segment.get("request"), dict):
            raise LongVideoError(f"segment receipt {index} does not echo its request")
        if segment["request"] != expected_requests[index]:
            raise LongVideoError(f"segment receipt {index} changed its canonical request")
        job_id = segment.get("job_id")
        if job_id is not None:
            validate_opaque_id(job_id, label=f"segment {index} job id")
        attempts = segment.get("attempts")
        if not isinstance(attempts, list):
            raise LongVideoError(f"segment receipt {index} attempts must be an array")
        for attempt_index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                raise LongVideoError(f"segment {index} attempt {attempt_index} must be an object")
            validate_opaque_id(attempt.get("id"), label=f"segment {index} attempt {attempt_index} id")
            if attempt.get("status") not in ATTEMPT_STATES:
                raise LongVideoError(f"segment {index} attempt {attempt_index} has invalid status")
            if "job_id" in attempt:
                validate_opaque_id(attempt["job_id"], label=f"segment {index} attempt {attempt_index} job id")
            elif attempt["status"] not in {"preparing", "failed"}:
                raise LongVideoError(f"segment {index} attempt {attempt_index} is missing job_id")
            if not isinstance(attempt.get("started_at"), (int, float)) or isinstance(attempt.get("started_at"), bool):
                raise LongVideoError(f"segment {index} attempt {attempt_index} has invalid started_at")
            if "finished_at" in attempt and (
                not isinstance(attempt["finished_at"], (int, float)) or isinstance(attempt["finished_at"], bool)
            ):
                raise LongVideoError(f"segment {index} attempt {attempt_index} has invalid finished_at")
            if "error" in attempt and not isinstance(attempt["error"], str):
                raise LongVideoError(f"segment {index} attempt {attempt_index} has invalid error")
            continuation = attempt.get("continuation")
            if not isinstance(continuation, dict) or continuation.get("mode") != segment["continuation"]:
                raise LongVideoError(f"segment {index} attempt {attempt_index} has invalid continuation evidence")
        started.append(bool(attempts) or segment["status"] in STARTED_SEGMENT_STATES)
    if enforce_order:
        for index in range(1, len(segments)):
            if started[index] and segments[index - 1].get("status") != "completed":
                raise LongVideoError(f"segment {index} started before segment {index - 1} completed")
        if sum(segment.get("status") in ACTIVE_SEGMENT_STATES for segment in segments) > 1:
            raise LongVideoError("more than one segment is active during sequential execution")
    merged = receipt.get("merged")
    if merged is not None:
        if not isinstance(merged, dict) or merged.get("status") not in {"merging", "completed", "failed"}:
            raise LongVideoError("project response has an invalid merged receipt")
    return receipt


def validate_continuation_evidence(receipt: dict[str, Any]) -> None:
    """Bind every completed continuation attempt to the preceding output receipt."""
    segments = receipt["segments"]
    for index, segment in enumerate(segments):
        for attempt_index, attempt in enumerate(segment["attempts"]):
            if attempt.get("status") != "completed" or segment["continuation"] == "none":
                continue
            continuation = attempt.get("continuation")
            if not isinstance(continuation, dict):
                raise LongVideoError(f"segment {index} attempt {attempt_index} lacks continuation evidence")
            expected_keys = {"mode", "source_segment_id", "source_job_id", "source_sha256", "asset_id"}
            optional_keys = {"asset_sha256", "asset_size", "asset_kind", "asset_reclaimed_at", "asset_reclaimed_reason"}
            if not expected_keys.issubset(continuation) or set(continuation) - expected_keys - optional_keys:
                raise LongVideoError(f"segment {index} attempt {attempt_index} has malformed continuation evidence")
            previous = segments[index - 1]
            if continuation["mode"] != segment["continuation"]:
                raise LongVideoError(f"segment {index} continuation evidence has the wrong mode")
            if continuation["source_segment_id"] != previous["id"]:
                raise LongVideoError(f"segment {index} continuation evidence has the wrong source segment")
            source_jobs = {item.get("job_id") for item in previous["attempts"]}
            if previous.get("job_id"):
                source_jobs.add(previous["job_id"])
            if continuation["source_job_id"] not in source_jobs:
                raise LongVideoError(f"segment {index} continuation evidence has the wrong source job")
            validate_opaque_id(continuation["source_job_id"], label=f"segment {index} continuation source job id")
            validate_opaque_id(continuation["asset_id"], label=f"segment {index} continuation asset id")
            source_sha = continuation["source_sha256"]
            if not isinstance(source_sha, str) or len(source_sha) != 64 or any(char not in "0123456789abcdef" for char in source_sha):
                raise LongVideoError(f"segment {index} continuation evidence has an invalid source SHA-256")
            asset_sha = continuation.get("asset_sha256")
            if asset_sha is not None and (
                not isinstance(asset_sha, str) or len(asset_sha) != 64
                or any(char not in "0123456789abcdef" for char in asset_sha)
            ):
                raise LongVideoError(f"segment {index} continuation evidence has an invalid asset SHA-256")
            asset_size = continuation.get("asset_size")
            if asset_size is not None and (type(asset_size) is not int or asset_size <= 0):
                raise LongVideoError(f"segment {index} continuation evidence has an invalid asset size")
            expected_kind = "image" if segment["continuation"] == "tail_frame" else "video"
            if continuation.get("asset_kind", expected_kind) != expected_kind:
                raise LongVideoError(f"segment {index} continuation evidence has the wrong asset kind")


def _rerun_requests(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    requests = [deepcopy(item["request"]) for item in manifest["project"]["segments"]]
    rerun = manifest.get("rerun")
    if rerun:
        target = requests[int(rerun["segment_index"])]
        target["prompt"] = rerun["prompt"]
        target["parameters"]["seed"] = rerun["seed"]
    return requests


def dry_run_plan(manifest: dict[str, Any], *, stop_after_index: int | None = None) -> dict[str, Any]:
    if stop_after_index is not None and not 0 <= stop_after_index < len(manifest["project"]["segments"]):
        raise ValueError("stop_after_index is outside the segment list")
    calls = ["POST /api/video-projects", "POST /api/video-projects/{project_id}/run", "GET /api/video-projects/{project_id} (poll)"]
    if stop_after_index is not None:
        calls += ["POST /api/video-projects/{project_id}/stop", "GET /api/video-projects/{project_id} (poll)"]
    else:
        if manifest.get("rerun"):
            calls += [
                "PUT /api/video-projects/{project_id}",
                "POST /api/video-projects/{project_id}/segments/{segment_id}/run",
                "GET /api/video-projects/{project_id} (poll)",
            ]
        calls += [
            "GET /api/jobs/{final_segment_job_id}",
            "POST /api/video-projects/{project_id}/merge", "GET /api/video-projects/{project_id} (poll)",
            "GET merged.download_url", "ffprobe", "write evidence JSON",
        ]
    return {
        "dry_run": True,
        "project": manifest["project"],
        "segment_count": len(manifest["project"]["segments"]),
        "expected_duration": expected_total_duration(manifest),
        "stop_after_index": stop_after_index,
        "rerun": manifest.get("rerun"),
        "would_call": calls,
    }


def _wait_project(
    client: Client,
    project_id: str,
    manifest: dict[str, Any],
    *,
    timeout: float,
    interval: float,
    events: list[dict[str, Any]],
    mode: str,
    previous_attempts: int = 0,
    rerun_index: int | None = None,
    stop_after_index: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    expected_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    deadline = monotonic() + timeout
    stop_sent = False
    while True:
        receipt = validate_project_receipt(
            client.json_request("GET", _project_path(project_id)), manifest,
            project_id=project_id, enforce_order=mode == "initial", expected_requests=expected_requests,
        )
        snapshot = _receipt_snapshot(receipt)
        if not events or snapshot != events[-1].get("receipt"):
            events.append({"phase": mode, "observed_at": time.time(), "receipt": snapshot})
        if receipt["status"] == "failed":
            raise LongVideoError(f"project failed during {mode}")
        if mode == "initial":
            if stop_sent and any(
                segment["status"] == "completed"
                for segment in receipt["segments"][stop_after_index + 1 :]  # type: ignore[operator]
            ):
                raise LongVideoError("stop boundary was exceeded by a later completed segment")
            if stop_after_index is not None and receipt["segments"][stop_after_index]["status"] == "completed" and not stop_sent:
                stop_receipt = client.json_request("POST", _project_path(project_id, "/stop"), {})
                receipt = validate_project_receipt(stop_receipt, manifest, project_id=project_id)
                events.append({"phase": "stop", "observed_at": time.time(), "receipt": _receipt_snapshot(stop_receipt)})
                if receipt["status"] == "completed":
                    raise LongVideoError("stop boundary was missed; the project completed before cancellation was accepted")
                if any(
                    segment["status"] == "completed"
                    for segment in receipt["segments"][stop_after_index + 1 :]
                ):
                    raise LongVideoError("stop boundary was exceeded by a later completed segment")
                stop_sent = True
            if stop_sent and receipt["status"] in {"stopped", "canceled", "cancelled"}:
                return receipt
            if not stop_sent and receipt["status"] == "completed":
                return receipt
        elif mode == "rerun":
            assert rerun_index is not None
            segment = receipt["segments"][rerun_index]
            if (
                len(segment["attempts"]) > previous_attempts
                and segment["status"] == "completed"
                and receipt["status"] in {"partial", "completed"}
            ):
                return receipt
        elif mode == "merge":
            merged = receipt.get("merged")
            if isinstance(merged, dict) and merged.get("status") == "failed":
                raise LongVideoError(f"merge failed: {merged.get('error', merged)}")
            if isinstance(merged, dict) and merged.get("status") == "completed":
                return receipt
        if monotonic() >= deadline:
            raise LongVideoError(f"project did not finish {mode} within {timeout:g}s")
        sleep(max(0.1, interval))


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _update_for_rerun(manifest: dict[str, Any], receipt: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    rerun = manifest["rerun"]
    index = int(rerun["segment_index"])
    segments = deepcopy(manifest["project"]["segments"])
    for position, segment in enumerate(segments):
        segment["id"] = receipt["segments"][position]["id"]
    target = segments[index]["request"]
    target["prompt"] = rerun["prompt"]
    target["parameters"]["seed"] = rerun["seed"]
    previous_attempts = len(receipt["segments"][index]["attempts"])
    return {"title": manifest["project"]["title"], "segments": segments}, index, previous_attempts


def _validate_merged_receipt(
    merged: Any,
    download: dict[str, Any],
    media: dict[str, Any],
    expected_duration: float,
    tolerance: float,
    expected_sources: list[dict[str, Any]],
) -> None:
    if not isinstance(merged, dict):
        raise LongVideoError("completed project has no merged receipt")
    url = merged.get("download_url")
    if not isinstance(url, str) or not url:
        raise LongVideoError("merged receipt has no download_url")
    sha256 = merged.get("sha256")
    if not isinstance(sha256, str) or len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise LongVideoError("merged receipt has no valid sha256")
    if download["sha256"] != sha256:
        raise LongVideoError("merged download SHA-256 differs from the receipt")
    if type(merged.get("size")) is not int or merged["size"] <= 0 or merged["size"] != download["size"]:
        raise LongVideoError("merged download size differs from the receipt")
    evidence = merged.get("media")
    if not isinstance(evidence, dict):
        raise LongVideoError("merged receipt has no media evidence")
    for key in ("width", "height"):
        if evidence.get(key) != media[key]:
            raise LongVideoError(f"merged receipt {key} differs from ffprobe")
    if not math.isclose(float(evidence.get("fps", 0)), 24, abs_tol=0.01):
        raise LongVideoError("merged receipt does not attest 24 fps")
    if bool(evidence.get("has_audio")) != bool(media["has_audio"]):
        raise LongVideoError("merged receipt audio evidence differs from ffprobe")
    if not math.isclose(float(media["duration"]), expected_duration, abs_tol=tolerance):
        raise LongVideoError(
            f"merged duration {media['duration']:g}s differs from segment sum {expected_duration:g}s"
        )
    if not math.isclose(float(evidence.get("duration", 0)), float(media["duration"]), abs_tol=0.08):
        raise LongVideoError("merged receipt duration differs from ffprobe")
    sources = merged.get("sources")
    if not isinstance(sources, list) or len(sources) != len(expected_sources):
        raise LongVideoError("merged source evidence is missing or has the wrong count")
    for position, (actual, expected) in enumerate(zip(sources, expected_sources, strict=True)):
        if not isinstance(actual, dict):
            raise LongVideoError(f"merged source {position} is not an object")
        for key in ("index", "segment_id", "job_id", "sha256", "size"):
            if actual.get(key) != expected.get(key):
                raise LongVideoError(f"merged source {position} {key} differs from the final segment job")


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise LongVideoError(f"{label} is not a valid SHA-256")
    return value


def _expected_attempt_request(
    manifest: dict[str, Any], segment_index: int, attempt_index: int, *, rerun_applied: bool,
) -> dict[str, Any]:
    request = deepcopy(manifest["project"]["segments"][segment_index]["request"])
    rerun = manifest.get("rerun")
    if rerun_applied and rerun and segment_index == int(rerun["segment_index"]) and attempt_index > 0:
        request["prompt"] = rerun["prompt"]
        request["parameters"]["seed"] = rerun["seed"]
    return request


def _h3_frames(duration: float) -> int:
    return min(5 + 17 * max(0, math.ceil((duration * H3_FPS - 5) / 17)), H3_MAX_FRAMES)


def _validate_job_evidence(
    job: Any,
    job_id: str,
    request: dict[str, Any],
    label: str,
    *,
    continuation: str = "none",
    continuation_evidence: dict[str, Any] | None = None,
    expected_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise LongVideoError(f"{label} job receipt is not an object")
    if job.get("status", job.get("state")) != "completed" or job.get("job_id", job.get("id")) != job_id:
        raise LongVideoError(f"{label} job receipt is not completed or changed identity")
    requested = request["parameters"]
    raw_prompt = job.get("raw_prompt")
    expected_raw = request["prompt"].strip()
    if continuation != "none":
        if not isinstance(continuation_evidence, dict):
            raise LongVideoError(f"{label} has no continuation evidence for prompt binding")
        derived_id = continuation_evidence.get("asset_id")
        if not isinstance(derived_id, str):
            raise LongVideoError(f"{label} continuation has no derived asset id")
        stable = f"@{{{derived_id}}}"
        suffix = (
            f"At 0.00 seconds, continue seamlessly from {stable}; preserve identity, wardrobe, color, key objects, "
            "composition, lighting, scene geography, spatial relationships, and screen direction."
            if continuation == "tail_frame" else
            f"Continue the preceding action, motion phase, camera trajectory, scene geography, and screen direction from {stable}; "
            "preserve the target identity and do not copy the source identity or reuse its audio."
        )
        expected_raw = "; ".join(part for part in (expected_raw, suffix) if part)
    if raw_prompt != expected_raw:
        raise LongVideoError(f"{label} raw prompt differs from the requested prompt")
    expected_parts = request.get("parts", {})
    if job.get("prompt_parts", {}) != expected_parts:
        raise LongVideoError(f"{label} prompt parts differ from the request")
    if not isinstance(job.get("prompt"), str) or not job["prompt"].strip():
        raise LongVideoError(f"{label} has no compiled model prompt")
    parameters = job.get("parameters")
    if not isinstance(parameters, dict):
        raise LongVideoError(f"{label} job has no resolved parameters")
    for key in ("profile_id", "profile_version", "profile_digest"):
        if parameters.get(key) != request[key]:
            raise LongVideoError(f"{label} resolved {key} differs from the pinned request")
    requested_steps = requested["steps"]
    if parameters.get("steps") != requested_steps:
        raise LongVideoError(f"{label} resolved steps differ from the request")
    resolved_seed = parameters.get("seed")
    if requested["seed"] == -1:
        if type(resolved_seed) is not int or resolved_seed < 0:
            raise LongVideoError(f"{label} resolved random seed is invalid")
    elif resolved_seed != requested["seed"]:
        raise LongVideoError(f"{label} resolved seed differs from the request")
    width, height = {"16:9": (1344, 768), "9:16": (768, 1344)}[requested["aspect_ratio"]]
    frames = _h3_frames(float(requested["duration"]))
    for key, expected in (("width", width), ("height", height), ("frames", frames), ("fps", 24)):
        if parameters.get(key) != expected:
            raise LongVideoError(f"{label} resolved {key} differs from the request")
    if not math.isclose(float(parameters.get("duration_requested", -1)), float(requested["duration"]), abs_tol=0.001):
        raise LongVideoError(f"{label} resolved duration differs from the request")
    if not math.isclose(float(parameters.get("duration_actual", -1)), round(frames / 24, 3), abs_tol=0.001):
        raise LongVideoError(f"{label} resolved frame-grid duration differs from the request")
    expected_denoise = float(requested.get("denoise", 1.0))
    resolved_denoise = parameters.get("denoise", 1.0)
    if isinstance(resolved_denoise, bool) or not isinstance(resolved_denoise, (int, float)) or not math.isclose(float(resolved_denoise), expected_denoise, abs_tol=0.001):
        raise LongVideoError(f"{label} resolved denoise differs from the request")
    sampling_mode = parameters.get("sampling_mode")
    if sampling_mode not in {"turbo4", "base"}:
        raise LongVideoError(f"{label} has an invalid sampling mode")
    expected_sampling_mode = expected_profile.get("sampling_mode") if isinstance(expected_profile, dict) else None
    if sampling_mode != expected_sampling_mode:
        raise LongVideoError(f"{label} sampling mode differs from the pinned profile")
    limits = expected_profile.get("limits", {}) if isinstance(expected_profile, dict) else {}
    step_limits = limits.get("steps") if isinstance(limits, dict) else None
    if not isinstance(step_limits, list) or len(step_limits) != 2 or not step_limits[0] <= requested_steps <= step_limits[1]:
        raise LongVideoError(f"{label} requested steps are outside the pinned profile")
    if sampling_mode == "turbo4":
        lora_limits = limits.get("lora_strength") if isinstance(limits, dict) else None
        expected_lora = requested.get("lora_strength", 0.75)
        if not isinstance(lora_limits, list) or len(lora_limits) != 2 or not lora_limits[0] <= expected_lora <= lora_limits[1]:
            raise LongVideoError(f"{label} requested model strength is outside the pinned profile")
    workflow_sha = _sha256(job.get("workflow_sha256"), f"{label} workflow hash")
    workflow = job.get("workflow_evidence")
    if not isinstance(workflow, dict) or workflow.get("sha256") != workflow_sha:
        raise LongVideoError(f"{label} job has no matching workflow evidence")
    compiled_prompt_sha = hashlib.sha256(job["prompt"].encode("utf-8")).hexdigest()
    if workflow.get("prompt_sha256") != compiled_prompt_sha:
        raise LongVideoError(f"{label} compiled prompt differs from the actual workflow")
    classes = workflow.get("node_classes")
    if not isinstance(classes, list) or "BasicScheduler" not in classes:
        raise LongVideoError(f"{label} workflow does not prove a BasicScheduler node")
    if workflow.get("steps") != requested_steps:
        raise LongVideoError(f"{label} workflow steps differ from the request")
    expected_sampler = "sa_solver" if sampling_mode == "turbo4" else "res_multistep"
    if workflow.get("sampler") != expected_sampler or workflow.get("scheduler") != "simple":
        raise LongVideoError(f"{label} workflow sampler/scheduler differs from the sampling profile")
    for key, expected in (("seed", resolved_seed), ("width", width), ("height", height), ("frames", frames)):
        if workflow.get(key) != expected:
            raise LongVideoError(f"{label} workflow {key} differs from the resolved request")
    workflow_denoise = workflow.get("denoise")
    if isinstance(workflow_denoise, bool) or not isinstance(workflow_denoise, (int, float)) or not math.isclose(float(workflow_denoise), expected_denoise, abs_tol=0.001):
        raise LongVideoError(f"{label} BasicScheduler denoise differs from the request")
    if sampling_mode == "turbo4":
        if not isinstance(workflow.get("lora"), str) or not workflow["lora"].strip():
            raise LongVideoError(f"{label} Turbo4 workflow does not prove its LoRA")
        expected_lora = float(requested.get("lora_strength", 0.75))
        resolved_lora = workflow.get("lora_strength")
        if isinstance(resolved_lora, bool) or not isinstance(resolved_lora, (int, float)) or not math.isclose(float(resolved_lora), expected_lora, abs_tol=0.001):
            raise LongVideoError(f"{label} Turbo4 LoRA strength differs from the request")
    else:
        strength = workflow.get("lora_strength", 0)
        if workflow.get("lora") not in {None, ""} or isinstance(strength, bool) or not isinstance(strength, (int, float)) or not math.isclose(float(strength), 0, abs_tol=0.001):
            raise LongVideoError(f"{label} Base workflow unexpectedly contains a Turbo LoRA")
    actual_references = job.get("references")
    if not isinstance(actual_references, list) or not all(isinstance(item, dict) for item in actual_references):
        raise LongVideoError(f"{label} has invalid resolved references")
    expected_references = request.get("references", [])
    implicit_count = 0 if continuation == "none" else 1
    if len(actual_references) != len(expected_references) + implicit_count:
        raise LongVideoError(f"{label} resolved reference count differs from the request")
    derived_id = continuation_evidence.get("asset_id") if isinstance(continuation_evidence, dict) else None
    expected_order = [str(item.get("asset_id", item.get("id"))) for item in expected_references]
    if continuation == "tail_frame":
        expected_order.insert(0, str(derived_id))
    elif continuation == "previous_video":
        expected_order.append(str(derived_id))
    if [item.get("asset_id") for item in actual_references] != expected_order:
        raise LongVideoError(f"{label} resolved reference order differs from the request")
    explicit_actual = actual_references[1:] if continuation == "tail_frame" else actual_references[:-1] if continuation == "previous_video" else actual_references
    for expected, actual in zip(expected_references, explicit_actual, strict=True):
        expected_kind = {
            "first_frame": "image", "last_frame": "image", "identity": "image", "style": "image", "composition": "image",
            "motion": "video", "camera": "video", "pacing": "video",
            "voice": "audio", "music": "audio", "rhythm": "audio",
        }.get(expected.get("role", "reference"))
        if expected_kind is not None and actual.get("kind") != expected_kind:
            raise LongVideoError(f"{label} resolved reference kind differs from its role")
        if actual.get("role") != expected.get("role", "reference"):
            raise LongVideoError(f"{label} resolved reference role differs from the request")
        if bool(actual.get("include_audio", False)) != bool(expected.get("include_audio", False)):
            raise LongVideoError(f"{label} resolved reference audio flag differs from the request")
        for key in ("voice_speaker", "voice_subject"):
            fallback = "" if key == "voice_speaker" else 0
            if (actual.get(key) or fallback) != (expected.get(key) or fallback):
                raise LongVideoError(f"{label} resolved reference {key} differs from the request")
    if continuation != "none":
        expected_kind, expected_role = ("image", "first_frame") if continuation == "tail_frame" else ("video", "motion")
        implicit = actual_references[0] if continuation == "tail_frame" else actual_references[-1]
        if implicit.get("asset_id") != derived_id or implicit.get("kind") != expected_kind or implicit.get("role") != expected_role:
            raise LongVideoError(f"{label} implicit continuation reference differs from the project mode")
        if bool(implicit.get("include_audio", False)) or implicit.get("voice_speaker") not in {None, ""} or implicit.get("voice_subject") not in {None, 0}:
            raise LongVideoError(f"{label} implicit continuation reference has unsupported audio or voice fields")
    counters = {"image": 0, "video": 0, "audio": 0}
    labels = {"image": "Picture", "video": "Video", "audio": "Audio"}
    for actual in actual_references:
        kind = actual.get("kind")
        if kind not in counters:
            raise LongVideoError(f"{label} resolved reference kind is invalid")
        counters[kind] += 1
        expected_label = f"<{labels[kind]} {counters[kind]}>"
        # `tag_label` is the user-visible asset label/filename. Newer servers
        # may additionally expose `model_tag`; the authoritative ordinal is
        # always proven by the compiled prompt and the exact reference order.
        if (actual.get("model_tag") not in {None, expected_label}) or expected_label not in job["prompt"]:
            raise LongVideoError(f"{label} resolved reference tag/order differs from the compiled prompt")
    outputs = job.get("outputs")
    if not isinstance(outputs, list) or not outputs or not isinstance(outputs[0], dict):
        raise LongVideoError(f"{label} completed job has no output evidence")
    output = outputs[0]
    output_sha = _sha256(output.get("sha256"), f"{label} output hash")
    if type(output.get("size")) is not int or output["size"] <= 0:
        raise LongVideoError(f"{label} output has no valid byte size")
    return {
        "job_id": job_id, "sampling_mode": sampling_mode,
        "parameters": parameters, "workflow_sha256": workflow_sha,
        "workflow_evidence": workflow, "output_sha256": output_sha,
        "output": output,
    }


def validate_all_attempt_evidence(
    client: Client,
    receipt: dict[str, Any],
    manifest: dict[str, Any],
    *,
    rerun_applied: bool,
    profile_contracts: dict[tuple[str, str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fetch and bind every completed attempt to its request, workflow and output hash."""
    fetched: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []

    def fetch_job(
        job_id: str, label: str, request: dict[str, Any], continuation: str,
        continuation_evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if job_id not in fetched:
            fetched[job_id] = client.json_request("GET", f"/api/jobs/{job_id}")
        profile_key = (request["profile_id"], request["profile_version"], request["profile_digest"])
        profile = profile_contracts.get(profile_key)
        if profile is None:
            raise LongVideoError(f"{label} request no longer matches an available pinned profile")
        return _validate_job_evidence(
            fetched[job_id], job_id, request, label,
            continuation=continuation, continuation_evidence=continuation_evidence,
            expected_profile=profile,
        )

    for segment_index, segment in enumerate(receipt["segments"]):
        for attempt_index, attempt in enumerate(segment["attempts"]):
            if attempt.get("status") != "completed":
                continue
            label = f"segment {segment_index} attempt {attempt_index}"
            job_id = validate_opaque_id(attempt.get("job_id"), label=f"{label} job id")
            request = _expected_attempt_request(
                manifest, segment_index, attempt_index, rerun_applied=rerun_applied,
            )
            attempt_workflow = attempt.get("workflow_evidence")
            continuation = attempt.get("continuation")
            job_evidence = fetch_job(job_id, label, request, segment["continuation"], continuation)
            if not isinstance(attempt_workflow, dict) or attempt_workflow.get("sha256") != job_evidence["workflow_sha256"]:
                raise LongVideoError(f"{label} workflow evidence differs from its job")
            if segment["continuation"] != "none":
                if not isinstance(continuation, dict):
                    raise LongVideoError(f"{label} has no continuation evidence")
                source_job_id = validate_opaque_id(continuation.get("source_job_id"), label=f"{label} source job id")
                source_segment = receipt["segments"][segment_index - 1]
                source_attempt_index = next(
                    (position for position, item in enumerate(source_segment["attempts"]) if item.get("job_id") == source_job_id),
                    None,
                )
                if source_attempt_index is None:
                    raise LongVideoError(f"{label} source job is not a preceding completed attempt")
                source_request = _expected_attempt_request(
                    manifest, segment_index - 1, source_attempt_index, rerun_applied=rerun_applied,
                )
                source_attempt = source_segment["attempts"][source_attempt_index]
                source_evidence = fetch_job(
                    source_job_id, f"{label} source", source_request, source_segment["continuation"],
                    source_attempt.get("continuation"),
                )
                if continuation.get("source_sha256") != source_evidence["output_sha256"]:
                    raise LongVideoError(f"{label} continuation source SHA differs from the actual source output")
            evidence.append({
                "segment_index": segment_index, "attempt_index": attempt_index,
                "request": request, **job_evidence,
            })
    if not evidence:
        raise LongVideoError("project contains no completed attempt evidence")
    return evidence


def resolve_profile_contracts(
    capabilities: Any, manifest: dict[str, Any],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    profiles = capabilities.get("profiles") if isinstance(capabilities, dict) else None
    if not isinstance(profiles, list):
        raise LongVideoError("capabilities response has no profile catalog")
    contracts: dict[tuple[str, str, str], dict[str, Any]] = {}
    for segment in manifest["project"]["segments"]:
        request = segment["request"]
        key = (request["profile_id"], request["profile_version"], request["profile_digest"])
        profile = next((
            item for item in profiles
            if isinstance(item, dict)
            and (item.get("id"), item.get("version"), item.get("manifest_sha256")) == key
        ), None)
        if not isinstance(profile, dict) or profile.get("available") is not True:
            raise LongVideoError(f"pinned profile {key[0]}@{key[1]} is unavailable or changed digest")
        if profile.get("output_type") != "video" or profile.get("sampling_mode") not in {"turbo4", "base"}:
            raise LongVideoError(f"pinned profile {key[0]} has an invalid video sampling contract")
        limits = profile.get("limits")
        step_limits = limits.get("steps") if isinstance(limits, dict) else None
        if not isinstance(step_limits, list) or len(step_limits) != 2 or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in step_limits):
            raise LongVideoError(f"pinned profile {key[0]} has no trusted step limits")
        if profile.get("sampling_mode") == "turbo4":
            strength_limits = limits.get("lora_strength")
            if not isinstance(strength_limits, list) or len(strength_limits) != 2 or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in strength_limits):
                raise LongVideoError(f"pinned profile {key[0]} has no trusted model-strength limits")
        contracts[key] = profile
    return contracts


def execute_manifest(
    client: Client,
    manifest: dict[str, Any],
    *,
    output_dir: Path,
    timeout: float = 3600,
    interval: float = 3,
    ffprobe_executable: str = "ffprobe",
    stop_after_index: int | None = None,
    probe: Callable[..., dict[str, Any]] = assert_output,
) -> dict[str, Any]:
    output_root = output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if stop_after_index is not None and not 0 <= stop_after_index < len(manifest["project"]["segments"]):
        raise LongVideoError("stop_after_index is outside the segment list")
    manifest_sha256 = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    events: list[dict[str, Any]] = []
    project_id: str | None = None
    evidence: dict[str, Any] = {
        "version": 1, "manifest_sha256": manifest_sha256,
        "started_at": time.time(), "events": events,
    }
    try:
        capabilities = client.json_request("GET", "/api/capabilities")
        profile_contracts = resolve_profile_contracts(capabilities, manifest)
        evidence["profile_contracts"] = [
            {
                "id": profile["id"], "version": profile["version"],
                "manifest_sha256": profile["manifest_sha256"],
                "sampling_mode": profile["sampling_mode"],
            }
            for profile in profile_contracts.values()
        ]
        created = validate_project_receipt(
            client.json_request("POST", "/api/video-projects", manifest["project"]), manifest,
        )
        project_id = created["id"]
        evidence["project_id"] = project_id
        evidence["create_receipt"] = created
        started = validate_project_receipt(
            client.json_request("POST", _project_path(project_id, "/run"), {}), manifest,
            project_id=project_id, enforce_order=True,
        )
        events.append({"phase": "start", "observed_at": time.time(), "receipt": _receipt_snapshot(started)})
        receipt = _wait_project(
            client, project_id, manifest, timeout=timeout, interval=interval, events=events,
            mode="initial", stop_after_index=stop_after_index,
        )
        evidence["initial_receipt"] = receipt
        if stop_after_index is not None:
            if receipt["status"] not in {"stopped", "canceled", "cancelled"}:
                raise LongVideoError("stop acceptance did not reach a stopped terminal state")
            evidence.update({"status": "stopped", "final_receipt": receipt, "completed_at": time.time()})
            evidence_path = output_root / f"long-video-{project_id}.json"
            _atomic_json(evidence_path, evidence)
            evidence["evidence_path"] = str(evidence_path)
            return evidence

        validate_continuation_evidence(receipt)
        evidence["initial_segment_jobs"] = validate_all_attempt_evidence(
            client, receipt, manifest, rerun_applied=False, profile_contracts=profile_contracts,
        )

        if manifest.get("rerun"):
            update, index, previous_attempts = _update_for_rerun(manifest, receipt)
            rerun_requests = _rerun_requests(manifest)
            updated = validate_project_receipt(
                client.json_request("PUT", _project_path(project_id), update), manifest, project_id=project_id,
                expected_requests=rerun_requests,
            )
            target = updated["segments"][index]
            if target["request"].get("prompt") != manifest["rerun"]["prompt"]:
                raise LongVideoError("updated segment receipt did not preserve the changed prompt")
            if target["request"].get("parameters", {}).get("seed") != manifest["rerun"]["seed"]:
                raise LongVideoError("updated segment receipt did not preserve the changed seed")
            rerun_started = validate_project_receipt(
                client.json_request("POST", _segment_path(project_id, target["id"]), {}), manifest,
                project_id=project_id, expected_requests=rerun_requests,
            )
            events.append({"phase": "rerun-start", "observed_at": time.time(), "receipt": _receipt_snapshot(rerun_started)})
            receipt = _wait_project(
                client, project_id, manifest, timeout=timeout, interval=interval, events=events,
                mode="rerun", previous_attempts=previous_attempts, rerun_index=index,
                expected_requests=rerun_requests,
            )
            # A single-segment endpoint must never silently spend on dependent
            # clips. The acceptance client therefore makes each stale
            # downstream regeneration explicit and auditable.
            for downstream_index in range(index + 1, len(receipt["segments"])):
                downstream = receipt["segments"][downstream_index]
                if downstream["status"] != "stale":
                    continue
                downstream_attempts = len(downstream["attempts"])
                rerun_started = validate_project_receipt(
                    client.json_request("POST", _segment_path(project_id, downstream["id"]), {}), manifest,
                    project_id=project_id, expected_requests=rerun_requests,
                )
                events.append({
                    "phase": f"rerun-dependent-{downstream_index}", "observed_at": time.time(),
                    "receipt": _receipt_snapshot(rerun_started),
                })
                receipt = _wait_project(
                    client, project_id, manifest, timeout=timeout, interval=interval, events=events,
                    mode="rerun", previous_attempts=downstream_attempts, rerun_index=downstream_index,
                    expected_requests=rerun_requests,
                )
            validate_continuation_evidence(receipt)
            evidence["rerun_receipt"] = receipt

        evidence["final_segment_jobs"] = validate_all_attempt_evidence(
            client, receipt, manifest, rerun_applied=bool(manifest.get("rerun")),
            profile_contracts=profile_contracts,
        )

        merge_started = validate_project_receipt(
            client.json_request("POST", _project_path(project_id, "/merge"), {}), manifest,
            project_id=project_id,
            expected_requests=_rerun_requests(manifest) if manifest.get("rerun") else None,
        )
        events.append({"phase": "merge-start", "observed_at": time.time(), "receipt": _receipt_snapshot(merge_started)})
        receipt = _wait_project(
            client, project_id, manifest, timeout=timeout, interval=interval, events=events, mode="merge",
            expected_requests=_rerun_requests(manifest) if manifest.get("rerun") else None,
        )
        merged = receipt.get("merged")
        if not isinstance(merged, dict) or not isinstance(merged.get("download_url"), str):
            raise LongVideoError("merge completed without a downloadable receipt")
        destination = (output_root / manifest["acceptance"]["output_name"]).resolve()
        try:
            destination.relative_to(output_root)
        except ValueError as error:
            raise LongVideoError("merged output path escapes output_dir") from error
        download = client.download(merged["download_url"], destination)
        media = probe(
            destination, output_type="video",
            expected_width=manifest["acceptance"]["width"],
            expected_height=manifest["acceptance"]["height"],
            expect_audio=manifest["acceptance"]["expect_audio"],
            executable=ffprobe_executable,
        )
        duration = expected_total_duration(manifest)
        current_jobs = {
            item["job_id"]: item for item in evidence["final_segment_jobs"]
        }
        expected_sources: list[dict[str, Any]] = []
        for segment in receipt["segments"]:
            job_id = segment.get("job_id")
            current = current_jobs.get(job_id)
            if current is None:
                raise LongVideoError("merged segment has no validated current job evidence")
            expected_sources.append({
                "index": segment["index"], "segment_id": segment["id"], "job_id": job_id,
                "sha256": current["output_sha256"], "size": current["output"]["size"],
            })
        _validate_merged_receipt(
            merged, download, media, duration, manifest["acceptance"]["duration_tolerance"],
            expected_sources,
        )
        evidence.update({
            "status": "completed", "final_receipt": receipt, "download": download,
            "media": media, "expected_duration": duration, "completed_at": time.time(),
        })
        evidence_path = output_root / f"long-video-{project_id}.json"
        _atomic_json(evidence_path, evidence)
        evidence["evidence_path"] = str(evidence_path)
        return evidence
    except (E2EError, OSError, ValueError, KeyboardInterrupt) as error:
        cancellation: dict[str, Any] = {"attempted": False}
        if project_id is not None:
            try:
                response = client.json_request("POST", _project_path(project_id, "/stop"), {})
                cancellation = {"attempted": True, "ok": True, "response": response}
            except Exception as stop_error:  # best effort must not hide the primary acceptance failure
                cancellation = {"attempted": True, "ok": False, "error": str(stop_error)}
        evidence.update({
            "status": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
            "error": str(error) or type(error).__name__, "cancellation": cancellation,
            "completed_at": time.time(),
        })
        if project_id is not None:
            evidence_path = output_root / f"long-video-{project_id}.partial.json"
            _atomic_json(evidence_path, evidence)
        if isinstance(error, LongVideoError):
            error.partial_evidence = evidence
            raise
        wrapped = LongVideoError(str(error) or type(error).__name__)
        wrapped.partial_evidence = evidence
        raise wrapped from error


def make_client(base_url: str, api_key: str, timeout: float) -> ApiClient:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.path not in {"", "/"}:
        raise ValueError("base_url must be an origin without a path")
    return ApiClient(base_url, api_key=api_key, timeout=timeout)
