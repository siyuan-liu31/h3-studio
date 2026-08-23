import assert from "node:assert/strict";
import test from "node:test";

import {
  removeVideoProjectSegment,
  resolveVideoSequencePosition,
  videoSequenceDuration,
  videoSequenceTimeForSegment,
} from "../app/video-project.ts";

const digest = "a".repeat(64);
const fps = 24;

function segment(id, frames, overrides = {}) {
  return {
    id,
    continuation: "none",
    status: "draft",
    attempts: [],
    request: {
      prompt: `shot ${id}`,
      prompt_mode: "preserve_tags_only",
      parts: {},
      parameters: {
        aspect_ratio: "16:9",
        duration: frames / fps,
        steps: 4,
        lora_strength: 0.75,
        denoise: 1,
        seed: -1,
      },
      profile_id: "h3",
      profile_version: "1",
      profile_digest: digest,
      references: [],
    },
    ...overrides,
  };
}

function close(actual, expected, epsilon = 1e-9) {
  assert.ok(Math.abs(actual - expected) <= epsilon, `${actual} should be within ${epsilon} of ${expected}`);
}

test("the editor has one canonical 24fps coordinate system for ruler, shots and playhead", () => {
  const segments = [segment("short", 124), segment("medium", 243), segment("long", 362)];
  const totalFrames = 124 + 243 + 362;
  const total = videoSequenceDuration(segments);

  close(total, totalFrames / fps);
  close(videoSequenceTimeForSegment(segments, 0), 0);
  close(videoSequenceTimeForSegment(segments, 1), 124 / fps);
  close(videoSequenceTimeForSegment(segments, 2), (124 + 243) / fps);

  // A time-scaled timeline must use these fractions directly. Pixel minimums
  // may affect zoom/scroll width, but may not change the logical proportions.
  const fractions = segments.map((item) => item.request.parameters.duration / total);
  close(fractions[0], 124 / totalFrames);
  close(fractions[1], 243 / totalFrames);
  close(fractions[2], 362 / totalFrames);
  close(fractions.reduce((sum, value) => sum + value, 0), 1);

  const secondStart = resolveVideoSequencePosition(segments, 124 / fps);
  assert.equal(secondStart.segment?.id, "medium");
  assert.equal(secondStart.index, 1);
  close(secondStart.localTime, 0);

  const oneFrameIntoSecond = resolveVideoSequencePosition(segments, 125 / fps);
  assert.equal(oneFrameIntoSecond.segment?.id, "medium");
  close(oneFrameIntoSecond.localTime, 1 / fps);

  const lastFrame = resolveVideoSequencePosition(segments, (totalFrames - 1) / fps);
  assert.equal(lastFrame.segment?.id, "long");
  close(lastFrame.localTime, 361 / fps);
  close(resolveVideoSequencePosition(segments, Number.POSITIVE_INFINITY).time, total);
});

test("frame stepping remains exact across a segment boundary and clamps at sequence ends", () => {
  const segments = [segment("A", 124), segment("B", 141)];
  const total = videoSequenceDuration(segments);

  const beforeBoundary = resolveVideoSequencePosition(segments, 123 / fps);
  assert.equal(beforeBoundary.segment?.id, "A");
  close(beforeBoundary.localTime, 123 / fps);

  const boundary = resolveVideoSequencePosition(segments, beforeBoundary.time + 1 / fps);
  assert.equal(boundary.segment?.id, "B");
  close(boundary.localTime, 0);

  close(resolveVideoSequencePosition(segments, -1 / fps).time, 0);
  close(resolveVideoSequencePosition(segments, total + 1 / fps).time, total);
});

test("context deletion removes the exact retained id without corrupting selection order", () => {
  const project = {
    title: "editor",
    status: "draft",
    selected_segment_ids: ["A", "B", "C"],
    segments: [segment("A", 124), segment("B", 243), segment("C", 362)],
  };

  const next = removeVideoProjectSegment(project, "B");
  assert.deepEqual(next.segments.map((item) => item.id), ["A", "C"]);
  assert.deepEqual(next.selected_segment_ids, ["A", "C"]);
  assert.equal(next.segments[0].request.prompt, "shot A");
  assert.equal(next.segments[1].request.prompt, "shot C");

  // A stale menu id is a no-op, and the final remaining shot stays protected.
  assert.equal(removeVideoProjectSegment(next, "missing"), next);
  const one = { ...project, segments: [segment("only", 124)] };
  assert.equal(removeVideoProjectSegment(one, "only"), one);
});
