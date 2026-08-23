import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workspace = await readFile(new URL("../app/video-director-workspace.tsx", import.meta.url), "utf8");
const timeline = await readFile(new URL("../app/video-timeline.tsx", import.meta.url), "utf8");
const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");

function between(source, start, end) {
  const from = source.indexOf(start);
  assert.notEqual(from, -1, `missing behavior boundary: ${start}`);
  const to = source.indexOf(end, from + start.length);
  assert.notEqual(to, -1, `missing behavior boundary: ${end}`);
  return source.slice(from, to);
}

function cssRulesFor(source, selector) {
  const rules = [];
  for (const match of source.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = match[1].split(",").map((value) => value.trim());
    if (selectors.includes(selector)) rules.push(match[2]);
  }
  return rules;
}

function lastCssProperty(source, selector, property) {
  let value;
  for (const body of cssRulesFor(source, selector)) {
    const matches = [...body.matchAll(new RegExp(`${property}\\s*:\\s*([^;]+)`, "g"))];
    if (matches.length) value = matches.at(-1)[1].trim();
  }
  return value;
}

const sequenceMonitor = between(workspace, "export function SequenceVideoMonitor(", "export function SourceVideoMonitor(");
const storyboard = between(workspace, "export function StoryboardTimeline(", "export function SourceRangePanel(");
const overviewMount = between(timeline, '<div className="director-overview-column">', '<section className="director-segment-inspector"');
const inspectorMount = between(timeline, '<section className="director-segment-inspector"', '<footer className="timeline-actions">');

test("one monitor owns output playback and source cutting instead of rendering a detached source tool", () => {
  assert.doesNotMatch(overviewMount, /<details[^>]+director-source-tools/, "source cutting must not remain a separate collapsed card");
  assert.doesNotMatch(overviewMount, /<SourceVideoMonitor\b/, "the left workbench must not render a second independent monitor");
  assert.match(sequenceMonitor, /成片|序列/);
  assert.match(sequenceMonitor, /源素材|源视频/, "the unified monitor must expose its source-editing view in the same surface");
  assert.match(sequenceMonitor, /当前帧切分/);
  assert.match(sequenceMonitor, /onSourceChange|sourceId/, "the unified monitor must receive the selected source asset");
  assert.match(overviewMount, /<SequenceVideoMonitor\b/, "the unified monitor remains the project-level monitor");
});

test("the unified monitor has explicit playback, pause and exact frame transport", () => {
  assert.match(sequenceMonitor, /\.play\(\)/, "play must not depend only on a possibly clipped native controls bar");
  assert.match(sequenceMonitor, /\.pause\(\)/, "pause must have an explicit accessible control");
  assert.match(sequenceMonitor, /aria-label=\{[^}]*\?\s*"\u6682\u505c"\s*:\s*"\u64ad\u653e"|aria-label="\u64ad\u653e\/\u6682\u505c"/, "the transport needs a stable accessible play/pause name");
  assert.match(sequenceMonitor, /aria-label="\u4e0a\u4e00\u5e27"/);
  assert.match(sequenceMonitor, /aria-label="\u4e0b\u4e00\u5e27"/);
  assert.match(sequenceMonitor, /const transportFps\s*=[^;]*H3_GENERATION_FPS/, "the transport must fall back to the shared H3 24fps clock");
  assert.match(sequenceMonitor, /monitorTime\s*[+-]\s*1\s*\/\s*transportFps/, "frame transport must advance exactly one frame, not a guessed decimal");
  assert.match(sequenceMonitor, /const updateFromPlayer[\s\S]*onTimeChange/, "the player clock adapter must publish the shared sequence time");
  assert.match(sequenceMonitor, /onTimeUpdate=[\s\S]*updateFromPlayer\(event\.currentTarget\)/, "video playback must invoke the shared clock adapter");
});

test("monitor media uses contain for every aspect ratio and its parent cannot clip the stage or controls", () => {
  assert.equal(lastCssProperty(css, ".director-monitor-stage video", "object-fit"), "contain");
  assert.notEqual(lastCssProperty(css, ".director-monitor", "overflow"), "hidden", "the monitor's stage and transport must contribute their real height");
  assert.match(css, /\.director-monitor-stage\s*\{[^}]*display\s*:\s*grid[^}]*place-items\s*:\s*center/);
  assert.match(sequenceMonitor, /playsInline/);
  assert.match(sequenceMonitor, /poster=/, "portrait and square outputs still need a contained poster before playback");
});

