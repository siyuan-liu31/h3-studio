import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const projectPath = new URL("../app/video-project.ts", import.meta.url);
const timelinePath = new URL("../app/video-timeline.tsx", import.meta.url);
const cssPath = new URL("../app/globals.css", import.meta.url);

test("continuation ranges are optional 24fps model data with one to 360 frame normalization", async () => {
  const source = await readFile(projectPath, "utf8");

  assert.match(source, /export type VideoContinuationRange = \{\s*start_frame: number;\s*end_frame: number;\s*fps: number;/);
  assert.match(source, /continuation_range\?: VideoContinuationRange/);
  assert.match(source, /H3_MAX_CONTINUATION_FRAMES = 360/);
  assert.match(source, /Math\.max\(1, Math\.round\(duration \* H3_GENERATION_FPS\)\)/);
  assert.match(source, /end_frame: Math\.min\(videoContinuationFrameCount\(previousDuration\), H3_MAX_CONTINUATION_FRAMES\)/);
  assert.match(source, /Math\.max\(startFrame \+ 1, Math\.min\(totalFrames, startFrame \+ H3_MAX_CONTINUATION_FRAMES/);
});

test("serialization persists only explicit ranges while legacy omission stays passive", async () => {
  const source = await readFile(projectPath, "utf8");

  assert.match(source, /segment\.continuation === "previous_video" && index > 0 && segment\.continuation_range[\s\S]{0,220}continuation_range: normalizeVideoContinuationRange/);
  assert.match(source, /remoteContinuationRange \?\? fallback\.continuation_range/);
  assert.match(source, /continuationRange = continuation === "previous_video"/);
  assert.match(source, /segment\.continuation_range \? \{ \.\.\.segment, continuation_range: undefined \}/);
});

test("switching continuation initializes or clears the range from the previous segment duration", async () => {
  const source = await readFile(timelinePath, "utf8");

  assert.match(source, /continuation === "previous_video" && previous[\s\S]{0,120}defaultVideoContinuationRange\(previous\.request\.parameters\.duration\)/);
  assert.match(source, /continuation_range: continuationRange/);
  assert.match(source, /segment\.continuation === "previous_video" && previous && <ContinuationRangeEditor/);
  assert.doesNotMatch(source, /segment\.continuation !== "previous_video" && previous && <ContinuationRangeEditor/);
});

test("the previous-video editor previews, seeks, and exposes a bounded dual range", async () => {
  const source = await readFile(timelinePath, "utf8");

  assert.match(source, /aria-label="上一段视频续接选区"/);
  assert.match(source, /<video ref=\{videoRef\} src=\{previous\.preview_url\}/);
  assert.match(source, /videoRef\.current\.currentTime = Math\.min\(duration, Math\.max\(0, frame \/ H3_GENERATION_FPS\)\)/);
  assert.match(source, /aria-label="上一段视频续接入点"/);
  assert.match(source, /aria-label="上一段视频续接出点"/);
  assert.match(source, /type="number"[\s\S]{0,300}aria-label="上一段视频续接入点帧号"/);
  assert.match(source, /type="number"[\s\S]{0,300}aria-label="上一段视频续接出点帧号（不含）"/);
  assert.match(source, /入点 <b>\{\(normalized\.start_frame \/ H3_GENERATION_FPS\)\.toFixed\(2\)\}s<\/b> · F\{normalized\.start_frame\}/);
  assert.match(source, /出点（不含） <b>\{\(normalized\.end_frame \/ H3_GENERATION_FPS\)\.toFixed\(2\)\}s<\/b> · F\{normalized\.end_frame\}/);
  assert.match(source, /最后包含帧 <b>F\{normalized\.end_frame - 1\}<\/b>/);
  assert.doesNotMatch(source, /if \(!rangeMatches\) onChange\(normalized\)/);
  assert.match(source, /运行时自动裁剪静音副本，不修改上一段/);
});

test("dual range styling shows the selected interval and remains usable on narrow screens", async () => {
  const css = await readFile(cssPath, "utf8");

  assert.match(css, /\.continuation-range-editor \{[^}]*grid-template-columns:/);
  assert.match(css, /\.continuation-dual-range > span \{[^}]*left: var\(--range-start\);[^}]*right: calc\(100% - var\(--range-end\)\)/);
  assert.match(css, /\.continuation-dual-range input\[type="range"\]::-webkit-slider-thumb/);
  assert.match(css, /\.continuation-range-numbers \{[^}]*grid-template-columns:/);
  assert.match(css, /@media \(max-width: 680px\)[\s\S]*\.continuation-range-editor \{ grid-template-columns: 1fr; \}/);
});
