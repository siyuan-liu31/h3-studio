# Character Migration CLI Development Handoff

Updated: 2026-09-04

## Objective

Add an additive, agent-friendly CLI workflow for character migration across videos of arbitrary source duration. The command must accept local and remote assets, preserve the source performance and scene while replacing a selected subject with a supplied character, generate bounded H3 segments, trim and join them, restore or generate audio according to policy, and deliver one final movie.

The public command is:

```bash
h3ctl video migrate-character \
  --source ./source.mp4 \
  --character ./hero.png \
  --source-subject "the center performer" \
  --details-file details.txt \
  --profile minimax-h3-ref2va \
  --steps 4 \
  --segment-frames 243 \
  --overlap-frames 39 \
  --audio copy-source \
  --to final.mp4 \
  --timeout 0
```

Also support a versioned declarative input:

```bash
h3ctl video migrate-character --spec migration.json --to final.mp4
```

This is “unlimited duration” at the orchestration layer, not a claim that the model can generate an unlimited number of frames in one inference. Long sources are divided into resumable, bounded generation jobs.

## Non-negotiable compatibility requirements

- Do not change the behavior or schema validity of existing image generation, video generation, `h3ctl video compose`, project, media, upload, download, or Motion Context commands.
- Existing project JSON without character-migration metadata must remain valid and resumable.
- Build on the current durable `VideoProject`, per-segment jobs, source ranges, Motion Context, trim, concat, asset locators, profile pinning, and resource manager.
- Do not implement the whole production as one giant in-memory ComfyUI graph.
- Do not copy GPL-3.0 source from TimelineDirector into this repository. Reimplement only general concepts using the current architecture.
- Do not commit machine addresses, credentials, models, user media, generated media, run data, or machine-specific configuration.
- Do not push, tag, or publish without explicit authorization in the active user task.

## CLI contract

### Asset inputs

All media arguments should use the existing locator resolver and support:

- local filesystem paths;
- `asset:ID`;
- `job:ID#INDEX`;
- `media:ID`;
- `h3://CONTEXT/assets/ID`.

Required v1 inputs:

- `--source`: one source video;
- `--character`: one target character image;
- `--source-subject`: an unambiguous description of the person to replace.

Prompt and generation options:

- `--details` and `--details-file`: supplemental appearance, wardrobe, preservation, or performance constraints;
- `--prompt-file`: complete expert prompt, preserved without semantic rewriting except stable media-tag resolution;
- `--profile`: Base or Turbo-capable profile;
- `--steps`: explicit sampler step count, including Turbo four-step use;
- `--lora-strength`, `--seed`;
- `--segment-frames`, `--overlap-frames`;
- `--audio copy-source|reference-source|generate|mute`;
- standard durable execution flags: `--detach`, `--timeout`, `--poll-interval`, `--plan-only`, `--force`;
- `--to`: final local output path.

V1 may support one source person and one target character only. The versioned JSON spec should model targets as an array or mapping so multi-person migration can be added without breaking the schema. Proposed schema identifier: `h3.character-migration/v1`.

### Agent operations

Expose strict JSON Schema Draft 2020-12 operations with unknown fields rejected:

- `video.character_migration.plan`;
- `video.character_migration.produce`;
- `media.mux_audio`.

Expose a versioned `video.character_migration` capability that reports availability, recipe/schema version, supported profiles, overlap sizes, audio policies, and relevant limits.

## Planning algorithm

1. Resolve and upload the source video and character image through the common locator layer.
2. Probe and normalize the source once. Honor display rotation/orientation and normalize the planning timeline to 24 fps.
3. Let `G` be a legal H3 segment frame count (`17k + 5`, no more than 362) and let `O` be a supported Motion Context overlap. The current supported overlap values are 5, 22, 39, and 56 frames.
4. Use stride `G - O`. For a 24 fps source with `F` frames, plan:

   ```text
   N = 1 + ceil((F - G) / (G - O))
   ```

   with the one-segment case handled normally when `F <= G`.
5. Represent every window as a standard project segment using `source_range` on the same normalized source asset. The first segment has no continuation context. Every later segment uses Motion Context whose video and audio overlap agree with the project plan.
6. Pad only the final model input when needed to reach a legal H3 frame grid. Trim the final composition back to the exact source frame count and duration.
7. Use the current source window as `<Video 1>` and the target image as `<Picture 1>` for every segment.
8. Pin the selected profile version/digest in the project. Allow `steps` within that profile’s validated range.

The planner should be a pure, deterministic domain component where possible. It must return exact source ranges, generated frame counts, overlap ownership, final trim, prompt bindings, and estimated storage before paid work begins.

## Prompt policy

The default builder must be deterministic and must not invoke a hidden LLM rewrite. Follow the existing Ref2VA prompt contract:

- bind the source person as `<Subject 1>` from `<Video 1>`;
- bind the target character as `<Subject 2>` from `<Picture 1>`;
- explicitly direct `<Subject 2>` to fully replace `<Subject 1>`;
- mark `<Subject 1>: identity_not_preserved`;
- mark `<Picture 1>: fully_referenced`;
- mark `<Subject 2>: identity_fully_preserved`;
- preserve from `<Video 1>` motion, pose, timing, position, camera movement, framing, environment, lighting, composition, and interactions;
- exclude the original identity, identity leakage, blending, morphing, duplicates, temporal flicker, and unintended wardrobe drift.

