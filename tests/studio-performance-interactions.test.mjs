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

test("result videos lazy-load and keep their central play-pause control synchronized", async () => {
  const source = await readFile(studioPath, "utf8");
  const styles = await readFile(globalStylesPath, "utf8");
  const resultThumbnail = between(source, "function ResultThumbnail(", "function ResultLibrary(");
  const resultLibrary = between(source, "function ResultLibrary(", "function ParameterPanel(");

  assert.match(resultThumbnail, /activated && videoSource[\s\S]*?<video\b/, "the video element is mounted only after activation");
  assert.doesNotMatch(resultThumbnail.split("activated && videoSource")[0], /<video\b/, "opening Results must not eagerly fetch video bodies");
  assert.match(resultThumbnail, /autoPlay playsInline preload="metadata"[^>]*onClick=\{togglePlayback\}/, "an explicit play click starts a player whose frame also toggles playback");
  assert.doesNotMatch(resultThumbnail, /<video\b[^>]*\bcontrols\b/, "compact result cards do not duplicate the custom button with native controls");
  assert.match(resultThumbnail, /video\.paused \|\| video\.ended[\s\S]*?video\.play\(\)[\s\S]*?video\.pause\(\)/, "the central result control toggles both states");
  assert.match(resultThumbnail, /onPlay=\{\(\) => setPaused\(false\)\}[^>]*onPause=\{\(\) => setPaused\(true\)\}[^>]*onEnded=\{\(\) => setPaused\(true\)\}/, "native player events synchronize the overlay icon");
  assert.match(resultThumbnail, /paused \? "▶" : "Ⅱ"/);
  assert.match(resultThumbnail, /className="library-video-play result-library-video-play"/, "the existing asset-player interaction is reused visually");
  assert.match(resultLibrary, /item\.media === "video" \? <div className="result-library-preview"/, "video controls are never nested inside the legacy preview button");
  assert.match(resultLibrary, /className="result-library-preview-label result-library-open-canvas"[^>]*onClick=\{\(\) => onSelect\(item\)\}/, "canvas preview remains an independent action");
  assert.match(styles, /\.result-library-video-play\s*>\s*img\s*\{[^}]*object-fit:\s*contain/s, "portrait result thumbnails retain their full frame");
});

test("asset and derived-result audio lazy-load and keep play-pause state synchronized", async () => {
  const source = await readFile(studioPath, "utf8");
  const styles = await readFile(globalStylesPath, "utf8");
  const audioPlayer = between(source, "function LazyAudioPlayer(", "function LibraryAssetPreview(");
  const assetPreview = between(source, "function LibraryAssetPreview(", "function AssetLibrary(");
  const resultLibrary = between(source, "function ResultLibrary(", "function ParameterPanel(");

  assert.match(audioPlayer, /activated && <audio\b/, "audio is mounted only after the first play action");
  assert.doesNotMatch(audioPlayer.split("activated && <audio")[0], /<audio\b/, "drawers must not eagerly fetch audio bodies");
  assert.match(audioPlayer, /autoPlay preload="metadata"/, "the first explicit click starts audio playback");
  assert.match(audioPlayer, /audio\.paused \|\| audio\.ended[\s\S]*?audio\.play\(\)[\s\S]*?audio\.pause\(\)/, "the audio icon toggles both playback states");
  assert.match(audioPlayer, /onPlay=\{\(\) => setPaused\(false\)\}[^>]*onPause=\{\(\) => setPaused\(true\)\}[^>]*onEnded=\{\(\) => setPaused\(true\)\}/, "real audio events synchronize the icon");
  assert.match(audioPlayer, /paused \? "▶" : "Ⅱ"/);
  assert.match(assetPreview, /<LazyAudioPlayer source=\{item\.contentUrl\} label=\{item\.filename\}/, "asset audio uses the interactive player");
  assert.match(resultLibrary, /item\.kind === "audio" \? <LazyAudioPlayer source=\{item\.contentUrl\} label=\{item\.displayName\}/, "derived result audio uses the same player");
  assert.match(styles, /\.library-audio-toggle\s*\{[^}]*cursor:\s*pointer/s);
  assert.match(styles, /\.library-audio-state\s*\{[^}]*left:\s*50%[^}]*top:\s*50%[^}]*translate:\s*-50%\s+-50%/s, "the audio play-pause button is centered like the video control");
  assert.match(styles, /\.library-audio-player\.playing \.library-audio-state/);
});

