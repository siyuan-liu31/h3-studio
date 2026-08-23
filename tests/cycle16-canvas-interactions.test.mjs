import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const studioPath = new URL("../app/studio.tsx", import.meta.url);

test("a primary pointer press selects any node body before child controls handle the event", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /onPointerDownCapture=\{\(event\) => \{ if \(event\.button === 0\) \{ setSelectedId\(node\.id\); canvasInteractionActiveRef\.current = true; \} \}\}/);
  assert.match(source, /data-selected=\{selectedId === node\.id\}/);
});

test("Delete and Backspace remove the selected node outside editable controls", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /event\.key === "Delete" \|\| event\.code === "Backspace"/);
  assert.match(source, /isEditableEventTarget\(target\)/);
  assert.match(source, /const node = nodes\.find\(\(item\) => item\.id === selectedId\)/);
  assert.match(source, /removeNode\(node\)/);
});

test("a primary press on blank canvas cancels the pending connection and dashed preview immediately", async () => {
  const source = await readFile(studioPath, "utf8");

  const pointerDown = source.slice(source.indexOf("function startCanvasPan("), source.indexOf("function moveCanvasPan("));
  assert.match(pointerDown, /setConnecting\(undefined\);\s*setConnectionPointer\(undefined\)/);
  assert.match(source, /const clickedBlankCanvas = !target\.closest\("\.studio-node, \.wire-group, \.canvas-toolbar, \.canvas-context-menu, \.connection-tip"\)/);
  assert.match(pointerDown, /event\.button === 0 && clickedBlankCanvas && connecting/);
  assert.match(source, /\{connecting && connectionPointer/);
});

test("ordinary primary-button dragging on blank canvas pans the viewport", async () => {
  const source = await readFile(studioPath, "utf8");
  const pointerDown = source.slice(source.indexOf("function startCanvasPan("), source.indexOf("function moveCanvasPan("));

  assert.match(pointerDown, /event\.button === 1 \|\| wantsSpacePan \|\| \(event\.button === 0 && clickedBlankCanvas\)/);
  assert.match(pointerDown, /panRef\.current = \{/);
  assert.match(source, /onPointerMove=\{moveCanvasPan\}/);
});

test("the overview is a draggable viewport navigator with visible node and viewport markers", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /aria-label="画布导航概览，拖动以定位视口"/);
  assert.match(source, /function navigateFromOverview\(clientX: number, clientY: number\)/);
  assert.match(source, /onPointerDown=\{startOverviewNavigation\}/);
  assert.match(source, /onPointerMove=\{moveOverviewNavigation\}/);
  assert.match(source, /overviewPointerRef\.current = event\.pointerId/);
  assert.match(source, /nodes\.map\(\(node\) => <span key=\{`overview-\$\{node\.id\}`\}/);
  assert.match(source, /const overviewExtent = useMemo\(\(\) => canvasOverviewExtent\(nodes, viewport, viewportSize\)/);
  assert.match(source, /const extent = overviewGestureExtentRef\.current \?\? overviewExtent/);
  assert.match(source, /extent\.left \+ ratioX \* extent\.width/);
});
