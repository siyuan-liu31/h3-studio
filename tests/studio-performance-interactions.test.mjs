import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const studioPath = new URL("../app/studio.tsx", import.meta.url);
const historyPath = new URL("../app/studio-history.ts", import.meta.url);
const proxyPath = new URL("../app/api/[...path]/route.ts", import.meta.url);
const promptMentionsPath = new URL("../app/prompt-mentions.tsx", import.meta.url);
const globalStylesPath = new URL("../app/globals.css", import.meta.url);

function between(source, start, end) {
  const from = source.indexOf(start);
  assert.notEqual(from, -1, `missing source marker: ${start}`);
  const to = source.indexOf(end, from + start.length);
  assert.notEqual(to, -1, `missing source marker: ${end}`);
  return source.slice(from, to);
}

test("result drawer bounds mounted previews and never eagerly fetches every video's metadata", async () => {
  const source = await readFile(studioPath, "utf8");
  const resultLibrary = between(source, "function ResultLibrary(", "function ParameterPanel(");
  const resultThumbnail = between(source, "function ResultThumbnail(", "function ResultLibrary(");

  assert.doesNotMatch(
    resultLibrary,
    /<video\b[^>]*\bpreload="(?:auto|metadata)"/,
    "opening history must not make every completed video issue a metadata/range request",
  );
  if (/<video\b/.test(resultLibrary)) {
    assert.match(resultLibrary, /<video\b[^>]*\bpreload="none"/);
  }
  assert.match(resultThumbnail, /<img\b[^>]*\bloading="lazy"[^>]*\bdecoding="async"|<img\b[^>]*\bdecoding="async"[^>]*\bloading="lazy"/);
  assert.match(resultLibrary, /<ResultThumbnail job=\{item\}/);
  assert.match(resultThumbnail, /thumb_retry=\$\{retry\}/, "transient thumbnail failures receive a bounded cache-busted retry");
  assert.match(resultThumbnail, /retry < 2/, "thumbnail retries are bounded");

  assert.match(
    resultLibrary,
    /\.slice\(0,\s*(?:visible|displayed|shown|result)[A-Za-z]*(?:Count|Limit|End)?\)/i,
    "only a bounded first page of result cards should be mounted",
  );
  assert.match(
    resultLibrary,
    /(?:加载更多|显示更多|load more)/i,
    "the bounded list needs an explicit way to reveal another page",
  );
  assert.match(resultLibrary, /onClick=\{\(\) => onSelect\(item\)\}/, "preview remains a real click action");
  assert.match(resultLibrary, /onClick=\{\(\) => (?:void )?(?:onAdd|addToCanvas)\(item\)\}/, "add-to-canvas remains a real click action");
  assert.match(resultLibrary, /<DownloadAnchor\b[^>]*href=\{item\.downloadUrl\}/, "download remains an actionable control");
  assert.match(source, /function DownloadAnchor\([\s\S]*?document\.createElement\("a"\)[\s\S]*?anchor\.href = href[\s\S]*?anchor\.download = ""/, "downloads retain a native browser fallback");
  assert.match(source, /response\.body\.getReader\(\)/, "supported browsers stream downloads directly to disk");
});

test("result preview play icon and caption use independent positioning hooks", async () => {
  const source = await readFile(studioPath, "utf8");
  const styles = await readFile(globalStylesPath, "utf8");
  const resultLibrary = between(source, "function ResultLibrary(", "function ParameterPanel(");

  assert.match(source, /thumbnail_recipe=2/, "a renderer recipe query must invalidate immutable legacy thumbnails");
  assert.match(resultLibrary, /className="result-library-preview-label">在画布预览/);
  assert.match(styles, /\.result-library-preview-label\s*\{/);
  assert.doesNotMatch(
    styles,
    /\.result-library-preview\s*>\s*span\s*\{/,
    "the caption rule must not capture and displace the centered play overlay",
  );
  assert.match(styles, /\.library-video-overlay\s*\{[^}]*left:\s*50%[^}]*top:\s*50%[^}]*translate:\s*-50%\s+-50%/s);
});

test("workflow persistence is debounced so pointer movement does not synchronously serialize local storage", async () => {
  const source = await readFile(studioPath, "utf8");
  assert.match(source, /localStorage\.setItem\(STORAGE_KEY/, "workflow persistence must remain enabled");
  assert.match(source, /workflowSaveTimerRef\s*=\s*useRef/);
  assert.match(
    source,
    /if \(workflowSaveTimerRef\.current !== undefined\) window\.clearTimeout\(workflowSaveTimerRef\.current\);[\s\S]{0,180}workflowSaveTimerRef\.current = window\.setTimeout\(/,
    "storage writes should be cancelled and rescheduled until interaction settles",
  );
});

test("media controls and result actions cannot accidentally start a node drag", async () => {
  const source = await readFile(studioPath, "utf8");
  const startDrag = between(source, "function startDrag(", "function moveDrag(");

  const selector = startDrag.match(/\.closest\((['"`])([\s\S]*?)\1\)/)?.[2]
    ?? source.match(/const NON_DRAGGABLE_SELECTOR\s*=\s*(['"`])([\s\S]*?)\1/)?.[2]
    ?? "";
  assert.ok(selector, "startDrag must use an interactive-descendant guard");
  for (const interactive of ["button", "input", "textarea", "select", "a", "video", "audio"]) {
    assert.match(selector, new RegExp(`(?:^|,)\\s*${interactive}(?:\\b|$)`), `${interactive} must be excluded from node drag capture`);
  }
  assert.match(source, /<video[^>]*\bcontrols\b/, "the output player keeps native media controls");
  assert.match(source, /<DownloadAnchor[^>]*className="download-button"[^>]*href=\{job\.downloadUrl \?\? job\.previewUrl\}/);
});

test("remote video asset nodes mount their player only after an explicit preview action", async () => {
  const source = await readFile(studioPath, "utf8");
  const assetPreview = between(source, "function AssetPreview(", "function OutputPreview(");

  assert.doesNotMatch(
    assetPreview,
    /asset\.media === "video" && asset\.localUrl \? <video/,
    "restoring result videos onto the canvas must not immediately fetch metadata for every node",
  );
  assert.match(assetPreview, /(?:加载|播放|打开)(?:视频)?预览|(?:load|play|open) video preview/i);
  assert.match(assetPreview, /onClick=\{[^}]*set[A-Za-z]*(?:Video|Preview)[A-Za-z]*\(/);
  assert.match(
    assetPreview,
    /(?:video|preview)[A-Za-z]*(?:Active|Loaded|Open|Visible)[\s\S]{0,500}<video\b/i,
    "the player must be gated by per-node preview state",
  );
  assert.match(assetPreview, /<video\b[^>]*\bcontrols\b/, "the explicitly loaded asset preview remains usable");
});

test("node drag rendering is animation-frame coalesced and pending work is safely cancelled", async () => {
  const source = await readFile(studioPath, "utf8");
  const moveDrag = between(source, "function moveDrag(", "function beginConnection(");

  assert.doesNotMatch(
    moveDrag,
    /setNodes\(\(current\)/,
    "every raw pointermove must not synchronously rebuild the full node graph",
  );
  assert.match(
    source,
    /(?:drag|position|move)[A-Za-z]*Frame[A-Za-z]*Ref\s*=\s*useRef/i,
    "drag scheduling needs a stable animation-frame ref",
  );
  assert.match(source, /requestAnimationFrame\(/);
  assert.match(source, /cancelAnimationFrame\(/);
  assert.match(
    source,
    /(?:(?:pending|queued|next)[A-Za-z]*(?:Drag|Position|Move)|dragPointer)[A-Za-z]*Ref\s*=\s*useRef/i,
    "the latest pointer position must replace older unpainted positions",
  );
  assert.match(source, /onPointerUp=\{finishCanvasPan\}/, "pointer release must enter the shared pan/drag cleanup path");
  assert.match(source, /if \(!pan\) finishDrag\(\)/, "the shared release handler must flush the drag scheduler when no pan is active");
  assert.match(
    source,
    /(?:return \(\) =>|useEffect\(\(\) => \(\) =>)[\s\S]{0,300}cancelAnimationFrame/i,
    "unmount must cancel a scheduled drag frame",
  );
});

test("result downloads keep the authenticated same-origin API route end to end", async () => {
  const [studio, history, proxy] = await Promise.all([
    readFile(studioPath, "utf8"),
    readFile(historyPath, "utf8"),
    readFile(proxyPath, "utf8"),
  ]);

  assert.match(history, /`\/api\/download\?id=\$\{encodeURIComponent\(id\)\}&index=0`/);
  assert.match(history, /`\/api\/preview\?id=\$\{encodeURIComponent\(id\)\}&index=0`/);
  assert.match(proxy, /process\.env\.H3_STUDIO_PROXY_API_KEY/);
  assert.match(proxy, /headers\.set\("X-API-Key", apiKey\)/);
  assert.match(studio, /<DownloadAnchor[^>]*className="download-button"[^>]*href=\{job\.downloadUrl \?\? job\.previewUrl\}/);
  assert.match(studio, /<DownloadAnchor[^>]*href=\{item\.downloadUrl\}[^>]*ariaLabel=\{`下载任务 \$\{item\.id\}`\}/);
  assert.match(studio, /function DownloadAnchor\([\s\S]*?document\.createElement\("a"\)[\s\S]*?anchor\.href = href[\s\S]*?anchor\.download = ""/);
  assert.match(studio, /response\.body\.getReader\(\)/);
});

test("the @ asset picker represents videos without eagerly loading their metadata", async () => {
  const composer = await readFile(promptMentionsPath, "utf8");

  assert.doesNotMatch(composer, /\.preload\s*=\s*"metadata"|preload="metadata"/);
  assert.doesNotMatch(
    composer,
    /item\.kind === "video"[^\n]{0,180}<video\b/,
    "opening an @ picker must not mount a player for every remote video",
  );
  assert.match(composer, /item\.kind === "video" \? "\u25b6"/);
});

test("job history uses the list receipt directly and coalesces overlapping refreshes", async () => {
  const source = await readFile(studioPath, "utf8");
  const historyLoader = between(source, "const loadJobHistory", "const handleTimelineResultCreated");

  assert.match(historyLoader, /limit:\s*"20",\s*summary:\s*"1",\s*results:\s*"1"/);
  assert.doesNotMatch(historyLoader, /\/api\/status/, "history paint must not wait for per-job status hydration");
  assert.match(historyLoader, /cache:\s*"no-cache"/);
  assert.match(historyLoader, /AbortSignal\.timeout\(12_000\)/);
  assert.match(source, /(?:jobHistory|history)[A-Za-z]*(?:Request|Load|Flight)[A-Za-z]*Ref\s*=\s*useRef/i);
  assert.match(
    historyLoader,
    /(?:Request|Load|Flight)[A-Za-z]*Ref\.current[\s\S]{0,180}await (?:jobHistory|history)[A-Za-z]*(?:Request|Load|Flight)[A-Za-z]*Ref\.current/i,
    "a second refresh must reuse, skip, or cancel the in-flight request",
  );
  assert.match(historyLoader, /finally\s*\{[\s\S]{0,160}\.current\s*=\s*(?:false|undefined|null)/);
  assert.match(historyLoader, /jobHistoryPendingRefreshRef\.current[\s\S]{0,240}loadJobHistory\(true\)/);
});

test("debounced workflow persistence flushes its latest snapshot on pagehide and unmount", async () => {
  const source = await readFile(studioPath, "utf8");

  assert.match(source, /(?:workflow|persist|storage)[A-Za-z]*Snapshot[A-Za-z]*Ref\s*=\s*useRef/i);
  assert.match(source, /addEventListener\("pagehide",\s*[A-Za-z]+/);
  assert.match(source, /removeEventListener\("pagehide",\s*[A-Za-z]+/);
  assert.match(
    source,
    /return \(\) => \{[\s\S]{0,320}removeEventListener\("pagehide"[\s\S]{0,220}(?:flush|persist|save)[A-Za-z]*\(\)/i,
    "the final in-memory snapshot must be written before the component disappears",
  );
});

test("canvas pointer coordinates account for CSS scaling before moving nodes", async () => {
  const source = await readFile(studioPath, "utf8");
  const startDrag = between(source, "function startDrag(", "function applyPointerUpdate(");
  const pointerUpdate = between(source, "function applyPointerUpdate(", "function moveDrag(");

  assert.match(
    source,
    /(?:rect\.width\s*\/\s*\(?(?:canvas\.)?offsetWidth|(?:canvas\.)?offsetWidth\s*\/\s*rect\.width)/,
    "horizontal CSS transform scale must be derived from layout and rendered widths",
  );
  assert.match(
    source,
    /(?:rect\.height\s*\/\s*\(?(?:canvas\.)?offsetHeight|(?:canvas\.)?offsetHeight\s*\/\s*rect\.height)/,
    "vertical CSS transform scale must be derived from layout and rendered heights",
  );
  assert.match(startDrag, /(?:canvasPoint|pointerToCanvas|canvasCoordinates|clientToCanvas)[A-Za-z]*\(/i);
  assert.match(pointerUpdate, /(?:canvasPoint|pointerToCanvas|canvasCoordinates|clientToCanvas)[A-Za-z]*\(/i);
  assert.doesNotMatch(pointerUpdate, /pointer\.clientX\s*-\s*rect\.left\s*-\s*active\.dx/);
});
