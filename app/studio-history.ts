export type JobStatus = "idle" | "submitting" | "queued" | "running" | "completed" | "failed";

export type GenerationParameters = Record<string, unknown> & {
  output_type?: "video" | "image";
  profile_id?: string;
  profile_version?: string;
  profile_digest?: string;
  sampling_mode?: string;
  mode?: string;
  director_mode?: string;
  source_asset_id?: string;
  width?: number;
  height?: number;
  duration_actual?: number;
  frames?: number;
  steps?: number;
  sampler?: string;
  scheduler?: string;
  lora?: string | null;
  lora_strength?: number;
  denoise?: number;
  seed?: number;
};

export type StudioJob = {
  id?: string;
  status: JobStatus;
  progress: number;
  message: string;
  previewUrl?: string;
  thumbnailUrl?: string;
  downloadUrl?: string;
  media?: "video" | "image";
  snapshot?: string;
  parameters?: GenerationParameters;
  workflowSha256?: string;
  workflowEvidence?: Record<string, unknown>;
  prompt?: string;
  createdAt?: string;
  updatedAt?: string;
  pinned?: boolean;
  resume?: {
    supported: boolean;
    can_resume: boolean;
    reason?: string | null;
    current_steps: number;
    max_total_steps: number;
    latest_job_id?: string;
    checkpoint_created_at?: number;
    checkpoint_expires_at?: number;
  };
};

/**
 * Job receipts can outlive an SSH tunnel or a cloned machine.  Keep API media
 * links on the currently-open Studio origin instead of retaining a stale
 * host/port from an older receipt.
 */
