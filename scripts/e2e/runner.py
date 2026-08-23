"""Scenario orchestration and ffprobe assertions."""

from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .client import ApiClient, E2EError, validate_opaque_id
from .scenarios import H3_FPS, H3_MAX_FRAMES, SCENARIOS, build_request, resolve_profile


def _rate(raw: Any) -> float:
    try:
        numerator, denominator = str(raw).split("/", 1)
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def ffprobe(path: Path, *, executable: str = "ffprobe") -> dict[str, Any]:
    command = [
        executable, "-v", "error", "-show_entries",
        "format=duration,format_name:stream=codec_type,codec_name,width,height,avg_frame_rate,nb_frames",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise E2EError(f"ffprobe failed to start for {path}: {error}") from error
    if completed.returncode:
        raise E2EError(f"ffprobe rejected {path}: {completed.stderr.strip()[:500]}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise E2EError("ffprobe returned invalid JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("streams"), list):
        raise E2EError("ffprobe did not return stream metadata")
    return value


def assert_output(
    path: Path,
    *,
    output_type: str,
    expected_width: int | None = None,
    expected_height: int | None = None,
    expect_audio: bool = True,
    expected_duration: float | None = None,
    expected_frames: int | None = None,
    executable: str = "ffprobe",
) -> dict[str, Any]:
    value = ffprobe(path, executable=executable)
    streams = value["streams"]
    video = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"), None)
    if video is None:
        raise E2EError("output has no decodable image/video stream")
    width, height = int(video.get("width", 0) or 0), int(video.get("height", 0) or 0)
    if width <= 0 or height <= 0:
        raise E2EError("output has invalid dimensions")
    if expected_width and width != expected_width:
        raise E2EError(f"output width {width} != expected {expected_width}")
    if expected_height and height != expected_height:
        raise E2EError(f"output height {height} != expected {expected_height}")
    evidence: dict[str, Any] = {
        "width": width, "height": height, "video_codec": video.get("codec_name"),
        "has_audio": audio is not None,
    }
    if output_type == "video":
        duration = float((value.get("format") or {}).get("duration", 0) or 0)
        fps = _rate(video.get("avg_frame_rate", "0/1"))
        if duration <= 0:
            raise E2EError("video output has no positive duration")
        if not math.isclose(fps, 24, abs_tol=0.01):
            raise E2EError(f"video output fps {fps:g} != 24")
        if expect_audio and audio is None:
            raise E2EError("H3 video output has no audio stream")
        try:
            frame_count = int(video.get("nb_frames", 0) or 0)
        except (TypeError, ValueError):
            frame_count = 0
        if expected_duration is not None and not math.isclose(duration, expected_duration, abs_tol=0.08):
            raise E2EError(f"video duration {duration:g}s != expected {expected_duration:g}s")
        if expected_frames is not None:
            if frame_count <= 0:
                raise E2EError("video output does not expose a verifiable frame count")
            if frame_count != expected_frames:
                raise E2EError(f"video frame count {frame_count} != expected {expected_frames}")
        evidence.update({
            "duration": duration, "fps": fps, "frames": frame_count,
            "audio_codec": audio.get("codec_name") if audio else None,
        })
    return evidence


def _local_expectations(request: dict[str, Any], profile: dict[str, Any], output_type: str) -> dict[str, Any]:
    submitted = request["parameters"]
    landscape = submitted.get("aspect_ratio", "16:9") == "16:9"
    width, height = ((1344, 768) if landscape else (768, 1344)) if output_type == "video" else ((1024, 576) if landscape else (576, 1024))
    result: dict[str, Any] = {
        "width": width, "height": height, "steps": submitted["steps"],
        "sampler": "euler_ancestral", "scheduler": "normal", "lora_strength": 0.0, "expects_lora": False,
    }
    if output_type == "video":
        requested_duration = float(submitted["duration"])
        frames = min(5 + 17 * max(0, math.ceil((requested_duration * H3_FPS - 5) / 17)), H3_MAX_FRAMES)
        turbo = profile.get("sampling_mode") == "turbo4"
        result.update({
            "duration_requested": requested_duration,
            "frames": frames,
            "duration_actual": frames / H3_FPS,
            "sampling_mode": profile.get("sampling_mode"),
            "sampler": "sa_solver" if turbo else "res_multistep",
            "scheduler": "simple",
            "lora_strength": float(submitted.get("lora_strength", 0)) if turbo else 0.0,
            "denoise": float(submitted.get("denoise", 1.0)),
            "expects_lora": turbo,
        })
    return result


def execute_run(
    client: ApiClient,
    run: dict[str, Any],
    *,
    output_dir: Path,
    timeout: float = 1800,
    interval: float = 3,
    ffprobe_executable: str = "ffprobe",
    on_status: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    scenario = SCENARIOS[str(run["scenario"])]
    capabilities = client.capabilities()
    profiles = capabilities.get("profiles")
    if not isinstance(profiles, list):
        raise E2EError("capabilities response has no profiles array")
    profile = resolve_profile(
        profiles, scenario,
        sampling_mode=str(run.get("sampling_mode", "turbo4")),
        profile_id=str(run.get("profile_id", "")),
    )
    uploaded: list[dict[str, Any]] = []
    for path_value, slot in zip(run.get("assets", []), scenario.references, strict=True):
        asset = client.upload(Path(path_value), slot.kind)
        if slot.kind == "video" and run.get("include_audio") is True:
            asset["include_audio"] = True
        uploaded.append(asset)
    request = build_request(run, profile, uploaded)
    submitted = client.submit(request)
    job_id = validate_opaque_id(submitted.get("job_id", submitted.get("id")), label="job id")
    try:
        result = client.wait(job_id, timeout=timeout, interval=interval, on_status=on_status)
    except (E2EError, KeyboardInterrupt) as error:
        cancellation: dict[str, Any]
        try:
            response = client.cancel(job_id)
            cancellation = {"attempted": True, "ok": True, "response": response}
        except E2EError as cancel_error:
            cancellation = {"attempted": True, "ok": False, "error": str(cancel_error)}
        partial = {
            "scenario": scenario.name, "job_id": job_id, "request_id": request["request_id"],
            "status": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
            "error": str(error) or type(error).__name__, "cancellation": cancellation,
            "completed_at": time.time(),
        }
        if isinstance(error, E2EError):
            error.partial_evidence = partial
            raise
        interrupted = E2EError("run interrupted after submission; cancellation was attempted")
        interrupted.partial_evidence = partial
        raise interrupted from error
    parameters = result.get("parameters")
    if not isinstance(parameters, dict):
        raise E2EError("completed result has no resolved parameters")
    expected_profile = {
        "profile_id": profile["id"], "profile_version": profile["version"],
        "profile_digest": profile["manifest_sha256"],
    }
    for key, expected in expected_profile.items():
        if parameters.get(key) != expected:
            raise E2EError(f"resolved {key} does not match the pinned profile")
    expected = _local_expectations(request, profile, scenario.output_type)
    for key in ("width", "height", "steps"):
        if parameters.get(key) != expected[key]:
            raise E2EError(f"resolved {key} does not match the locally derived request contract")
    if scenario.output_type == "video":
        for key in ("sampling_mode", "sampler", "scheduler", "frames"):
            if parameters.get(key) != expected[key]:
                raise E2EError(f"resolved {key} does not match the locally derived sampling contract")
        for key in ("duration_requested", "duration_actual", "lora_strength", "denoise"):
            if not isinstance(parameters.get(key), (int, float)) or not math.isclose(float(parameters[key]), float(expected[key]), abs_tol=0.001):
                raise E2EError(f"resolved {key} does not match the locally derived sampling contract")
        actual_lora = parameters.get("lora")
        if expected["expects_lora"] and not isinstance(actual_lora, str):
            raise E2EError("Turbo result does not prove the resolved LoRA")
        if not expected["expects_lora"] and actual_lora not in {None, ""}:
            raise E2EError("Base result unexpectedly resolved a LoRA")
    workflow_sha256 = result.get("workflow_sha256")
    if not isinstance(workflow_sha256, str) or len(workflow_sha256) != 64 or any(char not in "0123456789abcdef" for char in workflow_sha256):
        raise E2EError("completed result has no valid workflow SHA-256")
    workflow_evidence = result.get("workflow_evidence")
    if not isinstance(workflow_evidence, dict) or workflow_evidence.get("sha256") != workflow_sha256:
        raise E2EError("completed result has no matching final-workflow evidence")
    for key in ("steps", "sampler", "scheduler"):
        if workflow_evidence.get(key) != expected[key]:
            raise E2EError(f"final workflow {key} does not match the locally derived contract")
    if scenario.output_type == "video":
        evidence_denoise = workflow_evidence.get("denoise")
        if isinstance(evidence_denoise, bool) or not isinstance(evidence_denoise, (int, float)) or not math.isclose(float(evidence_denoise), float(expected["denoise"]), abs_tol=0.001):
            raise E2EError("final workflow denoise does not match the locally derived contract")
    evidence_lora = workflow_evidence.get("lora")
    evidence_strength = workflow_evidence.get("lora_strength", 0)
    if isinstance(evidence_strength, bool):
        raise E2EError("final workflow has invalid LoRA strength evidence")
    try:
        numeric_strength = float(evidence_strength)
    except (TypeError, ValueError, OverflowError) as error:
        raise E2EError("final workflow has invalid LoRA strength evidence") from error
    if expected["expects_lora"]:
        if not isinstance(evidence_lora, str) or not math.isclose(numeric_strength, float(expected["lora_strength"]), abs_tol=0.001):
            raise E2EError("final workflow does not prove the expected Turbo LoRA contract")
    elif evidence_lora not in {None, ""} or not math.isclose(numeric_strength, 0.0, abs_tol=0.001):
        raise E2EError("final workflow unexpectedly contains a LoRA")
    outputs = result.get("outputs")
    if not isinstance(outputs, list) or not outputs or not isinstance(outputs[0], dict):
        raise E2EError("completed result has no output metadata")
    output = outputs[0]
    url = result.get("download_url", output.get("download_url"))
    if not isinstance(url, str) or not url:
        url = f"/api/download?id={job_id}&index=0"
    suffix = Path(str(output.get("filename", ""))).suffix or (".mp4" if scenario.output_type == "video" else ".png")
    output_root = output_dir.resolve()
    destination = (output_root / f"{scenario.name}-{job_id}{suffix}").resolve()
    try:
        destination.relative_to(output_root)
    except ValueError as error:
        raise E2EError("refusing to write outside the output directory") from error
    downloaded = client.download(url, destination)
    expected_sha = output.get("sha256")
    if not isinstance(expected_sha, str) or len(expected_sha) != 64 or any(char not in "0123456789abcdef" for char in expected_sha):
        raise E2EError("server output has no valid SHA-256 evidence")
    if downloaded["sha256"] != expected_sha:
        raise E2EError("download SHA-256 differs from server output evidence")
    media = assert_output(
        destination,
        output_type=scenario.output_type,
        expected_width=int(expected["width"]),
        expected_height=int(expected["height"]),
        expect_audio=scenario.output_type == "video",
        expected_duration=float(expected["duration_actual"]) if scenario.output_type == "video" else None,
        expected_frames=int(expected["frames"]) if scenario.output_type == "video" else None,
        executable=ffprobe_executable,
    )
    return {
        "scenario": scenario.name,
        "job_id": job_id,
        "profile": {
            "id": profile["id"], "version": profile["version"], "manifest_sha256": profile["manifest_sha256"],
        },
        "request_id": request["request_id"],
        "workflow_sha256": workflow_sha256,
        "workflow_evidence": workflow_evidence,
        "resolved_parameters": parameters,
        "uploads": [{key: value for key, value in asset.items() if key in {"id", "filename", "sha256", "kind", "media"}} for asset in uploaded],
        "download": downloaded,
        "media": media,
        "completed_at": time.time(),
    }
