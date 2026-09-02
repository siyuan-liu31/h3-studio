import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const [studio, timeline] = await Promise.all([
  readFile(new URL("../app/studio.tsx", import.meta.url), "utf8"),
  readFile(new URL("../app/video-timeline.tsx", import.meta.url), "utf8"),
]);

function functionBody(source, name, nextName) {
  const start = source.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `${name} must exist`);
  const end = nextName ? source.indexOf(`function ${nextName}`, start + 1) : source.length;
  assert.notEqual(end, -1, `${nextName} must follow ${name}`);
  return source.slice(start, end);
}

test("ordinary canvas references do not expose creative action/camera/pacing role selectors", () => {
  const roleOptions = studio.match(/const ROLE_OPTIONS[\s\S]*?const DEFAULT_ROLE/)?.[0] ?? "";
  const assetPreview = functionBody(studio, "AssetPreview", "MediaTools");

  assert.doesNotMatch(roleOptions, /video:\s*\[[^\]]*(?:\u52a8\u4f5c|\u8fd0\u955c|\u8282\u594f)/);
  assert.doesNotMatch(roleOptions, /audio:\s*\[[^\]]*\u8282\u594f/);
  assert.doesNotMatch(assetPreview, /ROLE_OPTIONS\[asset\.media\]/);
});

test("ordinary Ref2VA preview and generation delegate to the shared read-only prompt policy", () => {
  const generate = functionBody(studio, "generate", "cancelJob");
  const runGeneratorNode = functionBody(studio, "runGeneratorNode", "generate");
  const promptPreviewStart = studio.indexOf('fetch("/api/prompts/compile"');
  const promptPreview = studio.slice(Math.max(0, promptPreviewStart - 900), promptPreviewStart + 900);

  assert.match(generate, /buildGeneratorExecutionPlan\(document, nodeId\)/);
  assert.match(generate, /runGeneratorNode\(stepOutput, step\.nodeId, materializedAssets/);
  assert.match(runGeneratorNode, /promptModePayload\(output, outputPrompt\)/);
  assert.match(promptPreview, /promptModePayload\(generator, outputPrompt\)/);
  assert.match(studio, /open=\{selectedId === node\.id \|\| nodeCompile\.state === "error" \? true : undefined\}/);
  assert.match(studio, /H3 最终提示词预览（只读）/);
});

test("image surfaces use lazy thumbnail endpoints rather than eager original bytes", () => {
  const assetPreview = functionBody(studio, "AssetPreview", "MediaTools");
  const libraryAssetPreview = functionBody(studio, "LibraryAssetPreview", "AssetLibrary");
  const assetLibrary = functionBody(studio, "AssetLibrary", "ResultThumbnail");
  const resultThumbnail = functionBody(studio, "ResultThumbnail", "ResultLibrary");
  const resultLibrary = functionBody(studio, "ResultLibrary", "ParameterPanel");
  const mentionBuilder = studio.match(/const mentionItems = useCallback\([\s\S]*?\n\s*\}, \[[^\]]*\]\);/)?.[0] ?? "";

  assert.match(assetPreview, /const imageThumbnail = asset\.thumbnailUrl/);
  assert.match(assetPreview, /imagePreviewLoaded[\s\S]*?imageThumbnail && <img[^>]+loading="lazy"/);
  assert.doesNotMatch(assetPreview, /asset\.media === "image" && asset\.localUrl \? <img/);
  assert.match(assetPreview, /<img[^>]+loading="lazy"[^>]+decoding="async"/);
  assert.match(assetLibrary, /<LibraryAssetPreview item=\{item\}/);
  assert.match(libraryAssetPreview, /item\.thumbnailUrl/);
  assert.match(libraryAssetPreview, /loading="lazy"/);
  assert.doesNotMatch(libraryAssetPreview, /item\.kind === "image" \? item\.contentUrl/);
  assert.match(resultLibrary, /<ResultThumbnail job=\{item\}/);
  assert.match(resultThumbnail, /\/api\/jobs\/\$\{encodeURIComponent\(job\.id\)\}\/thumbnail\?index=0/);
  assert.match(resultThumbnail, /loading="lazy"/);
  assert.doesNotMatch(mentionBuilder, /asset\.media === "image" \? asset\.localUrl/);
});

test("job history is requested with a bounded server page instead of client-only slicing", () => {
  const loadHistory = studio.match(/const loadJobHistory = useCallback\([\s\S]*?\n\s*\}, \[\]\);/)?.[0] ?? "";

  assert.match(loadHistory, /\/api\/jobs\?/);
  assert.match(loadHistory, /limit\s*(?::|=)\s*"20"/);
  assert.doesNotMatch(loadHistory, /body\.jobs\) \? body\.jobs\.slice\(0,\s*20\)/);
});

test("continuation UI shows the final implicit tag and describes true tail extraction only", () => {
  const segment = functionBody(timeline, "SegmentCard", "AssetPicker");
  const tailStart = segment.indexOf('{segment.continuation === "tail_frame" && <div className="continuation-note"');
  const previousStart = segment.indexOf('{segment.continuation === "previous_video"', tailStart + 1);
  const referencesStart = segment.indexOf('<section className="timeline-references"', previousStart + 1);
  assert.notEqual(tailStart, -1, "tail-frame continuation note must exist");
  assert.notEqual(previousStart, -1, "previous-video continuation range editor must exist");
  assert.notEqual(referencesStart, -1, "reference section must follow continuation notes");
  const tailNote = segment.slice(tailStart, previousStart);
  const previousEditor = segment.slice(previousStart, referencesStart);

  assert.match(tailNote, /(?:\u6700\u540e\u53ef\u89e3\u7801|\u6700\u540e\u4e00\u4e2a\u53ef\u7528\u753b\u9762|\u771f\u5b9e\u5c3e\u5e27|\u6700\u540e\u4e00\u5e27)/);
  assert.match(tailNote, /Picture 1/);
  assert.doesNotMatch(tailNote, /\u4fdd\u7559\u4e3b\u4f53|preserve subject|\u5bf9\u9f50\u7ea6\u675f/);
  assert.match(segment, /const previousVideoTag = `<Video \$\{request\.references\.filter/);
  assert.match(previousEditor, /ContinuationRangeEditor/);
  assert.match(previousEditor, /videoTag=\{previousVideoTag\}/);
});

test("551d325 duplicate-edge protections remain at restore, interaction and submit boundaries", () => {
  assert.match(studio, /function dedupeEdges\(edges: Edge\[\]\)/);
  assert.match(studio, /setEdges\(dedupeEdges\(legacyEdgesFromDocument\(document\)\)\)/);
  assert.match(studio, /edge\.source === source\.id && edge\.target === target\.id\)\) return "\u8fd9\u4e24\u4e2a\u8282\u70b9\u5df2\u7ecf\u8fde\u63a5"/);
  assert.match(studio, /const transaction = connectMedia\(document, source\.id, target\.id/);
  assert.match(studio, /setEdges\(\(current\) => current\.some\(\(edge\) => edge\.source === source\.id && edge\.target === target\.id\) \? current/);
  assert.match(studio, /const relevantEdges = dedupeEdges\(edges\.filter/);
});
