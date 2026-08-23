import assert from "node:assert/strict";
import test from "node:test";

import {
  allRunSelection,
  buildSegmentDraftsFromSourceCuts,
  buildStoryboardDraft,
  equalizeSegmentDrafts,
  invertRunSelection,
  normalizeRunSelection,
  parseSceneCutSuggestions,
  selectedRunDependencyError,
  segmentIndexAtTime,
  splitSegmentDraftAtTime,
} from "../app/video-director-model.ts";
import { H3_DURATION_OPTIONS, isH3DurationOption } from "../app/video-project.ts";

const profile = {
  id: "h3-ref", version: "1", display_name: "H3 Ref", output_type: "video",
  compiler: "h3_ref", manifest_sha256: "a".repeat(64), sampling_mode: "turbo4", available: true,
  defaults: { duration: 124 / 24, steps: 4, lora_strength: 0.75, denoise: 1 },
  limits: { duration: [124 / 24, 362 / 24], steps: [4, 20], lora_strength: [0, 1], denoise: [0.05, 1] },
};

function segment(id, duration = 362 / 24) {
  return {
    id, continuation: "none", status: "completed", attempts: [{ status: "completed" }],
    preview_url: `/preview/${id}`, download_url: `/download/${id}`,
    request: {
      prompt: `shot ${id}`, prompt_mode: "preserve_tags_only", parts: {},
      parameters: { aspect_ratio: "16:9", duration, steps: 4, lora_strength: 0.75, denoise: 1, seed: -1 },
      profile_id: profile.id, profile_version: profile.version, profile_digest: profile.manifest_sha256,
      references: [{ asset_id: "b".repeat(32), role: "reference" }],
    },
  };
}

test("scene analysis suggestions accept common response shapes and remain sorted, bounded and unique", () => {
  assert.deepEqual(parseSceneCutSuggestions({ cuts: [8.2, 2.5, 8.2, 0, 20] }, 12).map((item) => item.seconds), [2.5, 8.2]);
  assert.deepEqual(parseSceneCutSuggestions({ scenes: [
    { start_sec: 0 }, { start_sec: 3.25, confidence: 0.91, label: "scene two" }, { start_sec: 9.5 },
  ] }, 9.5), [{ seconds: 3.25, confidence: 0.91, label: "scene two" }]);
  assert.deepEqual(parseSceneCutSuggestions({ unrelated: [] }, 10), []);
});

test("run selection uses stable segment ids for all, invert and remote normalization", () => {
  const segments = [segment("one"), segment("two"), segment("three")];
  assert.deepEqual([...allRunSelection(segments)], ["one", "two", "three"]);
  assert.deepEqual([...invertRunSelection(segments, new Set(["two"]))], ["one", "three"]);
  assert.deepEqual([...normalizeRunSelection(segments, new Set(["two", "removed"]))], ["two"]);
});

test("partial run exposes continuation dependency gaps without silently expanding selection", () => {
  const first = { ...segment("one"), status: "draft" };
  const second = { ...segment("two"), continuation: "previous_video" };
  assert.match(selectedRunDependencyError([first, second], new Set(["two"])), /未选中尚未完成的前驱/);
  assert.equal(selectedRunDependencyError([first, second], new Set(["one", "two"])), undefined);
  assert.equal(selectedRunDependencyError([{ ...first, status: "completed" }, second], new Set(["two"])), undefined);
  assert.deepEqual([...new Set(["two"])], ["two"], "validation must not mutate or auto-expand the user's selection");
});

test("manual playhead split produces fresh H3-grid drafts and clears stale execution output", () => {
  const source = [segment("one")];
  const result = splitSegmentDraftAtTime(source, 7.5, [profile], () => "two");
  assert.equal(result.length, 2);
  assert.deepEqual(result.map((item) => item.id), ["one", "two"]);
  assert.ok(result.every((item) => isH3DurationOption(item.request.parameters.duration)));
  assert.ok(result.every((item) => item.status === "draft" && item.attempts.length === 0));
  assert.ok(result.every((item) => item.preview_url === undefined && item.download_url === undefined));
  assert.equal(result[1].request.prompt, "shot one", "splitting must not rewrite the prompt");
  assert.notEqual(result[0].request.references, result[1].request.references, "reference arrays are not aliased");
  assert.equal(splitSegmentDraftAtTime(source, 1, [profile], () => "ignored"), source, "too-short cuts are refused");
});

test("equal split respects source duration, H3 maximum and the 64-shot UI safety cap", () => {
  let nextId = 1;
  const result = equalizeSegmentDrafts([segment("one")], 2, 30, [profile], () => `new-${nextId++}`);
  assert.equal(result.length, 2);
  assert.ok(result.every((item) => H3_DURATION_OPTIONS.includes(item.request.parameters.duration)));
  assert.ok(result.every((item) => item.request.parameters.duration <= 362 / 24));
  assert.equal(result[0].continuation, "none");
  assert.ok(equalizeSegmentDrafts([segment("one")], 1, 60, [profile], () => `more-${nextId++}`).length >= 4, "long sources are packed under the H3 maximum");
  assert.equal(segmentIndexAtTime(result, 0), 0);
  assert.equal(segmentIndexAtTime(result, 29), 1);
});

