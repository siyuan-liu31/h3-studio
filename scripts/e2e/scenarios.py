"""Declarative E2E scenario catalog and request-plan construction."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


H3_FPS = 24
H3_MAX_FRAMES = 362
H3_MAX_DURATION = H3_MAX_FRAMES / H3_FPS


@dataclass(frozen=True, slots=True)
class ReferenceSlot:
    kind: str
    role: str


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    output_type: str
    compiler: str
    references: tuple[ReferenceSlot, ...] = ()
    description: str = ""


SCENARIOS: dict[str, Scenario] = {
    "t2i": Scenario("t2i", "image", "checkpoint_t2i", description="text-to-image"),
    "img2img": Scenario(
        "img2img", "image", "checkpoint_img2img",
        (ReferenceSlot("image", "init_image"),), "single-image image-to-image",
    ),
    "t2v": Scenario("t2v", "video", "h3_fl", description="text-to-video"),
    "i2v": Scenario(
        "i2v", "video", "h3_fl",
        (ReferenceSlot("image", "first_frame"),), "first-frame image-to-video",
    ),
    "fl2va": Scenario(
        "fl2va", "video", "h3_fl",
        (ReferenceSlot("image", "first_frame"), ReferenceSlot("image", "last_frame")),
        "first/last-frame video generation",
    ),
    "ref-image": Scenario(
        "ref-image", "video", "h3_ref",
        (ReferenceSlot("image", "identity"),), "identity image reference-to-video",
    ),
    "ref-video": Scenario(
        "ref-video", "video", "h3_ref",
        (ReferenceSlot("video", "motion"),), "motion video reference-to-video",
    ),
}


_MANIFEST_KEYS = {"version", "runs"}
_RUN_KEYS = {
    "scenario", "prompt", "assets", "profile_id", "sampling_mode", "aspect_ratio",
    "duration", "steps", "lora_strength", "cfg", "denoise", "seed", "include_audio",
}


def _number_field(raw: dict[str, Any], key: str, minimum: float, maximum: float, *, integer: bool = False) -> None:
    if key not in raw:
        return
    value = raw[key]
    valid = isinstance(value, int if integer else (int, float)) and not isinstance(value, bool)
    if not valid or not minimum <= value <= maximum:
        kind = "integer" if integer else "number"
        raise ValueError(f"{key} must be a {kind} between {minimum:g} and {maximum:g}")


def validate_manifest(value: Any, *, base_dir: Path, asset_root: Path | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1:
        raise ValueError("manifest must be an object with version=1")
    unknown_top = set(value) - _MANIFEST_KEYS
    if unknown_top:
        raise ValueError(f"manifest has unsupported fields: {', '.join(sorted(unknown_top))}")
    runs = value.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("manifest runs must be a non-empty array")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(runs):
        if not isinstance(raw, dict):
            raise ValueError(f"run {index} must be an object")
        unknown = set(raw) - _RUN_KEYS
        if unknown:
            raise ValueError(f"run {index} has unsupported fields: {', '.join(sorted(unknown))}")
        name = raw.get("scenario")
        if name not in SCENARIOS:
            raise ValueError(f"run {index} has unknown scenario {name!r}")
        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12_000:
            raise ValueError(f"run {index} needs a 1-12000 character prompt")
        assets = raw.get("assets", [])
        if not isinstance(assets, list) or any(not isinstance(path, str) or not path or "\x00" in path for path in assets):
            raise ValueError(f"run {index} assets must be non-empty path strings without NUL bytes")
        expected = SCENARIOS[name].references
        if len(assets) != len(expected):
            raise ValueError(f"scenario {name} needs exactly {len(expected)} assets")
        for key in ("profile_id",):
            if key in raw and (not isinstance(raw[key], str) or len(raw[key]) > 200):
                raise ValueError(f"run {index} {key} must be a string no longer than 200 characters")
        if "sampling_mode" in raw and raw["sampling_mode"] not in {"turbo4", "base"}:
            raise ValueError(f"run {index} sampling_mode must be turbo4 or base")
        if "aspect_ratio" in raw and raw["aspect_ratio"] not in {"16:9", "9:16"}:
            raise ValueError(f"run {index} aspect_ratio must be 16:9 or 9:16")
        if "include_audio" in raw and not isinstance(raw["include_audio"], bool):
            raise ValueError(f"run {index} include_audio must be boolean")
        if SCENARIOS[name].output_type == "video" and "cfg" in raw:
            raise ValueError(f"run {index} video scenarios do not accept cfg")
        _number_field(raw, "duration", 5, H3_MAX_DURATION)
        _number_field(raw, "steps", 1, 100, integer=True)
        _number_field(raw, "lora_strength", 0, 2)
        _number_field(raw, "cfg", 1, 30)
        _number_field(raw, "denoise", 0.05, 1)
        _number_field(raw, "seed", -1, 2**63 - 1, integer=True)
        root = (asset_root or base_dir).resolve()
        normalized_assets: list[str] = []
        for asset in assets:
            candidate = Path(asset)
            resolved = candidate.resolve() if candidate.is_absolute() else (base_dir / candidate).resolve()
            try:
                resolved.relative_to(root)
            except ValueError as error:
                raise ValueError(f"run {index} asset escapes the allowed root: {asset}") from error
            normalized_assets.append(str(resolved))
        normalized = dict(raw)
        normalized["prompt"] = prompt.strip()
        normalized["assets"] = normalized_assets
        result.append(normalized)
    return result


def load_manifest(path: Path, *, asset_root: Path | None = None) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read manifest {path}: {error}") from error
    return validate_manifest(value, base_dir=path.parent, asset_root=asset_root)


def resolve_profile(
    profiles: Iterable[dict[str, Any]],
    scenario: Scenario,
    *,
    sampling_mode: str = "turbo4",
    profile_id: str = "",
) -> dict[str, Any]:
    candidates = [
        profile for profile in profiles
        if isinstance(profile, dict)
        and profile.get("output_type") == scenario.output_type
        and profile.get("compiler") == scenario.compiler
        and (not profile_id or profile.get("id") == profile_id)
        and (scenario.output_type != "video" or profile.get("sampling_mode") == sampling_mode)
    ]
    available = [profile for profile in candidates if profile.get("available") is True]
    if not available:
        detail = "; ".join(
            f"{item.get('id')}: {item.get('missing_nodes', [])}{item.get('missing_models', [])}{item.get('missing_options', [])}"
            for item in candidates
        )
        raise ValueError(f"no available profile for {scenario.name}/{sampling_mode}: {detail or 'no match'}")
    profile = available[0]
    version, digest = profile.get("version"), profile.get("manifest_sha256")
    if not isinstance(version, str) or not version:
        raise ValueError("capability profile has no version")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("capability profile has no valid manifest_sha256")
    return profile


def build_graph(scenario: Scenario, prompt: str, assets: list[dict[str, Any]]) -> dict[str, Any]:
    if len(assets) != len(scenario.references):
        raise ValueError(f"scenario {scenario.name} needs {len(scenario.references)} uploaded assets")
    nodes: list[dict[str, Any]] = [
        {"id": "prompt", "type": "prompt", "data": {"prompt": prompt}},
        {"id": "generator", "type": "generator", "data": {"output_type": scenario.output_type}},
        {"id": "output", "type": "output", "data": {}},
    ]
    edges: list[dict[str, Any]] = [
        {"id": "prompt-generator", "source": "prompt", "target": "generator", "role": "prompt", "data": {"role": "prompt"}},
        {"id": "generator-output", "source": "generator", "target": "output", "role": "output", "data": {"role": "output"}},
    ]
    for index, (slot, asset) in enumerate(zip(scenario.references, assets, strict=True), start=1):
        if asset.get("kind") != slot.kind:
            raise ValueError(f"reference {index} must be {slot.kind}, got {asset.get('kind')!r}")
        asset_id = asset.get("id", asset.get("asset_id"))
        if not isinstance(asset_id, str) or len(asset_id) != 32 or any(char not in "0123456789abcdef" for char in asset_id):
            raise ValueError(f"reference {index} upload has no valid asset id")
        node_id = f"asset-{index}"
        include_audio = bool(asset.get("include_audio", False))
        nodes.append({
            "id": node_id,
            "type": "asset",
            "data": {
                "kind": slot.kind, "assetId": asset_id, "role": slot.role,
                "label": asset.get("filename", node_id), "include_audio": include_audio,
            },
        })
        edges.append({
            "id": f"{node_id}-generator", "source": node_id, "target": "generator",
            "role": slot.role, "data": {"role": slot.role, "include_audio": include_audio},
        })
    return {"nodes": nodes, "edges": edges}


def build_request(
    run: dict[str, Any],
    profile: dict[str, Any],
    uploaded_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    scenario = SCENARIOS[str(run["scenario"])]
    prompt = str(run["prompt"])
    defaults = profile.get("defaults", {}) if isinstance(profile.get("defaults"), dict) else {}
    parameters: dict[str, Any] = {
        "aspect_ratio": run.get("aspect_ratio", "16:9"),
        "seed": int(run.get("seed", -1)),
        "steps": int(run.get("steps", defaults.get("steps", 4 if scenario.output_type == "video" else 24))),
    }
    if scenario.output_type == "video":
        parameters.update({
            "duration": float(run.get("duration", 124 / 24)),
            "lora_strength": float(run.get("lora_strength", defaults.get("lora_strength", 0))),
            "denoise": float(run.get("denoise", 1.0)),
            "mode": "auto",
        })
    else:
        parameters.update({
            "cfg": float(run.get("cfg", defaults.get("cfg", 7))),
            "denoise": float(run.get("denoise", defaults.get("denoise", 0.65 if scenario.references else 1))),
        })
    identity_material = json.dumps(
        {
            "scenario": scenario.name,
            "prompt": prompt,
            "profile": {"id": profile["id"], "version": profile["version"], "digest": profile["manifest_sha256"]},
            "assets": [
                {
                    "id": asset.get("id", asset.get("asset_id")), "sha256": asset.get("sha256"),
                    "kind": asset.get("kind"), "include_audio": bool(asset.get("include_audio", False)),
                }
                for asset in uploaded_assets
            ],
            "parameters": parameters,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    request_id = hashlib.sha256(identity_material).hexdigest()[:32]
    return {
        "request_id": request_id,
        "output_type": scenario.output_type,
        "prompt": prompt,
        "profile_id": profile["id"],
        "profile_version": profile["version"],
        "profile_digest": profile["manifest_sha256"],
        "parameters": parameters,
        "graph": build_graph(scenario, prompt, uploaded_assets),
    }


def dry_run_plan(run: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    scenario = SCENARIOS[str(run["scenario"])]
    assets = [
        {"path": path, "kind": slot.kind, "role": slot.role}
        for path, slot in zip(run.get("assets", []), scenario.references, strict=True)
    ]
    result: dict[str, Any] = {
        "dry_run": True,
        "scenario": scenario.name,
        "description": scenario.description,
        "output_type": scenario.output_type,
        "profile_query": {
            "compiler": scenario.compiler,
            "sampling_mode": run.get("sampling_mode", "turbo4") if scenario.output_type == "video" else "default",
            "profile_id": run.get("profile_id", ""),
        },
        "uploads": assets,
        "would_call": ["GET /api/capabilities", *["POST /api/assets" for _ in assets], "POST /api/generate", "GET /api/status", "GET /api/result", "GET /api/download", "ffprobe"],
    }
    if profile:
        placeholders = [
            {"id": uuid.uuid5(uuid.NAMESPACE_URL, asset["path"]).hex, "kind": asset["kind"], "filename": Path(asset["path"]).name, "sha256": "0" * 64}
            for asset in assets
        ]
        result["resolved_profile"] = {
            "id": profile["id"], "version": profile["version"], "manifest_sha256": profile["manifest_sha256"],
        }
        result["request_preview"] = build_request(run, profile, placeholders)
    return result