`--details` supplements this contract and must not invent story events or dialogue. `--prompt-file` is an expert escape hatch for a complete prompt. Continuity should primarily come from overlap and Motion Context; the literal phrase “start from the exact ending state of the previous segment” is not mandatory. If future specs allow per-range prompt overrides, later ranges may opt into an explicit continuity instruction.

## Audio policy

- `copy-source` (default): preserve the original source audio and mux it onto the exact-duration final video.
- `reference-source`: feed source-window audio into H3 for timing/lip-sync and retain generated audio.
- `generate`: use model-generated audio without source audio reference.
- `mute`: produce video without audio.

The existing source-range video derivation strips audio, so `reference-source` needs an explicit, range-aligned audio derivation or equivalent server implementation.

Add a generic atomic `media.mux_audio` server operation and `h3ctl media mux-audio` command if no safe equivalent already exists. It must validate all media and paths, call FFmpeg without a shell, preserve exact duration, and define behavior when streams differ in length. `copy-source` and `reference-source` must fail before generation when the source contains no usable audio.

## Resource and durability design

- Admit only one GPU generation at a time through the existing FIFO resource manager.
- Keep the same profile model resident across adjacent segments. Call ComfyUI `/free` only on profile switch or idle according to existing policy.
- Normalize the source once. Create per-window derivatives just in time, and reclaim temporary derivatives after they are no longer required for resume.
- Store segment videos and Motion Context latents on disk; never accumulate decoded RGB frames for the full source in memory.
- Continue using atomic Motion Context writes, SHA-256 verification, and quota checks. Reuse `H3_STUDIO_MAX_MOTION_CONTEXT_STORAGE_BYTES` unless a demonstrably separate quota is needed.
- Estimate disk requirements and reject insufficient capacity before paid generation.
- Preserve a durable `project_id` if the CLI disconnects, the process receives Ctrl-C, or the server restarts. Resume through the existing project commands and rerun only failed or invalidated downstream segments.
- Protect source assets, generated segments, and Motion Context artifacts required by unfinished projects from garbage collection.

## Persistence

Prefer an additive optional project field such as `recipe` or `recipe_metadata`:

```json
{
  "type": "character_migration",
  "version": "h3.character-migration/v1",
  "source_asset_id": "...",
  "source_sha256": "...",
  "targets": [],
  "prompt_policy": {},
  "segmentation": {},
  "audio_policy": "copy-source"
}
```

Only server asset IDs and hashes may be persisted after locator resolution; never persist client-local paths or secrets. A changed source hash or material recipe change must explicitly invalidate affected planning/generation state rather than silently reusing stale segments.

## Validation and error behavior

Validate before mutation or paid generation whenever possible:

- source resolves to a readable video and has positive duration;
- target resolves to a supported image;
- `source_subject` is nonempty and unambiguous enough to pass basic validation;
- no duplicate reference bindings;
- segment frames are legal for H3;
- overlap is supported, positive where required, and smaller than the segment;
- reference counts and durations respect profile limits;
- output aspect/dimensions are supported after rotation-aware probing;
- selected Base/Turbo profile and requested steps are available;
- required audio exists for `copy-source` and `reference-source`;
- storage quota and free disk are sufficient;
- an existing output file is not overwritten without `--force`.

Errors must name the invalid field, received value, allowed values, and suggested correction when possible. JSON mode must remain stable and machine-readable.

## Likely implementation areas

- `server/character_migration.py`: pure recipe/spec validation, segmentation, prompt construction, and storage estimation.
- `server/app.py`: API route/runtime wiring and capability exposure if planning is server-side.
- `server/video_projects.py`: optional recipe metadata, exact final trim, audio post-processing, and invalidation behavior; keep normal segment contracts reusable.
- `server/media.py`: audio mux derivation and any aligned source-audio derivation.
- `server/config.py`: only if an existing resource/quota setting cannot be reused.
- `server/tests/test_character_migration.py` plus focused app, media, and video-project tests.
- `cli/internal/command/video.go`: `migrate-character` UX, help, validation, spec loading, and output delivery.
- CLI media command package: `mux-audio` if added.
- `cli/internal/operation/registry.go` and `executor.go`: strict agent operations and durable execution.
- Corresponding Go command, registry, executor, and service tests.
- Documentation after behavior stabilizes: `docs/cli.md`, `docs/llm-wiki.md`, `README.md`, `README.zh-CN.md`, `README.ja.md`, and `CHANGELOG.md` under Unreleased.

Architecture or contract changes require updating `docs/llm-wiki.md`. A release/version bump is out of scope until explicitly requested.

## Required test matrix

Positive unit/integration cases:

