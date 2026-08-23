#!/usr/bin/env python3
"""Validate MiniMax H3 Ref2VA prompt structures."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


SECTIONS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
SECTION_RE = re.compile(r"(?m)^([a-z][a-z_]*):\s*$")
LABEL_RE = re.compile(r"<(Subject|Picture|Video|Audio)\s+([1-9]\d*)>")
SUBJECT_DEFINITION_RE = re.compile(r"(?m)^<Subject\s+([1-9]\d*)>\s+is\b")
RETENTION_LINE_RE = re.compile(
    r"^<(Subject|Picture|Video|Audio)\s+([1-9]\d*)>"
    r"(?:\s*\([^\n]*\))?:\s*([a-z_]+)(\s+-\s+|\.\s+|\s+for\s+|\s+as\s+)\S"
)
SHOT_RE = re.compile(r"\[Shot\s+([1-9]\d*)](?:\s+At\s+(\d{2}):(\d{2})\.(\d{3}),)?")
SUMMARY_PREFIX_RE = re.compile(r"^\[([^\]]+)]")

VISIBLE_MARKERS = {
    "fully_preserved",
    "partially_preserved",
    "attribute_transfer",
    "weak_reference",
}
AUDIO_MARKERS = {"fully_copy", "partially_copy", "reference", "weak_reference"}
TASK_TYPES = {
    "keyframe completion",
    "reference generation",
    "video editing",
    "video continuation",
    "audio reuse",
    "audio reference",
}
IDENTITY_MIGRATION_TASK_TYPES = {
    "character replacement",
    "object replacement",
    "identity migration",
    "three-view identity reference",
}
IDENTITY_MIGRATION_MARKERS = {
    "identity_not_preserved",
    "fully_referenced",
    "identity_fully_preserved",
}


def split_sections(prompt: str) -> tuple[list[str], dict[str, str]]:
    matches = list(SECTION_RE.finditer(prompt))
    names = [match.group(1) for match in matches]
    bodies: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(prompt)
        bodies[match.group(1)] = prompt[match.end() : end].strip()
    return names, bodies


def validate(prompt: str, profile: str = "official") -> list[str]:
    errors: list[str] = []
    names, bodies = split_sections(prompt)

    if profile not in {"official", "identity-migration"}:
        return [f"unknown validation profile: {profile}"]

    expected_orders = [list(SECTIONS)]
    if profile == "identity-migration":
        expected_orders.append([
            "subject_definitions",
            "summary",
            "retention_analysis",
            "reference_interpretation",
            "detailed_description",
            "overall_soundscape",
            "non_diegetic_music",
        ])
    if names not in expected_orders:
        errors.append("top-level sections are invalid for the selected profile")
    for section in SECTIONS:
        if not bodies.get(section, "").strip():
            errors.append(f"section is missing or empty: {section}")

    if profile == "official" and "reference_interpretation:" in prompt:
        errors.append("reference_interpretation is not an official top-level section")
    if re.search(r"<Image\s+[1-9]\d*>", prompt):
        errors.append("use <Picture N>, not <Image N>")
    if re.search(r"\\<(?:Subject|Picture|Video|Audio)\s+[1-9]\d*>", prompt):
        errors.append("reference labels must not be backslash-escaped")
    if re.search(r"(?m)^\s*\\\s*$", prompt):
        errors.append("remove literal backslash separator lines")
    if re.search(r"@[\w.-]+", prompt):
        errors.append("unresolved @asset placeholder remains")

    summary = bodies.get("summary", "")
    prefix = SUMMARY_PREFIX_RE.match(summary)
    if not prefix:
        errors.append("summary must begin with an official square-bracketed task prefix")
    else:
        task_types = {value.strip() for value in prefix.group(1).split("+")}
        allowed_task_types = TASK_TYPES | (IDENTITY_MIGRATION_TASK_TYPES if profile == "identity-migration" else set())
        for task_type in sorted(task_types - allowed_task_types):
            errors.append(f"invalid summary task type: {task_type}")

    definitions = bodies.get("subject_definitions", "")
    defined_subjects = {int(value) for value in SUBJECT_DEFINITION_RE.findall(definitions)}
    used_subjects = {
        int(number) for kind, number in LABEL_RE.findall(prompt) if kind == "Subject"
    }
    for number in sorted(used_subjects - defined_subjects):
        errors.append(f"<Subject {number}> is used but not defined")

    for kind in ("Subject", "Picture", "Video", "Audio"):
        numbers = sorted(
            {
                int(number)
                for label_kind, number in LABEL_RE.findall(prompt)
                if label_kind == kind
            }
        )
        if numbers and numbers != list(range(1, max(numbers) + 1)):
            errors.append(f"{kind} labels are not contiguous from 1: {numbers}")

    retention = bodies.get("retention_analysis", "")
    for line in retention.splitlines():
        if not LABEL_RE.match(line):
            continue
        match = RETENTION_LINE_RE.match(line)
        if not match:
            errors.append(f"invalid retention line format: {line}")
            continue
        kind, _number, marker, separator = match.groups()
        allowed = AUDIO_MARKERS if kind == "Audio" else VISIBLE_MARKERS
        if profile == "identity-migration" and kind != "Audio":
            allowed = allowed | IDENTITY_MIGRATION_MARKERS
        if marker not in allowed:
            errors.append(f"invalid {kind} retention marker: {marker}")
        if profile == "official" and not separator.strip().startswith("-"):
            errors.append(f"invalid retention line format: {line}")

    shots = SHOT_RE.findall(bodies.get("detailed_description", ""))
    if shots:
        shot_numbers = [int(number) for number, *_ in shots]
        if shot_numbers != list(range(1, len(shot_numbers) + 1)):
            errors.append(f"shot numbers must be consecutive from 1: {shot_numbers}")
        if shots[0][1]:
            errors.append("[Shot 1] must not have a timestamp")
        for number, minutes, seconds, _milliseconds in shots[1:]:
            if not minutes:
                errors.append(f"[Shot {number}] must use At MM:SS.mmm,")
            elif int(seconds) >= 60:
                errors.append(f"[Shot {number}] timestamp seconds must be below 60")

    if prompt.count("<d>") != prompt.count("</d>"):
        errors.append("dialogue tags are unbalanced")

    if profile == "identity-migration":
        summary_lower = bodies.get("summary", "").lower()
        retention_lower = bodies.get("retention_analysis", "").lower()
        detail_lower = bodies.get("detailed_description", "").lower()
        if not ({1, 2} <= defined_subjects):
            errors.append("identity migration must define source <Subject 1> and target <Subject 2>")
        replacement_directions = (
            "replace <subject 1> with <subject 2>",
            "<subject 2> completely replaces <subject 1>",
        )
        if not any(value in prompt.lower() for value in replacement_directions):
            errors.append("identity migration must explicitly replace <Subject 1> with <Subject 2>")
        required_relations = (
            ("<subject 1>", "identity_not_preserved"),
            ("<picture 1>", "fully_referenced"),
            ("<subject 2>", "identity_fully_preserved"),
        )
        for label, marker in required_relations:
            if not re.search(rf"(?m)^{re.escape(label)}[^\n]*:\s*{marker}\b", retention_lower):
                errors.append(f"identity migration requires {label} to use {marker}")
        if not re.search(r"(?m)^<video 1>[^\n]*:\s*fully_preserved\b", retention_lower):
            errors.append("identity migration requires <Video 1> non-identity content to be fully_preserved")
        if "character replacement" not in summary_lower and "object replacement" not in summary_lower and "identity migration" not in summary_lower:
            errors.append("identity migration summary must explicitly name the replacement operation")
        if "at 0.00 seconds" not in detail_lower:
            errors.append("identity migration must anchor the target identity at 0.00 seconds")

        declares_three_view = "three-view" in prompt.lower() or all(
            term in prompt.lower() for term in ("front view", "side view", "back view")
        )
        interpretation = bodies.get("reference_interpretation", "").lower()
        if declares_three_view:
            if not interpretation:
                errors.append("three-view identity migration requires reference_interpretation")
            for term in ("front", "side", "back", "same"):
                if term not in interpretation:
                    errors.append(f"three-view reference_interpretation must explain: {term}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("official", "identity-migration"),
        default="official",
    )
    parser.add_argument("prompt_file", type=Path)
    args = parser.parse_args()
    errors = validate(args.prompt_file.read_text(encoding="utf-8"), profile=args.profile)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"OK: valid H3 Ref2VA prompt ({args.profile})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
