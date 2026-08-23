import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const studioPath = new URL("../app/studio.tsx", import.meta.url);

test("destructive keyboard handling is scoped to a deliberate active canvas selection", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /canvasInteractionActiveRef/);
  assert.match(source, /event\.key !== "Delete"|event\.key === "Delete"/);
  assert.doesNotMatch(source, /event\.key === "Backspace"/);
  assert.match(source, /event\.repeat/);
  assert.match(source, /!canvasInteractionActiveRef\.current/);
  assert.match(source, /setSelectedId\(""\)/);
  assert.doesNotMatch(source, /remaining\.find\(\(item\) => item\.kind === "video"\)/);

  const deleteHandler = source.match(/const handleNodeDelete = \(event: KeyboardEvent\) => \{([\s\S]*?)\n {4}\};/)?.[1] ?? "";
  assert.ok(deleteHandler, "Delete handler must remain explicit and auditable");
  const repeatGuard = deleteHandler.indexOf("event.repeat");
  const activeGuard = deleteHandler.indexOf("canvasInteractionActiveRef.current");
  const removal = deleteHandler.indexOf("removeNode(");
  assert.ok(repeatGuard >= 0 && repeatGuard < removal, "held Delete must not remove more than one node");
  assert.ok(activeGuard >= 0 && activeGuard < removal, "Delete outside the active canvas must be ignored");
});

test("clipboard image import respects prevented events, editors, mixed editor content, and canvas activation", async () => {
  const source = await readFile(studioPath, "utf8");

  const pasteHandler = source.match(/const handleClipboardPaste = \(event: ClipboardEvent\) => \{([\s\S]*?)\n {4}\};/)?.[1] ?? "";
  assert.ok(pasteHandler, "clipboard paste handler must remain explicit and auditable");
  assert.match(pasteHandler, /event\.defaultPrevented/);
  assert.match(pasteHandler, /canvasInteractionActiveRef\.current/);
  assert.match(pasteHandler, /isEditableEventTarget\(event\.target\)/);
  assert.match(source, /function isEditableEventTarget[\s\S]*?closest\("input, textarea, select, \[contenteditable='true'\]"\)/);
  assert.match(pasteHandler, /clipboardData\?\.items|clipboardData\.items/);
  assert.match(pasteHandler, /item\.kind === "file"/);
  assert.match(pasteHandler, /item\.type\.startsWith\("image\/"\)/);

  const imageScan = pasteHandler.search(/clipboardData\?\.items|clipboardData\.items/);
  const preventedGuard = pasteHandler.indexOf("event.defaultPrevented");
  const editableGuard = pasteHandler.indexOf("isEditableEventTarget(");
  const activeGuard = pasteHandler.indexOf("canvasInteractionActiveRef.current");
  const preventDefault = pasteHandler.indexOf("event.preventDefault()");
  const addFiles = pasteHandler.indexOf("addFiles(");
  assert.ok(preventedGuard >= 0 && preventedGuard < imageScan, "already-handled paste must be left untouched");
  assert.ok(editableGuard >= 0 && editableGuard < imageScan, "image+text pasted into an editor must remain editor content");
  assert.ok(activeGuard >= 0 && activeGuard < imageScan, "an inactive canvas must not intercept global paste");
  assert.ok(preventDefault > imageScan && addFiles > preventDefault, "only a discovered image may be consumed and added");
});

test("context menu implements conventional wrapped keyboard navigation", async () => {
  const source = await readFile(studioPath, "utf8");
  const menuStart = source.indexOf("function CanvasContextMenu(");
  const menuEnd = source.indexOf("function PromptEditor(", menuStart);
  const menuBody = menuStart >= 0 && menuEnd > menuStart ? source.slice(menuStart, menuEnd) : "";

  assert.ok(menuBody, "CanvasContextMenu must be present");
  for (const key of ["ArrowDown", "ArrowUp", "Home", "End"]) {
    assert.match(menuBody, new RegExp(`"${key}"`));
  }
  assert.match(menuBody, /button:not\(:disabled\)/);
  assert.match(menuBody, /\.focus\(\)/);
  assert.match(menuBody, /preventDefault\(\)/);
});

test("node controls retain their native context menu while the node surface uses the custom menu", async () => {
  const source = await readFile(studioPath, "utf8");
  const handler = source.match(/function openNodeContextMenu\([^)]*\) \{([\s\S]*?)\n {2}\}/)?.[1] ?? "";

  assert.ok(handler, "node context-menu handler must be present");
  assert.match(handler, /(?:isEditableEventTarget|isNativeContextMenuTarget|isInteractiveContextTarget|closest\()/);
  for (const selector of ["input", "textarea", "select", "contenteditable", "a", "button"]) {
    assert.match(source, new RegExp(selector));
  }
  const nativeGuard = handler.search(/(?:isEditableEventTarget|isNativeContextMenuTarget|isInteractiveContextTarget|closest\()/);
  const preventDefault = handler.indexOf("event.preventDefault()");
  const openMenu = handler.indexOf("setContextMenu(");
  assert.ok(nativeGuard >= 0 && nativeGuard < preventDefault, "native controls must return before preventDefault");
  assert.ok(preventDefault >= 0 && openMenu > preventDefault, "right-clicking the node surface must still open the custom menu");
  assert.match(source, /onContextMenu=\{\(event\) => openNodeContextMenu\(event, node\)\}/);
});

test("keyboard users can open the canvas create menu and closing it restores focus", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /className="node-canvas"[^>]*tabIndex=\{?0\}?/);
  assert.match(source, /className="node-canvas"[^>]*onKeyDown=/);
  assert.match(source, /event\.key === "ContextMenu"|"ContextMenu"/);
  assert.match(source, /event\.key === "F10"|"F10"/);
  assert.match(source, /event\.shiftKey/);

  const keyboardHandler = source.match(/(?:function|const) (?:openCanvasContextMenuFromKeyboard|handleCanvasKeyDown)[\s\S]*?\{([\s\S]*?)\n {2}\}/)?.[1] ?? "";
  assert.ok(keyboardHandler, "canvas keyboard context-menu handler must be explicit");
  assert.match(keyboardHandler, /preventDefault\(\)/);
  assert.match(keyboardHandler, /kind:\s*"canvas"/);

  assert.match(source, /(?:contextMenuInvokerRef|contextMenuReturnFocusRef|returnFocusTo|invoker)/);
  const closeHandler = source.match(/(?:function|const) (?:closeContextMenu|dismissContextMenu)[\s\S]*?\{([\s\S]*?)\n {2}\}/)?.[1] ?? "";
  assert.ok(closeHandler, "menu close must use one focus-restoring path");
  assert.match(closeHandler, /\.focus\(\)/);
  assert.match(source, /onClose=\{(?:closeContextMenu|dismissContextMenu)\}/);
});