- short source produces one valid segment;
- a 60-second source produces the exact deterministic ranges and segment count;
- portrait video and rotation metadata produce correct display dimensions and output aspect;
- final padding is removed and output duration/frame count exactly matches the source;
- prompt tags bind the source subject and target character correctly;
- Base and Turbo profiles both plan correctly;
- Turbo accepts an explicit supported step count, including four steps;
- every audio policy follows its documented path;
- local and remote locators resolve through the common resolver;
- interrupted projects resume without regenerating valid completed segments.

Negative cases:

- wrong media kinds and unreadable media;
- empty source subject;
- illegal segment frame counts;
- overlap equal to or larger than a segment;
- unsupported overlap value;
- excessive or duplicate references;
- missing audio under `copy-source` or `reference-source`;
- unavailable profile or invalid steps;
- insufficient quota/free disk;
- source hash changed after planning;
- existing output without `--force`;
- unknown spec/operation fields.

Durability and regression cases:

- Ctrl-C does not cancel server work and the printed project ID remains resumable;
- restart/reconciliation restores in-flight state;
- failure reruns only the failed segment and its invalidated downstream dependents;
- garbage collection cannot remove artifacts needed by an unfinished project;
- existing `video compose`, Motion Context, trim, concat, asset, result, image, and ordinary video-generation tests remain green;
- CLI `--help` is complete and consistent with JSON schemas;
- operation schemas compile and reject unknown fields;
- `go test`, `go vet`, Go build, server tests, and the full repository test command pass;
- final diff check and secret scan pass.

If an authorized remote GPU machine is available, run an end-to-end smoke test with three overlapping five-second Turbo/four-step comic-drama segments and one character image. Verify final media with `ffprobe`: exact duration, display dimensions, stream layout, and playable output. Also smoke-test an existing generation/compose path. Do not add those inputs, outputs, logs, or machine configuration to Git.

## Reference implementation findings

The inspected upstream project was `Songssx/ComfyUI-MiniMaxH3-TimelineDirector` at commit `e4d6d5ea44eb00e2414d16dd94883be8e2661213`.

Useful general concepts:

- automatically split one long reference video/audio stream;
- reuse the same prompt and target images across windows;
- carry sampled A/V latent tail into the next segment head;
- use overlap masks aligned with actual sigmas;
- release audio overlap smoothly near the boundary;
- trim overlap with deterministic ownership;
- trim the final result to the exact requested duration.

Do not reproduce its runtime architecture: it expands all segments into one acyclic ComfyUI graph and joins decoded frames in memory, which limits resumability and resource scalability. This project should retain separate durable jobs, disk artifacts, bounded memory, and standard project reconciliation.

References:

- <https://github.com/Songssx/ComfyUI-MiniMaxH3-TimelineDirector>
- <https://github.com/Songssx/ComfyUI-MiniMaxH3-TimelineDirector/blob/e4d6d5ea44eb00e2414d16dd94883be8e2661213/minimax_h3_finite_segments.py#L118-L215>
- <https://github.com/Songssx/ComfyUI-MiniMaxH3-TimelineDirector/blob/e4d6d5ea44eb00e2414d16dd94883be8e2661213/minimax_h3_finite_segments.py#L635-L744>
- <https://github.com/Songssx/ComfyUI-MiniMaxH3-TimelineDirector/blob/e4d6d5ea44eb00e2414d16dd94883be8e2661213/experimental_latent_guide.py#L98-L215>
- <https://github.com/Songssx/ComfyUI-MiniMaxH3-TimelineDirector/blob/e4d6d5ea44eb00e2414d16dd94883be8e2661213/drift_control_av.py#L52-L201>
- <https://github.com/Songssx/ComfyUI-MiniMaxH3-TimelineDirector/blob/e4d6d5ea44eb00e2414d16dd94883be8e2661213/docs/LONG_REFERENCE_AUTO_SEGMENT_CN.md>

## Repository state at handoff

- Repository: H3 Studio.
- Branch: `main`, tracking `origin/main`.
- Base commit: `85500ad` (`feat(video): add Motion Context composition`), tagged `v0.5.0`.
- The working tree was clean before this handoff document was added.
- No implementation changes for character migration have been made yet.
- The handoff document itself is intentionally left as a working-tree change for the next task.

## Next task execution order

1. Re-read `AGENTS.md` and `docs/llm-wiki.md`; inspect Git status and the running version before editing.
2. Inspect current project, media, locator, profile, Motion Context, operation-schema, and CLI help contracts. Resolve any difference between this proposal and the current implementation in favor of compatibility.
3. Implement and test the pure planner/spec first.
4. Implement atomic media/audio operations and their positive/negative tests.
5. Wire durable project execution, invalidation, resource management, and capability reporting; test restart and resume.
6. Add the Go CLI and agent operations with full help and strict schemas; test both human and JSON paths.
7. Run targeted tests, fix failures, rerun them, then run the complete regression suite and review the diff/security boundary.
8. Update user documentation, LLM Wiki, and Unreleased changelog only after behavior is verified.
9. Run the remote three-segment E2E only if the active task still authorizes and provides an available machine. Never persist its credentials or artifacts.
10. Report implementation, tests, E2E evidence, remaining limitations, and uncommitted files. Do not push or release unless the user explicitly asks in that task.
