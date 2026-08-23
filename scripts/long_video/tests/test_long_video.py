from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.long_video.cli import main
from scripts.long_video.manifest import H3_MAX_DURATION, load_manifest, validate_manifest
from scripts.long_video.runner import LongVideoError, dry_run_plan, execute_manifest, make_client


PROJECT_ID = "a" * 32
SEGMENT_IDS = ["b" * 32, "c" * 32, "d" * 32]
JOB_IDS = ["1" * 32, "2" * 32, "3" * 32]
ATTEMPT_IDS = ["4" * 32, "5" * 32, "6" * 32]
MERGED_BYTES = b"mock merged video bytes"
MERGED_SHA = hashlib.sha256(MERGED_BYTES).hexdigest()
EXPECTED_DURATION = 15.5
WORKFLOW_SHA = "9" * 64
OUTPUT_SHA = "e" * 64


def manifest_value() -> dict:
    segments = []
    for index, continuation in enumerate(("none", "tail_frame", "previous_video")):
        parameters = {
            "aspect_ratio": "16:9", "duration": 124 / 24,
            "steps": 15, "lora_strength": 0.7, "seed": 100 + index, "mode": "auto",
        }
        if index == 2:
            parameters["denoise"] = 0.65
        segments.append({
            "continuation": continuation,
            "request": {
                "prompt": f"[Shot 1] segment {index}",
                "parameters": parameters,
                "profile_id": "profile", "profile_version": "1",
                "profile_digest": "f" * 64, "references": [],
            },
        })
    return validate_manifest({
        "version": 1,
        "project": {"title": "acceptance", "segments": segments},
        "rerun": {"segment_index": 1, "prompt": "[Shot 1] revised middle", "seed": 999},
        "acceptance": {
            "width": 1344, "height": 768, "expect_audio": True,
            "duration_tolerance": 0.25, "output_name": "joined.mp4",
        },
    })


def attempt(index: int, status: str = "completed", *, attempt_number: int = 0) -> dict:
    value = {
        "id": format(7 + index + attempt_number * 3, "x") * 32,
        "status": status,
        "job_id": JOB_IDS[index] if attempt_number == 0 else format(11 + index, "x") * 32,
        "started_at": 1.0 + attempt_number,
        "continuation": {"mode": ("none", "tail_frame", "previous_video")[index]},
        "workflow_evidence": {"sha256": WORKFLOW_SHA},
    }
    if status in {"completed", "failed", "canceled"}:
        value["finished_at"] = 2.0 + attempt_number
    if index > 0 and status == "completed":
        value["continuation"].update({
            "mode": ("tail_frame", "previous_video")[index - 1],
            "source_segment_id": SEGMENT_IDS[index - 1],
            "source_job_id": JOB_IDS[index - 1],
            "source_sha256": OUTPUT_SHA,
            "asset_id": format(13 + index + attempt_number, "032x"),
            "asset_sha256": OUTPUT_SHA,
            "asset_size": 50 + index,
            "asset_kind": "image" if index == 1 else "video",
        })
    return value


def receipt(
    requests: list[dict], statuses: list[str], *, project_status: str,
    current_index: int, rerun_middle: bool = False, stop_requested: bool = False,
    merged: dict | None = None, rerun_last: bool = False,
) -> dict:
    segments = []
    for index, status in enumerate(statuses):
        attempt_status = {"stopped": "canceled", "stale": "completed"}.get(status, status)
        attempts = [] if status == "pending" else [attempt(index, attempt_status)]
        if rerun_middle and index == 1:
            attempts.append(attempt(index, "completed" if status == "completed" else "running", attempt_number=1))
        if rerun_last and index == 2:
            last = attempt(index, "completed" if status == "completed" else "running", attempt_number=1)
            if rerun_middle:
                last["continuation"]["source_job_id"] = format(12, "x") * 32
            attempts.append(last)
        segments.append({
            "id": SEGMENT_IDS[index], "index": index,
            "continuation": ("none", "tail_frame", "previous_video")[index],
            "status": status, "request": deepcopy(requests[index]),
            "attempts": attempts,
            **({"job_id": attempts[-1]["job_id"]} if attempts else {}),
        })
    value = {
        "id": PROJECT_ID, "title": "acceptance", "status": project_status,
        "current_index": current_index, "stop_requested": stop_requested,
        "created_at": 1.0, "updated_at": 2.0, "segments": segments,
    }
    if merged is not None:
        value["merged"] = merged
    return value


