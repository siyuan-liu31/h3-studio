"use client";

import {
  VIDEO_DIRECTOR_MODE_HELP,
  VIDEO_DIRECTOR_MODE_LABELS,
  VIDEO_DIRECTOR_MODES,
  type VideoDirectorContract,
  type VideoDirectorMode,
  type VideoModeAsset,
} from "./studio-video-mode";

export function VideoDirectorControls({
  mode,
  sourceVideoId,
  sources,
  contract,
  onModeChange,
  onSourceChange,
}: {
  mode: VideoDirectorMode;
  sourceVideoId: string;
  sources: VideoModeAsset[];
  contract: VideoDirectorContract;
  onModeChange: (mode: VideoDirectorMode) => void;
  onSourceChange: (assetId: string) => void;
}) {
  const needsSource = contract.resolvedMode === "v2v" || contract.resolvedMode === "rv2v";
  const workflowMode = ["r2v", "v2v", "rv2v"].includes(contract.resolvedMode) ? contract.resolvedMode : undefined;
  const workflowHref = workflowMode ? `/api/workflows/director/${workflowMode}` : "/api/workflows/director";
  const uniqueSources = sources.filter((source, index) => source.assetId && sources.findIndex((item) => item.assetId === source.assetId) === index);

  return <section className={`video-director-controls ${contract.errors.length ? "has-error" : ""}`} aria-label="H3 创作模式">
    <header><div><strong>创作模式</strong><small>{mode === "auto" ? `已解析为 ${VIDEO_DIRECTOR_MODE_LABELS[contract.resolvedMode]}` : `低层工作流：${contract.lowLevelMode}`}</small></div><span>{contract.compiler}</span></header>
    <label className="control-label"><span>Director 模式 <em>只改变工作流，不修改 Prompt</em></span><select value={mode} onChange={(event) => onModeChange(event.target.value as VideoDirectorMode)}>{VIDEO_DIRECTOR_MODES.map((item) => <option key={item} value={item}>{VIDEO_DIRECTOR_MODE_LABELS[item]}</option>)}</select></label>
    {needsSource && <label className="control-label"><span>源视频 <em>固定映射为 &lt;Video 1&gt;</em></span><select value={sourceVideoId} onChange={(event) => onSourceChange(event.target.value)}><option value="">请选择已连接的视频…</option>{uniqueSources.map((source) => <option key={source.assetId} value={source.assetId}>{source.label}</option>)}</select><small>{uniqueSources.length ? "这里只列出已上传并连到 H3 Video 的视频节点。" : "请先将一个已上传视频节点连接到 H3 Video。"}</small></label>}
    <p>{VIDEO_DIRECTOR_MODE_HELP[mode]}</p>
    {contract.errors.length > 0 && <ul className="video-director-errors" role="alert">{contract.errors.map((error) => <li key={error}>{error}</li>)}</ul>}
    <nav className="video-workflow-actions" aria-label="Director 工作流"><a href={workflowHref} target="_blank" rel="noreferrer" aria-label="查看工作流模式合同">查看模式合同</a>{workflowMode ? <a href={`${workflowHref}?download=1`} download aria-label="导出工作流模板">导出工作流模板</a> : <button type="button" disabled title="R2V / V2V / RV2V 可导出工作流模板">导出工作流模板</button>}<small>{workflowMode ? "这里返回模式合同/官方节点模板，不冒充某次生成任务的实际执行图。" : "当前模式由 Profile 编译；Director 模板适用于 R2V / V2V / RV2V。"}</small></nav>
  </section>;
}
