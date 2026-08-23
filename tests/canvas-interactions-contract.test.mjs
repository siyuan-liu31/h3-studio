import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const studioPath = new URL("../app/studio.tsx", import.meta.url);

test("canvas and nodes expose an accessible custom context menu for creation and deletion", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /type CanvasContextMenuState/);
  assert.match(source, /const \[contextMenu, setContextMenu\] = useState<CanvasContextMenuState/);
  assert.match(source, /function openCanvasContextMenu\(/);
  assert.match(source, /function openNodeContextMenu\(/);
  assert.match(source, /event\.preventDefault\(\)/);
  assert.match(source, /onContextMenu=\{openCanvasContextMenu\}/);
  assert.match(source, /onContextMenu=\{\(event\) => openNodeContextMenu\(event, node\)\}/);
  assert.match(source, /<CanvasContextMenu[\s\S]*contextMenu/);
  assert.match(source, /role="menu"/);
  assert.match(source, /role="menuitem"/);
  assert.match(source, /function createNode\(kind: CoreNodeKind, position: XY\)/);
  assert.match(source, />[^<]*(?:新建|创建)[^<]*</);
  assert.match(source, />[^<]*删除节点[^<]*</);
});

test("Delete removes the selected node and attached edges without hijacking editors", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /selectedId === node\.id \? "selected" : ""/);
  assert.match(source, /addEventListener\("keydown"/);
  assert.match(source, /(?:deletePressed = )?event\.key === "Delete"/);
  assert.doesNotMatch(source, /event\.key === "Backspace"/);
  assert.match(source, /input, textarea, select, \[contenteditable='true'\]/);
  assert.match(source, /removeNode\(/);

  const removeNodeBody = source.match(/const removeNode = useCallback\(\(node: StudioNode\) => \{([\s\S]*?)\n {2}\}, \[[^\]]*\]\);/)?.[1]
    ?? source.match(/function removeNode\(node: StudioNode\) \{([\s\S]*?)\n {2}\}/)?.[1]
    ?? "";
  assert.ok(removeNodeBody, "removeNode implementation must be present");
  assert.doesNotMatch(removeNodeBody, /node\.kind !== "asset"/, "core nodes must be removable as well as asset nodes");
  assert.match(removeNodeBody, /filter\(\(item\) => item\.id !== node\.id\)/);
  assert.match(removeNodeBody, /edge\.source !== node\.id && edge\.target !== node\.id/);
});

test("results expose separate save and add actions, with a fresh canvas node every time", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /const saveResultToAssets = useCallback\(async \(result: Job\)/);
  assert.match(source, /`\/api\/jobs\/\$\{encodeURIComponent\(jobId\)\}\/assets`/);
  assert.match(source, /method:\s*"POST"/);
  assert.match(source, /body:\s*JSON\.stringify\(\{\s*index:\s*0\s*\}\)/);
  assert.match(source, /remoteAssetToLibraryItem\(/);
  assert.match(source, /addLibraryAsset\(/);
  assert.match(source, /<ResultLibrary[\s\S]*onAdd=\{addResultToCanvas\}[\s\S]*onSave=\{saveResultToAssets\}/);
  assert.match(source, /function ResultLibrary\(\{ jobs, derivedResults,[^}]*savedResultAssets, currentId,[^}]*onSelect, onAdd, onSave, onDelete, onAddDerived, onSaveDerived, onDeleteDerived, onClose \}/);
  assert.match(source, /await onSave\(item\)/);
  assert.match(source, /await onDelete\(item\)/);
  assert.match(source, /aria-label=\{`[^`]*\$\{item\.id\}[^`]*添加到画布`\}/);
  assert.match(source, />保存到资产</);

  const addResultBody = source.match(/const addResultToCanvas = useCallback\([\s\S]*?\n {2}\}, \[addLibraryAsset, assetLibrary, savedResultAssets\]\);/)?.[0] ?? "";
  assert.match(addResultBody, /crypto\.randomUUID\(\)/, "repeated result additions need unique canvas node IDs");
  assert.match(addResultBody, /setNodes\(\(current\) => \[\.\.\.current,/);
  assert.match(addResultBody, /kind:\s*"asset"/);
  assert.match(addResultBody, /sourceJobId:\s*jobId/);
  assert.doesNotMatch(addResultBody, /importJobOutput|setAssetLibrary/, "adding to canvas must not implicitly save the result as an asset");
});

test("derived media is a durable result, supports context-menu saving, and node previews cannot trigger file import", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /listDerivedMedia\(controller\.signal\)/);
  assert.match(source, /setDerivedResults/);
  assert.match(source, /addDerivedResultToCanvas\(derived, nodeId\)/);
  assert.match(source, /onSaveAsset=.*saveDerivedNode/);
  assert.match(source, />保存到资产</);
  assert.match(source, /dataTransferContainsFiles\(event\.dataTransfer\)/);
  assert.match(source, /onDragOver=\{handleCanvasDragOver\}/);
  assert.match(source, /onDragStart=\{\(event\) => event\.preventDefault\(\)\}/);
  assert.match(source, /draggable=\{false\}/);

  const removeNodeBody = source.match(/const removeNode = useCallback\(\(node: StudioNode\) => \{([\s\S]*?)\n {2}\}, \[[^\]]*\]\);/)?.[1] ?? "";
  assert.doesNotMatch(removeNodeBody, /deleteDerivedMedia/, "removing a canvas node must retain its result receipt");
  assert.match(source, /const deleteDerivedResult = useCallback/);
  assert.match(source, /await deleteDerivedMedia\(derived\.id\)/);
});

test("pasting clipboard images creates asset nodes without swallowing ordinary text paste", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /addEventListener\("paste"/);
  assert.match(source, /clipboardData\?\.items|clipboardData\.items/);
  assert.match(source, /item\.kind === "file"/);
  assert.match(source, /item\.type\.startsWith\("image\/"\)/);
  assert.match(source, /item\.getAsFile\(\)/);
  assert.match(source, /if \(!(?:file|imageFile|imageFiles|files)(?:\.length)?\) return;/);

  const pasteHandler = source.match(/const handle(?:Clipboard)?Paste = \(event: ClipboardEvent\) => \{([\s\S]*?)\n {4}\};/)?.[1] ?? "";
  assert.ok(pasteHandler, "paste handler must be present");
  const emptyGuard = pasteHandler.search(/if \(![^)]+(?:\.length)?\) return/);
  const preventDefault = pasteHandler.indexOf("event.preventDefault()");
  const addFilesCall = pasteHandler.indexOf("addFiles(");
  assert.ok(emptyGuard >= 0, "non-image clipboard data must return early");
  assert.ok(preventDefault > emptyGuard, "plain text paste must not be prevented");
  assert.ok(addFilesCall > preventDefault, "image files must flow through the normal asset creation path");
  assert.match(source, /removeEventListener\("paste"/);
});
