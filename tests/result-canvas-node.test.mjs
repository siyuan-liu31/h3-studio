import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../app/studio.tsx", import.meta.url), "utf8");

test("completed results stay in results until an explicit save or add action", () => {
  const body = source.match(/const importJobOutput = useCallback\(async \(jobId: string\)[\s\S]*?\n {2}\}, \[\]\);/)?.[0] ?? "";

  assert.match(body, /fetch\(`\/api\/jobs\/\$\{encodeURIComponent\(jobId\)\}\/assets`/);
  assert.match(body, /body: JSON\.stringify\(\{ index: 0 \}\)/);
  assert.match(body, /remoteAssetToLibraryItem\(data\.asset \?\? data\)/);
  assert.match(body, /setAssetLibrary/);
  assert.doesNotMatch(body, /addLibraryAsset\(library\)/);
  assert.match(source, /onSave=\{saveResultToAssets\}/);
  assert.match(source, />保存到资产</);
  const addBody = source.match(/const addResultToCanvas = useCallback\([\s\S]*?\n {2}\}, \[addLibraryAsset, assetLibrary, savedResultAssets\]\);/)?.[0] ?? "";
  assert.doesNotMatch(addBody, /importJobOutput|saveResultToAssets|setAssetLibrary/);
  assert.match(addBody, /sourceJobId: jobId/);
});

test("each result action creates a fresh unsaved node retaining its durable job id", () => {
  const addLibraryStart = source.indexOf("const addLibraryAsset = useCallback");
  const addLibraryEnd = source.indexOf("const importJobOutput = useCallback", addLibraryStart);
  const addLibraryBody = source.slice(addLibraryStart, addLibraryEnd);

  assert.notEqual(addLibraryStart, -1, "addLibraryAsset must exist");
  assert.notEqual(addLibraryEnd, -1, "addLibraryAsset must end before importJobOutput");
  assert.match(addLibraryBody, /`library-\$\{item\.id\}-\$\{crypto\.randomUUID\(\)\}`/);
  assert.match(addLibraryBody, /remoteId: item\.id/);
  assert.match(source, /`result-\$\{jobId\}-\$\{crypto\.randomUUID\(\)\}`/);
  assert.match(source, /sourceJobId: jobId/);
  assert.match(source, /onAdd=\{addResultToCanvas\}/);
  assert.match(source, /aria-label=\{`将任务 \$\{item\.id\} 的\$\{item\.media === "image" \? "图片" : "视频"\}添加到画布`\}/);
  assert.match(source, /＋ 添加到画布/);
});