test("derived result videos use a real lazy play-pause control instead of a decorative icon", async () => {
  const source = await readFile(studioPath, "utf8");
  const videoPlayer = between(source, "function LazyVideoPlayer(", "function LibraryAssetPreview(");
  const resultLibrary = between(source, "function ResultLibrary(", "function ParameterPanel(");

  assert.match(videoPlayer, /if \(!activated\) return <button[^>]*className="library-video-play result-library-video-play"[^>]*onClick=\{\(\) => setActivated\(true\)\}/, "the idle play icon is an actionable button");
  assert.doesNotMatch(videoPlayer.split("if (!activated)")[0], /<video\b/, "derived video bodies stay unloaded before activation");
  assert.match(videoPlayer, /<video\b[^>]*autoPlay playsInline preload="metadata"[^>]*onClick=\{togglePlayback\}/, "the first click mounts and starts the player");
  assert.doesNotMatch(videoPlayer, /<video\b[^>]*\bcontrols\b/, "derived cards expose only the centered custom control");
  assert.match(videoPlayer, /video\.paused \|\| video\.ended[\s\S]*?video\.play\(\)[\s\S]*?video\.pause\(\)/, "the central control toggles playback");
  assert.match(videoPlayer, /onPlay=\{\(\) => setPaused\(false\)\}[^>]*onPause=\{\(\) => setPaused\(true\)\}[^>]*onEnded=\{\(\) => setPaused\(true\)\}/, "native events synchronize the play-pause icon");
  assert.match(resultLibrary, /item\.kind === "video" \? <LazyVideoPlayer source=\{item\.contentUrl\} label=\{item\.displayName\} thumbnailUrl=\{item\.thumbnailUrl\}/, "every derived video card uses the interactive player");
  assert.doesNotMatch(resultLibrary, /item\.kind === "video" && <span className="library-video-overlay"/, "no decorative-only derived video control remains");
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

test("asset-library videos preserve portrait framing and play immediately on explicit click", async () => {
  const source = await readFile(studioPath, "utf8");
  const styles = await readFile(globalStylesPath, "utf8");
  const preview = between(source, "function LibraryAssetPreview(", "function AssetLibrary(");

  assert.match(preview, /mediaDisplayOrientation\(item\.media\)/, "server dimensions and rotation classify the display orientation");
  assert.match(preview, /className="library-video-play"[^>]*onClick=\{\(\) => setActivated\(true\)\}/, "the play overlay is a real button action");
  assert.match(preview, /activated \? [\s\S]*?<video\b/, "video bytes remain lazy until the user clicks play");
  assert.match(preview, /<video\b[^>]*\bautoPlay\b[^>]*onClick=\{togglePlayback\}/, "a clicked card mounts an autoplaying player whose frame toggles playback");
  assert.doesNotMatch(preview, /<video\b[^>]*\bcontrols\b/, "asset cards do not render a second native play button");
  assert.match(preview, /video\.paused \|\| video\.ended[\s\S]*?video\.play\(\)[\s\S]*?video\.pause\(\)/, "the custom overlay toggles playback in both directions");
  assert.match(preview, /onPlay=\{\(\) => setPaused\(false\)\}[^>]*onPause=\{\(\) => setPaused\(true\)\}/, "native playback keeps the visible overlay state synchronized");
  assert.match(preview, /paused \? "▶" : "Ⅱ"/, "the overlay exposes an unambiguous play or pause icon");
  assert.doesNotMatch(preview.split("activated ?")[0], /<video\b/, "the idle card must not eagerly mount a player");
  assert.match(styles, /\.library-video-play\s*>\s*img\s*\{[^}]*object-fit:\s*contain/s, "portrait thumbnails are never landscape-cropped");
  assert.match(styles, /\.library-video-player\s*>\s*video\s*\{[^}]*object-fit:\s*contain/s, "video playback never crops its source frame");
  assert.match(styles, /\.library-video-player\s*\{[^}]*width:\s*100%[^}]*height:\s*100%/s, "the absolute player gives percentage-sized video a definite box");
  assert.match(styles, /\.library-video-player\s*>\s*video\s*\{[^}]*position:\s*absolute[^}]*inset:\s*0[^}]*width:\s*100%[^}]*max-width:\s*100%[^}]*height:\s*100%[^}]*max-height:\s*100%[^}]*object-fit:\s*contain/s, "playback is constrained to the card before its portrait frame is contained");
  assert.match(styles, /\.library-video-player\.paused\s+\.library-video-toggle\s*\{[^}]*opacity:\s*1/s, "paused playback always exposes its central control");
  assert.match(styles, /\.asset-preview\[data-media-kind="video"\][^{]*\{[^}]*object-fit:\s*contain/s, "canvas video previews also preserve their full frame");
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
