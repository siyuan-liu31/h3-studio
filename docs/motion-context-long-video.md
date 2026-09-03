# Motion Context long-video composition

MiniMax H3 Video Studio can build a complete long video from ordered H3
segments with the external
[`ComfyUI-H3-Motion-Context`](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context)
node package. The primary CLI entry point is:

```bash
h3ctl video compose --spec trilogy.json --to final.mp4 --timeout 0
```

`video compose` is an orchestration command over the existing durable project
operations. It resolves missing Profile versions from `/api/capabilities`,
creates the project, runs segments in order, waits, starts the validated merge,
waits again, and atomically downloads the final MP4. The same pieces remain
available independently through `h3ctl project create|run|wait|merge|download`,
`h3ctl video trim`, and `h3ctl video concat`.

## Install the external node

The integration is pinned to upstream tag `v0.5.1`, commit
`429e952ae5c09b54f44cb6e3bef7331d998f0656`, and requires ComfyUI 0.34.0 or
newer. Install it next to, rather than inside, this repository:

```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context.git
git -C ComfyUI-H3-Motion-Context checkout 429e952ae5c09b54f44cb6e3bef7331d998f0656
```

Restart ComfyUI after installing the node. This project does not vendor or
relicense the GPL-3.0 upstream package. `/api/capabilities` reports
`video.motion_context.available=true` only when the required node and input
contracts are present.

The bundled `H3StudioSaveLatent` and `H3StudioLoadLatent` nodes must also be
installed under ComfyUI `custom_nodes`. They atomically persist the exact H3
video/audio nested latent used between adjacent segments.

## Project contract

The first segment uses `continuation: "none"`. A later generated segment can
request lossless latent continuation as follows:

```json
{
  "continuation": "motion_context",
  "motion_context": {
    "video_frames": 5,
    "audio_frames": 24
  },
  "request": {
    "prompt": "Continue from the delivered boundary state...",
    "profile_id": "minimax-h3-fl2va",
    "parameters": {
      "aspect_ratio": "16:9",
      "duration": 5,
      "steps": 4,
      "lora_strength": 0.75,
      "seed": 102
    },
    "references": []
  }
}
```

`video_frames` accepts `5`, `22`, `39`, or `56`; `22` is the default.
`audio_frames` accepts an integer from `0` through `240`; `24` is the default.
The trim node removes the reused head frames and matches the audio tail before
the segment enters the final concatenation.

Both `minimax-h3-fl2va` Turbo LoRA and `minimax-h3-fl2va-base` are supported,
as are their Ref2VA counterparts when explicit reference media is present.
Turbo4 describes the sampling preset; it does not force the request to exactly
four steps. `request.parameters.steps` is preserved and validated against the
selected Profile range. Motion Context continuation and Base resumable-sampling
checkpoints are separate mechanisms and cannot be enabled on the same render.

## Dimensions and media inputs

Uploaded images and videos keep the existing input contracts. Reference media
can be prepared with `h3ctl media prepare-reference`; direct video segments are
normalized by the existing merge pipeline to the project output canvas while
preserving display orientation and aspect ratio.

Motion Context itself operates on generated latents, so adjacent generated
segments in one latent chain must use the same exact output dimensions. The
server rejects a mismatch before paid generation. A direct imported-media
segment cannot be a Motion Context predecessor; use a new independent generated
chain after it.

## Durability, memory, and recovery

The CLI connection is only a monitor. Closing it or pressing Ctrl-C does not
cancel remote generation. Every accepted composition returns a durable
`project_id`; resume with:

```bash
h3ctl project get PROJECT_ID
h3ctl project run PROJECT_ID
h3ctl project wait PROJECT_ID --timeout 0
h3ctl video concat PROJECT_ID
h3ctl project wait PROJECT_ID --timeout 0
h3ctl project download PROJECT_ID --to final.mp4
```

The existing single-GPU FIFO coordinator remains the only submission path.
Motion Context does not lower resolution, precision, model quality, or requested
steps to handle memory pressure. ComfyUI model residency and idle release remain
under the existing GPU resource manager.

Context latents are durable disk state, not resident GPU or Python memory. They
are copied atomically, SHA-256 checked before reuse, pruned when a project chain
changes, and governed by `H3_STUDIO_MAX_MOTION_CONTEXT_STORAGE_BYTES` (default
200 GiB). The MP4 is the primary output: if saving a context latent fails after
the MP4 was written, that segment stays completed and reports an unavailable
context; the following dependent segment then fails explicitly instead of
silently falling back to pixel continuation.