class HappyClient:
    def __init__(self, manifest: dict) -> None:
        self.requests = [deepcopy(item["request"]) for item in manifest["project"]["segments"]]
        self.initial_requests = deepcopy(self.requests)
        self.calls: list[tuple[str, str, dict | None]] = []
        self.gets = 0
        self.rerun = False
        self.rerun_last = False
        self.merging = False

    def json_request(self, method, path, payload=None):
        self.calls.append((method, path, deepcopy(payload)))
        if method == "GET" and path == "/api/capabilities":
            profiles = []
            seen = set()
            for request in self.requests:
                key = (request["profile_id"], request["profile_version"], request["profile_digest"])
                if key in seen:
                    continue
                seen.add(key)
                profiles.append({
                    "id": key[0], "version": key[1], "manifest_sha256": key[2],
                    "output_type": "video", "sampling_mode": "turbo4", "available": True,
                    "limits": {"steps": [4, 50], "lora_strength": [0, 2]},
                })
            return {"profiles": profiles}
        if method == "POST" and path == "/api/video-projects":
            return receipt(self.requests, ["pending"] * 3, project_status="draft", current_index=0)
        if method == "POST" and path.endswith("/run") and "/segments/" not in path:
            return receipt(self.requests, ["running", "pending", "pending"], project_status="running", current_index=0)
        if method == "PUT":
            self.requests = [deepcopy(item["request"]) for item in payload["segments"]]
            return receipt(self.requests, ["completed", "pending", "completed"], project_status="draft", current_index=-1)
        if method == "POST" and "/segments/" in path:
            if path.endswith(f"/{SEGMENT_IDS[2]}/run"):
                self.rerun_last = True
                return receipt(self.requests, ["completed", "completed", "running"], project_status="running", current_index=2, rerun_middle=True, rerun_last=True)
            self.rerun = True
            return receipt(self.requests, ["completed", "running", "stale"], project_status="running", current_index=1, rerun_middle=True)
        if method == "POST" and path.endswith("/merge"):
            self.merging = True
            return receipt(
                self.requests, ["completed"] * 3, project_status="merging", current_index=-1,
                rerun_middle=True, rerun_last=self.rerun_last, merged={"status": "merging"},
            )
        if method == "POST" and path.endswith("/stop"):
            return receipt(self.requests, ["completed"] * 3, project_status="completed", current_index=-1, rerun_middle=self.rerun, rerun_last=self.rerun_last)
        if method == "GET" and path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            initial_ids = {value: index for index, value in enumerate(JOB_IDS)}
            rerun_ids = {format(11 + index, "x") * 32: index for index in range(3)}
            index = initial_ids.get(job_id, rerun_ids.get(job_id))
            if index is None:
                raise AssertionError(f"unexpected job id {job_id}")
            request = self.initial_requests[index] if job_id in initial_ids else self.requests[index]
            parameters = request["parameters"]
            frames = 124
            continuation = ("none", "tail_frame", "previous_video")[index]
            attempt_number = 0 if job_id in initial_ids else 1
            derived_id = format(13 + index + attempt_number, "032x")
            label = "<Picture 1>" if continuation == "tail_frame" else "<Video 1>"
            role_kind = {
                "first_frame": "image", "last_frame": "image", "identity": "image", "style": "image", "composition": "image", "reference": "image",
                "motion": "video", "camera": "video", "pacing": "video", "voice": "audio", "music": "audio", "rhythm": "audio",
            }
            explicit = [{
                "asset_id": item.get("asset_id", item.get("id")), "kind": role_kind[item.get("role", "reference")],
                "role": item.get("role", "reference"), "include_audio": bool(item.get("include_audio", False)),
                "voice_speaker": item.get("voice_speaker", ""), "voice_subject": item.get("voice_subject", 0),
            } for item in request.get("references", [])]
            implicit = [] if continuation == "none" else [{
                "asset_id": derived_id,
                "kind": "image" if continuation == "tail_frame" else "video",
                "role": "first_frame" if continuation == "tail_frame" else "motion",
                "include_audio": False, "voice_speaker": "", "voice_subject": 0,
            }]
            references = implicit + explicit if continuation == "tail_frame" else explicit + implicit
            counters = {"image": 0, "video": 0, "audio": 0}
            names = {"image": "Picture", "video": "Video", "audio": "Audio"}
            for item in references:
                counters[item["kind"]] += 1
                item["tag_label"] = f"<{names[item['kind']]} {counters[item['kind']]}>"
            stable = f"@{{{derived_id}}}"
            suffix = (
                f"At 0.00 seconds, continue seamlessly from {stable}; preserve identity, wardrobe, color, key objects, composition, lighting, scene geography, spatial relationships, and screen direction."
                if continuation == "tail_frame" else
                f"Continue the preceding action, motion phase, camera trajectory, scene geography, and screen direction from {stable}; preserve the target identity and do not copy the source identity or reuse its audio."
            )
            raw_prompt = request["prompt"] if continuation == "none" else f"{request['prompt']}; {suffix}"
            compiled_prompt = "compiled H3 model prompt " + " ".join(item["tag_label"] for item in references)
            return {
                "id": job_id, "job_id": job_id, "status": "completed",
                "raw_prompt": raw_prompt,
                "prompt": compiled_prompt, "prompt_parts": request.get("parts", {}),
                "references": references,
                "parameters": {
                    "profile_id": request["profile_id"], "profile_version": request["profile_version"],
                    "profile_digest": request["profile_digest"], "steps": parameters["steps"],
                    "denoise": parameters.get("denoise", 1.0), "seed": parameters["seed"],
                    "width": 1344, "height": 768, "frames": frames, "fps": 24,
                    "duration_requested": parameters["duration"], "duration_actual": round(frames / 24, 3),
                    "sampling_mode": "turbo4", "lora_strength": parameters.get("lora_strength", 0.75),
                },
                "workflow_sha256": WORKFLOW_SHA,
                "workflow_evidence": {
                    "sha256": WORKFLOW_SHA, "node_classes": ["BasicScheduler", "LoraLoaderModelOnly"],
                    "steps": parameters["steps"], "denoise": parameters.get("denoise", 1.0),
                    "sampler": "sa_solver", "scheduler": "simple",
                    "seed": parameters["seed"], "width": 1344, "height": 768, "frames": frames,
                    "prompt_sha256": hashlib.sha256(compiled_prompt.encode("utf-8")).hexdigest(),
                    "lora": "turbo.safetensors", "lora_strength": parameters.get("lora_strength", 0.75),
                },
                "outputs": [{"filename": f"segment-{index}.mp4", "sha256": OUTPUT_SHA, "size": 100 + index}],
            }
        if method == "GET":
            if self.merging:
                return receipt(
                    self.requests, ["completed"] * 3, project_status="completed", current_index=-1,
                    rerun_middle=True, rerun_last=self.rerun_last,
                    merged={
                        "status": "completed", "download_url": "/api/download?id=" + PROJECT_ID,
                        "preview_url": "/api/preview?id=" + PROJECT_ID,
                        "sha256": MERGED_SHA, "size": len(MERGED_BYTES),
                        "sources": [
                            {
                                "index": index, "segment_id": SEGMENT_IDS[index],
                                "job_id": (
                                    JOB_IDS[0] if index == 0 else
                                    format(11 + index, "x") * 32 if index == 1 or self.rerun_last else JOB_IDS[index]
                                ),
                                "sha256": OUTPUT_SHA, "size": 100 + index,
                            }
                            for index in range(3)
                        ],
                        "media": {
                            "duration": EXPECTED_DURATION, "has_video": True, "has_audio": True,
                            "video_codec": "h264", "audio_codec": "aac", "width": 1344, "height": 768,
                            "fps": 24.0, "frame_count": 372,
                        },
                    },
                )
            if self.rerun_last:
                return receipt(self.requests, ["completed"] * 3, project_status="completed", current_index=-1, rerun_middle=True, rerun_last=True)
            if self.rerun:
                return receipt(self.requests, ["completed", "completed", "stale"], project_status="partial", current_index=-1, rerun_middle=True)
            self.gets += 1
            states = [
                (["completed", "running", "pending"], 1),
                (["completed", "completed", "running"], 2),
                (["completed", "completed", "completed"], 3),
            ]
            statuses, current = states[min(self.gets - 1, 2)]
            return receipt(self.requests, statuses, project_status="completed" if current == 3 else "running", current_index=-1 if current == 3 else current)
        raise AssertionError((method, path, payload))

    def download(self, path, destination):
        self.calls.append(("DOWNLOAD", path, None))
        destination.write_bytes(MERGED_BYTES)
        return {"path": str(destination), "size": len(MERGED_BYTES), "sha256": MERGED_SHA}


