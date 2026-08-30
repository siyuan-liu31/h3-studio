"""Versioned H3 reference-video sizing and long-sequence risk policy.

This module is deliberately free of ffmpeg and storage side effects.  The
media layer owns materialisation while generation uses the same receipt math
for its safety preflight and workflow evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .errors import ApiError


ALGORITHM_VERSION = "h3-reference-low-token/v1"
TOKEN_ESTIMATOR_VERSION = "h3-packed-sequence/v1"
SAFETY_POLICY_VERSION = "h3-sm120-sage/v1"
DEFAULT_RISK_THRESHOLD = 150_000


@dataclass(frozen=True, slots=True)
class ReferenceParameters:
    max_short_edge: int = 480
    max_long_edge: int = 864
    fps: int = 24
    max_duration: float = 15.0
    audio: str = "remove"
    fit: str = "contain"
    alignment: int = 32
    pad_mode: str = "edge"

    @classmethod
    def parse(cls, value: dict[str, Any]) -> "ReferenceParameters":
        preset = value.get("preset")
        if preset not in {None, "h3-low-token"}:
            raise ApiError(400, "invalid_parameter", "preset must be h3-low-token")
        allowed = {
            "operation", "source", "display_name", "preset", "max_short_edge",
            "max_long_edge", "fps", "max_duration", "audio", "fit",
            "alignment", "pad_mode",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ApiError(400, "invalid_parameter", f"unknown prepare_h3_reference parameter: {unknown[0]}")
        raw = {
            "max_short_edge": value.get("max_short_edge", 480),
            "max_long_edge": value.get("max_long_edge", 864),
            "fps": value.get("fps", 24),
            "max_duration": value.get("max_duration", 15.0),
            "audio": value.get("audio", "remove" if preset == "h3-low-token" else None),
            "fit": value.get("fit", "contain"),
            "alignment": value.get("alignment", 32),
            "pad_mode": value.get("pad_mode", "edge"),
        }
        for key in ("max_short_edge", "max_long_edge", "fps", "alignment"):
            item = raw[key]
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                raise ApiError(400, "invalid_dimensions", f"{key} must be a positive integer")
        duration = raw["max_duration"]
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(float(duration)) or float(duration) <= 0:
            raise ApiError(400, "invalid_duration", "max_duration must be a positive finite number")
        if raw["audio"] not in {"keep", "remove"}:
            raise ApiError(400, "invalid_parameter", "audio must be explicitly set to keep or remove")
        if raw["fit"] != "contain":
            raise ApiError(400, "invalid_parameter", "fit must be contain")
        if raw["pad_mode"] != "edge":
            raise ApiError(400, "invalid_parameter", "pad_mode must be edge")
        if raw["alignment"] != 32:
            raise ApiError(400, "invalid_dimensions", "H3 reference alignment must be 32")
        if raw["fps"] != 24:
            raise ApiError(400, "invalid_parameter", "H3 reference fps must be 24")
        if raw["max_short_edge"] > raw["max_long_edge"]:
            raise ApiError(400, "invalid_dimensions", "max_short_edge cannot exceed max_long_edge")
        return cls(**raw)

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReferencePlan:
    source_width: int
    source_height: int
    rotation: int
    display_width: int
    display_height: int
    orientation: str
    content_width: int
    content_height: int
    canvas_width: int
    canvas_height: int
    pad_left: int
    pad_right: int
    pad_top: int
    pad_bottom: int
    scaled: bool

    def public(self) -> dict[str, Any]:
        return asdict(self)


def normalize_rotation(value: Any) -> int:
    try:
        rotation = int(round(float(value or 0))) % 360
    except (TypeError, ValueError, OverflowError):
        rotation = 0
    nearest = min((0, 90, 180, 270), key=lambda item: abs(((rotation - item + 180) % 360) - 180))
    return nearest


def calculate_reference_plan(width: Any, height: Any, rotation: Any, parameters: ReferenceParameters) -> ReferencePlan:
    if isinstance(width, bool) or isinstance(height, bool):
        raise ApiError(422, "invalid_dimensions", "source video dimensions are invalid")
    try:
        source_width, source_height = int(width), int(height)
    except (TypeError, ValueError, OverflowError) as error:
        raise ApiError(422, "invalid_dimensions", "source video dimensions are invalid") from error
    if source_width <= 0 or source_height <= 0 or source_width > 16384 or source_height > 16384:
        raise ApiError(422, "invalid_dimensions", "source video dimensions are invalid")
    normalized_rotation = normalize_rotation(rotation)
    display_width, display_height = (
        (source_height, source_width) if normalized_rotation in {90, 270}
        else (source_width, source_height)
    )
    if display_width == display_height:
        orientation = "square"
        box_width = box_height = parameters.max_short_edge
    elif display_width > display_height:
        orientation = "landscape"
        box_width, box_height = parameters.max_long_edge, parameters.max_short_edge
    else:
        orientation = "portrait"
        box_width, box_height = parameters.max_short_edge, parameters.max_long_edge

    scale = min(1.0, box_width / display_width, box_height / display_height)
    # YUV420P needs even content dimensions.  Rounding to the nearest even
    # pixel minimises aspect drift; the aligned canvas absorbs the remainder.
    content_width = max(2, min(box_width, int(round(display_width * scale / 2)) * 2))
    content_height = max(2, min(box_height, int(round(display_height * scale / 2)) * 2))
    alignment = parameters.alignment
    canvas_width = min(box_width, max(alignment, math.ceil(content_width / alignment) * alignment))
    canvas_height = min(box_height, max(alignment, math.ceil(content_height / alignment) * alignment))
    # Configured boxes are required to be aligned or there is no safe contain
    # canvas at their boundary.
    if canvas_width % alignment or canvas_height % alignment:
        raise ApiError(400, "invalid_dimensions", "reference limits must produce an alignment-safe canvas")
    left = (canvas_width - content_width) // 2
    top = (canvas_height - content_height) // 2
    return ReferencePlan(
        source_width=source_width,
        source_height=source_height,
        rotation=normalized_rotation,
        display_width=display_width,
        display_height=display_height,
        orientation=orientation,
        content_width=content_width,
        content_height=content_height,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        pad_left=left,
        pad_right=canvas_width - content_width - left,
        pad_top=top,
        pad_bottom=canvas_height - content_height - top,
        scaled=scale < 1.0,
    )


def idempotency_key(source_sha256: str, parameters: ReferenceParameters) -> str:
    canonical = {
        "algorithm_version": ALGORITHM_VERSION,
        "source_sha256": source_sha256,
        "parameters": parameters.public(),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def estimate_video_tokens(width: int, height: int, frames: int) -> int:
    if width <= 0 or height <= 0 or frames <= 0:
        return 0
    return math.ceil(width / 32) * math.ceil(height / 32) * math.ceil(frames / 4)


def estimate_packed_tokens(
    references: Iterable[dict[str, Any]], *, target_width: int, target_height: int,
    target_frames: int, image_reserve: int = 4096, text_audio_reserve: int = 4096,
) -> dict[str, Any]:
    reference_values = []
    for item in references:
        width = int(item.get("width", 0) or 0)
        height = int(item.get("height", 0) or 0)
        frames = int(item.get("frames", 0) or 0)
        tokens = estimate_video_tokens(width, height, frames)
        reference_values.append({"width": width, "height": height, "frames": frames, "tokens": tokens})
    target = estimate_video_tokens(target_width, target_height, target_frames)
    reserve = max(0, int(image_reserve)) + max(0, int(text_audio_reserve))
    return {
        "version": TOKEN_ESTIMATOR_VERSION,
        "reference_videos": reference_values,
        "reference_tokens": sum(item["tokens"] for item in reference_values),
        "target_tokens": target,
        "reserved_tokens": reserve,
        "total_tokens": target + reserve + sum(item["tokens"] for item in reference_values),
    }


def risk_assessment(tokens: int, *, gpu_architecture: str, attention_backend: str, threshold: int = DEFAULT_RISK_THRESHOLD) -> dict[str, Any]:
    architecture = gpu_architecture.strip().lower()
    attention = attention_backend.strip().lower()
    sm120 = any(marker in architecture for marker in ("sm120", "sm_120", "compute 12.0", "blackwell"))
    sage = "sage" in attention
    risky = sm120 and sage and tokens >= threshold
    return {
        "policy_version": SAFETY_POLICY_VERSION,
        "threshold": threshold,
        "estimated_tokens": int(tokens),
        "gpu_architecture": gpu_architecture or "unknown",
        "attention_backend": attention_backend or "unknown",
        "level": "high" if risky else "none",
        "requires_reference_optimization": risky,
        "warning": (
            "sm120 + SageAttention long-sequence risk detected; low-token reference derivation is required by the active safety policy"
            if risky else None
        ),
        "options": [
            "prepare_h3_reference", "reduce_target_resolution", "shorten_reference",
            "use_verified_attention_backend", "confirm_original_parameters",
        ] if risky else [],
    }


def public_safety_policy() -> dict[str, Any]:
    return {
        "version": SAFETY_POLICY_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
        "risk_threshold": DEFAULT_RISK_THRESHOLD,
        "match": {"gpu_architecture": "sm120", "attention_backend": "SageAttention"},
        "automatic_operation": "prepare_h3_reference",
        "preset": "h3-low-token",
        "parameters": ReferenceParameters(audio="remove").public(),
    }
