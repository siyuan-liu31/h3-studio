import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  removeVideoProjectSegment,
  resolveVideoSequencePosition,
  videoSequenceDuration,
  videoSequenceTimeForSegment,
} from "../app/video-project.ts";

const studio = await readFile(new URL("../app/studio.tsx", import.meta.url), "utf8");
const timeline = await readFile(new URL("../app/video-timeline.tsx", import.meta.url), "utf8");
const workspace = await readFile(new URL("../app/video-director-workspace.tsx", import.meta.url), "utf8");

/**
 * Keep source contracts local to a named behavior boundary. This avoids the
 * brittle pattern of accepting an unrelated token elsewhere in a large TSX
 * file while still allowing implementation details inside that boundary to
 * evolve.
 */
function between(source, start, end) {
  const from = source.indexOf(start);
  assert.notEqual(from, -1, `missing behavior boundary: ${start}`);
  const to = source.indexOf(end, from + start.length);
  assert.notEqual(to, -1, `missing behavior boundary: ${end}`);
  return source.slice(from, to);
}

const digest = "a".repeat(64);
function approximately(actual, expected) {
  assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} should approximately equal ${expected}`);
}
function videoSegment(id, duration = 124 / 24, overrides = {}) {
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
      profile_id: "h3",
      profile_version: "1",
      profile_digest: digest,
      references: [],
    },
    ...overrides,
  };
}

test("clicking a node selects that exact node and Delete removes the selected node only", () => {
  const selectByPointer = between(studio, "function startDrag(", "function applyPointerUpdate(");
  const deleteByKeyboard = between(studio, "const handleNodeDelete", "window.addEventListener");
  const nodeSurface = between(studio, "return <article key={node.id}", "{node.kind !== \"asset\"");

  assert.match(nodeSurface, /data-selected=\{selectedId === node\.id\}/, "the node surface must expose selection state");
  assert.match(nodeSurface, /onPointerDown=\{\(event\) => startDrag\(event, node\)\}/, "the node surface must route a pointer click through the node-aware handler");
  assert.match(selectByPointer, /setSelectedId\(node\.id\)/, "selection must use the clicked instance id");
  assert.match(deleteByKeyboard, /event\.key === "Delete"/, "the canvas must support the requested Delete key");
  assert.match(deleteByKeyboard, /nodes\.find\(\(item\) => item\.id === selectedId\)/, "Delete must resolve the current selection, not a fixed node");
  assert.match(deleteByKeyboard, /removeNode\(node\)/, "Delete must reuse the canonical node removal transaction");
  assert.match(deleteByKeyboard, /canvasInteractionActiveRef\.current/, "Delete must remain scoped to an active canvas");
  assert.match(deleteByKeyboard, /isEditableEventTarget\(target\)/, "Delete must not hijack text editors");
});

test("a primary-button press on empty canvas cancels a pending connection", () => {
  const canvasPointerDown = between(studio, "function startCanvasPan(", "function moveCanvasPan(");

  assert.match(canvasPointerDown, /event\.button === 0/, "primary-button blank-canvas interaction must be handled");
  assert.match(
    canvasPointerDown,
    /event\.target === event\.currentTarget|target\.closest\([^)]*(?:studio-node|port)[^)]*\)/,
    "cancellation must be scoped to canvas background rather than node or port controls",
  );
  assert.match(canvasPointerDown, /setConnecting\(undefined\)/, "blank canvas must cancel the pending source node");
  assert.match(canvasPointerDown, /setConnectionPointer\(undefined\)/, "blank canvas must also clear the preview wire");
  assert.match(
    studio,
    /className="canvas-scroll"[^>]+onPointerDown=\{startCanvasPan\}/,
    "the cancellation/pan handler must be mounted on the visible viewport",
  );
});

test("the canvas viewport pans with an ordinary primary-button background drag", () => {
  const startPan = between(studio, "function startCanvasPan(", "function moveCanvasPan(");
  const movePan = between(studio, "function moveCanvasPan(", "function finishCanvasPan(");

  assert.match(startPan, /event\.button === 0 && clickedBlankCanvas/, "ordinary blank-canvas left drag must work without holding Space");
  assert.match(startPan, /wantsSpacePan/, "Space+left drag must additionally pan when starting over a node");
  assert.match(startPan, /panRef\.current\s*=\s*\{/, "pointer down must capture a viewport-pan origin");
  assert.match(movePan, /setViewport\(/, "pointer movement must update the viewport rather than node positions");
  assert.match(movePan, /originX[\s\S]*event\.clientX[\s\S]*originY[\s\S]*event\.clientY/, "viewport movement must preserve the drag delta in both axes");
  assert.match(studio, /onPointerMove=\{moveCanvasPan\}/);
  assert.match(studio, /onPointerUp=\{finishCanvasPan\}/);
  assert.match(studio, /translate3d\(\$\{viewport\.x\}px, \$\{viewport\.y\}px, 0\) scale\(\$\{viewport\.zoom\}\)/, "the rendered overview must consume the same viewport state");
});

test("the top monitor and storyboard share an output-sequence playhead independent from source cutting", () => {
  const seekSequence = between(timeline, "const seekSequence", "const sourceDescriptor");
  const directorMount = between(timeline, '<div className="director-workspace">', '<section className="director-segment-inspector"');
  const storyboard = between(workspace, "export function StoryboardTimeline(", "export function SourceRangePanel(");
  const sequenceMonitor = between(workspace, "export function SequenceVideoMonitor(", "export function SourceVideoMonitor(");

  const segments = [videoSegment("A"), videoSegment("B", 141 / 24), videoSegment("C", 158 / 24)];
  approximately(videoSequenceDuration(segments), (124 + 141 + 158) / 24);
  approximately(videoSequenceTimeForSegment(segments, 2), (124 + 141) / 24);
  const boundary = resolveVideoSequencePosition(segments, 124 / 24);
  assert.equal(boundary.index, 1);
  assert.equal(boundary.localTime, 0);

  assert.match(timeline, /const \[sequenceTime, setSequenceTime\] = useState\(0\)/, "the parent must own the output-sequence playhead");
  assert.match(timeline, /const \[sourceTime, setSourceTime\] = useState\(0\)/, "source cutting must retain a separate source-media playhead");
  assert.match(directorMount, /<SequenceVideoMonitor[\s\S]*currentTime=\{sequenceTime\}[\s\S]*onTimeChange=\{seekSequence\}/, "the top monitor must publish output-sequence time");
  assert.match(directorMount, /<StoryboardTimeline[\s\S]*currentTime=\{sequenceTime\}[\s\S]*onTimeChange=\{seekSequence\}/, "storyboard must render and update that same output-sequence time");
  assert.match(directorMount, /<SequenceVideoMonitor[\s\S]*sourceTime=\{sourceTime\}[\s\S]*onSourceTimeChange=\{setSourceTime\}/, "source cutting must use a separate clock inside the shared monitor");
  assert.doesNotMatch(directorMount, /<SourceVideoMonitor\b/, "the source cutting surface must not be rendered as a detached second monitor");
  assert.match(directorMount, /segments=\{project\.segments\}/, "storyboard order must be the durable project segment order");
  assert.match(seekSequence, /resolveVideoSequencePosition\([^,]+, seconds\)/, "sequence seeking must resolve an output segment and local time");
  assert.match(seekSequence, /setSequenceTime\(\(current\)[\s\S]*position\.time\)/);
  assert.match(seekSequence, /setSelectedIndex\(\(current\)[\s\S]*position\.index\)/, "sequence seeking must keep the selected shot synchronized without redundant renders");
  assert.match(sequenceMonitor, /segment\?\.status === "completed"[\s\S]*segment\.preview_url \|\| segment\.download_url/, "completed sequence positions must preview generated output, not the source asset");
  assert.match(directorMount, /<SequenceVideoMonitor[\s\S]*merged=\{dirty \? undefined : project\.merged\}/, "an old merged movie must be hidden while project edits are dirty");
  assert.match(sequenceMonitor, /merged\?\.status === "completed"[\s\S]*merged\.preview_url \|\| merged\.download_url/, "the monitor should prefer a completed merged movie when present");
  assert.match(sequenceMonitor, /const updateFromPlayer[\s\S]*if \(mergedUrl\) onTimeChange\(player\.currentTime\)[\s\S]*position\.segmentStart \+ player\.currentTime/, "merged playback is already global time while segment playback must be offset into sequence time");
  assert.match(storyboard, /segments\.map\(\(segment, index\)/, "the visual sequence must preserve array order");
  assert.match(storyboard, /left:\s*currentFrame\s*\*\s*pxPerFrame/, "the visual playhead must derive from the canonical output-frame scale, never source duration");
});

test("the storyboard timeline itself can seek the shared playhead", () => {
  const storyboard = between(workspace, "export function StoryboardTimeline(", "export function SourceRangePanel(");
  const directorMount = between(timeline, "<StoryboardTimeline", '<section className="director-segment-inspector"');

  assert.match(storyboard, /onSeek|onTimeChange/, "StoryboardTimeline must accept an explicit seek callback");
  assert.match(
    storyboard,
    /on(?:PointerDown|PointerUp|Click|Change)=\{[^}]*\b(?:onSeek|onTimeChange)\b/,
    "clicking, dragging, or changing the timeline must invoke its seek callback",
  );
  assert.match(
    storyboard,
    /(?:clientX|getBoundingClientRect|valueAsNumber|Number\(event\.target\.value\))/,
    "timeline input must convert a pointer/range position into time",
  );
  assert.match(directorMount, /(?:onSeek|onTimeChange)=\{seekSequence\}/, "timeline seek must update the same output-sequence playhead used by the top monitor");
});

test("right-clicking a storyboard segment exposes deletion for that segment", () => {
  const storyboard = between(workspace, "export function StoryboardTimeline(", "export function SourceRangePanel(");
  const directorMount = between(timeline, "<StoryboardTimeline", '<section className="director-segment-inspector"');

  assert.match(storyboard, /onContextMenu=\{/, "each storyboard shot needs a conventional right-click entry point");
  assert.match(storyboard, /setContextShot\(\{ id: segment\.id, index,/, "the right-click action must retain the clicked segment identity");
  assert.match(storyboard, /删除分段/, "the context action must be clearly labelled");
  assert.match(storyboard, /onDelete\(id\)/, "the context action must delete the retained segment id");
  assert.match(storyboard, /contextDeleteRef\.current\?\.focus\(\)/, "opening the context menu must focus its keyboard action");
  assert.match(directorMount, /onDelete=\{removeSegment\}/, "the timeline parent must own the durable deletion transaction");
});

test("storyboard deletion allows source-bound shots, merges their range, and protects only active or last-shot projects", () => {
  const segmentCard = between(timeline, "function SegmentCard(", "function AssetPicker(");
  const storyboard = between(workspace, "export function StoryboardTimeline(", "export function SourceRangePanel(");
  const directorMount = between(timeline, "<StoryboardTimeline", '<section className="director-segment-inspector"');
  const sourceId = "b".repeat(32);
  const project = {
    title: "source storyboard",
    status: "completed",
    selected_segment_ids: ["A", "B", "C"],
    storyboard: { source_asset_id: sourceId, fps: 20, frame_count: 300, cut_frames: [100, 200] },
    merged: { status: "completed", download_url: "/merged.mp4" },
    segments: [
      videoSegment("A", undefined, { source_range: { asset_id: sourceId, start_frame: 0, end_frame: 100, fps: 20 } }),
      videoSegment("B", undefined, { source_range: { asset_id: sourceId, start_frame: 100, end_frame: 200, fps: 20 } }),
      videoSegment("C", undefined, { continuation: "previous_video", source_range: { asset_id: sourceId, start_frame: 200, end_frame: 300, fps: 20 } }),
    ],
  };
  const removed = removeVideoProjectSegment(project, "B");
  assert.notEqual(removed, project, "a source-bound storyboard is not itself a deletion lock");
  assert.deepEqual(removed.segments.map((item) => item.id), ["A", "C"]);
  assert.deepEqual(removed.segments[1].source_range, { asset_id: sourceId, start_frame: 100, end_frame: 300, fps: 20 }, "deleting the middle cut must close the source-frame gap");
  assert.deepEqual(removed.storyboard.cut_frames, [100]);
  assert.equal(removed.segments[1].status, "stale", "the merged range must be regenerated");
  assert.equal(removed.merged, undefined, "a prior merged movie is invalid after structural deletion");

  const only = { title: "one", status: "draft", segments: [videoSegment("only")] };
  assert.equal(removeVideoProjectSegment(only, "only"), only, "the last segment is protected");
  const active = { title: "active", status: "running", segments: [videoSegment("A"), videoSegment("B")] };
  assert.equal(removeVideoProjectSegment(active, "A"), active, "active projects are protected");

  assert.match(segmentCard, /disabled=\{disabled \|\| total === 1\}[^>]+onClick=\{onRemove\}/, "the inspector delete action must not treat storyboard/source binding as a lock");
  assert.doesNotMatch(segmentCard, /disabled=\{disabled \|\| structureLocked \|\| total === 1\}[^>]+onClick=\{onRemove\}/, "source-bound storyboard shots must remain deletable");
  assert.match(storyboard, /disabled=\{disabled \|\| segments\.length <= 1\}/, "right-click deletion must expose active/operation and last-segment protection");
  assert.match(directorMount, /disabled=\{active \|\| Boolean\(action\)\}/, "the parent must pass active/operation state into the right-click protection");
  assert.match(timeline, /removeVideoProjectSegment\(current, segmentId\)/, "all UI deletion routes must share the pure range-merging transaction");
});
