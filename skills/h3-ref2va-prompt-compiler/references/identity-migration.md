# Identity migration for source-video replacement

Use this profile only when a source person or object in a Video must be completely replaced by an identity from a wired Picture or Video while retaining the source performance and scene. Do not use it for costume-only, style-only, motion-only, or partial appearance transfer.

## Required semantic hierarchy

1. Define the source entity as `<Subject 1>` and the target identity as `<Subject 2>` unless current wiring or an existing authored prompt requires different contiguous numbers.
2. State the direction twice: `<Subject 2>` completely replaces `<Subject 1>`; the reverse must never be implied.
3. Describe `<Video 1>` as the source of non-identity content: motion, pose, timing, spatial role, camera, framing, environment, lighting, composition, and interactions requested by the user.
4. Use identity-specific retention relations:
   - `<Subject 1>: identity_not_preserved.` Remove its face, hair, body appearance, clothing, colors, and identity to the degree the user requested.
   - `<Picture 1>: fully_referenced` as the target identity/appearance source.
   - `<Subject 2>: identity_fully_preserved.` Keep the target identity consistent across frames and visible angles.
   - `<Video 1>: fully_preserved for ...` only the requested performance and scene properties; explicitly exclude source identity.
5. In `detailed_description`, anchor the target identity at `0.00 seconds`, then restate the replacement direction, preserved source performance/scene, and source-identity exclusion.
6. Repeat the identity boundary where it resolves ambiguity, but do not add unrelated cinematic detail.

Do not use `attribute_transfer` for the source entity in a complete replacement. Do not use `weak_reference` or `partially_preserved` for a source Video whose performance and scene must be followed closely. Those markers make sense for deliberately loose or partial transfer, not identity replacement.

The summary should make the operation explicit, for example `[video editing + character replacement]` or `[video editing + object replacement]`. This is an empirically validated compatibility profile: the identity-specific phrases are deliberate even though the ordinary Ref2VA profile uses only the public guide's generic task and retention vocabulary.

## Multi-view target references

Activate these additions only when the current request explicitly says the reference is multi-view or the image was inspected and verified as such:

- Define `<Picture 1>` as multiple views of one single target entity, not multiple entities.
- Add `three-view identity reference` to the summary only for an observed front/side/back sheet.
- Add `reference_interpretation` after `retention_analysis` and before `detailed_description`.
- Explain that the front, side, and back figures are the same entity; assign each view only the identity/appearance role it visibly supports.
- Forbid reproducing the sheet layout, dividers, labels, blank background, or duplicate copies.
- In `detailed_description`, map front-facing, side-facing, and back-facing appearances to the corresponding views.

Do not infer a multi-view layout, age, gender, clothing, colors, or body details from a filename or from a previous request.

## Validation

Validate this profile with:

```bash
python3 scripts/validate_h3_ref2va_prompt.py --profile identity-migration PROMPT_FILE
```

The validator enforces the source/target identity boundary and requires the multi-view interpretation block only when the prompt itself declares a three-view reference.
