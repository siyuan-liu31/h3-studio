# Official H3 Ref2VA format

## Sections

Use exactly these six nonempty sections in order:

```text
subject_definitions:
summary:
retention_analysis:
detailed_description:
overall_soundscape:
non_diegetic_music:
```

Do not add `reference_interpretation` or any other top-level section. Put observed composite-image or multi-view interpretation inside `subject_definitions`, `retention_analysis`, and `detailed_description`.

Exception: complete source-video character/object identity migration uses the empirically validated profile in `identity-migration.md`. That profile may add `reference_interpretation` for an actually observed or explicitly declared multi-view sheet and may use identity-specific retention relations. Keep the ordinary Ref2VA profile strict for every other task.

## Labels

- `<Subject N>`: visible reusable content such as a person, object, environment, clothing, action, pose, or style.
- `<Picture N>`: a wired image. If it only supplies a Subject's identity/style, cite it inside that Subject definition rather than inventing a standalone role.
- `<Video N>`: a wired video used for direct editing, continuation, motion, camera, cuts, rhythm, or temporal structure.
- `<Audio N>`: a wired audio signal or explicitly enabled video audio track.

Keep each label's meaning stable throughout. Number each media category independently according to current wiring.

## Summary task types

Use only applicable official types:

- `keyframe completion`
- `reference generation`
- `video editing`
- `video continuation`
- `audio reuse`
- `audio reference`

For direct source-video editing, begin after the prefix with `The target video is an edited version of <Video N>.`

## Retention markers

Visible-content markers:

- `fully_preserved`
- `partially_preserved`
- `attribute_transfer`
- `weak_reference`

Audio markers:

- `fully_copy`
- `partially_copy`
- `reference`
- `weak_reference`

Use `attribute_transfer` when a source Subject's requested performance or attributes transfer to a different target Subject. State in ordinary English which attributes transfer and which identity characteristics are excluded.

## Detailed description

Describe the target in playback order. At each reference's first relevant appearance, state what it contributes. Use only shot divisions and timestamps supported by the current request or inspected source.

For replacement, explicitly state:

- which source Subject is replaced by which target Subject;
- that target identity/appearance comes from its actual reference asset;
- which source action, pose, timing, position, camera, framing, environment, lighting, and composition remain, to the degree requested;
- that source identity must not leak, blend, morph, duplicate, or reappear.

If an inspected Picture is a multi-view sheet, state that the views depict one subject and explain their observed roles, but do not assume this from the asset label alone.

## Audio

`overall_soundscape` summarizes ambience and physical sounds. `non_diegetic_music` covers audience-only score. Do not label or copy a source video's audio unless it is actually wired or explicitly requested.

Official sources:

- https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing
- https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md
