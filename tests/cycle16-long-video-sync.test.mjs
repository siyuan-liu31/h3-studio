import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  removeVideoProjectSegment,
  resolveVideoSequencePosition,
  videoSequenceDuration,
  videoSequenceTimeForSegment,
} from "../app/video-project.ts";

const digest = "a".repeat(64);
const approximately = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} ≈ ${expected}`);
function segment(id, duration = 124 / 24, overrides = {}) {
  return {
    id,
    continuation: "none",
    status: "completed",
    attempts: [],
    preview_url: `/preview/${id}.mp4`,
    request: {
      prompt: id,
      prompt_mode: "preserve_tags_only",
      parts: {},
      parameters: { aspect_ratio: "16:9", duration, steps: 4, lora_strength: 0.75, denoise: 1, seed: -1 },
      profile_id: "h3", profile_version: "1", profile_digest: digest, references: [],
    },
    ...overrides,
  };
}

test("project playhead resolves output sequence boundaries and local media time", () => {
  const segments = [segment("A"), segment("B", 141 / 24), segment("C", 158 / 24)];
  const firstDuration = segments[0].request.parameters.duration;
  approximately(videoSequenceDuration(segments), (124 + 141 + 158) / 24);
  approximately(videoSequenceTimeForSegment(segments, 2), (124 + 141) / 24);
  const boundary = resolveVideoSequencePosition(segments, firstDuration);
  assert.equal(boundary.index, 1);
  assert.equal(boundary.segment, segments[1]);
  assert.equal(boundary.localTime, 0);
  approximately(boundary.segmentStart, firstDuration);
  approximately(boundary.segmentEnd, (124 + 141) / 24);
  approximately(boundary.totalDuration, (124 + 141 + 158) / 24);
  const end = resolveVideoSequencePosition(segments, Number.POSITIVE_INFINITY);
  assert.equal(end.index, 2);
  assert.equal(end.time, end.totalDuration);
  const tail = resolveVideoSequencePosition(segments, 999);
  assert.equal(tail.index, 2);
  assert.equal(tail.time, tail.totalDuration);
  approximately(tail.localTime, 158 / 24);
});

test("deleting a storyboard shot closes its source range and invalidates the merged shot", () => {
  const source = "b".repeat(32);
  const project = {
    title: "film", status: "completed", selected_segment_ids: ["A", "B", "C"],
    storyboard: { source_asset_id: source, fps: 20, frame_count: 300, cut_frames: [100, 200] },
    merged: { status: "completed", download_url: "/merged.mp4" },
    segments: [
      segment("A", undefined, { source_range: { asset_id: source, start_frame: 0, end_frame: 100, fps: 20 } }),
      segment("B", undefined, { source_range: { asset_id: source, start_frame: 100, end_frame: 200, fps: 20 } }),
      segment("C", undefined, { continuation: "previous_video", source_range: { asset_id: source, start_frame: 200, end_frame: 300, fps: 20 } }),
    ],
  };
  const next = removeVideoProjectSegment(project, "B");
  assert.deepEqual(next.segments.map((item) => item.id), ["A", "C"]);
  assert.deepEqual(next.segments[1].source_range, { asset_id: source, start_frame: 100, end_frame: 300, fps: 20 });
  assert.equal(next.segments[1].status, "stale");
  assert.deepEqual(next.storyboard.cut_frames, [100]);
  assert.deepEqual(next.selected_segment_ids, ["A", "C"]);
  assert.equal(next.merged, undefined);
});

test("deletion protects active projects and the last remaining shot", () => {
  const only = { title: "film", status: "draft", segments: [segment("A")] };
  assert.equal(removeVideoProjectSegment(only, "A"), only);
  const active = { title: "film", status: "running", segments: [segment("A"), segment("B")] };
  assert.equal(removeVideoProjectSegment(active, "A"), active);

  const two = { title: "film", status: "draft", segments: [segment("A"), segment("B", undefined, { continuation: "previous_video" })] };
  const withoutFirst = removeVideoProjectSegment(two, "A");
  assert.equal(withoutFirst.segments[0].id, "B");
  assert.equal(withoutFirst.segments[0].continuation, "none");
  assert.equal(withoutFirst.segments[0].status, "stale");
});

test("long-video UI exposes sequence monitor, draggable playhead and guarded context deletion", async () => {
  const workspace = await readFile(new URL("../app/video-director-workspace.tsx", import.meta.url), "utf8");
  const timeline = await readFile(new URL("../app/video-timeline.tsx", import.meta.url), "utf8");
  assert.match(workspace, /完整分镜序列监视器/);
  assert.match(workspace, /segment\?\.status === "completed" \? \(segment\.preview_url \|\| segment\.download_url\)/);
  assert.match(workspace, /aria-label="拖动成片时间线播放头"/);
  assert.match(workspace, /onEnded=\{\(\) => \{[\s\S]*if \(!mergedUrl && position\.index < segments\.length - 1\) seekSegment/);
  assert.match(workspace, /mergedUrl \? `merged:\$\{mergedUrl\}`/, "a merged movie must remain one continuous media source");
  assert.match(timeline, /<SequenceVideoMonitor segments=\{project\.segments\}/);
  assert.match(timeline, /<SequenceVideoMonitor[\s\S]*sourceTime=\{sourceTime\}/, "source cutting now lives in the shared monitor");
  assert.doesNotMatch(timeline, /源视频切分工具（可选）/, "the detached cutting card was intentionally removed");
  assert.match(timeline, /onContextMenu=/);
  assert.match(timeline, /项目运行或操作期间不能删除分段/);
  assert.match(timeline, /长视频项目至少需要保留一个分段/);
  assert.match(timeline, /removeVideoProjectSegment\(current, segmentId\)/);
});
