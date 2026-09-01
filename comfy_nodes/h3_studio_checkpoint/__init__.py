"""MiniMax H3-aware, best-effort latent checkpoint nodes for ComfyUI."""

from __future__ import annotations

import json
import os
import uuid

import folder_paths
import safetensors.torch
import torch

import comfy.nested_tensor
import comfy.utils


CHECKPOINT_VERSION = 1
NESTED_COUNT = 2


def _metadata(prompt, extra_pnginfo):
    value = {"h3_studio_checkpoint": str(CHECKPOINT_VERSION)}
    if prompt is not None:
        value["prompt"] = json.dumps(prompt)
    if extra_pnginfo is not None:
        for key, item in extra_pnginfo.items():
            value[key] = json.dumps(item)
    return value


class H3StudioSaveLatent:
    """Save the H3 video/audio NestedTensor without blocking the video output."""

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "samples": ("LATENT",),
                # This dependency forces SaveVideo to finish before checkpoint I/O.
                "video_done": ("VIDEO",),
                "filename_prefix": ("STRING", {"default": "h3-studio/checkpoints/ComfyUI"}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("samples",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "model/latent/minimax"

    def save(self, samples, video_done, filename_prefix, prompt=None, extra_pnginfo=None):
        del video_done
        temporary = None
        try:
            nested = samples.get("samples")
            if not getattr(nested, "is_nested", False):
                raise TypeError("H3 checkpoint input is not a NestedTensor")
            tensors = list(nested.unbind())
            if len(tensors) != NESTED_COUNT:
                raise ValueError("H3 checkpoint must contain video and audio tensors")

            full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
                filename_prefix, self.output_dir
            )
            file_name = f"{filename}_{counter:05}_.latent"
            destination = os.path.join(full_output_folder, file_name)
            temporary = f"{destination}.tmp-{uuid.uuid4().hex}"
            payload = {
                "h3_studio_checkpoint_version": torch.tensor([CHECKPOINT_VERSION], dtype=torch.int64),
                "h3_studio_nested_count": torch.tensor([len(tensors)], dtype=torch.int64),
                **{f"latent_tensor_{index}": tensor.contiguous() for index, tensor in enumerate(tensors)},
            }
            comfy.utils.save_torch_file(payload, temporary, metadata=_metadata(prompt, extra_pnginfo))
            os.replace(temporary, destination)
            return {
                "ui": {"latents": [{"filename": file_name, "subfolder": subfolder, "type": "output"}]},
                "result": (samples,),
            }
        except Exception:
            # A checkpoint is optional evidence. The already-saved video remains
            # the primary result and must never be converted into a failed job.
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            return {"ui": {"checkpoint_errors": [{"code": "checkpoint_write_failed"}]}, "result": (samples,)}


class H3StudioLoadLatent:
    """Restore the exact H3 video/audio pair written by H3StudioSaveLatent."""

    @classmethod
    def INPUT_TYPES(cls):
        input_dir = folder_paths.get_input_directory()
        discovered, _ = folder_paths.recursive_search(input_dir)
        files = [name for name in discovered if name.endswith(".latent")]
        return {"required": {"latent": (sorted(files),)}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "load"
    CATEGORY = "model/latent/minimax"

    def load(self, latent):
        path = folder_paths.get_annotated_filepath(latent)
        payload = safetensors.torch.load_file(path, device="cpu")
        version = int(payload.get("h3_studio_checkpoint_version", torch.tensor([-1]))[0].item())
        count = int(payload.get("h3_studio_nested_count", torch.tensor([-1]))[0].item())
        if version != CHECKPOINT_VERSION or count != NESTED_COUNT:
            raise ValueError("unsupported H3 Studio checkpoint format")
        tensors = [payload[f"latent_tensor_{index}"].float() for index in range(count)]
        if tensors[0].ndim != 5 or tensors[1].ndim != 4:
            raise ValueError("invalid H3 video/audio latent shapes")
        return ({"samples": comfy.nested_tensor.NestedTensor(tensors)},)


NODE_CLASS_MAPPINGS = {
    "H3StudioSaveLatent": H3StudioSaveLatent,
    "H3StudioLoadLatent": H3StudioLoadLatent,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "H3StudioSaveLatent": "H3 Studio Save Latent (Best Effort)",
    "H3StudioLoadLatent": "H3 Studio Load Latent",
}