export function currentOriginApiUrl(value: unknown, fallback?: string): string | undefined {
  if (typeof value !== "string" || !value.trim()) return fallback;
  const raw = value.trim();
  const isMediaPath = (path: string) => path.startsWith("/api/") || path === "/preview" || path === "/download";
  if (!raw.includes("\\") && isMediaPath(raw.split(/[?#]/, 1)[0])) return raw;
  try {
    const parsed = new URL(raw, "http://h3-studio.invalid");
    return isMediaPath(parsed.pathname) && !parsed.pathname.includes("\\")
      ? `${parsed.pathname}${parsed.search}${parsed.hash}`
      : fallback;
  } catch {
    return fallback;
  }
}

export function rebaseStudioJobMedia(job: StudioJob): StudioJob {
  if (!job.id || job.status !== "completed") return job;
  const id = encodeURIComponent(job.id);
  return {
    ...job,
    previewUrl: currentOriginApiUrl(job.previewUrl, `/api/preview?id=${id}&index=0`),
    thumbnailUrl: currentOriginApiUrl(job.thumbnailUrl, `/api/jobs/${id}/thumbnail?index=0`),
    downloadUrl: currentOriginApiUrl(job.downloadUrl, `/api/download?id=${id}&index=0`),
  };
}

export type ParameterRow = { label: string; value: string };

export const HISTORY_LIMIT = 100;
export const JOB_HISTORY_CACHE_KEY = "h3-studio:job-history:v1";
export const JOB_HISTORY_CACHE_TTL_MS = 10 * 60 * 1000;

type CachedJobHistory = { savedAt: number; instanceId?: string; jobs: StudioJob[] };
export type ParsedJobHistoryCache = { instanceId?: string; jobs: StudioJob[] };

function timestamp(value?: string): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function compactCachedJob(job: StudioJob): StudioJob {
  const parameterKeys: (keyof GenerationParameters)[] = [
    "output_type", "profile_id", "profile_version", "profile_digest", "sampling_mode", "mode", "director_mode",
    "source_asset_id", "width", "height", "duration_actual", "frames", "steps", "sampler", "scheduler",
    "lora", "lora_strength", "denoise", "seed",
  ];
  const parameters = job.parameters
    ? Object.fromEntries(parameterKeys.flatMap((key) => job.parameters?.[key] === undefined ? [] : [[key, job.parameters[key]]])) as GenerationParameters
    : undefined;
  return {
    ...(job.id ? { id: job.id } : {}),
    status: job.status,
    progress: job.progress,
    message: job.message,
    ...(job.previewUrl ? { previewUrl: currentOriginApiUrl(job.previewUrl) } : {}),
    ...(job.thumbnailUrl ? { thumbnailUrl: currentOriginApiUrl(job.thumbnailUrl) } : {}),
    ...(job.downloadUrl ? { downloadUrl: currentOriginApiUrl(job.downloadUrl) } : {}),
    ...(job.media ? { media: job.media } : {}),
    ...(parameters && Object.keys(parameters).length ? { parameters } : {}),
    ...(job.workflowSha256 ? { workflowSha256: job.workflowSha256 } : {}),
    ...(job.prompt ? { prompt: job.prompt.slice(0, 512) } : {}),
    ...(job.createdAt ? { createdAt: job.createdAt } : {}),
    ...(job.updatedAt ? { updatedAt: job.updatedAt } : {}),
    ...(job.pinned ? { pinned: true } : {}),
  };
}

export function serializeJobHistoryCache(jobs: StudioJob[], now = Date.now(), instanceId?: string): string {
  const payload: CachedJobHistory = { savedAt: now, ...(instanceId ? { instanceId } : {}), jobs: jobs.filter((job) => job.id).slice(0, HISTORY_LIMIT).map(compactCachedJob) };
  return JSON.stringify(payload);
}

export function parseJobHistoryCacheEnvelope(raw: string | null, now = Date.now()): ParsedJobHistoryCache {
  if (!raw) return { jobs: [] };
  try {
    const value = JSON.parse(raw) as Partial<CachedJobHistory>;
    const savedAt = Number(value.savedAt);
    if (!Number.isFinite(savedAt) || savedAt > now + 60_000 || now - savedAt > JOB_HISTORY_CACHE_TTL_MS || !Array.isArray(value.jobs)) return { jobs: [] };
    const validStatuses = new Set<JobStatus>(["idle", "submitting", "queued", "running", "completed", "failed"]);
    return {
      ...(typeof value.instanceId === "string" ? { instanceId: value.instanceId } : {}),
      jobs: value.jobs
      .filter((job): job is StudioJob => Boolean(job && typeof job === "object" && typeof job.id === "string" && validStatuses.has(job.status)))
      .slice(0, HISTORY_LIMIT)
      .map(rebaseStudioJobMedia),
    };
  } catch {
    return { jobs: [] };
  }
}

export function parseJobHistoryCache(raw: string | null, now = Date.now()): StudioJob[] {
  return parseJobHistoryCacheEnvelope(raw, now).jobs;
}

export function mergeJobReceipt(
  listReceipt: Record<string, unknown>,
  statusReceipt: Record<string, unknown>,
): Record<string, unknown> {
  // /api/jobs owns durable creation/update timestamps while /api/status owns
  // the freshest execution fields. Merge instead of replacing so history
  // cards retain their original receipt time even with older servers.
  return { ...listReceipt, ...statusReceipt };
}

export function serverJobToStudioJob(receipt: Record<string, unknown>): StudioJob | undefined {
  const id = typeof receipt.id === "string"
    ? receipt.id
    : typeof receipt.job_id === "string" ? receipt.job_id : undefined;
  if (!id) return undefined;
  const rawStatus = String(receipt.status ?? receipt.state ?? "failed").toLowerCase();
  const status: JobStatus = rawStatus === "submitting" || rawStatus === "queued" || rawStatus === "running" || rawStatus === "completed"
    ? rawStatus
    : "failed";
  const created = Number(receipt.created_at ?? receipt.updated_at);
  const updated = Number(receipt.updated_at);
  const rawProgress = Number(receipt.progress);
  const progress = Number.isFinite(rawProgress)
    ? Math.max(0, Math.min(100, rawProgress <= 1 ? rawProgress * 100 : rawProgress))
    : status === "completed" || status === "failed" ? 100 : 0;
  const parameters = receipt.parameters && typeof receipt.parameters === "object"
    ? receipt.parameters as GenerationParameters
    : undefined;
  return {
    id,
    status,
    progress,
    message: typeof receipt.message === "string"
      ? receipt.message
      : status === "completed" ? "生成完成" : status === "failed" ? "生成失败" : status === "queued" ? "排队中…" : "生成中…",
    media: receipt.output_type === "image" ? "image" : "video",
    previewUrl: currentOriginApiUrl(
      receipt.preview_url,
      status === "completed" ? `/api/preview?id=${encodeURIComponent(id)}&index=0` : undefined,
    ),
    thumbnailUrl: currentOriginApiUrl(
      receipt.thumbnail_url,
      status === "completed" && (receipt.output_type === "image" || receipt.output_type === "video")
        ? `/api/jobs/${encodeURIComponent(id)}/thumbnail?index=0`
        : undefined,
    ),
    downloadUrl: currentOriginApiUrl(
      receipt.download_url,
      status === "completed" ? `/api/download?id=${encodeURIComponent(id)}&index=0` : undefined,
    ),
    parameters,
    workflowSha256: typeof receipt.workflow_sha256 === "string" ? receipt.workflow_sha256 : undefined,
    workflowEvidence: receipt.workflow_evidence && typeof receipt.workflow_evidence === "object" ? receipt.workflow_evidence as Record<string, unknown> : undefined,
    prompt: typeof receipt.prompt === "string" ? receipt.prompt : undefined,
    createdAt: Number.isFinite(created) && created > 0 ? new Date(created * 1000).toISOString() : undefined,
    updatedAt: Number.isFinite(updated) && updated > 0 ? new Date(updated * 1000).toISOString() : undefined,
    pinned: receipt.pinned === true,
    ...(receipt.resume && typeof receipt.resume === "object" ? { resume: receipt.resume as StudioJob["resume"] } : {}),
  };
}

export async function resumeGenerationJob(jobId: string, additionalSteps: number): Promise<Record<string, unknown>> {
  if (!/^[0-9a-f]{32}$/.test(jobId)) throw new Error("任务 ID 无效");
  if (!Number.isInteger(additionalSteps) || additionalSteps <= 0) throw new Error("追加步数必须是正整数");
  const requestId = crypto.randomUUID().replaceAll("-", "");
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ additional_steps: additionalSteps, request_id: requestId }),
  });
  const body = await response.json().catch(() => ({})) as Record<string, unknown> & { error?: { message?: string } };
  if (!response.ok) throw new Error(body.error?.message ?? `续跑提交失败 (${response.status})`);
  if (typeof body.job_id !== "string" || !/^[0-9a-f]{32}$/.test(body.job_id)) throw new Error("服务端未返回有效的续跑任务 ID");
  return body;
}