class ManifestTests(unittest.TestCase):
    def test_manifest_and_duration_plan_support_the_362_frame_endpoint(self) -> None:
        value = manifest_value()
        for segment in value["project"]["segments"]:
            segment["request"]["parameters"]["duration"] = H3_MAX_DURATION
        normalized = validate_manifest(value)
        plan = dry_run_plan(normalized)
        self.assertEqual(plan["expected_duration"], 3 * 362 / 24)

        value["project"]["segments"][0]["request"]["parameters"]["duration"] = H3_MAX_DURATION + 0.001
        with self.assertRaisesRegex(ValueError, "between 5 and 15.0833"):
            validate_manifest(value)
        schema = json.loads(Path(__file__).parents[1].joinpath("manifest.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["$defs"]["request"]["properties"]["parameters"]["properties"]["duration"]["maximum"], H3_MAX_DURATION)

    def test_manifest_requires_three_ordered_modes_and_explicit_profiles(self) -> None:
        manifest = manifest_value()
        self.assertEqual([item["continuation"] for item in manifest["project"]["segments"]], ["none", "tail_frame", "previous_video"])
        self.assertEqual(manifest["rerun"]["seed"], 999)
        bad = deepcopy(manifest)
        bad["project"]["segments"][1]["continuation"] = "previous_video"
        with self.assertRaisesRegex(ValueError, "first three continuation"):
            validate_manifest(bad)

    def test_manifest_rejects_unknown_fields_traversal_bool_and_unchanged_rerun(self) -> None:
        for mutate, message in (
            (lambda value: value.update({"unknown": True}), "unsupported fields"),
            (lambda value: value["acceptance"].update({"output_name": "../escape.mp4"}), "safe .mp4 basename"),
            (lambda value: value["project"]["segments"][0]["request"]["parameters"].update({"steps": True}), "steps must be"),
            (lambda value: value.update({"rerun": {"segment_index": 1, "prompt": "[Shot 1] segment 1", "seed": 101}}), "must change"),
            (lambda value: value["acceptance"].update({"expect_audio": False}), "must be true"),
            (lambda value: value["project"]["segments"][2]["request"]["parameters"].update({"denoise": 0.01}), "denoise must be"),
        ):
            value = manifest_value()
            mutate(value)
            with self.assertRaisesRegex(ValueError, message):
                validate_manifest(value)

    def test_continuation_reserves_one_reference_slot(self) -> None:
        value = manifest_value()
        value["project"]["segments"][1]["request"]["references"] = [
            {"id": format(index, "x") * 32, "role": "reference"}
            for index in range(1, 7)
        ]
        with self.assertRaisesRegex(ValueError, "reserves one"):
            validate_manifest(value)

    def test_reference_alias_and_default_role_are_canonicalized_for_receipt_comparison(self) -> None:
        value = manifest_value()
        value["project"]["segments"][0]["request"]["references"] = [{"id": "a" * 32}]
        normalized = validate_manifest(value)
        self.assertEqual(
            normalized["project"]["segments"][0]["request"]["references"],
            [{"asset_id": "a" * 32, "role": "reference"}],
        )

    def test_dry_run_is_offline_and_declares_all_operations(self) -> None:
        plan = dry_run_plan(manifest_value())
        self.assertEqual(plan["segment_count"], 3)
        self.assertAlmostEqual(plan["expected_duration"], EXPECTED_DURATION)
        self.assertTrue(any("segments/{segment_id}/run" in item for item in plan["would_call"]))
        self.assertTrue(any("ffprobe" in item for item in plan["would_call"]))

    def test_cli_dry_run_does_not_construct_client_or_write_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest_value()), encoding="utf-8")
            output = io.StringIO()
            with patch("scripts.long_video.cli.make_client", side_effect=AssertionError("network client created")), redirect_stdout(output):
                self.assertEqual(main(["--manifest", str(path), "--output-dir", str(root / "outputs"), "--dry-run"]), 0)
            self.assertFalse((root / "outputs").exists())
            self.assertIn('"dry_run": true', output.getvalue())

    def test_base_url_must_be_a_bare_safe_origin(self) -> None:
        with self.assertRaisesRegex(ValueError, "without a path"):
            make_client("https://example.invalid/api", "test-key", 1)
        credential_url = "https://" + "user" + ":" + "password" + "@example.invalid"
        with self.assertRaisesRegex(ValueError, r"http\(s\)"):
            make_client(credential_url, "test-key", 1)


