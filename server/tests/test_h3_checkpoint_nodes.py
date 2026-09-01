from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


NODE_SOURCE = Path(__file__).parents[2] / "comfy_nodes" / "h3_studio_checkpoint" / "__init__.py"


class FakeTensor:
    def __init__(self, value=0, *, ndim=1):
        self.value = value
        self.ndim = ndim

    def contiguous(self):
        return self

    def float(self):
        return self

    def __getitem__(self, _index):
        return self

    def item(self):
        return self.value


class FakeNestedTensor:
    is_nested = True

    def __init__(self, tensors):
        self.tensors = list(tensors)

    def unbind(self):
        return self.tensors


class H3CheckpointNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.output = self.root / "output"
        (self.input / "h3-studio-checkpoints").mkdir(parents=True)
        self.output.mkdir()
        (self.input / "h3-studio-checkpoints" / "resume.latent").write_bytes(b"latent")
        self.saved_payload = None

        folder_paths = types.ModuleType("folder_paths")
        folder_paths.get_output_directory = lambda: str(self.output)
        folder_paths.get_input_directory = lambda: str(self.input)
        folder_paths.get_save_image_path = lambda prefix, _root: (
            str(self.output), Path(prefix).name, 1, "", prefix,
        )
        folder_paths.recursive_search = lambda _root: (["h3-studio-checkpoints/resume.latent"], {})
        folder_paths.get_annotated_filepath = lambda value: str(self.input / value)

        fake_torch = types.ModuleType("torch")
        fake_torch.int64 = "int64"
        fake_torch.tensor = lambda values, dtype=None: FakeTensor(values[0])

        safetensors = types.ModuleType("safetensors")
        safetensors.__path__ = []
        safetensors_torch = types.ModuleType("safetensors.torch")
        safetensors_torch.load_file = lambda _path, device=None: self.saved_payload
        safetensors.torch = safetensors_torch

        comfy = types.ModuleType("comfy")
        comfy.__path__ = []
        comfy_nested = types.ModuleType("comfy.nested_tensor")
        comfy_nested.NestedTensor = FakeNestedTensor
        comfy_utils = types.ModuleType("comfy.utils")

        def save_torch_file(payload, path, metadata=None):
            self.saved_payload = payload
            Path(path).write_bytes(b"safe")

        comfy_utils.save_torch_file = save_torch_file
        comfy.nested_tensor = comfy_nested
        comfy.utils = comfy_utils
        modules = {
            "folder_paths": folder_paths,
            "torch": fake_torch,
            "safetensors": safetensors,
            "safetensors.torch": safetensors_torch,
            "comfy": comfy,
            "comfy.nested_tensor": comfy_nested,
            "comfy.utils": comfy_utils,
        }
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        spec = importlib.util.spec_from_file_location("h3_checkpoint_nodes_under_test", NODE_SOURCE)
        assert spec and spec.loader
        self.nodes = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.nodes)

    def tearDown(self) -> None:
        self.modules.stop()
        self.temp.cleanup()

    def test_nested_video_audio_checkpoint_round_trip_and_recursive_input_discovery(self) -> None:
        video = FakeTensor(ndim=5)
        audio = FakeTensor(ndim=4)
        samples = {"samples": FakeNestedTensor([video, audio])}
        saved = self.nodes.H3StudioSaveLatent().save(
            samples, video_done=object(), filename_prefix="h3-studio/checkpoints/job",
        )
        self.assertEqual(saved["ui"]["latents"][0]["filename"], "job_00001_.latent")
        self.assertTrue((self.output / "job_00001_.latent").is_file())
        self.assertEqual(self.saved_payload["h3_studio_nested_count"].item(), 2)
        self.assertIn("latent_tensor_0", self.saved_payload)
        self.assertEqual(
            self.nodes.H3StudioLoadLatent.INPUT_TYPES()["required"]["latent"][0],
            ["h3-studio-checkpoints/resume.latent"],
        )
        restored = self.nodes.H3StudioLoadLatent().load("h3-studio-checkpoints/resume.latent")[0]["samples"]
        self.assertTrue(restored.is_nested)
        self.assertEqual([tensor.ndim for tensor in restored.unbind()], [5, 4])

    def test_unsupported_checkpoint_input_is_best_effort_and_never_raises(self) -> None:
        result = self.nodes.H3StudioSaveLatent().save(
            {"samples": FakeTensor(ndim=5)}, video_done=object(),
            filename_prefix="h3-studio/checkpoints/job",
        )
        self.assertEqual(result["ui"]["checkpoint_errors"][0]["code"], "checkpoint_write_failed")
        self.assertFalse((self.output / "job_00001_.latent").exists())


if __name__ == "__main__":
    unittest.main()
