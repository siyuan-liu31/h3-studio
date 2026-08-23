import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const studioPath = new URL("../app/studio.tsx", import.meta.url);
const cssPath = new URL("../app/globals.css", import.meta.url);

test("canvas nodes and wire surface are not constrained by a fixed document rectangle", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.doesNotMatch(source, /CANVAS_BOUNDS|3200|2200/);
  assert.match(source, /const position = \{ x: point\.x - active\.dx, y: point\.y - active\.dy \}/);
  assert.match(source, /position: \{ \.\.\.position \}/);
  assert.match(source, /<svg className="wires" width="1" height="1"/);
});

test("blank canvas drag and wheel gestures pan both axes without clamping", async () => {
  const source = await readFile(studioPath, "utf8");
  const pan = source.slice(source.indexOf("function startCanvasPan("), source.indexOf("function navigateFromOverview("));
  const wheel = source.slice(source.indexOf("function handleCanvasWheel("), source.indexOf("function startCanvasPan("));

  assert.match(pan, /wantsSpacePan/);
  assert.match(pan, /event\.button === 1 \|\| wantsSpacePan \|\| \(event\.button === 0 && clickedBlankCanvas\)/);
  assert.match(pan, /x: pan\.originX \+ event\.clientX - pan\.clientX, y: pan\.originY \+ event\.clientY - pan\.clientY/);
  assert.match(wheel, /event\.deltaMode === 1 \? 16 : event\.deltaMode === 2/);
  assert.match(wheel, /x: current\.x - deltaX, y: current\.y - deltaY/);
  assert.match(wheel, /event\.shiftKey && deltaX === 0/);
});

test("overview and grid follow the unbounded viewport instead of fixed canvas dimensions", async () => {
  const [source, css] = await Promise.all([readFile(studioPath, "utf8"), readFile(cssPath, "utf8")]);

  assert.match(source, /function canvasOverviewExtent\(/);
  assert.match(source, /const rawLeft = Math\.min\(visibleLeft, contentLeft\)/);
  assert.match(source, /const extent = overviewGestureExtentRef\.current \?\? overviewExtent/);
  assert.match(source, /extent\.left \+ ratioX \* extent\.width/);
  assert.match(source, /"--canvas-grid-x": `\$\{viewport\.x\}px`/);
  assert.match(source, /"--canvas-grid-y": `\$\{viewport\.y\}px`/);
  assert.match(css, /background-position:\s*var\(--canvas-grid-x/);
  assert.match(css, /\.node-canvas\s*\{[^}]*width:\s*1px;[^}]*height:\s*1px;[^}]*overflow:\s*visible;/);
});

test("viewport-sized blank surface owns context menu and file drop interactions", async () => {
  const source = await readFile(studioPath, "utf8");
  const scrollStart = source.indexOf('<div className="canvas-scroll"');
  const scrollTag = source.slice(scrollStart, source.indexOf("{/* A graph canvas", scrollStart));
  const nodeCanvasTag = source.match(/<div className="node-canvas"[^>]+>/)?.[0] ?? "";

  assert.match(scrollTag, /onContextMenu=\{openCanvasContextMenu\}/);
  assert.match(scrollTag, /onDrop=\{handleDrop\}/);
  assert.doesNotMatch(nodeCanvasTag, /onContextMenu=|onDrop=/);
});
