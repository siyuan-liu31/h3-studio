import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const [studio, styles] = await Promise.all([
  readFile(new URL("../app/studio.tsx", import.meta.url), "utf8"),
  readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
]);

function between(source, start, end) {
  const from = source.indexOf(start);
  assert.notEqual(from, -1, `missing source marker: ${start}`);
  const to = source.indexOf(end, from + start.length);
  assert.notEqual(to, -1, `missing source marker: ${end}`);
  return source.slice(from, to);
}

test("result thumbnails retry transient gateway failures and recover after a cooldown", () => {
  const thumbnail = between(studio, "function ResultThumbnail(", "function ResultLibrary(");
  assert.match(thumbnail, /\/api\/jobs\/\$\{encodeURIComponent\(job\.id\)\}\/thumbnail\?index=0/);
  assert.match(thumbnail, /retry < 2/);
  assert.match(thumbnail, /setCoolingDown\(true\)/);
  assert.match(thumbnail, /12_000/);
  assert.match(thumbnail, /recoveryCycle >= 4/, "permanently missing thumbnails must stop automatic retry rounds");
  assert.match(thumbnail, /setCoolingDown\(false\)/);
  assert.match(thumbnail, /\u7f29\u7565\u56fe\u6062\u590d\u4e2d/);
});

test("result cards expose the primary media actions and explicit deletion", () => {
  const resultLibrary = between(studio, "function ResultLibrary(", "function ParameterPanel(");
  assert.match(resultLibrary, /className="result-library-actions"/);
  assert.match(resultLibrary, /\uff0b \u6dfb\u52a0\u5230\u753b\u5e03/);
  assert.match(resultLibrary, /\u4fdd\u5b58\u5230\u8d44\u4ea7/);
  assert.match(resultLibrary, /<DownloadAnchor href=\{item\.downloadUrl\}/);
  assert.match(resultLibrary, /删除结果/);
  assert.match(resultLibrary, /window\.confirm/);
  assert.doesNotMatch(resultLibrary, /<ActualWorkflowActions job=\{item\}/);
  assert.match(resultLibrary, /aria-label=\{`\u5c06\u4efb\u52a1 \$\{item\.id\} \u7684\$\{item\.media === "image" \? "\u56fe\u7247" : "\u89c6\u9891"\}\u4fdd\u5b58到资产`\}/);
});

test("result preview resolves and focuses the active canvas output UUID", () => {
  const preview = between(studio, "const previewResultOnCanvas", "const deriveAssetNode");
  assert.match(preview, /resolveResultPreviewTarget\(/);
  assert.match(preview, /setJobForNode\(target\.generatorId, result\)/);
  assert.match(preview, /setSelectedId\(target\.outputId\)/);
  assert.match(preview, /focusCanvasNode\(target\.outputId\)/);
  assert.doesNotMatch(preview, /setSelectedId\("output"\)/);
  assert.doesNotMatch(preview, /nodes\.find\(\(node\) => node\.kind === "output"\)\?\.id/, "preview must not fall back to an unrelated Output");
  assert.match(preview, /当前同类型生成节点尚未连接 Output/);
  assert.match(studio, /onSelect=\{previewResultOnCanvas\}/);
});

test("result actions use compact responsive proportions", () => {
  assert.match(styles, /\.result-library-actions\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/s);
  assert.match(styles, /\.result-library-actions a,\s*\.result-library-actions button\s*\{[^}]*min-height:\s*32px/s);
  assert.match(styles, /@media \(max-width:\s*680px\)[\s\S]*\.result-library-actions a,\s*\.result-library-actions button\s*\{\s*min-height:\s*40px;/);
  assert.match(styles, /@media \(max-width:\s*680px\)[\s\S]*\.result-library-actions a,\s*\.result-library-actions button\s*\{[^}]*font-size:\s*11px;/);
});

test("assets and results expose guarded destructive actions", () => {
  const assetLibrary = between(studio, "function AssetLibrary(", "function ResultThumbnail(");
  const resultLibrary = between(studio, "function ResultLibrary(", "function ParameterPanel(");
  const deletion = between(studio, "const deleteResult", "useEffect(() => {\n    const handleNodeDelete");
  assert.match(assetLibrary, /window\.confirm/);
  assert.match(assetLibrary, /await onDelete\(item\)/);
  assert.match(assetLibrary, /删除资产/);
  assert.match(resultLibrary, /window\.confirm/);
  assert.match(resultLibrary, /await onDelete\(item\)/);
  assert.match(resultLibrary, /删除结果/);
  assert.match(deletion, /\/api\/jobs\/\$\{encodeURIComponent\(jobId\)\}[\s\S]{0,100}method:\s*"DELETE"/);
});

test("result deletion is optimistic, polling-safe, rollback-capable and idempotent", () => {
  const deletion = between(studio, "const deleteResult", "useEffect(() => {\n    const handleNodeDelete");
  assert.match(studio, /const deletedJobIdsRef = useRef\(new Set<string>\(\)\)/);
  assert.match(deletion, /deletedJobIdsRef\.current\.add\(jobId\)/);
  assert.match(deletion, /setJobHistory\(\(current\) => current\.filter/);
  assert.match(deletion, /response\.status !== 404/);
  assert.match(deletion, /deletedJobIdsRef\.current\.delete\(jobId\)/);
  assert.match(deletion, /mergeJobHistory\(current, item, 100\)/);
  assert.match(studio, /remoteJobs = receipts[\s\S]{0,320}!deletedJobIdsRef\.current\.has/);
  assert.match(studio, /results:\s*"1"/);
  assert.match(studio, /runtime\.job\.id && trustedJobIdsRef\.current\.has\(runtime\.job\.id\)/);
  assert.doesNotMatch(studio, /Object\.values\(generatorStates\)\.forEach/);
});

test("Output keeps download but omits actual-workflow buttons", () => {
  const preview = between(studio, "function OutputPreview(", "function ActualWorkflowActions(");
  const history = between(studio, "function TaskHistory(", "function AssetLibrary(");
  assert.match(preview, /<DownloadAnchor/);
  assert.doesNotMatch(preview, /<ActualWorkflowActions/);
  assert.match(history, /aria-label=\{`下载任务/);
  assert.doesNotMatch(history, /<ActualWorkflowActions/);
});
