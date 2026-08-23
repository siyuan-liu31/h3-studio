# Internal official H3 schema selection

Select a schema only from the current request and actual asset roles. This decision is internal and is not part of the user-facing answer unless requested.

## Text and keyframe schemas

- T2VA: no reference media; output `integrated_multimodal_description`, `overall_soundscape`, and `non_diegetic_music`.
- I2VA: one Picture is explicitly the first frame; prepend the official 0.00-second Picture alignment, then output the three fields.
- FL2VA: two Pictures are explicitly the first and last frames; prepend their start/end alignment, then output the three fields.
- L2VA: one Picture is explicitly the last frame; prepend its final-time alignment, then output the three fields.

Do not treat an identity, costume, style, scene, or composition reference as a keyframe merely because it is an image.

## Full-reference schema

Use Ref2VA when the current request uses assets for identity, appearance, style, costume, scene, object, pose, motion, camera, edit rhythm, direct source-video editing, continuation, or audio. Directly replacing a person in a source Video with a person from a Picture is a full-reference request.

## Shared official constraints

- Structural prose is English; supplied dialogue, lyrics, and visible text retain original wording and language.
- `[Shot 1]` has no timestamp. Later shots use `[Shot N] At MM:SS.mmm, ...` with increasing times inside the known duration.
- Keep speaker IDs stable and put dialogue or lyrics inside `<d>[Language] ...</d>`.
- Preserve exact Picture/Video/Audio numbering from runtime wiring.
- If duration or shot boundaries are unknown, do not fabricate cut timestamps.

Official source: https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing
