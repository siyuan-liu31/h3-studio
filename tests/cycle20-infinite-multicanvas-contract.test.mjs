import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import * as documentModel from "../app/studio-document.ts";
import * as workspaceModel from "../app/studio-workspace.ts";
import { assetPayloadFromStudio, studioAssetFromDocument } from "../app/studio-asset-roundtrip.ts";

const studioSource = readFileSync(new URL("../app/studio.tsx", import.meta.url), "utf8");

function ids(prefix = "canvas") {
  let next = 0;
  return () => `${prefix}-${++next}`;
}

function requiredFunction(module, name, moduleName) {
  const candidate = module[name];
  assert.equal(typeof candidate, "function", `${name} must be exported by ${moduleName}`);
  return candidate;
}

function documentFunction(name) {
  return requiredFunction(documentModel, name, "studio-document.ts");
}

function workspaceFunction(name) {
  return requiredFunction(workspaceModel, name, "studio-workspace.ts");
}

function workspaceCanvases(workspace) {
  assert.ok(Array.isArray(workspace?.canvases), "workspace.canvases must be an ordered array");
  return workspace.canvases;
}

function canvasDocument(entry) {
  assert.ok(entry?.document && typeof entry.document === "object", "each canvas entry must own a document");
  return entry.document;
}

function activeCanvas(workspace) {
  const entry = workspaceCanvases(workspace).find((canvas) => canvas.id === workspace.activeCanvasId);
  assert.ok(entry, "activeCanvasId must resolve to an existing canvas");
  return entry;
}

function sourceBlock(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  assert.notEqual(start, -1, `missing source marker: ${startMarker}`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `missing source end marker: ${endMarker}`);
  return source.slice(start, end);
}

function clone(value) {
  return structuredClone(value);
}

test("canvas document round-trip preserves negative and far-away node coordinates", () => {
  const createDefaultCanvasDocument = documentFunction("createDefaultCanvasDocument");
  const parseCanvasDocument = documentFunction("parseCanvasDocument");
  const serializeCanvasDocument = documentFunction("serializeCanvasDocument");
  const document = createDefaultCanvasDocument(ids("node"));
  document.nodes[0].position = { x: -48_000, y: 73_500 };
  document.nodes[1].position = { x: 1_250_000, y: -910_000 };

  const parsed = parseCanvasDocument(serializeCanvasDocument(document));

  assert.equal(parsed.ok, true);
  assert.deepEqual(parsed.document.nodes.map((node) => node.position), [
    { x: -48_000, y: 73_500 },
    { x: 1_250_000, y: -910_000 },
  ]);
});

test("infinite canvas drag and creation do not clamp positions to the legacy 3200x2200 surface", () => {
  const drag = sourceBlock(studioSource, "function applyPointerUpdate", "function moveDrag");
  const create = sourceBlock(studioSource, "function createNode", "const removeNode");
  assert.doesNotMatch(drag, /CANVAS_BOUNDS|Math\.(?:min|max)\s*\(/, "node drag must retain the world coordinate produced by the viewport transform");
  assert.match(drag, /x:\s*point\.x\s*-\s*active\.dx/);
  assert.match(drag, /y:\s*point\.y\s*-\s*active\.dy/);
  assert.doesNotMatch(create, /CANVAS_BOUNDS|boundedPosition|Math\.(?:min|max)\s*\(/, "new nodes must be creatable left/above and beyond the old surface");
  assert.match(create, /position:\s*\{\s*\.\.\.position\s*\}|position\s*[,}]/, "the requested world position must be stored without clamping");
  assert.doesNotMatch(studioSource, /style=\{\{\s*width:\s*CANVAS_BOUNDS\.width,\s*height:\s*CANVAS_BOUNDS\.height/, "the world surface must not be a fixed-size DOM box");
  assert.doesNotMatch(studioSource, /<svg[^>]+width=\{CANVAS_BOUNDS\.width\}[^>]+height=\{CANVAS_BOUNDS\.height\}/, "wires must not be clipped to the old finite surface");
});

test("wheel and pointer panning translate the viewport on both horizontal and vertical axes", () => {
  const wheel = sourceBlock(studioSource, "function handleCanvasWheel", "function startCanvasPan");
  const pointer = sourceBlock(studioSource, "function moveCanvasPan", "function finishCanvasPan");
  assert.match(wheel, /(?:deltaX\s*=\s*event\.deltaX|x:\s*current\.x\s*-\s*event\.deltaX)/);
  assert.match(wheel, /(?:deltaY\s*=\s*event\.deltaY|y:\s*current\.y\s*-\s*event\.deltaY)/);
  assert.match(wheel, /x:\s*current\.x\s*-\s*(?:event\.)?deltaX/);
  assert.match(wheel, /y:\s*current\.y\s*-\s*(?:event\.)?deltaY/);
  assert.match(pointer, /x:\s*pan\.originX\s*\+\s*event\.clientX\s*-\s*pan\.clientX/);
  assert.match(pointer, /y:\s*pan\.originY\s*\+\s*event\.clientY\s*-\s*pan\.clientY/);
  assert.doesNotMatch(`${wheel}\n${pointer}`, /CANVAS_BOUNDS/, "viewport panning must not stop at a world boundary");
});

test("a legacy single CanvasDocumentV7 migrates into one active workspace canvas without data loss", () => {
  const createDefaultCanvasDocument = documentFunction("createDefaultCanvasDocument");
  const parseCanvasDocument = documentFunction("parseCanvasDocument");
  const serializeCanvasDocument = documentFunction("serializeCanvasDocument");
  const createCanvasWorkspace = workspaceFunction("createCanvasWorkspace");
  const legacyDocument = createDefaultCanvasDocument(ids("legacy-node"));
  legacyDocument.viewport = { x: -812, y: 427, zoom: 0.73 };
  legacyDocument.nodes[0].position = { x: -9000, y: 22_000 };

  const restored = parseCanvasDocument(serializeCanvasDocument(legacyDocument));
  assert.equal(restored.ok, true);
  const workspace = createCanvasWorkspace(restored.document, ids("migrate"), () => 101);

  assert.equal(workspaceCanvases(workspace).length, 1);
  assert.deepEqual(canvasDocument(activeCanvas(workspace)), legacyDocument);
});

test("creating a canvas retains the old canvas document and activates an isolated new document", () => {
  const createDefaultCanvasDocument = documentFunction("createDefaultCanvasDocument");
  const createCanvasWorkspace = workspaceFunction("createCanvasWorkspace");
  const addCanvasWorkspaceTab = workspaceFunction("addCanvasWorkspaceTab");
  const original = createCanvasWorkspace(createDefaultCanvasDocument(ids("first-node")), ids("create"), () => 10);
  const workspaceSnapshot = clone(original);
  const originalEntry = activeCanvas(original);
  const originalSnapshot = clone(canvasDocument(originalEntry));
  const newDocument = createDefaultCanvasDocument(ids("new-node"));

  const added = addCanvasWorkspaceTab(original, newDocument, ids("new"), () => 20);

  assert.equal(workspaceCanvases(added).length, 2);
  assert.notEqual(added.activeCanvasId, original.activeCanvasId, "the new tab should become active");
  assert.deepEqual(canvasDocument(workspaceCanvases(added).find((canvas) => canvas.id === originalEntry.id)), originalSnapshot);
  assert.deepEqual(original, workspaceSnapshot, "adding a tab must not mutate the caller's workspace");
});

test("switching canvases restores independent viewport and nodes for each tab", () => {
  const createDefaultCanvasDocument = documentFunction("createDefaultCanvasDocument");
  const createCanvasWorkspace = workspaceFunction("createCanvasWorkspace");
  const addCanvasWorkspaceTab = workspaceFunction("addCanvasWorkspaceTab");
  const updateCanvasWorkspaceDocument = workspaceFunction("updateCanvasWorkspaceDocument");
  const base = createCanvasWorkspace(createDefaultCanvasDocument(ids("first-isolation-node")), ids("isolation"), () => 1);
  const firstId = base.activeCanvasId;
  const withSecond = addCanvasWorkspaceTab(base, createDefaultCanvasDocument(ids("second-isolation-node")), ids("second"), () => 2);
  const secondId = withSecond.activeCanvasId;
  const firstDocument = clone(canvasDocument(workspaceCanvases(withSecond).find((canvas) => canvas.id === firstId)));
  firstDocument.viewport = { x: -350, y: 940, zoom: 0.6 };
  firstDocument.nodes[0].title = "only-first";
  const secondDocument = clone(canvasDocument(activeCanvas(withSecond)));
  secondDocument.viewport = { x: 5400, y: -2100, zoom: 1.45 };
  secondDocument.nodes[0].title = "only-second";

  const updatedFirst = updateCanvasWorkspaceDocument(withSecond, firstId, firstDocument, () => 3);
  const updatedBoth = updateCanvasWorkspaceDocument(updatedFirst, secondId, secondDocument, () => 4);
  const selectedFirst = updateCanvasWorkspaceDocument(updatedBoth, firstId, firstDocument, () => 5);
  const selectedSecond = updateCanvasWorkspaceDocument(selectedFirst, secondId, secondDocument, () => 6);

  assert.deepEqual(canvasDocument(activeCanvas(selectedFirst)), firstDocument);
  assert.deepEqual(canvasDocument(activeCanvas(selectedSecond)), secondDocument);
  assert.deepEqual(canvasDocument(workspaceCanvases(selectedSecond).find((canvas) => canvas.id === firstId)), firstDocument, "selecting another tab must not overwrite the first tab");
  assert.deepEqual(canvasDocument(workspaceCanvases(selectedFirst).find((canvas) => canvas.id === secondId)), secondDocument, "switching back must not overwrite the second tab");
});

test("workspace persistence round-trips all tabs and the active tab", () => {
  const createDefaultCanvasDocument = documentFunction("createDefaultCanvasDocument");
  const createCanvasWorkspace = workspaceFunction("createCanvasWorkspace");
  const addCanvasWorkspaceTab = workspaceFunction("addCanvasWorkspaceTab");
  const parseCanvasWorkspace = workspaceFunction("parseCanvasWorkspace");
  const serializeCanvasWorkspace = workspaceFunction("serializeCanvasWorkspace");
  const first = createCanvasWorkspace(createDefaultCanvasDocument(ids("persist-node")), ids("persist"), () => 1);
  const workspace = addCanvasWorkspaceTab(first, createDefaultCanvasDocument(ids("persist-new-node")), ids("persist-new"), () => 2);

  const parsed = parseCanvasWorkspace(serializeCanvasWorkspace(workspace));

  assert.equal(parsed.ok, true);
  assert.deepEqual(parsed.workspace, workspace);
});

test("job and derivation assets preserve preview provenance and binding roles across a Studio adapter round-trip", () => {
  const createCanvasNode = documentFunction("createCanvasNode");
  const createDefaultCanvasDocument = documentFunction("createDefaultCanvasDocument");
  const document = createDefaultCanvasDocument(ids("asset-roundtrip"));
  const generator = document.nodes.find((node) => node.kind === "video-generator");
  assert.ok(generator);
  const asset = createCanvasNode("asset", { x: -300, y: 800 }, ids("derived-asset"));
  asset.mediaKind = "video";
  asset.asset = assetPayloadFromStudio({
    media: "video",
    fileName: "derived-preview.mp4",
    localUrl: "blob:derived-preview",
    derivationId: "derive-17",
    sourceJobId: "job-42",
    thumbnailUrl: "/api/jobs/job-42/thumbnail",
    uploadState: "ready",
    role: "motion",
    mediaMeta: { duration: 4.2, has_audio: true },
  });
  generator.bindings = [{ id: "binding-derived", kind: "video", slot: 1, sourceNodeId: asset.id, sourceOutputHandle: "media", role: "camera" }];
  document.nodes.push(asset);

  const restored = studioAssetFromDocument(asset, document, "reference");

  assert.equal(restored.localUrl, "blob:derived-preview");
  assert.equal(restored.derivationId, "derive-17");
  assert.equal(restored.sourceJobId, "job-42");
  assert.equal(restored.role, "camera", "the target binding role must win over the asset fallback role");
  assert.deepEqual(restored.mediaMeta, { duration: 4.2, has_audio: true });
});

test("remote assets ignore stale blob URLs after refresh while unsaved job media keeps its local preview", () => {
  const createCanvasNode = documentFunction("createCanvasNode");
  const createDefaultCanvasDocument = documentFunction("createDefaultCanvasDocument");
  const document = createDefaultCanvasDocument(ids("remote-preview"));
  const remote = createCanvasNode("asset", { x: 0, y: 0 }, ids("remote-asset"));
  remote.mediaKind = "image";
  remote.asset = assetPayloadFromStudio({ media: "image", fileName: "saved.png", localUrl: "blob:http://localhost/stale", remoteId: "asset-remote", uploadState: "ready", role: "identity" });
  document.nodes.push(remote);
  const restoredRemote = studioAssetFromDocument(remote, document, "reference");
  assert.equal(restoredRemote.localUrl, "/api/assets/asset-remote/content");
  assert.equal(remote.asset.source.localUrl, undefined);

  const unsaved = createCanvasNode("asset", { x: 0, y: 0 }, ids("job-asset"));
  unsaved.mediaKind = "video";
  unsaved.asset = assetPayloadFromStudio({ media: "video", fileName: "job.mp4", localUrl: "/api/preview?id=job-1", sourceJobId: "job-1", uploadState: "ready", role: "motion" });
  document.nodes.push(unsaved);
  assert.equal(studioAssetFromDocument(unsaved, document, "reference").localUrl, "/api/preview?id=job-1");
});

test("workspace storage writes recovery first and the workspace key remains the only commit point", () => {
  const commit = workspaceFunction("commitCanvasWorkspaceStorage");
  const calls = [];
  const success = commit({ setItem(key, value) { calls.push([key, value]); } }, "workspace-v2", { key: "recovery", value: "snapshot" });
  assert.deepEqual(success, { ok: true });
  assert.deepEqual(calls.map(([key]) => key), ["recovery", workspaceModel.CANVAS_WORKSPACE_STORAGE_KEY]);

  const recoveryFailureCalls = [];
  const recoveryFailure = commit({ setItem(key) { recoveryFailureCalls.push(key); throw new Error("quota"); } }, "workspace-v2", { key: "recovery", value: "snapshot" });
  assert.deepEqual(recoveryFailure, { ok: false, stage: "recovery" });
  assert.deepEqual(recoveryFailureCalls, ["recovery"], "workspace must not be committed after a recovery write failure");

  const workspaceFailureCalls = [];
  const workspaceFailure = commit({ setItem(key) { workspaceFailureCalls.push(key); if (key === workspaceModel.CANVAS_WORKSPACE_STORAGE_KEY) throw new Error("quota"); } }, "workspace-v2", { key: "recovery", value: "snapshot" });
  assert.deepEqual(workspaceFailure, { ok: false, stage: "workspace" });
  assert.deepEqual(workspaceFailureCalls, ["recovery", workspaceModel.CANVAS_WORKSPACE_STORAGE_KEY]);
});

test("workspace parser quarantines partial corruption and duplicate ids without silently discarding raw entries", () => {
  const createDefaultCanvasDocument = documentFunction("createDefaultCanvasDocument");
  const createCanvasWorkspace = workspaceFunction("createCanvasWorkspace");
  const parseCanvasWorkspace = workspaceFunction("parseCanvasWorkspace");
  const workspace = createCanvasWorkspace(createDefaultCanvasDocument(ids("quarantine-node")), ids("quarantine"), () => 1);
  const valid = workspace.canvases[0];
  const raw = JSON.stringify({
    ...workspace,
    canvases: [
      valid,
      { id: "broken", title: "损坏画布", document: { version: 7, nodes: "bad", edges: [] }, updatedAt: 2 },
      { ...valid, title: "重复画布" },
    ],
  });

  const parsed = parseCanvasWorkspace(raw);

  assert.equal(parsed.ok, true);
  assert.equal(parsed.workspace.canvases.length, 1);
  assert.equal(parsed.issues.length, 2);
  assert.equal(parsed.workspace.quarantined.length, 2);
  assert.equal(parsed.workspace.quarantined[0].raw.title, "损坏画布");
  assert.equal(parsed.workspace.quarantined[1].reason, "画布 ID 重复");
});

test("workspace parser reports every quarantined item when no canvas remains recoverable", () => {
  const parseCanvasWorkspace = workspaceFunction("parseCanvasWorkspace");
  const parsed = parseCanvasWorkspace(JSON.stringify({ version: 1, activeCanvasId: "broken", canvases: [{ id: "broken", title: "损坏", document: null }] }));
  assert.equal(parsed.ok, false);
  assert.equal(parsed.issues.length, 1);
  assert.equal(parsed.issues[0].canvasId, "broken");
});

test("removing tabs chooses a valid fallback and can never remove the final canvas", () => {
  const createDefaultCanvasDocument = documentFunction("createDefaultCanvasDocument");
  const createCanvasWorkspace = workspaceFunction("createCanvasWorkspace");
  const addCanvasWorkspaceTab = workspaceFunction("addCanvasWorkspaceTab");
  const removeCanvasWorkspaceTab = workspaceFunction("removeCanvasWorkspaceTab");
  const firstOnly = createCanvasWorkspace(createDefaultCanvasDocument(ids("delete-node")), ids("delete"), () => 1);
  const firstId = firstOnly.activeCanvasId;
  const two = addCanvasWorkspaceTab(firstOnly, createDefaultCanvasDocument(ids("delete-new-node")), ids("delete-new"), () => 2);
  const secondId = two.activeCanvasId;

  const one = removeCanvasWorkspaceTab(two, secondId);
  assert.equal(workspaceCanvases(one).length, 1);
  assert.equal(one.activeCanvasId, firstId);
  const protectedLast = removeCanvasWorkspaceTab(one, firstId);
  assert.deepEqual(protectedLast, one, "deleting the last canvas must be a no-op");
  assert.equal(workspaceCanvases(protectedLast).length, 1);
  assert.equal(protectedLast.activeCanvasId, firstId);
});

test("Studio renders accessible canvas tabs and persists the workspace rather than only the active document", () => {
  const createTab = sourceBlock(studioSource, "function createCanvasTab", "function selectCanvasTab");
  const selectTab = sourceBlock(studioSource, "function selectCanvasTab", "function removeCanvasTab");
  assert.match(studioSource, /role=["{][^\n>]*tablist/);
  assert.match(studioSource, /role=["{][^\n>]*tab["}]/);
  assert.match(studioSource, /aria-selected=/);
  assert.match(studioSource, /ArrowLeft.*ArrowRight.*Home.*End/);
  assert.match(studioSource, /onKeyDown=\{\(event\) => handleCanvasTabKeyDown/);
  assert.match(studioSource, /aria-labelledby=\{canvasWorkspace \? canvasTabElementId/);
  assert.match(studioSource, /addCanvasWorkspaceTab|createCanvasTab|addWorkspaceCanvas/);
  assert.match(studioSource, /removeCanvasWorkspaceTab|removeCanvasTab|removeWorkspaceCanvas/);
  assert.match(studioSource, /parseCanvasWorkspace/);
  assert.match(studioSource, /serializeCanvasWorkspace/);
  assert.match(studioSource, /createCanvasWorkspace/, "legacy single-document storage must be wrapped in a workspace rather than overwritten");
  assert.ok(createTab.indexOf("flushWorkflowSnapshot()") < createTab.indexOf("addCanvasWorkspaceTab"), "new canvas creation must snapshot the outgoing canvas first");
  assert.ok(createTab.indexOf("addCanvasWorkspaceTab") < createTab.indexOf("restoreCanvasState"), "the newly-owned document must be hydrated after it is added");
  assert.ok(selectTab.indexOf("flushWorkflowSnapshot()") < selectTab.indexOf("restoreCanvasState"), "tab switching must snapshot the outgoing document before hydrating the target");
});

test("canvas hydration swaps the snapshot ref before changing active workspace ownership", () => {
  const restore = sourceBlock(studioSource, "const restoreCanvasState", "const selected =");
  const selectTab = sourceBlock(studioSource, "function selectCanvasTab", "function handleCanvasTabKeyDown");
  assert.match(restore, /workflowSnapshotRef\.current = structuredClone\(document\)/);
  assert.ok(selectTab.indexOf("restoreCanvasState(active.document)") < selectTab.indexOf("activeCanvasIdRef.current = canvasId"));
  assert.match(studioSource, /const persistCanvasWorkspace = useCallback/);
  assert.match(studioSource, /CANVAS_WORKSPACE_BACKUP_KEY/);
  assert.match(studioSource, /if \(!flushWorkflowSnapshot\(\)\) return false/);
  assert.match(studioSource, /if \(!flushWorkflowSnapshot\(\)\) return;/);
  assert.match(restore, /overviewPointerRef\.current = undefined/);
  assert.match(restore, /overviewGestureExtentRef\.current = undefined/);
  assert.match(restore, /setOverviewGestureExtent\(undefined\)/);
});

test("initial SSR defaults cannot autosave or pagehide-flush before restored workspace state commits", () => {
  const hydration = sourceBlock(studioSource, "useEffect(() => {\n    try {\n      const workspaceRaw", "useEffect(() => {\n    // The first render intentionally uses SSR-safe defaults");
  const autosave = sourceBlock(studioSource, "useEffect(() => {\n    // The first render intentionally uses SSR-safe defaults", "useEffect(() => {\n    const flushOnPageHide");
  const pagehide = sourceBlock(studioSource, "useEffect(() => {\n    const flushOnPageHide", "useEffect(() => () => {\n    if (dragFrameRef.current");
  assert.match(studioSource, /const \[workflowHydrated, setWorkflowHydrated\] = useState\(false\)/);
  assert.match(hydration, /restoreCanvasState\(active\.document\);\s*setWorkflowHydrated\(true\)/);
  assert.match(hydration, /restoreCanvasState\(document\);[\s\S]*?setWorkflowHydrated\(true\)/);
  assert.match(autosave, /if \(!workflowHydrated\) return;/);
  assert.match(pagehide, /if \(workflowHydrated\) flushWorkflowSnapshot\(\)/);
  assert.doesNotMatch(pagehide, /return \(\) => \{[\s\S]*?\n\s*flushWorkflowSnapshot\(\);/, "the initial effect cleanup must not flush SSR defaults");
});