class IntegrationTests(unittest.TestCase):
    @staticmethod
    def probe(_path, **kwargs):
        assert kwargs["expected_width"] == 1344
        assert kwargs["expected_height"] == 768
        assert kwargs["expect_audio"] is True
        return {
            "width": 1344, "height": 768, "video_codec": "h264", "has_audio": True,
            "duration": EXPECTED_DURATION, "fps": 24.0, "frames": 372, "audio_codec": "aac",
        }

    def test_sequential_run_rerun_merge_download_hash_and_probe(self) -> None:
        manifest = manifest_value()
        client = HappyClient(manifest)
        with tempfile.TemporaryDirectory() as directory:
            result = execute_manifest(
                client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe,
            )
            evidence = json.loads(Path(result["evidence_path"]).read_text(encoding="utf-8"))
            self.assertEqual(Path(result["download"]["path"]).read_bytes(), MERGED_BYTES)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(evidence["download"]["sha256"], MERGED_SHA)
        self.assertEqual(len(evidence["initial_segment_jobs"]), 3)
        self.assertEqual(len(evidence["final_segment_jobs"]), 5)
        rerun_job = next(item for item in evidence["final_segment_jobs"] if item["segment_index"] == 1 and item["attempt_index"] == 1)
        self.assertEqual(rerun_job["request"]["prompt"], manifest["rerun"]["prompt"])
        self.assertEqual(rerun_job["parameters"]["seed"], manifest["rerun"]["seed"])
        final = next(item for item in evidence["final_segment_jobs"] if item["segment_index"] == 2)
        self.assertEqual(final["workflow_evidence"]["denoise"], 0.65)
        methods = [(method, path) for method, path, _payload in client.calls]
        self.assertLess(methods.index(("POST", f"/api/video-projects/{PROJECT_ID}/run")), methods.index(("PUT", f"/api/video-projects/{PROJECT_ID}")))
        self.assertIn(("POST", f"/api/video-projects/{PROJECT_ID}/segments/{SEGMENT_IDS[1]}/run"), methods)
        self.assertIn(("POST", f"/api/video-projects/{PROJECT_ID}/segments/{SEGMENT_IDS[2]}/run"), methods)
        self.assertEqual(client.requests[1]["prompt"], manifest["rerun"]["prompt"])
        self.assertEqual(client.requests[1]["parameters"]["seed"], 999)
        final_attempts = result["final_receipt"]["segments"]
        self.assertEqual(final_attempts[1]["attempts"][0]["continuation"]["mode"], "tail_frame")
        self.assertEqual(final_attempts[2]["attempts"][0]["continuation"]["mode"], "previous_video")

    def test_stop_after_segment_prevents_rerun_merge_and_download(self) -> None:
        manifest = manifest_value()

        class StopClient(HappyClient):
            def __init__(self, value):
                super().__init__(value)
                self.stopped = False

            def json_request(self, method, path, payload=None):
                if method == "GET" and path != "/api/capabilities":
                    return receipt(self.requests, ["completed", "pending", "pending"], project_status="stopped" if self.stopped else "running", current_index=-1 if self.stopped else 1, stop_requested=self.stopped)
                if method == "POST" and path.endswith("/stop"):
                    self.calls.append((method, path, payload))
                    self.stopped = True
                    return receipt(self.requests, ["completed", "pending", "pending"], project_status="stopped", current_index=-1, stop_requested=True)
                return super().json_request(method, path, payload)

        client = StopClient(manifest)
        with tempfile.TemporaryDirectory() as directory:
            result = execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, stop_after_index=0, probe=self.probe)
        self.assertEqual(result["status"], "stopped")
        paths = [path for _method, path, _payload in client.calls]
        self.assertIn(f"/api/video-projects/{PROJECT_ID}/stop", paths)
        self.assertFalse(any(path.endswith("/merge") for path in paths))
        self.assertFalse(any(method == "DOWNLOAD" for method, _path, _payload in client.calls))

    def test_stop_after_segment_rejects_project_that_already_completed(self) -> None:
        manifest = manifest_value()

        class TooLateClient(HappyClient):
            def json_request(self, method, path, payload=None):
                if method == "GET" and path != "/api/capabilities":
                    return receipt(self.requests, ["completed"] * 3, project_status="completed", current_index=-1)
                if method == "POST" and path.endswith("/stop"):
                    return receipt(self.requests, ["completed"] * 3, project_status="completed", current_index=-1, stop_requested=True)
                return super().json_request(method, path, payload)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LongVideoError, "stop boundary was missed"):
                execute_manifest(TooLateClient(manifest), manifest, output_dir=Path(directory), timeout=1, interval=0.001, stop_after_index=0, probe=self.probe)

    def test_stop_boundary_is_rechecked_until_terminal_receipt(self) -> None:
        manifest = manifest_value()

        class LateCompletionClient(HappyClient):
            def __init__(self, value):
                super().__init__(value)
                self.stop_sent = False

            def json_request(self, method, path, payload=None):
                if method == "POST" and path.endswith("/stop"):
                    self.stop_sent = True
                    return receipt(self.requests, ["completed", "pending", "pending"], project_status="stopping", current_index=1, stop_requested=True)
                if method == "GET" and path != "/api/capabilities":
                    if self.stop_sent:
                        return receipt(self.requests, ["completed", "completed", "stopped"], project_status="stopped", current_index=-1, stop_requested=True)
                    return receipt(self.requests, ["completed", "running", "pending"], project_status="running", current_index=1)
                return super().json_request(method, path, payload)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LongVideoError, "stop boundary was exceeded"):
                execute_manifest(LateCompletionClient(manifest), manifest, output_dir=Path(directory), timeout=1, interval=0.001, stop_after_index=0, probe=self.probe)

    def test_order_violation_is_rejected_stopped_and_records_partial_evidence(self) -> None:
        manifest = manifest_value()

        class BrokenClient(HappyClient):
            def json_request(self, method, path, payload=None):
                if method == "GET" and path != "/api/capabilities":
                    return receipt(self.requests, ["running", "running", "pending"], project_status="running", current_index=0)
                if method == "POST" and path.endswith("/stop"):
                    self.calls.append((method, path, payload))
                    return receipt(self.requests, ["stopped", "stopped", "pending"], project_status="stopped", current_index=0, stop_requested=True)
                return super().json_request(method, path, payload)

        client = BrokenClient(manifest)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LongVideoError, "started before") as raised:
                execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe)
            partials = list(Path(directory).glob("*.partial.json"))
            self.assertEqual(len(partials), 1)
            partial = json.loads(partials[0].read_text(encoding="utf-8"))
        self.assertTrue(raised.exception.partial_evidence["cancellation"]["ok"])
        self.assertEqual(partial["status"], "failed")

    def test_bad_continuation_receipt_is_rejected_and_stopped(self) -> None:
        manifest = manifest_value()
        client = HappyClient(manifest)
        original = client.json_request

        def broken(method, path, payload=None):
            value = original(method, path, payload)
            if method == "GET" and path == f"/api/video-projects/{PROJECT_ID}" and not client.rerun and not client.merging and value["status"] == "completed":
                value["segments"][1]["attempts"][0]["continuation"]["source_sha256"] = "d" * 64
            return value

        client.json_request = broken
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LongVideoError, "actual source output"):
                execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe)
        self.assertTrue(any(method == "POST" and path.endswith("/stop") for method, path, _payload in client.calls))

    def test_canonical_request_echo_mutation_is_rejected_before_merge(self) -> None:
        manifest = manifest_value()
        client = HappyClient(manifest)
        original = client.json_request

        def broken(method, path, payload=None):
            value = original(method, path, payload)
            if method == "GET" and path == f"/api/video-projects/{PROJECT_ID}":
                value["segments"][0]["request"]["prompt"] = "silently changed"
            return value

        client.json_request = broken
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LongVideoError, "changed its canonical request"):
                execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe)

    def test_earlier_attempt_workflow_and_output_hash_are_not_skipped(self) -> None:
        for mutation, message in (
            (lambda value: value["workflow_evidence"].update({"steps": 5}), "segment 0 attempt 0 workflow steps"),
            (lambda value: value["workflow_evidence"].update({"lora_strength": []}), "segment 0 attempt 0 Turbo4 LoRA strength"),
            (lambda value: value["outputs"][0].update({"sha256": "z" * 64}), "segment 0 attempt 0 output hash"),
            (lambda value: value.update({"raw_prompt": value["raw_prompt"] + "; malicious appended instruction"}), "segment 0 attempt 0 raw prompt"),
            (lambda value: value["parameters"].update({"frames": 5}), "segment 0 attempt 0 resolved frames"),
            (lambda value: value.update({"references": [{"asset_id": "a" * 32, "kind": "image", "role": "identity"}]}), "segment 0 attempt 0 resolved reference count"),
            (lambda value: value["workflow_evidence"].update({"sampler": "malicious_sampler"}), "segment 0 attempt 0 workflow sampler/scheduler"),
            (lambda value: value["workflow_evidence"].update({"seed": 999999}), "segment 0 attempt 0 workflow seed"),
            (lambda value: value["workflow_evidence"].update({"width": 768}), "segment 0 attempt 0 workflow width"),
            (lambda value: value.update({"prompt": "MALICIOUS MODEL PROMPT"}), "segment 0 attempt 0 compiled prompt"),
        ):
            manifest = manifest_value()
            client = HappyClient(manifest)
            original = client.json_request

            def broken(method, path, payload=None, *, mutate=mutation):
                value = original(method, path, payload)
                if method == "GET" and path == f"/api/jobs/{JOB_IDS[0]}":
                    mutate(value)
                return value

            client.json_request = broken
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(LongVideoError, message):
                    execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe)

    def test_capability_profile_sampling_contract_is_independent_of_job_echo(self) -> None:
        manifest = manifest_value()
        client = HappyClient(manifest)
        original = client.json_request

        def broken(method, path, payload=None):
            value = original(method, path, payload)
            if method == "GET" and path == "/api/capabilities":
                for profile in value["profiles"]:
                    profile["sampling_mode"] = "base"
            return value

        client.json_request = broken
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LongVideoError, "sampling mode differs from the pinned profile"):
                execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe)

    def test_explicit_reference_order_tag_and_default_voice_fields_are_bound(self) -> None:
        for mutation, message in (
            (lambda refs: refs.reverse(), "reference order"),
            (lambda refs: refs[0].update({"kind": "video", "tag_label": "<Video 1>"}), "reference kind"),
            (lambda refs: refs[0].update({"model_tag": "<Picture 9>"}), "reference tag/order"),
        ):
            manifest = manifest_value()
            manifest["project"]["segments"][0]["request"]["references"] = [
                {"asset_id": "a" * 32, "role": "identity"},
                {"asset_id": "b" * 32, "role": "style"},
            ]
            client = HappyClient(manifest)
            original = client.json_request

            def broken(method, path, payload=None, *, mutate=mutation):
                value = original(method, path, payload)
                if method == "GET" and path == f"/api/jobs/{JOB_IDS[0]}":
                    mutate(value["references"])
                return value

            client.json_request = broken
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(LongVideoError, message):
                    execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe)

    def test_requested_random_seed_accepts_and_records_resolved_nonnegative_seed(self) -> None:
        manifest = manifest_value()
        manifest["project"]["segments"][0]["request"]["parameters"]["seed"] = -1
        client = HappyClient(manifest)
        original = client.json_request

        def resolved(method, path, payload=None):
            value = original(method, path, payload)
            if method == "GET" and path == f"/api/jobs/{JOB_IDS[0]}":
                value["parameters"]["seed"] = 123456789
                value["workflow_evidence"]["seed"] = 123456789
            return value

        client.json_request = resolved
        with tempfile.TemporaryDirectory() as directory:
            result = execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe)
        first = next(item for item in result["initial_segment_jobs"] if item["segment_index"] == 0)
        self.assertEqual(first["parameters"]["seed"], 123456789)

    def test_attempt_workflow_receipt_must_match_fetched_job(self) -> None:
        manifest = manifest_value()
        client = HappyClient(manifest)
        original = client.json_request

        def broken(method, path, payload=None):
            value = original(method, path, payload)
            if method == "GET" and path == f"/api/video-projects/{PROJECT_ID}" and value["status"] == "completed":
                value["segments"][0]["attempts"][0]["workflow_evidence"]["sha256"] = "8" * 64
            return value

        client.json_request = broken
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LongVideoError, "workflow evidence differs from its job"):
                execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe)

    def test_non_default_final_denoise_must_match_basic_scheduler_evidence(self) -> None:
        manifest = manifest_value()
        client = HappyClient(manifest)
        original = client.json_request

        def broken(method, path, payload=None):
            value = original(method, path, payload)
            if method == "GET" and path.startswith("/api/jobs/"):
                value["workflow_evidence"]["denoise"] = 1.0
            return value

        client.json_request = broken
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LongVideoError, "BasicScheduler denoise"):
                execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe)
        self.assertTrue(any(method == "POST" and path.endswith("/stop") for method, path, _payload in client.calls))

    def test_implicit_previous_video_cannot_enable_audio_or_voice_binding(self) -> None:
        manifest = manifest_value()
        client = HappyClient(manifest)
        original = client.json_request

        def broken(method, path, payload=None):
            value = original(method, path, payload)
            if method == "GET" and path == f"/api/jobs/{JOB_IDS[2]}":
                value["references"][-1]["include_audio"] = True
            return value

        client.json_request = broken
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LongVideoError, "implicit continuation reference has unsupported audio"):
                execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe)

    def test_merged_sources_are_bound_to_current_ordered_segment_jobs(self) -> None:
        for mutation, message in (
            (lambda merged: merged.pop("sources"), "source evidence is missing"),
            (lambda merged: merged["sources"].reverse(), "merged source 0"),
            (lambda merged: merged["sources"][1].update({"job_id": JOB_IDS[1]}), "merged source 1 job_id"),
        ):
            manifest = manifest_value()
            client = HappyClient(manifest)
            original = client.json_request

            def broken(method, path, payload=None, *, mutate=mutation):
                value = original(method, path, payload)
                if method == "GET" and path == f"/api/video-projects/{PROJECT_ID}" and value.get("merged", {}).get("status") == "completed":
                    mutate(value["merged"])
                return value

            client.json_request = broken
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(LongVideoError, message):
                    execute_manifest(client, manifest, output_dir=Path(directory), timeout=1, interval=0.001, probe=self.probe)


if __name__ == "__main__":
    unittest.main()