function printable(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return typeof value === "string" && value.trim() ? value.trim() : "—";
}

export function mergeJobHistory(history: StudioJob[], job: StudioJob, limit = HISTORY_LIMIT): StudioJob[] {
  if (!job.id) return history.slice(0, limit);
  const previous = history.find((item) => item.id === job.id);
  const definedJob = Object.fromEntries(Object.entries(job).filter(([, value]) => value !== undefined)) as StudioJob;
  let merged = previous ? { ...previous, ...definedJob } : job;
  if (previous) {
    const previousUpdated = timestamp(previous.updatedAt);
    const nextUpdated = timestamp(job.updatedAt);
    const stale = nextUpdated > 0 && previousUpdated > nextUpdated;
    const terminalRegression = ["completed", "failed"].includes(previous.status) && !["completed", "failed"].includes(job.status) && nextUpdated <= previousUpdated;
    if (stale || terminalRegression) merged = { ...job, ...previous };
    if (!merged.previewUrl && previous.previewUrl) merged.previewUrl = previous.previewUrl;
    if (!merged.thumbnailUrl && previous.thumbnailUrl) merged.thumbnailUrl = previous.thumbnailUrl;
    if (!merged.downloadUrl && previous.downloadUrl) merged.downloadUrl = previous.downloadUrl;
    if (!merged.parameters && previous.parameters) merged.parameters = previous.parameters;
    if (!merged.workflowSha256 && previous.workflowSha256) merged.workflowSha256 = previous.workflowSha256;
    if (!merged.workflowEvidence && previous.workflowEvidence) merged.workflowEvidence = previous.workflowEvidence;
  }
  return [merged, ...history.filter((item) => item.id !== job.id)].slice(0, Math.max(1, limit));
}

function cursorBoundary(value?: string): { time: number; id: string } | undefined {
  if (!value) return undefined;
  const separator = value.lastIndexOf(":");
  const time = Number(value.slice(0, separator)) * 1000;
  const id = value.slice(separator + 1);
  return separator > 0 && Number.isFinite(time) && Boolean(id) ? { time, id } : undefined;
}

function olderThan(job: StudioJob, boundary: { time: number; id: string }): boolean {
  const time = timestamp(job.createdAt);
  return time < boundary.time || (time === boundary.time && Boolean(job.id) && job.id! < boundary.id);
}

