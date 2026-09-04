---
name: h3-character-dialogue-video
description: Generate a voiced MiniMax H3 video from user-supplied character reference images and dialogue, including speaker binding, H3 prompt compilation, h3ctl execution, and direct delivery for user review. Use for character-image-plus-dialogue videos and multilingual scene variants; do not use for dubbing an existing video or copying motion from a source clip.
---

# H3 Character Dialogue Video

Turn character reference images plus dialogue into one downloadable H3 video with model-generated voices, lip movement, diegetic sound, and visuals. The user performs final playback review; do not spend time inspecting the generated content unless they explicitly ask for diagnosis or verification.

## Required companion Skill

Before compiling an H3 prompt, read and follow the bundled [`h3-ref2va-prompt-compiler`](../h3-ref2va-prompt-compiler/SKILL.md). This Skill owns the production workflow; the compiler owns H3's prompt structure, labels, evidence boundary, and validator.

## Scope boundary

- Use this workflow when the user supplies one or more character images and lines they want those characters to speak in a newly generated video.
- A request to change the language and generate again is a new H3 generation variant, not an audio-only edit. Preserve the previous output unless the user explicitly asks to replace it.
- If the user wants to keep every frame of an existing video and only replace its soundtrack, route to a dubbing or TTS editing workflow.
- If a source video supplies dance, acting, camera, or edit rhythm, use [`h3-character-migration`](../h3-character-migration/SKILL.md) for one-person same-scene replacement, or [`h3-dance-replication`](../h3-dance-replication/SKILL.md) for multi-person mapping or a new background.
- Do not submit a render when the user asks only for a prompt, storyboard, or cost estimate.

## Resolve the production contract

Use the user's explicit choices first. Otherwise make low-risk defaults and state them briefly while continuing:

- Actual cast count and one stable image-to-character mapping.
- Exact line order, speaker ownership, language, and who stays silent.
- Scene, blocking, dramatic turn, and final reaction implied by the request.
- Aspect ratio: use 9:16 when the request is clearly a vertical social short; otherwise preserve a supplied ratio or ask only if the choice materially changes the result.
- Duration: choose a supported integer duration long enough for the dialogue. Never compress important lines merely to hit an arbitrary short duration.
- Audio: H3 generates dialogue, physical SFX, and quiet ambience from text. Do not imply that a supplied voice recording exists when none is wired.
- BGM and subtitles: off unless the user requests them.
- Model/Profile, steps, seed, and other parameters: honor exact user values. For a Turbo LoRA request with a custom step count, explicitly pass that step count and verify the live Profile supports it.

Ask one narrow question only when character-to-line ownership, required reference media, or another material production choice cannot be inferred safely.

## Inspect and bind the references

1. Inspect every supplied image before describing it. Never infer appearance or sheet layout from filenames.
2. Record the exact `--ref` order that will be submitted.
3. Number `<Picture N>` from that order and bind each reusable character as a stable `<Subject N>`.
4. For an observed multi-view character sheet, state that all panels show one person, use them as identity/wardrobe views, and exclude the sheet layout, background, panel boundaries, and duplicates from the video.
5. Keep character identity, hair, clothing, accessories, voice, screen position, and dialogue ownership mutually exclusive when several people appear.

Changing reference order, asset version, role, or enabled audio invalidates the old mapping. Rebuild the complete label map and prompt.

## Compile the dialogue scene

Use ordinary Ref2VA with the six official sections required by the compiler:

```text
subject_definitions:
summary:
retention_analysis:
detailed_description:
overall_soundscape:
non_diegetic_music:
```

Write structural prose in English. Put every spoken line exactly once in `<d>[Language] ...</d>` using the correct language tag. For each line, make these facts explicit:

- speaker identity and stable voice description;
- delivery and approximate placement in the scene;
- only that speaker moves their mouth;
- listeners keep lips closed and perform a visible listening task;
- behavior before, during, and after the line;
- silence, SFX, ambience, and BGM policy.

Lock exact cast count, character locations, scene axis, primary action, reaction chain, and final frame. Forbid extra dialogue, duplicated people, identity swaps, sheet/contact-grid leakage, subtitles, visible text, logos, watermarks, and music unless requested.

Do not silently rewrite user dialogue. When the user asks for translation, produce a natural duration-safe translation that preserves the intent, then use only that language in the variant prompt.

## Preflight before generation

Run read-only checks against the current deployment:

```bash
h3ctl version
h3ctl doctor
h3ctl profile show PROFILE_ID
```

Confirm the selected Profile supports the reference count, Ref2VA mode, duration, aspect ratio, steps, and generated audio. Run the compiler validator and repair every prompt error.

Freeze a compact execution receipt before submission:

- prompt file and SHA-256;
- ordered reference locators and Picture/Subject mapping;
- Profile ID/version and sampling mode;
- duration, aspect ratio, steps, seed, and output path.

## Generate with h3ctl

Use a unique output name so earlier language versions and takes remain recoverable:

```bash
h3ctl --json generate video \
  --mode r2v \
  --profile PROFILE_ID \
  --prompt-file PROMPT_FILE \
  --ref CHARACTER_1_IMAGE \
  --ref CHARACTER_2_IMAGE \
  --duration DURATION \
  --aspect-ratio ASPECT_RATIO \
  --steps STEPS \
  --seed SEED \
  --wait \
  --download OUTPUT.mp4
```

Add or remove repeated `--ref` arguments to match the frozen mapping. Omit `--seed` when the user wants a fresh random take. For a language-only regeneration, keep references, Profile, steps, scene, and optionally the prior seed fixed so language is the main changed variable.

Do not use `--force` unless the user explicitly wants to overwrite an existing output. When a long-running command yields a session, keep waiting on the same job and update the user periodically. Do not submit duplicate jobs merely because generation is slow.

## Stop at successful delivery

Once `h3ctl` reports completion and downloads the file:

1. Confirm only that the command succeeded and the declared output path exists.
2. Do not extract frames, listen to audio, transcribe dialogue, compare identities, or assign an artistic verdict unless the user asks.
3. Do not regenerate because of suspected defects the user has not reported.
4. Deliver the local video immediately with the job ID, Profile, steps, duration, seed when available, and output path.
5. In Codex Desktop, embed the absolute `.mp4` path and also provide a clickable file link.

Keep the prompt hash, ordered references, job/request IDs, generation parameters, output path, and any reported issue needed to reproduce or repair the take. Do not label it accepted or final before the user reviews it.
