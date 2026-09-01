# H3 Studio checkpoint nodes

These ComfyUI nodes serialize MiniMax H3's video/audio `NestedTensor`, which the
stock `SaveLatent` / `LoadLatent` nodes do not support.

Install this directory as `ComfyUI/custom_nodes/h3_studio_checkpoint` and restart
ComfyUI. `H3StudioSaveLatent` depends on the completed `SaveVideo` output and
treats checkpoint I/O as best effort, so checkpoint failure cannot invalidate an
already-rendered video.