test("the visible playhead hit target supports click and drag with pointer capture", () => {
  assert.match(storyboard, /onPointerDown=\{/, "the time-scaled track needs a direct pointer entry point");
  assert.match(storyboard, /setPointerCapture\(/, "dragging must remain active when the pointer leaves the thin red line");
  assert.match(storyboard, /onPointerMove=\{|pointermove/, "the playhead must update continuously while dragging");
  assert.match(storyboard, /onPointerUp=\{|pointerup|releasePointerCapture/, "the drag session must end explicitly");
  assert.match(storyboard, /getBoundingClientRect\(\)/, "client coordinates must be normalized against the actual track");
  assert.match(storyboard, /onTimeChange\(/, "track seeking must publish the same sequence time as the monitor");
  assert.match(storyboard, /aria-label="(?:\u62d6\u52a8)?\u6210\u7247\u65f6\u95f4\u7ebf\u64ad\u653e\u5934"/, "the draggable hit target needs an accessible name");
});

test("ruler, duration-scaled shots and playhead share the same timeline coordinate space", () => {
  assert.match(storyboard, /segmentFrames[\s\S]*H3_GENERATION_FPS/, "shot durations must first be quantized onto the canonical 24fps frame clock");
  assert.match(storyboard, /left:\s*frame\s*\*\s*pxPerFrame/, "ruler ticks must use the canonical frame-to-pixel scale");
  assert.match(storyboard, /width:\s*frames\s*\*\s*pxPerFrame/, "shot widths must use the same frame-to-pixel scale");
  assert.match(storyboard, /left:\s*currentFrame\s*\*\s*pxPerFrame/, "the playhead must use the same frame-to-pixel scale");
  assert.match(storyboard, /director-storyboard-track[\s\S]*director-ruler[\s\S]*director-segment-row[\s\S]*director-playhead/, "ticks, clips, and playhead must be siblings in one positioned track");
  assert.match(storyboard, /maxWidth:\s*"none"/, "inline time geometry must override any legacy card-width cap");
});

test("the left storyboard can append and immediately select a blank generation segment", () => {
  const insertBlank = between(timeline, "const insertBlankSegment", "const active");
  assert.match(storyboard, /\u7a7a\u767d\u7247\u6bb5|\u7a7a\u767d\u5206\u6bb5/, "the add action belongs beside the storyboard, not only in the right inspector");
  assert.match(storyboard, /onInsertAfter\?\.\(selectedIndex\)/, "StoryboardTimeline must identify the insertion point explicitly");
  assert.match(overviewMount, /onInsertAfter=\{insertBlankSegment\}/, "the parent must wire the left-side add action");
  assert.match(insertBlank, /draftVideoSegment\(/, "new storyboard entries must be real generation drafts");
  assert.match(insertBlank, /setSelectedIndex\(/, "the new blank shot must become the current inspector target");
  assert.match(insertBlank, /setRunSelection(?:Ids)?\(/, "the new blank shot must participate in the next selected run");
  assert.doesNotMatch(insertBlank, /Boolean\(project\.storyboard\)|!project\.storyboard/, "binding a source storyboard must not disable adding an independent blank shot");
});

test("the inspector clearly branches direct media, source-backed editing and blank-shot generation", () => {
  assert.match(timeline, /const inspectorMode\s*=\s*selectedSegment\s*&&\s*isVideoMediaSegment\(selectedSegment\)[\s\S]*selectedSegment\?\.source_range/, "direct media, source-backed and blank segments need an explicit inspector mode");
  assert.match(inspectorMount, /data-editor-mode=\{inspectorMode\}/, "the active branch must be observable without relying on layout position");
  assert.match(inspectorMount, /inspectorMode === "source"\s*&&\s*\([\s\S]*<SourceRangePanel\b/, "crop/save operations belong only to a source-backed shot");
  assert.match(inspectorMount, /\u5f85\u751f\u6210\u7247\u6bb5|\u586b\u5199\u63d0\u793a\u8bcd\u548c\u53c2\u6570\u540e\u751f\u6210\u8fd9\u4e00\u6bb5/, "a blank shot must be described as a generation task");
  assert.match(inspectorMount, /<SegmentCard\b[\s\S]*onRun=/, "the selected blank shot exposes its generation action");
});

test("right-click selection and deletion retain the exact segment id", () => {
  assert.match(storyboard, /onContextMenu=\{/);
  assert.match(storyboard, /onSelect\(index\)[\s\S]*onTimeChange\(videoSequenceTimeForSegment\(segments, index\)\)[\s\S]*setContextShot\(\{\s*id:\s*segment\.id/, "right-click must select and seek the clicked clip before opening its menu");
  assert.match(storyboard, /const id = contextShot\.id[\s\S]*onDelete\(id\)/, "delete must use the retained id rather than the current array index");
  assert.match(overviewMount, /onDelete=\{removeSegment\}/);
  assert.match(timeline, /removeVideoProjectSegment\(current, segmentId\)/, "all deletion surfaces must share the exact-id transaction");
});

test("playback and scrubbing avoid duplicate decoders and redundant state churn", () => {
  const monitor = between(workspace, "export function SequenceVideoMonitor(", "export function SourceVideoMonitor(");
  const inspector = between(timeline, "function MediaSegmentInspector(", "function SegmentCard(");
  assert.match(monitor, /if \(!player\.paused\) return;/, "active playback must not be micro-seeked by state feedback");
  assert.match(storyboard, /requestAnimationFrame/);
  assert.match(storyboard, /cancelAnimationFrame/);
  assert.doesNotMatch(inspector, /<video\b/, "the inspector must not decode a duplicate copy of the monitor video");
  assert.match(inspector, /timeline-media-poster/);
});

test("saved long-video projects can be deleted safely from the project bar", async () => {
  const api = await readFile(new URL("../app/video-project-api.ts", import.meta.url), "utf8");
  assert.match(api, /async delete\(projectId:\s*string\)/);
  assert.match(api, /this\.request\([^\n]+,\s*"DELETE"\)/);
  assert.match(timeline, /const deleteProject = async/);
  assert.match(timeline, /window\.confirm/);
  assert.match(timeline, />\{action === "deleting-project" \? "删除中…" : "删除项目"\}</);
  assert.match(timeline, /disabled=\{!project\?\.id \|\| active \|\| Boolean\(action\)\}/);
});
