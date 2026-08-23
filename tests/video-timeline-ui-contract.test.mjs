import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../app/video-timeline.tsx", import.meta.url), "utf8");
const mentions = await readFile(new URL("../app/prompt-mentions.tsx", import.meta.url), "utf8");
const director = await readFile(new URL("../app/video-director-workspace.tsx", import.meta.url), "utf8");
const directorModel = await readFile(new URL("../app/video-director-model.ts", import.meta.url), "utf8");

test("long-video prompt uses stable asset mention composer without rewriting free text", () => {
  assert.match(source, /<PromptMentionComposer[\s\S]*onChange=\{\(prompt\) => setRequest\(\{ prompt \}\)\}/);
  assert.match(source, /onSelectItem=\{\(item\) => addReference\(item\.id\)\}/);
  assert.doesNotMatch(source, /translate|rewritePrompt|enhancePrompt/);
  assert.match(source, /@ 仅映射为下方 H3 标签，不改写创意语义/);
  assert.match(source, /只读提交模式/);
  assert.match(source, /H3 最终提示词预览（只读）/);
  assert.match(source, /H3_REFERENCE_PROMPT_TEMPLATE/);
  assert.doesNotMatch(source, /结构化 Prompt parts/);
});

test("long-video duration uses the shared H3 17k+5 options instead of a free number field", () => {
  assert.match(source, /profileDurationOptions\(profile\)/);
  assert.match(source, /\{duration\.toFixed\(2\)\} \u79d2 \u00b7 \{Math\.round\(duration \* 24\)\} \u5e27/);
  assert.doesNotMatch(source, /<input type="number"[^>]+value=\{request\.parameters\.duration\}/);
});

test("long-video sampling resolves a version-pinned trusted profile and survives in-flight polling", () => {
  assert.match(source, /\$\{profile\.id\}@\$\{profile\.version\}/);
  assert.match(source, /retargetSegmentSampling/);
  assert.match(source, /editRevisionRef\.current \+= 1/);
  assert.match(source, /revision === editRevisionRef\.current/);
  assert.match(source, /projectRef\.current = next/);
});

test("long-video result cards mount video bytes only after an explicit preview click", () => {
  const lazy = source.match(/function LazyVideoPreview[\s\S]*?\n\}/)?.[0] ?? "";
  assert.match(lazy, /thumbnailUrl \? <img/);
  assert.match(lazy, /onClick=\{\(\) => setLoaded\(true\)\}/);
  assert.match(lazy, /if \(loaded\)[\s\S]*<video/);
  assert.match(source, /thumbnailUrl=\{segment\.thumbnail_url\}/);
  assert.match(source, /thumbnailUrl=\{project\.merged\.thumbnail_url\}/);
});

