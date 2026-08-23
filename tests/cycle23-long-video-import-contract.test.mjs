import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { currentOriginApiUrl, serverJobToStudioJob } from "../app/studio-history.ts";
import { remoteAssetToLibraryItem } from "../app/studio-library.ts";

const [page, studio, timeline, workspace] = await Promise.all([
  readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
  readFile(new URL("../app/studio.tsx", import.meta.url), "utf8"),
  readFile(new URL("../app/video-timeline.tsx", import.meta.url), "utf8"),
  readFile(new URL("../app/video-director-workspace.tsx", import.meta.url), "utf8"),
]);

const longVideoSurface = `${timeline}\n${workspace}`;

test("the official long-video entry is the root Studio route and does not require a cache-version query", () => {
  assert.match(page, /<Studio\b/, "the root route must render the production Studio directly");
  assert.match(studio, /aria-controls="video-timeline-drawer"/, "the persistent Studio rail must expose the long-video workspace");
  assert.match(studio, /setRailPanel\([\s\S]*timeline|railPanel === "timeline"/, "opening long video must be application state, not a special URL");
  assert.doesNotMatch(`${page}\n${studio}\n${timeline}`, /(?:\?|&)v=|searchParams\.(?:get|has)\(["']v["']\)/, "a version query may not be required to reach the current UI");
});

test("the long-video source chooser exposes assets, completed results and a local-video file entry", () => {
  assert.match(longVideoSurface, /从资产选择视频|资产库视频/, "saved video assets remain a first-class source");
  assert.match(longVideoSurface, /生成结果|历史结果|从结果选择/, "completed generated videos must be selectable without visiting another drawer");
  assert.match(longVideoSurface, /本地文件|本地视频|从本地导入/, "the same chooser must explain the local import path");
  assert.match(longVideoSurface, /type="file"[^>]*accept="video\/\*"|accept="video\/\*"[^>]*type="file"/, "local import must use a video-only file input");
});

test("Studio supplies completed video history to long video instead of only sending saved assets", () => {
  assert.match(timeline, /type Props\s*=\s*\{[^}]*\b(?:results|jobs|history)\s*:/s, "VideoTimeline needs a typed result collection in addition to LibraryAsset[]");
  assert.match(studio, /<VideoTimeline\b[\s\S]*\b(?:results|jobs|history)=\{[^}]+\}/, "the Studio integration must pass its durable result history into the long-video workspace");
  assert.match(longVideoSurface, /status\s*===\s*"completed"[\s\S]*(?:media|output_type)\s*===?\s*"video"|(?:media|output_type)\s*===?\s*"video"[\s\S]*status\s*===\s*"completed"/, "the result source must exclude unfinished and non-video jobs");
});

test("result and local imports converge on a durable LibraryAsset before source binding", () => {
  assert.match(longVideoSurface, /onAssetCreated\(/, "newly materialized media must update the shared asset library");
  assert.match(longVideoSurface, /FormData\(|uploadAsset\(|\/api\/assets/, "a local File must be uploaded rather than retained only as a transient browser object URL");
  assert.match(longVideoSurface, /(?:job_id|sourceJobId|importJobOutput|\/api\/jobs\/)/, "a generated result must be materialized through its durable job identity");
  assert.match(longVideoSurface, /setSourceId\(|onSourceChange\?\.\(|bindSource\(/, "after materialization the imported asset must become the monitor/source selection");
});

test("asset and result media URLs are rebased to loadable same-origin API routes", () => {
  const id = "a".repeat(32);
  const asset = remoteAssetToLibraryItem({
    id,
    kind: "video",
    display_name: "source.mp4",
    content_url: "https://old-instance.invalid/api/assets/stale/content",
    thumbnail_url: "https://old-instance.invalid/api/assets/stale/thumbnail",
    media: { duration: 5.17, width: 1344, height: 768 },
  });
  assert.equal(asset?.contentUrl, `/api/assets/${id}/content`, "foreign asset URLs must fall back to the current asset id");
  assert.equal(asset?.thumbnailUrl, `/api/assets/${id}/thumbnail`);

  const job = serverJobToStudioJob({
    id: "generated-video",
    status: "completed",
    output_type: "video",
    preview_url: "http://old-instance.invalid/api/preview?id=generated-video&index=0",
    thumbnail_url: "http://old-instance.invalid/api/jobs/generated-video/thumbnail?index=0",
    download_url: "http://old-instance.invalid/api/download?id=generated-video&index=0",
  });
  assert.equal(job?.previewUrl, "/api/preview?id=generated-video&index=0");
  assert.equal(job?.thumbnailUrl, "/api/jobs/generated-video/thumbnail?index=0");
  assert.equal(job?.downloadUrl, "/api/download?id=generated-video&index=0");
  assert.equal(currentOriginApiUrl("javascript:alert(1)", "/api/fallback"), "/api/fallback");
});

test("the monitor consumes the selected asset content URL in an explicit metadata-load video", () => {
  assert.match(workspace, /selectedSource\?\.contentUrl|activeSource\?\.contentUrl/, "the selected imported asset must drive the monitor URL");
  assert.match(workspace, /<video[\s\S]*src=\{playableUrl\}[\s\S]*preload="metadata"[\s\S]*playsInline|<video[\s\S]*src=\{playableUrl\}[\s\S]*playsInline[\s\S]*preload="metadata"/, "the selected URL must be attached to a browser-loadable video element");
  assert.match(workspace, /onLoadedMetadata=/, "the UI must wait for browser metadata before trusting dimensions or duration");
});
