"""Deterministic MiniMax H3 prompt compilation.

The rules in this module intentionally cover only transformations that can be
performed without inspecting media content or inventing creative details.  In
particular, source-file labels (Picture/Video/Audio) and reusable visible
subjects remain distinct, and an enabled video soundtrack receives its own
independently numbered Audio label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import ApiError


TAG_PATTERN = re.compile(r"<(Picture|Video|Audio)\s+(\d+)>", re.IGNORECASE)
REFERENCE_TOKEN_PATTERN = re.compile(r"@\{([^{}]+)\}")
SHOT_PATTERN = re.compile(r"\[Shot\s+(\d+)\]", re.IGNORECASE)
DIALOGUE_LINE_PATTERN = re.compile(
    r"^(?P<directions>(?:\[(?:offscreen|cross-cut|cutoff)\]\s*)*)"
    r"(?:(?P<speaker>\(S\d+(?:,S\d+)*\))\s*)?"
    r"(?P<body><d>\[[^\]\r\n]{2,32}\]\s*.+?</d>)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _ReferenceLayout:
    primary: dict[int, str]
    paired_audio: dict[int, str]
    aliases: dict[str, frozenset[str]]
    counts: dict[str, int]


def _field(reference: Any, name: str, default: Any = None) -> Any:
    if isinstance(reference, dict):
        return reference.get(name, default)
    return getattr(reference, name, default)


def _kind(reference: Any) -> str:
    return str(_field(reference, "kind", "")).lower()


def _role(reference: Any) -> str:
    return str(_field(reference, "role", "reference")).lower()


def _reference_layout(references: tuple[Any, ...]) -> _ReferenceLayout:
    """Mirror the native ComfyUI H3 reference presentation order.

    Pictures are presented first.  Videos follow, with each enabled soundtrack
    emitted as an Audio item immediately before its paired Video item.  All
    standalone audio follows the videos.  Ordinals are independent per type.
    """

    primary: dict[int, str] = {}
    paired_audio: dict[int, str] = {}
    picture_count = 0
    video_count = 0

    for index, reference in enumerate(references):
        kind = _kind(reference)
        if kind == "image":
            picture_count += 1
            primary[index] = f"<Picture {picture_count}>"
        elif kind == "video":
            video_count += 1
            primary[index] = f"<Video {video_count}>"
        elif kind != "audio":
            raise ApiError(400, "invalid_reference", f"unsupported prompt reference kind {kind!r}")

    audio_count = 0
    # The native node emits paired soundtracks before their videos and before
    # any standalone audio, irrespective of canvas connection order.
    for index, reference in enumerate(references):
        if _kind(reference) == "video" and bool(_field(reference, "include_audio", False)):
            audio_count += 1
            paired_audio[index] = f"<Audio {audio_count}>"
    for index, reference in enumerate(references):
        if _kind(reference) == "audio":
            audio_count += 1
            primary[index] = f"<Audio {audio_count}>"

    mutable_aliases: dict[str, set[str]] = {}
    for index, reference in enumerate(references):
        tag = primary[index]
        aliases = _field(reference, "aliases", ())
        if isinstance(aliases, str):
            aliases = (aliases,)
        if isinstance(aliases, Iterable):
            for alias in aliases:
                if isinstance(alias, str) and alias.strip():
                    mutable_aliases.setdefault(alias.strip(), set()).add(tag)

    return _ReferenceLayout(
        primary=primary,
        paired_audio=paired_audio,
        aliases={alias: frozenset(tags) for alias, tags in mutable_aliases.items()},
        counts={"picture": picture_count, "video": video_count, "audio": audio_count},
    )


def _replace_reference_mentions(text: str, layout: _ReferenceLayout) -> str:
    def resolve(alias: str) -> str:
        matches = layout.aliases.get(alias)
        if not matches:
            raise ApiError(400, "unknown_reference", f"@{{{alias}}} is not connected to this generator")
        if len(matches) != 1:
            raise ApiError(400, "ambiguous_reference", f"reference alias {alias!r} is ambiguous; use the asset id")
        return next(iter(matches))

    result = REFERENCE_TOKEN_PATTERN.sub(lambda match: resolve(match.group(1)), text)
    aliases = sorted(layout.aliases, key=len, reverse=True)
    if aliases:
        alternation = "|".join(re.escape(alias) for alias in aliases)
        bare = re.compile(rf"(?<![\w@])@({alternation})(?![\w.\-/])")
        result = bare.sub(lambda match: resolve(match.group(1)), result)
    dangling = re.search(r"(?<![\w@])@([^\s@<>]+)", result)
    if dangling:
        raise ApiError(400, "unknown_reference", f"@{dangling.group(1)} is not connected to this generator")

    for kind, number in TAG_PATTERN.findall(result):
        if int(number) < 1 or int(number) > layout.counts[kind.lower()]:
            raise ApiError(400, "invalid_reference_tag", f"<{kind} {number}> has no connected reference")
    return result


def replace_reference_tokens(text: str, references: tuple[Any, ...]) -> str:
    """Resolve only user-authored reference tokens using native H3 ordering.

    This narrow public entry point is used by long-video prompt preservation.
    It deliberately does not trim, normalize, label, or append prose.
    """

    return _replace_reference_mentions(text, _reference_layout(references))


def _parts(parts: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    if not isinstance(parts, dict):
        return values
    for key in ("subject", "action", "scene", "camera", "light", "style", "dialogue", "sound", "music"):
        value = parts.get(key)
        if isinstance(value, str) and value.strip():
            values[key] = value.strip()
    return values


def _normalized_dialogue(value: str) -> tuple[str, tuple[str, ...]]:
    """Validate official speaker/language syntax without rewriting dialogue text."""

    lines = [line.strip() for line in value.splitlines() if line.strip()]
    normalized: list[str] = []
    speakers: list[str] = []
    for index, line in enumerate(lines, start=1):
        match = DIALOGUE_LINE_PATTERN.fullmatch(line)
        if not match:
            raise ApiError(
                400,
                "invalid_dialogue",
                "each dialogue line must use '(S1) <d>[Language] exact words</d>'; optional [offscreen] or [cross-cut] goes first",
            )
        speaker_group = (match.group("speaker") or f"(S{index})").upper()
        speaker_ids = re.findall(r"S\d+", speaker_group)
        for speaker in speaker_ids:
            if speaker not in speakers:
                speakers.append(speaker)
        directions = match.group("directions").lower()
        body = match.group("body")
        simultaneous = len(speaker_ids) > 1
        if "[offscreen]" in directions:
            clause = f"{speaker_group} {'say' if simultaneous else 'says'} in an off-screen voiceover while every visible character keeps their lips closed: {body}"
        else:
            clause = f"{speaker_group} {'say simultaneously' if simultaneous else 'says'}: {body}"
        if "[cross-cut]" in directions:
            clause = f"{clause} <scenetrans> The same voice continues seamlessly through the scene transition."
        if "[cutoff]" in directions:
            clause = f"{clause} <cutoff>"
        normalized.append(clause)
    return " ".join(normalized), tuple(speakers)


def _timeline(text: str) -> tuple[str, int]:
    """Return a timeline with one opening Shot 1 and validated numbering."""

    matches = [int(value) for value in SHOT_PATTERN.findall(text)]
    if not matches:
        return f"[Shot 1] {text}".rstrip(), 1
    expected = list(range(1, max(matches) + 1))
    if matches != expected:
        raise ApiError(400, "invalid_shot_sequence", "shot labels must appear once in consecutive order starting at [Shot 1]")
    return text, matches[-1]


_NO_MUSIC = {"n/a", "none", "no music", "without music", "无配乐", "不要配乐"}
_COMPLETE_SILENCE = {"n/a", "complete silence", "silent throughout", "全程安静", "完全无声"}


def _sound_fields(
    values: dict[str, str],
    *,
    all_audio_tags: tuple[str, ...],
    ambient_audio_tags: tuple[str, ...],
    music_audio_tags: tuple[str, ...],
) -> tuple[str, str]:
    raw_sound = values.get("sound", "")
    raw_music = values.get("music", "")
    sound_key = raw_sound.strip().lower()
    music_key = raw_music.strip().lower()
    complete_silence = sound_key in _COMPLETE_SILENCE
    no_music = not raw_music or music_key in _NO_MUSIC

    if complete_silence and not no_music:
        raise ApiError(400, "contradictory_audio", "complete silence cannot be combined with non-diegetic music")
    if complete_silence and all_audio_tags:
        raise ApiError(400, "contradictory_audio", "complete silence cannot be combined with an enabled audio reference")
    if music_audio_tags and raw_music and no_music:
        raise ApiError(400, "contradictory_audio", "a music reference cannot be combined with an explicit request for no non-diegetic music")

    if complete_silence:
        soundscape = "N/A"
    elif raw_sound:
        soundscape = raw_sound
    elif ambient_audio_tags:
        joined = ", ".join(ambient_audio_tags)
        soundscape = f"Natural ambience and physical sounds remain temporally coherent with the enabled audio reference(s) {joined}."
    else:
        soundscape = "Natural diegetic ambience and physical action sounds match the visible scene."
    if music_audio_tags:
        joined = ", ".join(music_audio_tags)
        reference_clause = f"Use {joined} as music-style reference for instrumentation, tempo, rhythm, and dynamic development without copying the source signal."
        music = f"{raw_music} {reference_clause}".strip() if raw_music else reference_clause
    else:
        music = "N/A" if no_music else raw_music
    return soundscape, music


def _base_text(prompt: str, values: dict[str, str]) -> str:
    visual = [values.get(key, "") for key in ("subject", "action", "scene", "camera", "light", "style")]
    base = "; ".join(segment for segment in (prompt.strip(), *visual) if segment)
    dialogue = values.get("dialogue")
    if dialogue:
        # Do not silently translate or rewrite dialogue.  The structured editor
        # is responsible for supplying the official (Sx) and <d>[Language]
        # syntax when it knows the speaker and language.
        normalized, _ = _normalized_dialogue(dialogue)
        base = f"{base} Dialogue/voiceover (preserve the exact wording and original language): {normalized}".strip()
    return base


def _ref_sections(
    base: str,
    summary_base: str,
    references: tuple[Any, ...],
    layout: _ReferenceLayout,
    soundscape: str,
    music: str,
    dialogue_speakers: tuple[str, ...],
) -> str:
    definitions: list[str] = []
    retention: list[str] = []
    applications: list[str] = []
    summary_labels: list[str] = []
    subject_count = 0

    voice_bindings: set[tuple[str, int]] = set()
    subject_total = sum(
        _kind(reference) == "image" and _role(reference) in {"identity", "style"}
        or _kind(reference) == "video" and _role(reference) == "motion"
        for reference in references
    )
    for index, reference in enumerate(references):
        kind = _kind(reference)
        role = _role(reference)
        tag = layout.primary[index]
        if kind == "image" and role in {"identity", "style"}:
            subject_count += 1
            subject = f"<Subject {subject_count}>"
            noun = "reusable visible identity" if role == "identity" else "reusable visual style"
            definitions.append(f"{subject} is the {noun} shown in {tag}.")
            marker = "fully_preserved" if role == "identity" else "weak_reference"
            retention.append(f"{subject} (appears in [Shot 1]): {marker} - preserve the defined {role} relationship from {tag}.")
            applications.append(f"Apply {subject}'s defined {role} relationship where it is visible in the target video.")
            summary_labels.append(subject)
        elif kind == "image":
            relation = "composition anchor" if role == "composition" else "visual reference"
            definitions.append(f"{tag} is a {relation} for [Shot 1].")
            marker = "fully_preserved" if role == "composition" else "weak_reference"
            retention.append(f"{tag} ([Shot 1] {relation}): {marker} - retain only the defined {relation} relationship.")
            applications.append(f"Use {tag} as the {relation} without inventing an unrelated identity constraint.")
            summary_labels.append(tag)
        elif kind == "video" and role == "motion":
            subject_count += 1
            subject = f"<Subject {subject_count}>"
            definitions.append(f"{subject} is the reusable visible motion pattern demonstrated in {tag}, not the source identity.")
            retention.append(f"{subject} (appears in [Shot 1]): attribute_transfer - transfer the motion from {tag} to the target subject.")
            applications.append(f"Transfer {subject}'s motion while preserving the target subject's own identity.")
            summary_labels.append(subject)
        elif kind == "video":
            relationship = {"camera": "camera-movement structure", "pacing": "cut and pacing structure"}.get(role, "whole-video temporal structure")
            definitions.append(f"{tag} is the {relationship} reference for the target video.")
            retention.append(f"{tag} ({relationship}): weak_reference - follow the broad structure without copying unrelated source identity.")
            applications.append(f"Follow {tag}'s {relationship} where it applies to the target timeline.")
            summary_labels.append(tag)
        elif kind == "audio":
            relationship = {"voice": "voice timbre and delivery", "music": "music style", "rhythm": "beat and rhythm"}.get(role, "audio character")
            if role == "voice":
                raw_speaker = str(_field(reference, "voice_speaker", "")).upper()
                subject_number = int(_field(reference, "voice_subject", 0) or 0)
                if re.fullmatch(r"S[1-9]\d*", raw_speaker) is None or not 1 <= subject_number <= subject_total:
                    raise ApiError(400, "invalid_voice_binding", "each voice reference must explicitly target an existing Subject and speaker")
                if dialogue_speakers and raw_speaker not in dialogue_speakers:
                    raise ApiError(400, "invalid_voice_binding", f"voice target {raw_speaker} has no matching structured dialogue")
                binding = (raw_speaker, subject_number)
                if binding in voice_bindings:
                    raise ApiError(400, "ambiguous_voice_binding", "only one voice reference may target the same Subject/speaker pair")
                voice_bindings.add(binding)
                definitions.append(f"{tag} is the {relationship} reference for <Subject {subject_number}> ({raw_speaker}).")
            else:
                definitions.append(f"{tag} is the {relationship} reference for the target video.")
            retention.append(f"{tag}: reference - follow its {relationship} without claiming waveform copying.")
            applications.append(f"Use {tag} only for its defined {relationship} relationship.")
            summary_labels.append(tag)

        paired = layout.paired_audio.get(index)
        if paired:
            definitions.append(f"{paired} is the enabled synchronized audio track of {tag} and provides timing and sound-continuity reference.")
            retention.append(f"{paired}: reference - follow its synchronized timing and audible character without claiming waveform copying.")
            applications.append(f"Keep the audio relationship from {paired} temporally aligned with {tag}.")
            summary_labels.append(paired)

    timeline, _ = _timeline(base)
    if applications:
        timeline = f"{timeline} {' '.join(applications)}"
    task_types = ["reference generation"]
    if layout.counts["audio"]:
        task_types.append("audio reference")
    label_summary = f" References used: {', '.join(summary_labels)}." if summary_labels else ""
    definition_text = "\n".join(definitions) or "N/A"
    retention_text = "\n".join(retention) or "N/A"
    task_text = " + ".join(task_types)
    return (
        f"subject_definitions:\n{definition_text}\n\n"
        f"summary: [{task_text}] {summary_base}{label_summary}\n\n"
        f"retention_analysis:\n{retention_text}\n\n"
        f"detailed_description: {timeline}\n\n"
        f"overall_soundscape: {soundscape}\n\n"
        f"non_diegetic_music: {music}"
    )


def _keyframe_instruction(references: tuple[Any, ...], layout: _ReferenceLayout, final_shot: int, duration_actual: float) -> str:
    first = next((layout.primary[index] for index, ref in enumerate(references) if _role(ref) == "first_frame"), None)
    last = next((layout.primary[index] for index, ref in enumerate(references) if _role(ref) == "last_frame"), None)
    if last and duration_actual <= 0:
        raise ApiError(400, "invalid_duration", "effective duration is required for a last-frame alignment")
    if first and last:
        # MiniMax's published FL2VA instruction spells Picture labels without
        # angle brackets; retain that canonical wording exactly.
        return (
            "How the reference pictures align with the target video — "
            "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
            f"Picture 2 (from Shot {final_shot}) aligns with the {duration_actual:.2f}-second mark of the target video."
        )
    if first:
        return f"For the target video, at 0.00 seconds into the target video, {first} (from [Shot 1]) is fully referenced."
    if last:
        return (
            "How the reference pictures align with the target video — "
            f"{last} (from [Shot {final_shot}]) aligns with the {duration_actual:.2f}-second mark of the target video."
        )
    return ""


def compile_prompt(
    prompt: str,
    *,
    mode: str,
    references: tuple[Any, ...] = (),
    parts: Any = None,
    duration_actual: float = 0.0,
) -> str:
    """Compile stable UI fields to MiniMax H3's public prompt structures."""

    references = tuple(references)
    layout = _reference_layout(references)
    values = _parts(parts)
    dialogue_speakers: tuple[str, ...] = ()
    if values.get("dialogue"):
        _, dialogue_speakers = _normalized_dialogue(values["dialogue"])
    base = _replace_reference_mentions(_base_text(prompt, values), layout)
    summary_values = {key: value for key, value in values.items() if key != "dialogue"}
    summary_base = _replace_reference_mentions(_base_text(prompt, summary_values), layout)
    if not base:
        raise ApiError(400, "invalid_parameter", "prompt or prompt parts are required")

    if mode in {"text-to-image", "image-to-image"}:
        if mode == "image-to-image" and references and not any(tag.lower() in base.lower() for tag in layout.primary.values()):
            base = f"{base}\n\nUse {layout.primary[0]} as the source image."
        return _replace_reference_mentions(base, layout).strip()

    paired_audio_tags = tuple(layout.paired_audio.values())
    standalone_audio = tuple(
        layout.primary[index] for index, reference in enumerate(references) if _kind(reference) == "audio"
    )
    music_audio_tags = tuple(
        layout.primary[index]
        for index, reference in enumerate(references)
        if _kind(reference) == "audio" and _role(reference) == "music"
    )
    soundscape, music = _sound_fields(
        values,
        all_audio_tags=paired_audio_tags + standalone_audio,
        ambient_audio_tags=paired_audio_tags,
        music_audio_tags=music_audio_tags,
    )
    if mode == "ref2va":
        compiled = _ref_sections(base, summary_base, references, layout, soundscape, music, dialogue_speakers)
    else:
        timeline, final_shot = _timeline(base)
        first = next((layout.primary[index] for index, ref in enumerate(references) if _role(ref) == "first_frame"), None)
        last = next((layout.primary[index] for index, ref in enumerate(references) if _role(ref) == "last_frame"), None)
        if first and last:
            timeline = f"{timeline} The visual path starts from {first}, changes continuously through observable intermediate motion, and ends on {last}."
        elif first:
            timeline = (
                f"{timeline} The action starts from {first} and develops forward continuously. "
                f"At 0.00 seconds fully preserve from {first} the visible subject identity, clothing, colors, "
                "key objects, composition, and spatial relationships; introduce no unprompted changes."
            )
        elif last:
            timeline = f"{timeline} The final shot gradually converges to {last}."
        alignment = _keyframe_instruction(references, layout, final_shot, duration_actual)
        prefix = f"{alignment}\n\n" if alignment else ""
        compiled = (
            f"{prefix}integrated_multimodal_description: {timeline}\n\n"
            f"overall_soundscape: {soundscape}\n\n"
            f"non_diegetic_music: {music}"
        )
    return _replace_reference_mentions(compiled, layout).strip()