test("asset picker and selected references show visual media while roles are not editable", () => {
  assert.match(source, /function AssetPicker/);
  assert.match(source, /<AssetThumb asset=\{asset\}/);
  assert.match(source, /asset\.kind === "audio" \? "\u266a"/);
  const row = source.match(/function ReferenceRow[\s\S]*?\n\}/)?.[0] ?? "";
  assert.match(row, /按媒体类型标签引用/);
  assert.match(row, /tags\?\.primary/);
  assert.match(row, /tags\?\.pairedAudio/);
  assert.doesNotMatch(row, /<select/);
  assert.match(row, /aria-label=\{`\u79fb\u9664/);
});

test("mention picker uses server thumbnails and static icons without loading video bodies", () => {
  assert.match(mentions, /item\.previewUrl \? <img/);
  assert.doesNotMatch(mentions, /<video|mediaUrl|preload="metadata"/);
  assert.match(mentions, /item\.kind === "video" \? "\u25b6"/);
  assert.match(mentions, /item\.kind === "audio" \? "\u266a"/);
  const thumb = source.match(/function AssetThumb[\s\S]*?\n\}/)?.[0] ?? "";
  assert.doesNotMatch(thumb, /<video|preload/);
  assert.match(thumb, /asset\.kind === "video" \? "\u25b6"/);
});

test("long-video drawer composes a maintainable Director workspace instead of another monolith", () => {
  assert.match(source, /import \{ SequenceVideoMonitor, SourceRangePanel, StoryboardTimeline \} from "\.\/video-director-workspace"/);
  assert.match(source, /<SequenceVideoMonitor/);
  assert.doesNotMatch(source, /<SourceVideoMonitor/);
  assert.match(source, /<StoryboardTimeline/);
  assert.match(source, /director-segment-inspector/);
  assert.match(director, /export function SequenceVideoMonitor/);
  assert.match(director, /export function SourceVideoMonitor/);
  assert.match(director, /export function StoryboardTimeline/);
  assert.match(director, /preload="metadata"/);
  assert.match(director, /setLoaded\(true\)/);
  assert.match(director, /帧 \{frame\}/);
});

test("storyboard selection shows an explicit dependency-complete plan and stays non-destructive", () => {
  assert.match(director, /aria-label={`选择运行分段/);
  assert.match(director, /全选/);
  assert.match(director, /反选/);
  assert.match(source, /selectedTimelineRunPlan\(project\.segments, runSelection\)/);
  assert.match(source, /API\.runSelected\(saved\.id!, runPlan\.map/);
  assert.match(source, /自动补齐前驱/);
  assert.match(source, /执行计划 · 按分段顺序/);
  assert.match(source, /fetch\("\/api\/media\/analyze-scenes"/);
  assert.match(source, /parseSceneCutSuggestions/);
  assert.match(director, /不会自动提交、派生或改写项目/);
  assert.match(directorModel, /splitSegmentDraftAtTime/);
  assert.match(directorModel, /equalizeSegmentDrafts/);
  assert.match(directorModel, /prompt_mode: "preserve_tags_only"/);
  assert.match(source, /director-selection-warning/);
});

test("continuation cards separate workflow mode from sampling and expose implicit media", () => {
  assert.match(source, /工作流模式/);
  assert.match(source, /采样档/);
  assert.match(source, /timelineWorkflowModeLabel\(segment\)/);
  assert.match(source, /retargetSegmentSampling\(segment, profiles, nextSampling\)/);
  assert.doesNotMatch(source, /<span>工作流 Profile<\/span>/);
  assert.match(source, /隐式首帧：分段 \{index\} 尾帧/);
  assert.match(source, /严格传递上一段尾帧作为下一段首帧输入；模型连续性需预览并按需重跑/);
  assert.doesNotMatch(source, /首尾帧连续/);
  assert.match(source, /可选目标尾帧 → Picture 2/);
  assert.match(source, /隐式视频：分段 \{index\}/);
  assert.match(source, /音频：关闭（默认）/);
  assert.match(source, /\/api\/assets\/\$\{encodeURIComponent\(continuationEvidence\.asset_id\)\}\/thumbnail/);
  assert.match(source, /合并门禁：/);
});

test("tail-frame copy guarantees are distinguished from model continuity claims", () => {
  assert.match(source, /严格传递上一段尾帧作为下一段首帧输入；模型连续性需预览并按需重跑/);
  assert.doesNotMatch(source, /画面边界严格连续/);
});

test("source video binding is durable frame-based data and cropped shots remain reusable without duplicate references", () => {
  assert.match(source, /buildStoryboardDraft/);
  assert.match(source, /storyboard: draft\.storyboard, segments: draft\.segments/);
  assert.match(source, /Math\.round\(sourceTime \* current\.storyboard\.fps\)/);
  assert.match(source, /operation: "video_trim"/);
  assert.match(source, /deriveLibraryMedia/);
  assert.match(source, /saveDerivedMedia/);
  assert.match(source, /onAssetCreated\(saved\)/);
  assert.match(source, /来源区间隐式引用该视频/);
  assert.match(source, /Prompt 和参考保持不变/);
  assert.match(director, /export function SourceRangePanel/);
  assert.match(director, /来源区间 · 已绑定/);
  assert.match(director, /裁剪并保存为资产/);
  assert.match(source, /source\.media\.source_fps \?\? source\.media\.fps/);
  assert.match(director, /source\?\.media\.source_fps \?\? source\?\.media\.fps/);
  assert.match(director, /Math\.abs\(player\.currentTime - next\) > 0\.5 \/ fps/);
  assert.match(director, /data-shot-index/);
  assert.match(director, /ArrowLeft/);
});

test("all explicit long-video references auto-retarget to Ref2VA without rewriting prompt", () => {
  assert.match(source, /retargetSegmentForReferences\(\{ \.\.\.segment, request: \{ \.\.\.request, references \} \}, profiles\)/);
  assert.match(source, /sourceDuration < 2/);
  assert.match(source, /不符合 H3 视频参考的 2\.\.15 秒范围/);
  assert.match(source, /尾帧续接只接受图像参考/);
  assert.match(source, /request\.references\.filter/);
  assert.match(source, /onRemove=\{\(\) => onChange\(retargetSegmentForReferences/);
});

test("source ranges are visible implicit Ref2VA inputs with reserved budget and no tail-frame mode", () => {
  assert.match(source, /timelinePromptPreview\(request\.prompt, request\.references, assetKinds, segment\.continuation, Boolean\(segment\.source_range\)\)/);
  assert.match(source, /\+ \(segment\.source_range \? 1 : 0\)/);
  assert.match(source, /timelineRequiredCompiler\(segment\)/);
  assert.match(source, /filter\(\(choice\) => !\(segment\.source_range && choice === "tail_frame"\)\)/);
  assert.match(director, /隐式 Ref2VA 视频/);
});
