import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  H3_GENERATION_FPS,
  H3_MAX_CONTINUATION_FRAMES,
  defaultVideoContinuationRange,
  mergeVideoProject,
  normalizeVideoContinuationRange,
  serializeVideoProject,
  validateVideoProject,
  videoContinuationFrameCount,
} from "../app/video-project.ts";

const timelineSource = await readFile(new URL("../app/video-timeline.tsx", import.meta.url), "utf8");
const projectSource = await readFile(new URL("../app/video-project.ts", import.meta.url), "utf8");
const serverSource = await readFile(new URL("../server/video_projects.py", import.meta.url), "utf8");
const digest = "a".repeat(64);
const refProfile = {
  id: "minimax-h3-ref2va",
  version: "1",
  display_name: "H3 Ref2VA",
  output_type: "video",
  compiler: "h3_ref",
  manifest_sha256: digest,
  sampling_mode: "turbo4",
  available: true,
  defaults: { duration: 124 / 24, steps: 4, lora_strength: 0.75, denoise: 1 },
  limits: { duration: [124 / 24, 362 / 24], steps: [4, 20], lora_strength: [0, 1], denoise: [0.05, 1] },
};
const flProfile = {
  ...refProfile,
  id: "minimax-h3-fl2va",
  display_name: "H3 FL2VA",
  compiler: "h3_fl",
  manifest_sha256: "c".repeat(64),
};

function segment(id, overrides = {}) {
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
        duration: 124 / 24,
        steps: 4,
        lora_strength: 0.75,
        denoise: 1,
        seed: -1,
      },
      profile_id: refProfile.id,
      profile_version: refProfile.version,
      profile_digest: refProfile.manifest_sha256,
      references: [],
    },
    ...overrides,
  };
}

function projectWithRange(range, secondOverrides = {}) {
  return {
    title: "Continuation range contract",
    status: "draft",
    segments: [
      segment("A", {
        request: {
          ...segment("A").request,
          parameters: { ...segment("A").request.parameters, duration: 362 / 24 },
          profile_id: flProfile.id,
          profile_version: flProfile.version,
          profile_digest: flProfile.manifest_sha256,
        },
      }),
      segment("B", {
        continuation: "previous_video",
        ...(range === undefined ? {} : { continuation_range: range }),
        ...secondOverrides,
      }),
    ],
  };
}

function errorsFor(range, secondOverrides = {}, assets = []) {
  return validateVideoProject(projectWithRange(range, secondOverrides), [flProfile, refProfile], assets).join("\n");
}

