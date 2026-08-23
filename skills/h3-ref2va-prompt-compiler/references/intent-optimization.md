# Intent classification and faithful optimization

Infer operations from the user's verbs, constraints, asset descriptions, and declared asset roles. Media presence alone does not determine intent.

## Decision precedence

1. Explicit user instruction and prohibitions.
2. Current asset roles and wiring from the UI or manifest.
3. Concrete relationships in the current request.
4. Media content actually inspected for the current task.

Do not import asset properties from prior prompts or examples unless the user explicitly confirms they still apply. When signals conflict and the choice changes the requested result, ask one narrow question. When a neutral reference binding is sufficient, use it instead of guessing.

## Intent classes

Multiple classes may apply.

### Character or object replacement

Signals: replace, swap, change the person/character/object, keep the original movement.

- For complete identity replacement in a source video, route to `identity-migration.md`. Use an explicit character/object replacement task description; do not dilute it into generic `reference generation` merely because a Picture supplies the target identity.
- Define the source entity as one Subject and the replacement as a second Subject sourced from the appropriate Picture/Video. State the replacement direction explicitly; never leave the source entity as an unnamed “original person.”
- Preserve the source Video's requested action, pose, timing, position, camera, framing, environment, and lighting.
- Preserve the replacement Subject's identity and requested appearance attributes.
- Explicitly exclude source identity leakage, blending, morphing, duplicate subjects, temporal flicker, and unintended wardrobe drift.
- For a multi-view sheet, define the Picture itself, bind all views to one target Subject, assign front/side/back identity roles, and forbid copying the sheet layout.
- Apply multi-view rules only after the layout is observed or explicitly described in the current request.
- Do not apply `attribute_transfer` to the source person/object when the requested operation is complete identity replacement. Reserve it for requested motion, costume, style, or other bounded attributes.

### Motion, action, pose, or expression transfer

Signals: follow/copy the movement, dance like, use the pose/expression, keep action timing.

- The reusable person/object remains a Subject; the source motion can come from a Video or another Subject defined from it.
- Use `fully_preserved` only for exact requested transfer. Use `partially_preserved` when selected phases change and `weak_reference` for broad similarity.
- State timing, spatial path, contact, weight shift, and interaction only when visible or supplied; do not hallucinate unseen mechanics.

### Camera, edit rhythm, or composition reference

Signals: same camera move, match cuts, follow framing, use as storyboard/composition.

- A whole reference clip may remain `<Video N>` without turning every visible element into a Subject.
- Use `reference generation` when the clip guides camera/cuts but is not directly edited.
- Distinguish exact preservation from broad inspiration in `retention_analysis`.

### Scene or background replacement

Signals: replace background, move the subject into another environment, keep actor motion.

- Define the target environment as a Subject when it is reusable visible content.
- Separate foreground identity/motion preservation from environment transfer.
- Add compositing constraints that are implied by a believable replacement: consistent perspective, contact shadows, occlusion, scale, light direction, and color integration. These are technical coherence requirements, not new story content.

### Style, costume, or attribute transfer

Signals: use this style/outfit/color/material while keeping identity or action.

- Define the transferable visual unit as a Subject sourced from its asset.
- Use `attribute_transfer` when attributes move to another identifiable target.
- Enumerate only attributes explicitly requested or actually visible in an inspected asset.

### Video continuation

Signals: continue, extend, resume from the ending.

- Use `video continuation`, not `video editing` unless existing frames are also modified.
- Make the boundary state explicit: subject position, pose, motion vector, camera trajectory, lighting, environment, and active audio.
- Do not reset the scene or identities at the continuation boundary.

### Keyframe completion

Signals: start from this image, end at this image, interpolate between first and last frames.

- A concrete frame anchor is `<Picture N>` and task type includes `keyframe completion` if Ref2VA is still appropriate.
- If the request consists only of zero/one/two keyframes and text, select T2VA/I2VA/FL2VA/L2VA using `mode-routing.md`; do not invoke another Skill.

### Audio reuse or reference

Signals: keep original audio, copy soundtrack, use voice timbre, follow beat, use SFX texture.

- `audio reuse` means copying the signal in full or part; choose `fully_copy` or `partially_copy`.
- `audio reference` means generating new audio guided by timbre, beat, style, content, or texture; choose `reference` or `weak_reference`.
- Do not assume a video's audio track is enabled. Do not infer dialogue words from timbre-only reference.

## Faithful optimization boundary

Always improve:

- correct official structure and labels;
- unambiguous source-to-target bindings;
- exact preservation versus transfer versus weak reference;
- shot order, timing, continuity, and when a reference takes effect;
- identity consistency, temporal stability, and multi-view interpretation;
- technically necessary integration constraints for an explicitly requested edit.

Improve only when supported by inspected media or user text:

- appearance details, wardrobe, environment, lighting, camera path, shot boundaries, physical sound, and dialogue timing.

Never add without explicit permission:

- new plot events, characters, dialogue, lyrics, visible text, products, logos, injuries, nudity, music, or style changes;
- inferred private/sensitive traits;
- an exact copy relationship when the user requested only inspiration.

Default to faithful intent compilation. A user request such as "expand creatively" may authorize additional cinematic detail, but exact dialogue, asset identity, hard constraints, and prohibitions remain immutable.
