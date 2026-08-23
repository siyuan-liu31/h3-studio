"use client";

/* Server thumbnails and authenticated media routes are rendered directly. */
/* eslint-disable @next/next/no-img-element */
/* eslint-disable jsx-a11y/media-has-caption */

import { useEffect, useMemo, useRef, useState } from "react";
import type { LibraryAsset } from "./studio-library";
import {
  H3_GENERATION_FPS,
  h3EffectiveDuration,
  isVideoMediaSegment,
  resolveVideoSequencePosition,
  timelineStatusLabel,
  videoSequenceTimeForSegment,
  videoSegmentDuration,
  type MergedVideoResult,
  type VideoSegment,
} from "./video-project";
import type { SceneCutSuggestion } from "./video-director-model";

function formatTime(seconds: number): string {
  const safe = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(safe / 60);
  const remaining = safe - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${remaining.toFixed(2).padStart(5, "0")}`;
}

export function SequenceVideoMonitor({
  segments,
  merged,
  currentTime,
  onTimeChange,
  onSelect,
  assets = [],
  sourceId = "",
  sourceTime = 0,
  disabled = false,
  analyzing = false,
  suggestions = [],
  onSourceChange,
  onSourceTimeChange,
  onAnalyze,
  onSplit,
  onEqualize,
}: {
  segments: VideoSegment[];
  merged?: MergedVideoResult;
  currentTime: number;
  onTimeChange: (seconds: number) => void;
  onSelect: (index: number) => void;
  assets?: LibraryAsset[];
  sourceId?: string;
  sourceTime?: number;
  disabled?: boolean;
  analyzing?: boolean;
  suggestions?: SceneCutSuggestion[];
  onSourceChange?: (id: string) => void;
  onSourceTimeChange?: (seconds: number) => void;
  onAnalyze?: () => void;
  onSplit?: () => void;
  onEqualize?: (count: number) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [monitorMode, setMonitorMode] = useState<"sequence" | "source">("sequence");
  const [equalCount, setEqualCount] = useState(2);
  const [mediaAspect, setMediaAspect] = useState("16 / 9");
  const [loadedMedia, setLoadedMedia] = useState<{ url: string; duration: number }>();
  const position = resolveVideoSequencePosition(segments, currentTime);
  const segment = position.segment;
  const sourceVideos = useMemo(() => assets.filter((asset) => asset.kind === "video"), [assets]);
  const selectedSource = sourceVideos.find((asset) => asset.id === sourceId);
  const viewMode = monitorMode === "source" && selectedSource ? "source" : "sequence";
  const segmentSource = segment?.source_range
    ? sourceVideos.find((asset) => asset.id === segment.source_range?.asset_id)
    : undefined;
  const directSource = segment && isVideoMediaSegment(segment)
    ? sourceVideos.find((asset) => asset.id === segment.media_source?.asset_id)
    : undefined;
  const mergedUrl = merged?.status === "completed" ? (merged.preview_url || merged.download_url) : undefined;
  const segmentUrl = segment?.status === "completed" ? (segment.preview_url || segment.download_url) : undefined;
  const sequenceSourceFallback = !mergedUrl && !segmentUrl ? (directSource || segmentSource) : undefined;
  const activeSource = viewMode === "source" ? selectedSource : sequenceSourceFallback;
  const playableUrl = viewMode === "source" ? selectedSource?.contentUrl : (mergedUrl || segmentUrl || activeSource?.contentUrl);
  const posterUrl = viewMode === "source"
    ? selectedSource?.thumbnailUrl
    : mergedUrl ? merged?.thumbnail_url : segmentUrl ? segment?.thumbnail_url : directSource?.thumbnailUrl || activeSource?.thumbnailUrl;
  const sourceFps = Math.max(1, Number(activeSource?.media.source_fps ?? activeSource?.media.fps ?? activeSource?.media.reference_fps ?? H3_GENERATION_FPS));
  const transportFps = viewMode === "source" ? sourceFps : H3_GENERATION_FPS;
  const detectedDuration = loadedMedia && loadedMedia.url === playableUrl ? loadedMedia.duration : 0;
  const sourceDuration = Math.max(0, Number(activeSource?.media.duration ?? 0), detectedDuration);
  const sourceRangeStart = viewMode === "source" ? 0 : (segment?.media_source ? segment.media_source.start_frame / segment.media_source.fps : segment?.source_range ? segment.source_range.start_frame / segment.source_range.fps : 0);
  const sourceRangeEnd = viewMode === "source"
    ? sourceDuration
    : segment?.media_source ? segment.media_source.end_frame / segment.media_source.fps : segment?.source_range ? segment.source_range.end_frame / segment.source_range.fps : sourceDuration;
  const monitorTime = viewMode === "source" ? sourceTime : position.time;
  const monitorTotal = viewMode === "source" ? sourceDuration : position.totalDuration;

  useEffect(() => {
    const player = videoRef.current;
    if (!player || !playableUrl) return;
    // onTimeUpdate already drives the shared timeline while playback is active.
    // Writing currentTime back on every React render creates a micro-seek loop
    // and makes both playback and timeline scrubbing visibly stutter.
    if (!player.paused) return;
    const next = viewMode === "source"
      ? Math.max(0, sourceTime)
      : mergedUrl ? position.time : activeSource ? sourceRangeStart + position.localTime : position.localTime;
    if (Math.abs(player.currentTime - next) > 0.5 / transportFps) player.currentTime = next;
  }, [activeSource, mergedUrl, playableUrl, position.localTime, position.time, segment?.id, sourceRangeStart, sourceTime, transportFps, viewMode]);

  const seekSegment = (index: number) => {
    if (index < 0 || index >= segments.length) return;
    onSelect(index);
    onTimeChange(videoSequenceTimeForSegment(segments, index));
  };
  const seek = (seconds: number) => {
    const frame = Math.round(Math.max(0, seconds) * transportFps);
    const snapped = frame / transportFps;
    if (viewMode === "source") onSourceTimeChange?.(Math.min(sourceDuration || snapped, snapped));
    else onTimeChange(Math.min(position.totalDuration, snapped));
  };
  const togglePlayback = async () => {
    const player = videoRef.current;
    if (!player || !playableUrl) return;
    if (!player.paused) {
      player.pause();
      return;
    }
    try {
      await player.play();
    } catch {
      setPlaying(false);
    }
  };
  const updateFromPlayer = (player: HTMLVideoElement) => {
    if (viewMode === "source") {
      onSourceTimeChange?.(player.currentTime);
      return;
    }
    if (mergedUrl) onTimeChange(player.currentTime);
    else if (activeSource) onTimeChange(position.segmentStart + Math.max(0, player.currentTime - sourceRangeStart));
    else onTimeChange(position.segmentStart + player.currentTime);
  };
  const handleEnded = () => {
    setPlaying(false);
    if (viewMode === "source") return;
    if (!mergedUrl && position.index < segments.length - 1) seekSegment(position.index + 1);
    else onTimeChange(position.totalDuration);
  };
  const placeholder = !segment
    ? { title: "还没有分镜", detail: "添加分段后可在这里预览完整成片序列。" }
    : segment.status === "failed"
      ? { title: `分段 ${position.index + 1} 生成失败`, detail: segment.error || "修正设置并重新生成后，这一段会出现在成片序列中。" }
      : segment.status === "completed"
        ? { title: `分段 ${position.index + 1} 暂无可播放文件`, detail: "任务已完成，但服务端没有返回预览或下载地址。" }
        : { title: `分段 ${position.index + 1} · ${timelineStatusLabel(segment.status)}`, detail: "该位置尚无生成画面；时间轴仍保留它的预计时长。" };

  return <section className="director-monitor" aria-label="完整分镜序列监视器">
    <header><div><strong>成片序列监视器</strong><small>{viewMode === "source" ? "源素材帧级预览 · 切分不修改原文件" : mergedUrl ? "播放已合并的完整成片 · 与下方时间线同步" : "成片、素材和切分共用一个监视器"}</small></div><nav className="director-monitor-mode" aria-label="监视器模式"><button type="button" aria-pressed={viewMode === "sequence"} onClick={() => setMonitorMode("sequence")}>成片</button><button type="button" aria-pressed={viewMode === "source"} disabled={!selectedSource} onClick={() => setMonitorMode("source")}>源素材</button>{viewMode === "sequence" && segment && <span className={`timeline-status status-${segment.status}`}>分段 {position.index + 1}/{segments.length} · {timelineStatusLabel(segment.status)}</span>}</nav></header>
    <div className="director-monitor-stage" data-media-aspect={mediaAspect.replaceAll(" ", "")}>
      <div className="director-monitor-media-frame" style={{ aspectRatio: mediaAspect }}>
      {playableUrl ? <video
        key={viewMode === "source" ? `source:${sourceId}:${playableUrl}` : mergedUrl ? `merged:${mergedUrl}` : `${segment?.id ?? "none"}:${playableUrl}`}
        ref={videoRef}
        src={playableUrl}
        poster={posterUrl}
        playsInline
        preload="metadata"
        aria-label={viewMode === "source" ? "源素材预览" : mergedUrl ? "已合并的完整成片" : `分段 ${position.index + 1} 预览`}
        onLoadedMetadata={(event) => {
          const player = event.currentTarget;
          if (player.videoWidth > 0 && player.videoHeight > 0) setMediaAspect(`${player.videoWidth} / ${player.videoHeight}`);
          if (Number.isFinite(player.duration) && player.duration > 0) setLoadedMedia({ url: playableUrl, duration: player.duration });
          player.currentTime = viewMode === "source" ? sourceTime : mergedUrl ? position.time : activeSource ? sourceRangeStart + position.localTime : position.localTime;
        }}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onTimeUpdate={(event) => {
          if (activeSource && viewMode === "sequence" && sourceRangeEnd > sourceRangeStart && event.currentTarget.currentTime >= sourceRangeEnd) {
            event.currentTarget.pause();
            handleEnded();
            return;
          }
          updateFromPlayer(event.currentTarget);
        }}
        onEnded={() => {
          setPlaying(false);
          if (viewMode === "source") return;
          if (!mergedUrl && position.index < segments.length - 1) seekSegment(position.index + 1);
          else onTimeChange(position.totalDuration);
        }}
      /> : <div className="director-monitor-empty" role="status">{segment?.thumbnail_url && <img src={segment.thumbnail_url} alt="" style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover", opacity: .18 }}/>}<span style={{ position: "relative" }}>{segment?.status === "failed" ? "!" : "…"}</span><strong style={{ position: "relative" }}>{placeholder.title}</strong><small style={{ position: "relative", whiteSpace: "normal", textAlign: "center" }}>{placeholder.detail}</small></div>}
      </div>
    </div>
    <div className="director-monitor-transport">
      <button type="button" disabled={!playableUrl || monitorTime <= 0} onClick={() => seek(monitorTime - 1 / transportFps)} aria-label="上一帧">│◀</button>
      <button type="button" className="director-play-toggle" disabled={!playableUrl} onClick={() => void togglePlayback()} aria-label={playing ? "暂停" : "播放"}>{playing ? "Ⅱ" : "▶"}</button>
      <button type="button" disabled={!playableUrl || monitorTime >= monitorTotal} onClick={() => seek(monitorTime + 1 / transportFps)} aria-label="下一帧">▶│</button>
      <output aria-label="监视器时间码">{formatTime(monitorTime)} <small>/ {monitorTotal ? formatTime(monitorTotal) : "--:--.--"}</small></output>
      <span>{viewMode === "source" ? `帧 ${Math.round(sourceTime * sourceFps)} · ${sourceFps.toFixed(sourceFps % 1 ? 3 : 0)} fps` : segment ? `分段 ${position.index + 1} · 本段 ${formatTime(position.localTime)}` : "等待分镜"}</span>
    </div>
    {sourceVideos.length > 0 && <div className="director-monitor-tools director-monitor-source-tools">
      <label><span>源视频</span><select value={sourceId} disabled={disabled} onChange={(event) => { onSourceChange?.(event.target.value); setMonitorMode(event.target.value ? "source" : "sequence"); }}><option value="">从资产选择视频…</option>{sourceVideos.map((asset) => <option key={asset.id} value={asset.id}>{asset.filename}</option>)}</select></label>
      <button type="button" disabled={disabled || !selectedSource || !onSplit} onClick={onSplit}>✂ 当前帧切分</button>
      <label><span>均分</span><input type="number" min="1" max="64" value={equalCount} disabled={disabled || !selectedSource} onChange={(event) => setEqualCount(Math.max(1, Math.min(64, Math.round(Number(event.target.value)) || 1)))}/><button type="button" disabled={disabled || !selectedSource || !onEqualize} onClick={() => onEqualize?.(equalCount)}>建立草稿</button></label>
      <button type="button" disabled={disabled || !selectedSource || analyzing || !onAnalyze} onClick={onAnalyze}>{analyzing ? "分析中…" : "✦ 智能分镜"}</button>
    </div>}
    {viewMode === "source" && suggestions.length > 0 && <div className="director-scene-suggestions" role="status"><strong>建议切点</strong>{suggestions.map((item) => <button key={item.seconds} type="button" onClick={() => seek(item.seconds)} title={item.confidence === undefined ? undefined : `置信度 ${(item.confidence * 100).toFixed(0)}%`}>{formatTime(item.seconds)}</button>)}<small>点击只定位，再用“当前帧切分”确认。</small></div>}
  </section>;
}

export function SourceVideoMonitor({
  assets,
  sourceId,
  currentTime,
  disabled,
  analyzing,
  suggestions,
  onSourceChange,
  onTimeChange,
  onAnalyze,
  onSplit,
  onEqualize,
}: {
  assets: LibraryAsset[];
  sourceId: string;
  currentTime: number;
  disabled: boolean;
  analyzing: boolean;
  suggestions: SceneCutSuggestion[];
  onSourceChange: (id: string) => void;
  onTimeChange: (seconds: number) => void;
  onAnalyze: () => void;
  onSplit: () => void;
  onEqualize: (count: number) => void;
}) {
  const videos = useMemo(() => assets.filter((asset) => asset.kind === "video"), [assets]);
  const source = videos.find((asset) => asset.id === sourceId);
  const [loaded, setLoaded] = useState(false);
  const [equalCount, setEqualCount] = useState(2);
  const videoRef = useRef<HTMLVideoElement>(null);
  const duration = Math.max(0, Number(source?.media.duration ?? 0));
  const fps = Math.max(1, Number(source?.media.source_fps ?? source?.media.fps ?? source?.media.reference_fps ?? 24));
  const frame = Math.max(0, Math.round(currentTime * fps));

  useEffect(() => {
    const player = videoRef.current;
    if (!player || !loaded || !Number.isFinite(currentTime)) return;
    const next = Math.max(0, Math.min(duration || Number.POSITIVE_INFINITY, currentTime));
    if (Math.abs(player.currentTime - next) > 0.5 / fps) player.currentTime = next;
  }, [currentTime, duration, fps, loaded, sourceId]);

  const seek = (seconds: number) => {
    const next = Math.max(0, Math.min(duration || Number.POSITIVE_INFINITY, seconds));
    if (videoRef.current) videoRef.current.currentTime = next;
    onTimeChange(next);
  };

  return <section className="director-monitor" aria-label="源视频监视器">
    <header><div><strong>源视频监视器</strong><small>只读预览 · 分镜操作先形成草稿</small></div><label><span>源视频</span><select value={sourceId} disabled={disabled} onChange={(event) => onSourceChange(event.target.value)}><option value="">从资产选择视频…</option>{videos.map((asset) => <option key={asset.id} value={asset.id}>{asset.filename}</option>)}</select></label></header>
    <div className="director-monitor-stage">
      {!source && <div className="director-monitor-empty"><span>▶</span><strong>选择一个视频开始分镜</strong><small>原视频不会被修改</small></div>}
      {source && !loaded && <button type="button" className="director-monitor-load" onClick={() => setLoaded(true)}>{source.thumbnailUrl && <img src={source.thumbnailUrl} alt="" loading="lazy" decoding="async"/>}<span>▶</span><strong>加载视频预览</strong><small>{source.filename}</small></button>}
      {source && loaded && <video ref={videoRef} src={source.contentUrl} controls playsInline preload="metadata" aria-label={`${source.filename} 源视频`} onTimeUpdate={(event) => onTimeChange(event.currentTarget.currentTime)}/>} 
    </div>
    <div className="director-monitor-transport">
      <button type="button" disabled={!source} onClick={() => seek(currentTime - 1 / fps)} aria-label="上一帧">│◀</button>
      <output aria-label="当前时间码">{formatTime(currentTime)} <small>/ {duration ? formatTime(duration) : "--:--.--"}</small></output>
      <button type="button" disabled={!source} onClick={() => seek(currentTime + 1 / fps)} aria-label="下一帧">▶│</button>
      <span>帧 {frame} · {fps.toFixed(fps % 1 ? 2 : 0)} fps</span>
      <input type="range" min="0" max={duration || 1} step={1 / fps} value={Math.min(currentTime, duration || 1)} disabled={!source || !duration} onChange={(event) => seek(Number(event.target.value))} aria-label="源视频播放头"/>
    </div>
    <div className="director-monitor-tools">
      <button type="button" disabled={disabled || !source} onClick={onSplit}>✂ 在当前帧切分</button>
      <label><span>均分</span><input type="number" min="1" max="64" value={equalCount} disabled={disabled || !source} onChange={(event) => setEqualCount(Math.max(1, Number(event.target.value) || 1))}/><button type="button" disabled={disabled || !source} onClick={() => onEqualize(equalCount)}>建立草稿</button></label>
      <button type="button" disabled={disabled || !source || analyzing} onClick={onAnalyze}>{analyzing ? "分析中…" : "✦ 智能分镜"}</button>
      <small>智能分镜只显示建议切点，不会自动提交、派生或改写项目。</small>
    </div>
    {suggestions.length > 0 && <div className="director-scene-suggestions" role="status"><strong>建议切点</strong>{suggestions.map((item) => <button key={item.seconds} type="button" onClick={() => seek(item.seconds)} title={item.confidence === undefined ? undefined : `置信度 ${(item.confidence * 100).toFixed(0)}%`}>{formatTime(item.seconds)}</button>)}<small>点击只会定位播放头，再用“在当前帧切分”确认。</small></div>}
  </section>;
}

export function StoryboardTimeline({
  segments,
  selectedIndex,
  runSelection,
  currentTime,
  disabled,
  onSelect,
  onTimeChange,
  onDelete,
  onToggleRun,
  onSelectAll,
  onInvert,
  onImportAfter,
  onInsertAfter,
  onScrubStart,
  onScrubEnd,
  assets = [],
}: {
  segments: VideoSegment[];
  selectedIndex: number;
  runSelection: ReadonlySet<string>;
  currentTime: number;
  disabled: boolean;
  onSelect: (index: number) => void;
  onTimeChange: (seconds: number) => void;
  onDelete: (id: string) => void;
  onToggleRun: (id: string) => void;
  onSelectAll: () => void;
  onInvert: () => void;
  onImportAfter?: (index: number) => void;
  onInsertAfter?: (index: number) => void;
  onScrubStart?: () => void;
  onScrubEnd?: () => void;
  assets?: LibraryAsset[];
}) {
  const [contextShot, setContextShot] = useState<{ id: string; index: number; x: number; y: number }>();
  const [viewportWidth, setViewportWidth] = useState(0);
  const contextDeleteRef = useRef<HTMLButtonElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const dragPointerRef = useRef<number | undefined>(undefined);
  const pendingScrubFrameRef = useRef<number | null>(null);
  const scrubAnimationRef = useRef<number | null>(null);
  const publishedFrameRef = useRef(0);
  const sourceTotal = segments.reduce((sum, segment) => {
    const range = segment.source_range!;
    return sum + (range ? (range.end_frame - range.start_frame) / range.fps : 0);
  }, 0);
  const segmentFrames = useMemo(() => segments.map((segment) => Math.max(1, Math.round(videoSegmentDuration(segment) * H3_GENERATION_FPS))), [segments]);
  const totalFrames = segmentFrames.reduce((sum, frames) => sum + frames, 0);
  const outputTotal = totalFrames / H3_GENERATION_FPS;
  const currentFrame = Math.max(0, Math.min(totalFrames, Math.round(currentTime * H3_GENERATION_FPS)));
  const contentWidth = Math.max(viewportWidth, totalFrames * 1.15, 1);
  const pxPerFrame = totalFrames > 0 ? contentWidth / totalFrames : 1.15;
  const tickStepFrames = useMemo(() => {
    const candidates = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600].map((seconds) => seconds * H3_GENERATION_FPS);
    return candidates.find((frames) => frames * pxPerFrame >= 80) ?? candidates[candidates.length - 1];
  }, [pxPerFrame]);
  const rulerTicks = useMemo(() => {
    if (totalFrames <= 0) return [0];
    const ticks: number[] = [];
    for (let frame = 0; frame < totalFrames; frame += tickStepFrames) ticks.push(frame);
    if (ticks.at(-1) !== totalFrames) ticks.push(totalFrames);
    return ticks;
  }, [tickStepFrames, totalFrames]);
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const measure = () => setViewportWidth(Math.max(0, viewport.clientWidth));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!contextShot) return;
    contextDeleteRef.current?.focus();
    const close = () => setContextShot(undefined);
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", closeOnEscape);
    return () => { window.removeEventListener("pointerdown", close); window.removeEventListener("keydown", closeOnEscape); };
  }, [contextShot]);
  useEffect(() => () => {
    if (scrubAnimationRef.current !== null) window.cancelAnimationFrame(scrubAnimationRef.current);
  }, []);
  useEffect(() => { publishedFrameRef.current = currentFrame; }, [currentFrame]);
  const seekFrame = (frame: number) => {
    const nextFrame = Math.max(0, Math.min(totalFrames, Math.round(frame)));
    if (nextFrame === publishedFrameRef.current) return;
    publishedFrameRef.current = nextFrame;
    onTimeChange(nextFrame / H3_GENERATION_FPS);
  };
  const scheduleSeekFrame = (frame: number) => {
    pendingScrubFrameRef.current = frame;
    if (scrubAnimationRef.current !== null) return;
    scrubAnimationRef.current = window.requestAnimationFrame(() => {
      scrubAnimationRef.current = null;
      const pending = pendingScrubFrameRef.current;
      pendingScrubFrameRef.current = null;
      if (pending !== null) seekFrame(pending);
    });
  };
  const frameFromPointer = (clientX: number) => {
    const track = trackRef.current;
    if (!track) return currentFrame;
    const bounds = track.getBoundingClientRect();
    return Math.round((clientX - bounds.left) / pxPerFrame);
  };
  const beginScrub = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!segments.length || event.button !== 0 || (event.target as HTMLElement).closest("[data-run-toggle]")) return;
    event.preventDefault();
    dragPointerRef.current = event.pointerId;
    event.currentTarget.setPointerCapture(event.pointerId);
    onScrubStart?.();
    scheduleSeekFrame(frameFromPointer(event.clientX));
  };
  const continueScrub = (event: React.PointerEvent<HTMLDivElement>) => {
    if (dragPointerRef.current !== event.pointerId) return;
    scheduleSeekFrame(frameFromPointer(event.clientX));
    const viewport = viewportRef.current;
    if (!viewport) return;
    const bounds = viewport.getBoundingClientRect();
    if (event.clientX < bounds.left + 28) viewport.scrollLeft = Math.max(0, viewport.scrollLeft - 18);
    else if (event.clientX > bounds.right - 28) viewport.scrollLeft += 18;
  };
  const endScrub = (event: React.PointerEvent<HTMLDivElement>) => {
    if (dragPointerRef.current !== event.pointerId) return;
    if (scrubAnimationRef.current !== null) {
      window.cancelAnimationFrame(scrubAnimationRef.current);
      scrubAnimationRef.current = null;
    }
    const pending = pendingScrubFrameRef.current;
    pendingScrubFrameRef.current = null;
    if (pending !== null) seekFrame(pending);
    dragPointerRef.current = undefined;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    onScrubEnd?.();
  };
  const moveKeyboardSelection = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    const target = event.key === "ArrowLeft" ? index - 1 : event.key === "ArrowRight" ? index + 1 : event.key === "Home" ? 0 : event.key === "End" ? segments.length - 1 : index;
    if (target === index || target < 0 || target >= segments.length) return;
    event.preventDefault();
    onSelect(target);
    onTimeChange(videoSequenceTimeForSegment(segments, target));
    const buttons = event.currentTarget.closest(".director-segment-row")?.querySelectorAll<HTMLButtonElement>("button[data-shot-index]");
    buttons?.[target]?.focus();
  };
  return <section className="director-storyboard" aria-label="横向成片时间线">
    {contextShot && <div role="menu" aria-label={`分段 ${contextShot.index + 1} 菜单`} style={{ position: "fixed", zIndex: 1000, left: Math.min(contextShot.x, window.innerWidth - 190), top: Math.min(contextShot.y, window.innerHeight - 70), minWidth: 180, padding: 6, border: "1px solid #42495a", borderRadius: 8, background: "#171b24", boxShadow: "0 12px 32px #000a" }} onPointerDown={(event) => event.stopPropagation()}><button ref={contextDeleteRef} role="menuitem" type="button" disabled={disabled || segments.length <= 1} title={disabled ? "项目运行或操作期间不能删除分段" : segments.length <= 1 ? "长视频项目至少需要保留一个分段" : undefined} onClick={() => { const id = contextShot.id; setContextShot(undefined); onDelete(id); }} style={{ width: "100%", minHeight: 34, border: 0, borderRadius: 6, background: "#321c23", color: "#f1a1ad", textAlign: "left", padding: "0 10px" }}>删除分段</button>{(disabled || segments.length <= 1) && <small style={{ display: "block", padding: "5px 8px 2px", color: "#8d96a7" }}>{disabled ? "项目运行中不能删除" : "至少保留一个分段"}</small>}</div>}
    <header><div><strong>成片分镜时间线</strong><small>{segments.length} 段 · {totalFrames} 帧 · 预计成片 {outputTotal.toFixed(2)}s{sourceTotal ? ` · 绑定源素材 ${sourceTotal.toFixed(2)}s` : ""}</small></div><nav aria-label="时间线操作"><button className="director-import-video" type="button" disabled={disabled || !onImportAfter} onClick={() => onImportAfter?.(selectedIndex)}>＋ 导入视频</button><button className="director-add-blank" type="button" disabled={disabled || !onInsertAfter} onClick={() => onInsertAfter?.(selectedIndex)}>＋ 空白片段</button><button type="button" disabled={disabled} onClick={onSelectAll}>全选</button><button type="button" disabled={disabled} onClick={onInvert}>反选</button><span>已选 {runSelection.size}/{segments.length}</span></nav></header>
    <div ref={viewportRef} className="director-storyboard-scroll">
      <div ref={trackRef} className="director-storyboard-track" style={{ minWidth: contentWidth, width: contentWidth, paddingLeft: 0, paddingRight: 0, touchAction: "none" }} onPointerDown={beginScrub} onPointerMove={continueScrub} onPointerUp={endScrub} onPointerCancel={endScrub}>
        <div className="director-ruler" aria-hidden="true" style={{ position: "relative", width: contentWidth }}>
          {rulerTicks.map((frame) => <span key={frame} style={{ position: "absolute", left: frame * pxPerFrame, transform: frame === 0 ? "none" : frame === totalFrames ? "translateX(-100%)" : "translateX(-50%)" }}>{formatTime(frame / H3_GENERATION_FPS)}</span>)}
        </div>
        <div className="director-segment-row" style={{ width: contentWidth, gap: 0 }}>
          {!segments.length && <div className="director-storyboard-empty"><strong>还没有分镜</strong><small>选择源视频后，使用“当前帧切分”或“均分”建立可运行片段。</small></div>}
          {segments.map((segment, index) => {
            const frames = segmentFrames[index];
            const outputDuration = frames / H3_GENERATION_FPS;
            const range = segment.source_range;
            const sourceDuration = range ? (range.end_frame - range.start_frame) / range.fps : outputDuration;
            const direct = isVideoMediaSegment(segment);
            const directAsset = direct ? assets.find((asset) => asset.id === segment.media_source?.asset_id) : undefined;
            const thumbnail = directAsset?.thumbnailUrl || segment.thumbnail_url;
            return <article key={segment.id} className={`director-shot status-${segment.status}${selectedIndex === index ? " selected" : ""}`} style={{ flex: `0 0 ${frames * pxPerFrame}px`, width: frames * pxPerFrame, minWidth: 0, maxWidth: "none" }} onContextMenu={(event) => { event.preventDefault(); onSelect(index); onTimeChange(videoSequenceTimeForSegment(segments, index)); setContextShot({ id: segment.id, index, x: event.clientX, y: event.clientY }); }}>
              <label data-run-toggle>{direct ? <input type="checkbox" checked={false} disabled aria-label={`分段 ${index + 1} 为已有素材，无需生成`}/> : <input type="checkbox" checked={runSelection.has(segment.id)} disabled={disabled} onChange={() => onToggleRun(segment.id)} aria-label={`选择运行分段 ${index + 1}`}/>}<span>{String(index + 1).padStart(2, "0")}</span></label>
              <button type="button" data-shot-index={index} aria-current={selectedIndex === index ? "true" : undefined} onClick={(event) => { onSelect(index); if (event.detail === 0) onTimeChange(videoSequenceTimeForSegment(segments, index)); }} onKeyDown={(event) => moveKeyboardSelection(event, index)} aria-label={`编辑分段 ${index + 1}`}>{segment.status === "completed" && thumbnail ? <img src={thumbnail} alt="" loading="lazy" decoding="async"/> : <span className="director-shot-empty">{segment.status === "failed" ? "!" : segment.status === "completed" ? "▶" : "…"}</span>}<i>{direct ? "已有素材" : timelineStatusLabel(segment.status)}</i></button>
              <footer><strong>{outputDuration.toFixed(2)}s 成片</strong><small>{direct ? "直接拼接" : range ? `源 ${sourceDuration.toFixed(2)}s` : timelineStatusLabel(segment.status)}</small></footer>
            </article>;
          })}
        </div>
        <div className="director-playhead" role="slider" tabIndex={segments.length ? 0 : -1} aria-label="拖动成片时间线播放头" aria-valuemin={0} aria-valuemax={totalFrames} aria-valuenow={currentFrame} aria-valuetext={`${formatTime(currentFrame / H3_GENERATION_FPS)} · 帧 ${currentFrame}`} style={{ left: currentFrame * pxPerFrame, width: 14, transform: "translateX(-7px)", background: "transparent", pointerEvents: "auto", cursor: "ew-resize" }} onKeyDown={(event) => { if (event.key === "ArrowLeft") { event.preventDefault(); seekFrame(currentFrame - 1); } else if (event.key === "ArrowRight") { event.preventDefault(); seekFrame(currentFrame + 1); } else if (event.key === "Home") { event.preventDefault(); seekFrame(0); } else if (event.key === "End") { event.preventDefault(); seekFrame(totalFrames); } }}><span aria-hidden="true" style={{ position: "absolute", top: 0, bottom: 0, left: 7, width: 1, background: "#ff6868" }}/><i/></div>
      </div>
    </div>
  </section>;
}

export function SourceRangePanel({
  segment,
  source,
  busy,
  disabled,
  notice,
  onCropAndSave,
}: {
  segment: VideoSegment;
  source?: LibraryAsset;
  busy: boolean;
  disabled: boolean;
  notice?: string;
  onCropAndSave: () => void;
}) {
  const range = segment.source_range;
  if (!range) return <section className="director-source-range missing" aria-label="片段来源区间"><div><strong>未绑定源片段</strong><small>请先在上方选择源视频，再使用切分或均分。</small></div></section>;
  const start = range.start_frame / range.fps;
  const end = range.end_frame / range.fps;
  const duration = end - start;
  return <section className="director-source-range" aria-label="片段来源区间">
    <div><strong>来源区间 · 已绑定为隐式 Ref2VA 视频</strong><small title={source?.filename}>{source?.filename ?? range.asset_id}</small></div>
    <dl><div><dt>帧范围</dt><dd>{range.start_frame} → {range.end_frame}</dd></div><div><dt>时间范围</dt><dd>{formatTime(start)} → {formatTime(end)}</dd></div><div><dt>源片段</dt><dd>{duration.toFixed(2)}s · {range.fps.toFixed(range.fps % 1 ? 3 : 0)} fps</dd></div><div><dt>H3 输出</dt><dd>{h3EffectiveDuration(segment.request.parameters.duration).toFixed(2)}s · {Math.round(h3EffectiveDuration(segment.request.parameters.duration) * 24)} 帧</dd></div></dl>
    <div className="director-source-range-actions"><button type="button" disabled={disabled || busy || !source} onClick={onCropAndSave}>{busy ? "裁剪与保存中…" : "✂ 裁剪并保存为资产"}</button><small>{duration < 2 ? "不足 2 秒：只保存资产，Prompt 和参考保持不变。" : "保存一份可复用资产；本段已通过来源区间隐式引用，不会重复加入 references 或修改 Prompt。"}</small></div>
    {notice && <p role="status">{notice}</p>}
  </section>;
}