test("confirmed source cut points pack long ranges without applying detector suggestions implicitly", () => {
  let nextId = 1;
  const result = buildSegmentDraftsFromSourceCuts([segment("one")], [20], 30, [profile], () => `cut-${nextId++}`);
  assert.equal(result.length, 3, "the 20-second range is packed under the 15.08-second generation cap");
  assert.ok(result.every((item) => isH3DurationOption(item.request.parameters.duration)));
  const shortCut = buildSegmentDraftsFromSourceCuts([segment("one")], [2], 30, [profile], () => "short");
  assert.equal(shortCut.length, 3, "short source shots are retained while their H3 output maps to an allowed minimum grid");
  assert.ok(shortCut.every((item) => isH3DurationOption(item.request.parameters.duration)));
});

test("storyboard draft persists integer source frames, fills long gaps and keeps H3 output on its own grid", () => {
  let nextId = 1;
  const draft = buildStoryboardDraft(
    [segment("one")],
    { source_asset_id: "c".repeat(32), fps: 30, frame_count: 1000 },
    [500],
    [profile],
    () => `story-${nextId++}`,
  );
  assert.deepEqual(draft.storyboard, {
    source_asset_id: "c".repeat(32), fps: 30, frame_count: 1000, cut_frames: [250, 500, 750],
  });
  assert.deepEqual(draft.segments.map((item) => [item.source_range.start_frame, item.source_range.end_frame]), [[0, 250], [250, 500], [500, 750], [750, 1000]]);
  assert.ok(draft.segments.every((item) => Number.isInteger(item.source_range.start_frame) && Number.isInteger(item.source_range.end_frame)));
  assert.ok(draft.segments.every((item) => item.source_range.asset_id === "c".repeat(32) && item.source_range.fps === 30));
  assert.ok(draft.segments.every((item) => (item.source_range.end_frame - item.source_range.start_frame) / item.source_range.fps <= 15));
  assert.ok(draft.segments.every((item) => isH3DurationOption(item.request.parameters.duration)));
  assert.ok(draft.segments.every((item) => item.request.prompt === "shot one"), "source binding does not rewrite prompts");
});

test("source ranges automatically retarget FL templates to a compatible Ref2VA profile", () => {
  const fl = { ...profile, id: "h3-fl", compiler: "h3_fl", manifest_sha256: "f".repeat(64) };
  const template = { ...segment("fl-shot"), request: { ...segment("fl-shot").request, profile_id: fl.id, profile_digest: fl.manifest_sha256 } };
  const draft = buildStoryboardDraft(
    [template],
    { source_asset_id: "c".repeat(32), fps: 30, frame_count: 300 },
    [],
    [fl, profile],
    () => "unused",
  );
  assert.equal(draft.segments[0].request.profile_id, profile.id);
  assert.equal(draft.segments[0].request.prompt, template.request.prompt);
  assert.ok(draft.segments[0].source_range);
});

test("inserting a source cut inherits the maximum-overlap shot without crossing prompt, refs or profile", () => {
  const sourceId = "c".repeat(32);
  const profileB = { ...profile, id: "h3-ref-b", version: "2", manifest_sha256: "e".repeat(64) };
  const first = { ...segment("A"), source_range: { asset_id: sourceId, start_frame: 0, end_frame: 300, fps: 30 } };
  const second = {
    ...segment("B"),
    source_range: { asset_id: sourceId, start_frame: 300, end_frame: 600, fps: 30 },
    request: {
      ...segment("B").request,
      prompt: "shot B distinct",
      profile_id: profileB.id, profile_version: profileB.version, profile_digest: profileB.manifest_sha256,
      references: [{ asset_id: "d".repeat(32), role: "reference" }],
    },
  };
  const draft = buildStoryboardDraft(
    [first, second],
    { source_asset_id: sourceId, fps: 30, frame_count: 600, cut_frames: [300] },
    [150, 300],
    [profile, profileB],
    () => "A-split",
  );
  assert.deepEqual(draft.segments.map((item) => item.id), ["A", "A-split", "B"]);
  assert.deepEqual(draft.segments.map((item) => item.request.prompt), ["shot A", "shot A", "shot B distinct"]);
  assert.deepEqual(draft.segments.map((item) => item.request.references[0]?.asset_id), ["b".repeat(32), "b".repeat(32), "d".repeat(32)]);
  assert.deepEqual(draft.segments.map((item) => item.request.profile_id), [profile.id, profile.id, profileB.id]);
  assert.ok(draft.segments.every((item) => item.status === "draft" && item.attempts.length === 0 && !item.preview_url));
});

test("refining storyboard cuts preserves user-added blank generation shots", () => {
  const sourceId = "c".repeat(32);
  const ranged = {
    ...segment("source-shot"),
    source_range: { asset_id: sourceId, start_frame: 0, end_frame: 300, fps: 30 },
  };
  const blank = { ...segment("blank-shot"), request: { ...segment("blank-shot").request, prompt: "independent blank shot" } };
  const draft = buildStoryboardDraft(
    [ranged, blank],
    { source_asset_id: sourceId, fps: 30, frame_count: 300, cut_frames: [] },
    [150],
    [profile],
    () => "source-shot-2",
  );
  assert.equal(draft.segments.at(-1).id, "blank-shot");
  assert.equal(draft.segments.at(-1).source_range, undefined);
  assert.equal(draft.segments.at(-1).request.prompt, "independent blank shot");
  assert.deepEqual(draft.segments.slice(0, -1).map((item) => [item.source_range.start_frame, item.source_range.end_frame]), [[0, 150], [150, 300]]);
});