/** Merge a server page without letting older cursor pages jump ahead of the visible first page. */
export function mergeJobHistoryPage(history: StudioJob[], page: StudioJob[], append = false, hasMore = true, limit = HISTORY_LIMIT, requestCursor?: string): StudioJob[] {
  const incoming = page.filter((job): job is StudioJob & { id: string } => Boolean(job.id));
  if (!incoming.length) {
    if (!append) return [];
    const requestedBoundary = cursorBoundary(requestCursor);
    if (requestedBoundary && !hasMore) {
      return history.filter((job) => !olderThan(job, requestedBoundary)).slice(0, Math.max(1, limit));
    }
    return history.slice(0, Math.max(1, limit));
  }
  const existing = new Map(history.filter((job) => job.id).map((job) => [job.id!, job]));
  const mergedIncoming = incoming.map((job) => mergeJobHistory(existing.get(job.id) ? [existing.get(job.id)!] : [], job, 1)[0]);
  if (append) {
    const requestedBoundary = cursorBoundary(requestCursor);
    if (requestedBoundary) {
      const incomingIds = new Set(mergedIncoming.map((job) => job.id));
      const last = mergedIncoming[mergedIncoming.length - 1];
      const lastBoundary = { time: timestamp(last.createdAt), id: last.id! };
      const prior = history.filter((job) => !incomingIds.has(job.id) && (job.pinned || !olderThan(job, requestedBoundary)));
      const tail = hasMore ? history.filter((job) => !incomingIds.has(job.id) && olderThan(job, lastBoundary)) : [];
      return [...prior, ...mergedIncoming, ...tail].slice(0, Math.max(1, limit));
    }
    const replacements = new Map(mergedIncoming.map((job) => [job.id!, job]));
    const existingIds = new Set(history.map((job) => job.id).filter((id): id is string => Boolean(id)));
    const updated = history.map((job) => job.id && replacements.has(job.id) ? replacements.get(job.id)! : job);
    return [...updated, ...mergedIncoming.filter((job) => !existingIds.has(job.id!))].slice(0, Math.max(1, limit));
  }
  const incomingIds = new Set(mergedIncoming.map((job) => job.id));
  const boundary = incoming[incoming.length - 1];
  const boundaryTime = timestamp(boundary.createdAt);
  const retained = history.filter((job) => {
    if (!job.id || incomingIds.has(job.id)) return false;
    if (!hasMore) return false;
    const jobTime = timestamp(job.createdAt);
    return jobTime < boundaryTime || (jobTime === boundaryTime && job.id < boundary.id);
  });
  return [...mergedIncoming, ...retained].slice(0, Math.max(1, limit));
}

export function jobParameterRows(parameters?: GenerationParameters): ParameterRow[] {
  if (!parameters) return [];
  const profile = parameters.profile_id
    ? `${parameters.profile_id}@${printable(parameters.profile_version)}`
    : "Auto";
  const size = parameters.width && parameters.height ? `${parameters.width}×${parameters.height}` : "—";
  const duration = parameters.duration_actual
    ? `${parameters.duration_actual}s${parameters.frames ? ` / ${parameters.frames}f` : ""}`
    : "—";
  const sampling = [printable(parameters.sampler), printable(parameters.scheduler)].join(" / ");
  let lora = "—";
  if (parameters.output_type === "video" || parameters.sampling_mode) {
    lora = parameters.lora
      ? `${parameters.lora}${typeof parameters.lora_strength === "number" ? ` @ ${parameters.lora_strength}` : ""}`
      : "未加载";
  }
  const rows: ParameterRow[] = [
    { label: "Resolved Profile", value: profile },
    { label: "模式", value: printable(parameters.sampling_mode ?? parameters.mode) },
    ...(parameters.director_mode ? [{ label: "创作模式", value: printable(parameters.director_mode) }] : []),
    ...(parameters.source_asset_id ? [{ label: "源视频", value: printable(parameters.source_asset_id) }] : []),
    { label: "尺寸", value: size },
  ];
  if (parameters.output_type === "video" || parameters.duration_actual) rows.push({ label: "时长", value: duration });
  if (typeof parameters.denoise === "number") rows.push({ label: "调度去噪比例（实验）", value: printable(parameters.denoise) });
  rows.push(
    { label: "Steps", value: printable(parameters.steps) },
    { label: "Sampler / Scheduler", value: sampling },
    { label: "LoRA", value: lora },
    { label: "Seed", value: printable(parameters.seed) },
  );
  return rows;
}

export function formatJobTime(value?: string): string {
  if (!value) return "时间未知";
  const time = new Date(value);
  if (Number.isNaN(time.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(time);
}

/** User-perceived generation time, including queue and submission overhead. */
export function formatJobElapsed(createdAt?: string, updatedAt?: string): string {
  if (!createdAt || !updatedAt) return "耗时未知";
  const started = new Date(createdAt).getTime();
  const finished = new Date(updatedAt).getTime();
  if (!Number.isFinite(started) || !Number.isFinite(finished) || finished < started) return "耗时未知";
  const seconds = Math.max(0, Math.round((finished - started) / 1000));
  if (seconds < 60) return `耗时 ${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return remainder ? `耗时 ${minutes} 分 ${remainder} 秒` : `耗时 ${minutes} 分`;
  const hours = Math.floor(minutes / 60);
  const minuteRemainder = minutes % 60;
  return minuteRemainder ? `耗时 ${hours} 小时 ${minuteRemainder} 分` : `耗时 ${hours} 小时`;
}
