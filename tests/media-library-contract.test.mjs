import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const [studio, library] = await Promise.all([
  readFile(new URL("../app/studio.tsx", import.meta.url), "utf8"),
  readFile(new URL("../app/studio-library.ts", import.meta.url), "utf8"),
]);

test("video results and reusable video assets use lazy server thumbnails", () => {
  assert.match(studio, /`\/api\/jobs\/\$\{encodeURIComponent\(jobId\)\}\/thumbnail\?index=0`/);
  assert.match(studio, /<ResultThumbnail job=\{item\}/);
  assert.match(studio, /loading="lazy" decoding="async"/);
  assert.doesNotMatch(studio.match(/function ResultLibrary\([\s\S]*?function ParameterPanel/)?.[0] ?? "", /<video/);
  assert.match(library, /`\/api\/assets\/\$\{encodeURIComponent\(id\)\}\/thumbnail`/);
});

test("asset library supports search, folders, rename and move", () => {
  assert.match(studio, /placeholder="搜索资产名称"/);
  assert.match(studio, /aria-label="按文件夹筛选"/);
  assert.match(library, /\/api\/asset-folders/);
  assert.match(library, /method: "PATCH"/);
  assert.match(library, /display_name/);
  assert.match(library, /folder_id/);
  assert.match(studio, />改名</);
  assert.match(studio, />＋ 建文件夹</);
});

test("assets and results expose persistent pinning, result batch deletion, and safe folder deletion", () => {
  assert.match(library, /export async function deleteLibraryFolder/);
  assert.match(library, /export async function updateDerivedMedia/);
  assert.match(library, /export async function updateJobResult/);
  assert.match(studio, /删除当前文件夹/);
  assert.match(studio, /不会删除任何资产/);
  const resultLibrary = studio.match(/function ResultLibrary\([\s\S]*?function ParameterPanel/)?.[0] ?? "";
  assert.match(resultLibrary, /多选管理/);
  assert.match(resultLibrary, /全选当前/);
  assert.match(resultLibrary, /Promise\.allSettled/);
  assert.match(resultLibrary, /删除所选/);
  assert.match(resultLibrary, /onPinDerived/);
  assert.match(resultLibrary, /取消置顶/);
});

test("media tools call the non-destructive derivation API and create a new canvas node", () => {
  assert.match(library, /"\/api\/media\/derive"/);
  for (const operation of ["video_trim", "frame", "audio_trim", "extract_audio", "remove_audio"]) {
    assert.match(library, new RegExp(`operation: "${operation}"`));
  }
  assert.match(studio, /获取首帧/);
  assert.match(studio, /获取尾帧/);
  assert.match(studio, /获取播放器当前帧/);
  assert.match(studio, /指定时间（高级）/);
  assert.match(studio, /裁剪视频/);
  assert.match(studio, /裁剪音频/);
  assert.match(studio, /分离音频/);
  assert.match(studio, /setNodes\(\(current\) => \[\.\.\.current, \{ id, kind: "asset"/);
  assert.match(library, /`\/api\/derivations\/\$\{encodeURIComponent\(id\)\}\/assets`/);
  assert.match(studio, /保存到资产/);
});

test("result and derivative nodes can be edited directly while receipts stay in results until explicit deletion", () => {
  assert.match(library, /export type MediaDeriveSource/);
  assert.match(library, /type: "job"; job_id: string/);
  assert.match(library, /type: "derivation"; receipt_id: string/);
  assert.match(library, /body: JSON\.stringify\(\{ source, \.\.\.request,[\s\S]*background: true/);
  assert.match(library, /\/api\/media-tasks\/\$\{encodeURIComponent\(taskId\)\}/);
  assert.match(studio, /取消处理/);
  assert.match(library, /method: "DELETE"/);
  assert.match(studio, /source\?\.sourceJobId[\s\S]*type: "job"/);
  assert.match(studio, /source\?\.derivationId[\s\S]*type: "derivation"/);
  assert.match(studio, /const deleteDerivedResult = useCallback/);
  assert.match(studio, /await deleteDerivedMedia\(derived\.id\)/);
  const removeNodeBody = studio.match(/const removeNode = useCallback\(\(node: StudioNode\) => \{([\s\S]*?)\n {2}\}, \[[^\]]*\]\);/)?.[1] ?? "";
  assert.doesNotMatch(removeNodeBody, /deleteDerivedMedia/, "deleting a node must retain the durable result receipt");
});

test("current-frame capture reads the mounted player and keeps manual time entry", () => {
  assert.match(studio, /ref=\{videoRef\}/);
  assert.match(studio, /getPlaybackTime=\{\(\) => videoRef\.current\?\.currentTime\}/);
  assert.match(studio, /const current = getPlaybackTime\(\)/);
  assert.match(studio, /operation: "frame", time: current/);
  assert.match(studio, /指定时间（高级）/);
});

test("library errors, restored save state and long-video workspace entry stay explicit", () => {
  assert.match(studio, /restoredSavedAssets/);
  assert.match(studio, /firstOutput[\s\S]*asset_id/);
  assert.match(studio, /confirmedSavedResultAssets/);
  assert.match(studio, /创建文件夹失败/);
  assert.match(studio, /资产改名失败/);
  assert.match(studio, /移动资产失败/);
  assert.match(studio, /aria-controls="video-timeline-drawer"/);
  assert.match(studio, /<span>长视频<\/span>/);
  assert.match(studio, /railPanel === "timeline" && <VideoTimeline/);
  assert.doesNotMatch(studio, /<aside className=\{?`?inspector/);
  assert.doesNotMatch(studio, /timeline-context-hidden/);
});

test("canvas audio references stay lightweight while library audio is explicitly playable", () => {
  const preview = studio.match(/function AssetPreview\([\s\S]*?function MediaTools/)?.[0] ?? "";
  assert.match(preview, /role="img" aria-label="音频素材"/);
  assert.match(preview, />♫</);
  assert.doesNotMatch(preview, /<audio/);
  const libraryAudio = studio.match(/function LazyAudioPlayer\([\s\S]*?function LibraryAssetPreview/)?.[0] ?? "";
  assert.match(libraryAudio, /activated && <audio/);
  assert.match(libraryAudio, /aria-label=\{`\$\{paused \? "播放" : "暂停"\} \$\{label\}`\}/);
});

test("studio never offers an automatic prompt rewrite action", () => {
  assert.doesNotMatch(studio, /\/api\/prompts\/(?:enhance|rewrite)/);
  assert.doesNotMatch(studio, /(?:智能|自动)(?:增强|改写|优化)提示词/);
});