function sourceSection(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  assert.notEqual(start, -1, `missing source contract marker: ${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `missing source contract marker: ${endMarker}`);
  return source.slice(start, end);
}

test("previous-video range is frame-exact and never exceeds the 15-second/360-frame input contract", () => {
  assert.equal(H3_GENERATION_FPS, 24);
  assert.equal(H3_MAX_CONTINUATION_FRAMES, 360);
  assert.equal(videoContinuationFrameCount(362 / 24), 362, "H3 may output 362 frames even though a reference may use only 360");
  assert.deepEqual(defaultVideoContinuationRange(362 / 24), { start_frame: 0, end_frame: 360, fps: 24 });
  assert.deepEqual(defaultVideoContinuationRange(124 / 24), { start_frame: 0, end_frame: 124, fps: 24 });
  assert.deepEqual(
    normalizeVideoContinuationRange({ start_frame: 24, end_frame: 240, fps: 24 }, 362 / 24),
    { start_frame: 24, end_frame: 240, fps: 24 },
  );
  assert.deepEqual(
    normalizeVideoContinuationRange({ start_frame: 1, end_frame: 999, fps: 30 }, 362 / 24),
    { start_frame: 1, end_frame: 361, fps: 24 },
    "normalization clamps the interval itself to 360 frames and canonicalizes H3 reference fps",
  );
});

test("frontend serialization persists a selected range without mutating legacy omissions", () => {
  const selected = projectWithRange({ start_frame: 48, end_frame: 288, fps: 24 });
  const serialized = serializeVideoProject(selected);
  assert.deepEqual(serialized.segments[1].continuation_range, { start_frame: 48, end_frame: 288, fps: 24 });

  const legacy = projectWithRange(undefined);
  assert.equal(legacy.segments[1].continuation_range, undefined);
  assert.equal(
    serializeVideoProject(legacy).segments[1].continuation_range,
    undefined,
    "passively saving an old project must not materialize a range the user never selected",
  );

  const stray = projectWithRange(undefined, {
    continuation: "none",
    continuation_range: { start_frame: 10, end_frame: 20, fps: 24 },
  });
  assert.equal(serializeVideoProject(stray).segments[1].continuation_range, undefined);
});

test("hydration accepts old receipts, preserves explicit ranges, and clears ranges when mode changes", () => {
  const local = projectWithRange(undefined);
  const legacyRemote = {
    ...local,
    segments: local.segments.map((item) => ({ ...item })),
  };
  assert.equal(mergeVideoProject(local, legacyRemote).segments[1].continuation_range, undefined);

  const rangedRemote = {
    ...legacyRemote,
    segments: [legacyRemote.segments[0], {
      ...legacyRemote.segments[1],
      continuation_range: { start_frame: 100, end_frame: 300, fps: 24 },
    }],
  };
  assert.deepEqual(
    mergeVideoProject(local, rangedRemote).segments[1].continuation_range,
    { start_frame: 100, end_frame: 300, fps: 24 },
  );

  const disabledRemote = {
    ...rangedRemote,
    segments: [rangedRemote.segments[0], { ...rangedRemote.segments[1], continuation: "none" }],
  };
  assert.equal(mergeVideoProject(local, disabledRemote).segments[1].continuation_range, undefined);
});

test("frontend validation rejects malformed or misplaced ranges and budgets the selected duration", () => {
  assert.equal(errorsFor({ start_frame: 0, end_frame: 360, fps: 24 }), "");
  for (const range of [
    { start_frame: -1, end_frame: 120, fps: 24 },
    { start_frame: 120, end_frame: 120, fps: 24 },
    { start_frame: 0.5, end_frame: 120, fps: 24 },
    { start_frame: 0, end_frame: 361, fps: 24 },
    { start_frame: 350, end_frame: 363, fps: 24 },
    { start_frame: 0, end_frame: 120, fps: 30 },
  ]) assert.match(errorsFor(range), /continuation range/i, JSON.stringify(range));
  assert.match(
    errorsFor({ start_frame: 0, end_frame: 120, fps: 24 }, { continuation: "none" }),
    /continuation range requires previous-video continuation/i,
  );

  const explicitVideo = { id: "b".repeat(32), kind: "video", media: { duration: 5.01, has_audio: false } };
  const request = {
    ...segment("B").request,
    references: [{ asset_id: explicitVideo.id, role: "motion" }],
  };
  assert.match(
    errorsFor({ start_frame: 120, end_frame: 360, fps: 24 }, { request }, [explicitVideo]),
    /videos may total at most 15 seconds/i,
    "a 10-second selected continuation plus a 5.01-second explicit video must fail before generation",
  );
});

test("long-video UI exposes independent in/out controls only for previous_video and invalidates edited output", () => {
  assert.match(timelineSource, /continuation === "previous_video"[\s\S]{0,8000}continuation_range/);
  assert.match(timelineSource, /(?:入点|start frame)/i, "the range editor needs a visible/selectable in point");
  assert.match(timelineSource, /(?:出点|end frame)/i, "the range editor needs a visible/selectable out point");
  assert.match(timelineSource, /(?:type="range"|type="number")[\s\S]{0,1200}(?:type="range"|type="number")/, "both boundaries must be selectable rather than informational text");
  assert.match(timelineSource, /normalizeVideoContinuationRange/);
  assert.match(timelineSource, /onChange=\{\(continuationRange\) => onChange\(\{ \.\.\.segment, continuation_range: continuationRange \}\)\}/, "the editor must persist both boundaries through the segment definition");
  assert.match(projectSource, /continuation_range\?: VideoContinuationRange/);
});

test("backend schema validates continuation_range while keeping omission backward compatible", () => {
  const validation = sourceSection(serverSource, "def _validate_continuation_range", "def _validate_request");
  assert.match(serverSource, /set\(raw\)[\s\S]{0,300}"continuation_range"/, "the segment schema must accept the new field");
  assert.match(serverSource, /raw\.get\("continuation_range"\)/);
  assert.match(validation, /if value is None:\s+return None/, "old definitions without a range remain valid server inputs");
  assert.match(validation, /continuation != "previous_video"/, "a range must be legal only for previous_video");
  assert.match(validation, /frame_count > 360/, "server owns the 360-frame maximum");
  assert.match(validation, /math\.isclose\(fps, 24\.0/, "server validates canonical 24fps frame coordinates");
  assert.match(serverSource, /if reference_duration > 15:[\s\S]{0,900}"-t", "15"/, "legacy definitions still use the historic whole-video path capped at 15 seconds");
});

test("backend materializes the selected frames exactly as a video-only reference and records evidence", () => {
  const execution = sourceSection(serverSource, 'continuation_range = segment.get("continuation_range")', "def _prepare_source_range_reference");
  assert.match(execution, /trim=start_frame=\{start_frame\}:end_frame=\{end_frame\},["\s]*setpts=PTS-STARTPTS/, "previous_video must crop the selected source frames, not seek approximately by seconds");
  assert.match(execution, /"-frames:v", str\((?:selected_)?frame_count\)/, "ffmpeg output is bounded to the exact half-open frame count");
  assert.match(execution, /"-map", "0:v:0"[\s\S]{0,220}"-an"/, "the ranged continuation must never leak the previous video's audio");
  assert.match(execution, /evidence\["continuation_range"\]\s*=\s*\{/);
  const interval = sourceSection(execution, "interval = {", 'evidence["continuation_range"]');
  for (const field of ["start_frame", "end_frame", "fps", "frame_count", "duration"]) {
    assert.match(interval, new RegExp(`"${field}"`), `missing durable continuation-range evidence: ${field}`);
  }
  assert.match(execution, /"requested": dict\(requested_interval\)[\s\S]{0,300}"effective": effective_interval/, "receipt must distinguish requested and actually applied frame intervals");
  assert.match(execution, /evidence\["audio_policy"\] = "video_only"/);
  assert.match(execution, /evidence\["reference_has_audio"\] = False/);
});

test("range definition changes invalidate paid output and downstream continuation receipts", () => {
  assert.match(serverSource, /old\.get\("continuation_range"\)[\s\S]{0,300}segment\.get\("continuation_range"\)/);
  assert.match(serverSource, /continuation_range[\s\S]{0,2000}changed_indices/);
  assert.match(serverSource, /changed_indices[\s\S]{0,1800}downstream[\s\S]{0,500}"stale"/);
  assert.match(serverSource, /changed_indices[\s\S]{0,2500}pop\("merged"/);
});
