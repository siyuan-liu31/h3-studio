from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.e2e.scenarios import (
    H3_MAX_DURATION,
    SCENARIOS,
    build_graph,
    build_request,
    dry_run_plan,
    resolve_profile,
    validate_manifest,
)
from server.profiles import DEFAULT_REGISTRY
from server.workflows import parse_generation_request


def profile(compiler: str, output_type: str, sampling_mode: str = "default"):
    return {
        "id": f"{compiler}-{sampling_mode}",
        "version": "1.2",
        "manifest_sha256": "a" * 64,
        "compiler": compiler,
        "output_type": output_type,
        "sampling_mode": sampling_mode,
        "available": True,
        "defaults": {"steps": 4 if sampling_mode == "turbo4" else 20, "lora_strength": 0.75 if sampling_mode == "turbo4" else 0},
    }


class ScenarioTests(unittest.TestCase):
    def test_catalog_covers_the_required_matrix(self) -> None:
        self.assertEqual(
            set(SCENARIOS),
            {"t2i", "img2img", "t2v", "i2v", "fl2va", "ref-image", "ref-video"},
        )
        self.assertEqual([slot.role for slot in SCENARIOS["fl2va"].references], ["first_frame", "last_frame"])
        self.assertEqual(SCENARIOS["ref-video"].references[0].kind, "video")

    def test_manifest_paths_are_relative_to_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runs = validate_manifest({
                "version": 1,
                "runs": [{"scenario": "i2v", "prompt": "move", "assets": ["fixtures/frame.png"]}],
            }, base_dir=base)
            self.assertEqual(runs[0]["assets"], [str((base / "fixtures/frame.png").resolve())])
        with self.assertRaisesRegex(ValueError, "exactly 2 assets"):
            validate_manifest({
                "version": 1,
                "runs": [{"scenario": "fl2va", "prompt": "move", "assets": ["only.png"]}],
            }, base_dir=Path("."))

    def test_manifest_rejects_unknown_fields_and_assets_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory, "manifests")
            base.mkdir()
            outside = Path(directory, "secret.png")
            outside.write_bytes(b"secret")
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                validate_manifest({
                    "version": 1, "runs": [{"scenario": "t2v", "prompt": "ocean", "shell": "oops"}],
                }, base_dir=base)
            with self.assertRaisesRegex(ValueError, "escapes the allowed root"):
                validate_manifest({
                    "version": 1,
                    "runs": [{"scenario": "i2v", "prompt": "move", "assets": ["../secret.png"]}],
                }, base_dir=base)
            with self.assertRaisesRegex(ValueError, "version=1"):
                validate_manifest({"version": True, "runs": [{"scenario": "t2v", "prompt": "x"}]}, base_dir=base)
            with self.assertRaisesRegex(ValueError, "non-empty path"):
                validate_manifest({
                    "version": 1, "runs": [{"scenario": "i2v", "prompt": "x", "assets": [""]}],
                }, base_dir=base)
            with self.assertRaisesRegex(ValueError, "do not accept cfg"):
                validate_manifest({
                    "version": 1, "runs": [{"scenario": "t2v", "prompt": "x", "cfg": 7}],
                }, base_dir=base)

    def test_manifest_accepts_exact_362_frame_duration_and_rejects_larger_values(self) -> None:
        runs = validate_manifest({
            "version": 1,
            "runs": [{"scenario": "t2v", "prompt": "long grid clip", "duration": H3_MAX_DURATION}],
        }, base_dir=Path("."))
        self.assertEqual(runs[0]["duration"], 362 / 24)
        with self.assertRaisesRegex(ValueError, "between 5 and 15.0833"):
            validate_manifest({
                "version": 1,
                "runs": [{"scenario": "t2v", "prompt": "too long", "duration": H3_MAX_DURATION + 0.001}],
            }, base_dir=Path("."))
        schema = json.loads(Path(__file__).parents[1].joinpath("manifest.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["runs"]["items"]["properties"]["duration"]["maximum"], H3_MAX_DURATION)

    def test_profile_resolution_requires_available_versioned_digest(self) -> None:
        selected = resolve_profile([profile("h3_fl", "video", "turbo4")], SCENARIOS["t2v"], sampling_mode="turbo4")
        self.assertEqual(selected["id"], "h3_fl-turbo4")
        missing_digest = profile("h3_fl", "video", "base")
        missing_digest["manifest_sha256"] = "bad"
        with self.assertRaisesRegex(ValueError, "manifest_sha256"):
            resolve_profile([missing_digest], SCENARIOS["t2v"], sampling_mode="base")

    def test_graph_has_typed_roles_and_request_pins_profile_identity(self) -> None:
        assets = [
            {"id": "1" * 32, "kind": "image", "filename": "first.png", "sha256": "1" * 64},
            {"id": "2" * 32, "kind": "image", "filename": "last.png", "sha256": "2" * 64},
        ]
        selected = profile("h3_fl", "video", "base")
        run = {"scenario": "fl2va", "prompt": "transition", "assets": ["first.png", "last.png"], "sampling_mode": "base"}
        request = build_request(run, selected, assets)
        self.assertEqual(request["profile_id"], selected["id"])
        self.assertEqual(request["profile_version"], "1.2")
        self.assertEqual(request["profile_digest"], "a" * 64)
        self.assertEqual(request["parameters"]["steps"], 20)
        self.assertEqual(request["parameters"]["lora_strength"], 0)
        self.assertEqual(request["parameters"]["denoise"], 1.0)
        roles = [edge["role"] for edge in request["graph"]["edges"] if edge["source"].startswith("asset-")]
        self.assertEqual(roles, ["first_frame", "last_frame"])
        self.assertEqual(len(request["request_id"]), 32)
        changed = {**selected, "id": "another-base-profile", "manifest_sha256": "c" * 64}
        self.assertNotEqual(request["request_id"], build_request(run, changed, assets)["request_id"])

    def test_dry_run_can_be_fully_offline_or_exactly_resolved(self) -> None:
        run = {"scenario": "ref-video", "prompt": "transfer motion", "assets": ["motion.mp4"]}
        offline = dry_run_plan(run)
        self.assertTrue(offline["dry_run"])
        self.assertNotIn("request_preview", offline)
        self.assertEqual(offline["uploads"][0]["role"], "motion")
        exact = dry_run_plan(run, profile("h3_ref", "video", "turbo4"))
        self.assertEqual(exact["resolved_profile"]["manifest_sha256"], "a" * 64)
        self.assertEqual(exact["request_preview"]["profile_digest"], "a" * 64)

    def test_all_scenario_payloads_are_accepted_by_server_parser(self) -> None:
        expected_modes = {
            "t2i": "text-to-image", "img2img": "image-to-image", "t2v": "text",
            "i2v": "fl2va", "fl2va": "fl2va", "ref-image": "ref2va", "ref-video": "ref2va",
        }
        for name, scenario in SCENARIOS.items():
            with self.subTest(name=name):
                selected = next(
                    item for item in DEFAULT_REGISTRY.all()
                    if item.compiler == scenario.compiler
                    and (scenario.output_type != "video" or item.sampling_mode == "turbo4")
                )
                assets: list[dict] = []
                metadata: dict[str, dict] = {}
                for index, slot in enumerate(scenario.references, start=1):
                    asset_id = f"{index:032x}"
                    asset = {
                        "id": asset_id, "kind": slot.kind, "filename": f"asset-{index}",
                        "comfy_path": f"h3-studio/asset-{index}", "sha256": str(index) * 64,
                    }
                    if slot.kind == "video":
                        asset["media"] = {"duration": 5, "fps": 24, "reference_fps": 24, "has_audio": True}
                    assets.append(asset)
                    metadata[asset_id] = asset
                run = {"scenario": name, "prompt": "A reproducible test", "assets": ["fixture"] * len(assets)}
                request = build_request(run, {**selected.public(), "available": True}, assets)
                spec = parse_generation_request(request, metadata.__getitem__)
                self.assertEqual(spec.mode, expected_modes[name])
                self.assertEqual(spec.profile_id, selected.id)


if __name__ == "__main__":
    unittest.main()
