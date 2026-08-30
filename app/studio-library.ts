export type LibraryMediaKind = "image" | "video" | "audio";
export type LibraryMedia = {
  duration?: number;
  has_audio?: boolean;
  fps?: number;
  source_fps?: number;
  reference_fps?: number;
  frame_count?: number;
  normalized_to_24fps?: boolean;
  width?: number;
  height?: number;
  rotation?: number;
};
export type LibraryAsset = {
  id: string;
  kind: LibraryMediaKind;
  filename: string;
  contentUrl: string;
  thumbnailUrl: string;
  contentHash?: string;
  size?: number;
  folderId?: string;
  pinned: boolean;
  createdAt?: string;
  media: LibraryMedia;
};

export type AssetMutation = { display_name?: string; folder_id?: string | null; pinned?: boolean };
export type DerivedMedia = {
  id: string;
  kind: LibraryMediaKind;
  displayName: string;
  contentUrl: string;
  thumbnailUrl?: string;
  downloadUrl?: string;
  createdAt?: string;
  assetId?: string;
  pinned: boolean;
  media: LibraryMedia;
  preprocessing?: Record<string, unknown>;
};
export type LibraryFolder = { id: string; name: string; parentId?: string };
export type MediaDeriveRequest =
  | { operation: "video_trim"; start: number; end: number }
  | { operation: "frame"; position: "first" | "last" }
  | { operation: "frame"; time: number }
  | { operation: "extract_audio" }
  | { operation: "remove_audio" }
  | { operation: "prepare_h3_reference"; preset?: "h3-low-token"; max_short_edge?: number; max_long_edge?: number; fps?: 24; max_duration?: number; audio: "keep" | "remove"; fit?: "contain"; alignment?: 32; pad_mode?: "edge" }
  | { operation: "audio_trim"; start: number; end: number };
export type MediaDeriveSource =
  | { type: "asset"; asset_id: string }
  | { type: "job"; job_id: string; index?: number }
  | { type: "derivation"; receipt_id: string };
export type MediaDeriveOptions = {
  signal?: AbortSignal;
  onProgress?: (progress: number, status: string) => void;
  pollIntervalMs?: number;
};

const ASSET_ID = /^[0-9a-f]{32}$/;
const MEDIA_KINDS = new Set<LibraryMediaKind>(["image", "video", "audio"]);

function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}
function sameOriginApiPath(value: unknown, fallback: string): string {
  return typeof value === "string" && value.startsWith("/api/") && !value.includes("\\") ? value : fallback;
}

export function remoteAssetToLibraryItem(receipt: unknown): LibraryAsset | undefined {
  if (!receipt || typeof receipt !== "object") return undefined;
  const value = receipt as Record<string, unknown>;
  const id = value.id;
  const kind = value.kind;
  if (typeof id !== "string" || !ASSET_ID.test(id) || typeof kind !== "string" || !MEDIA_KINDS.has(kind as LibraryMediaKind)) return undefined;
  const rawMedia = value.media && typeof value.media === "object" ? value.media as Record<string, unknown> : {};
  const media: LibraryMedia = {};
  for (const key of ["duration", "fps", "source_fps", "reference_fps", "frame_count", "width", "height", "rotation"] as const) {
    const number = finiteNumber(rawMedia[key]);
    if (number !== undefined) media[key] = number;
  }
  if (typeof rawMedia.has_audio === "boolean") media.has_audio = rawMedia.has_audio;
  if (typeof rawMedia.normalized_to_24fps === "boolean") media.normalized_to_24fps = rawMedia.normalized_to_24fps;
  const created = finiteNumber(value.created_at);
  return {
    id,
    kind: kind as LibraryMediaKind,
    filename: typeof value.display_name === "string" && value.display_name.trim()
      ? value.display_name.trim()
      : typeof value.filename === "string" && value.filename.trim() ? value.filename : `${kind}-${id.slice(0, 8)}`,
    contentUrl: sameOriginApiPath(value.content_url, `/api/assets/${encodeURIComponent(id)}/content`),
    thumbnailUrl: kind === "audio" ? "" : sameOriginApiPath(value.thumbnail_url, `/api/assets/${encodeURIComponent(id)}/thumbnail`),
    ...(typeof value.content_hash === "string" && /^[0-9a-f]{64}$/.test(value.content_hash) ? { contentHash: value.content_hash } : {}),
    ...(finiteNumber(value.size) !== undefined ? { size: finiteNumber(value.size) } : {}),
    ...(typeof value.folder_id === "string" && value.folder_id.trim() ? { folderId: value.folder_id.trim() } : {}),
    pinned: value.pinned === true,
    ...(created !== undefined && created > 0 ? { createdAt: new Date(created * 1000).toISOString() } : {}),
    media,
  };
}

async function jsonRequest(path: string, init: RequestInit): Promise<unknown> {
  const response = await fetch(path, init);
  const body = await response.json().catch(() => ({})) as { error?: { message?: string } };
  if (!response.ok) throw new Error(body.error?.message ?? `资产操作失败 (${response.status})`);
  return body;
}

export async function updateLibraryAsset(id: string, patch: AssetMutation): Promise<LibraryAsset> {
  const body = await jsonRequest(`/api/assets/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  const asset = remoteAssetToLibraryItem((body as { asset?: unknown }).asset ?? body);
  if (!asset) throw new Error("服务端未返回有效的资产记录");
  return asset;
}

export async function deleteLibraryAsset(id: string): Promise<void> {
  await jsonRequest(`/api/assets/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function createLibraryFolder(name: string, parentId?: string): Promise<{ id: string; name: string }> {
  const body = await jsonRequest("/api/asset-folders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, ...(parentId ? { parent_id: parentId } : {}) }),
  }) as { folder?: { id?: string; name?: string }; id?: string; name?: string };
  const value = body.folder ?? body;
  if (typeof value.id !== "string" || typeof value.name !== "string") throw new Error("服务端未返回有效的文件夹");
  return { id: value.id, name: value.name };
}

export async function deleteLibraryFolder(id: string): Promise<{ assetsMoved: number; subfoldersMoved: number }> {
  const body = await jsonRequest(`/api/asset-folders/${encodeURIComponent(id)}`, { method: "DELETE" }) as Record<string, unknown>;
  return {
    assetsMoved: finiteNumber(body.assets_moved) ?? 0,
    subfoldersMoved: finiteNumber(body.subfolders_moved) ?? 0,
  };
}

export async function listLibraryFolders(query = ""): Promise<LibraryFolder[]> {
  const suffix = query.trim() ? `?q=${encodeURIComponent(query.trim())}` : "";
  const body = await jsonRequest(`/api/asset-folders${suffix}`, { method: "GET", cache: "no-store" }) as { folders?: unknown[] };
  return (Array.isArray(body.folders) ? body.folders : []).flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const value = entry as Record<string, unknown>;
    if (typeof value.id !== "string" || typeof value.name !== "string") return [];
    return [{ id: value.id, name: value.name, ...(typeof value.parent_id === "string" ? { parentId: value.parent_id } : {}) }];
  });
}

export async function deriveLibraryMedia(source: MediaDeriveSource, request: MediaDeriveRequest, options: MediaDeriveOptions = {}): Promise<DerivedMedia> {
  const response = await fetch("/api/media/derive", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source, ...request, ...(request.operation === "prepare_h3_reference" ? { background: true } : {}) }),
  });
  let body = await response.json().catch(() => ({})) as Record<string, unknown> & { error?: { message?: string } };
  if (!response.ok) throw new Error(body.error?.message ?? `资产操作失败 (${response.status})`);
  if (response.status === 202) {
    const taskId = typeof body.task_id === "string" && ASSET_ID.test(body.task_id) ? body.task_id : undefined;
    if (!taskId) throw new Error("服务端未返回有效的媒体任务 ID");
    let cancellationRequested = false;
    const cancel = () => {
      if (cancellationRequested) return;
      cancellationRequested = true;
      void fetch(`/api/media-tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).catch(() => undefined);
    };
    options.signal?.addEventListener("abort", cancel, { once: true });
    try {
      for (;;) {
        if (options.signal?.aborted) { cancel(); throw new DOMException("媒体处理已取消", "AbortError"); }
        const statusResponse = await fetch(`/api/media-tasks/${encodeURIComponent(taskId)}`, { cache: "no-store" });
        body = await statusResponse.json().catch(() => ({})) as typeof body;
        if (!statusResponse.ok) throw new Error(body.error?.message ?? `媒体任务读取失败 (${statusResponse.status})`);
        const status = typeof body.status === "string" ? body.status : "";
        const progress = Math.max(0, Math.min(100, Math.floor(finiteNumber(body.progress) ?? 0)));
        options.onProgress?.(progress, status);
        if (status === "completed") {
          if (!body.receipt || typeof body.receipt !== "object") throw new Error("媒体任务完成但缺少派生回执");
          body = body.receipt as typeof body;
          break;
        }
        if (status === "failed" || status === "canceled") {
          const failure = body.error && typeof body.error === "object" ? body.error as { message?: string } : {};
          throw new Error(failure.message ?? (status === "canceled" ? "媒体处理已取消" : "媒体处理失败"));
        }
        await new Promise<void>((resolve) => globalThis.setTimeout(resolve, Math.max(200, options.pollIntervalMs ?? 500)));
      }
    } finally {
      options.signal?.removeEventListener("abort", cancel);
    }
  }
  const value = body.derivation && typeof body.derivation === "object" ? body.derivation as Record<string, unknown> : body;
  const derived = remoteDerivationToResult(value);
  if (!derived) throw new Error("服务端未返回有效的派生媒体");
  return derived;
}

export function remoteDerivationToResult(receipt: unknown): DerivedMedia | undefined {
  if (!receipt || typeof receipt !== "object") return undefined;
  const value = receipt as Record<string, unknown>;
  const kind = value.kind;
  if (typeof value.id !== "string" || !ASSET_ID.test(value.id) || typeof kind !== "string" || !MEDIA_KINDS.has(kind as LibraryMediaKind)) return undefined;
  const created = finiteNumber(value.created_at);
  return {
    id: value.id,
    kind: kind as LibraryMediaKind,
    displayName: typeof value.display_name === "string" && value.display_name.trim() ? value.display_name : `derived-${value.id.slice(0, 8)}`,
    contentUrl: sameOriginApiPath(value.preview_url ?? value.content_url ?? value.download_url, ""),
    thumbnailUrl: typeof value.thumbnail_url === "string" ? sameOriginApiPath(value.thumbnail_url, "") : undefined,
    downloadUrl: typeof value.download_url === "string" ? sameOriginApiPath(value.download_url, "") : undefined,
    ...(created !== undefined && created > 0 ? { createdAt: new Date(created * 1000).toISOString() } : {}),
    ...(typeof value.asset_id === "string" && ASSET_ID.test(value.asset_id) ? { assetId: value.asset_id } : {}),
    pinned: value.pinned === true,
    media: value.media && typeof value.media === "object" ? value.media as LibraryMedia : {},
    ...(value.preprocessing && typeof value.preprocessing === "object" ? { preprocessing: value.preprocessing as Record<string, unknown> } : {}),
  };
}

export function estimateH3ReferenceCanvas(width: number, height: number, rotation = 0): { width: number; height: number } | undefined {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) return undefined;
  const normalized = ((Math.round(rotation / 90) * 90) % 360 + 360) % 360;
  const [displayWidth, displayHeight] = normalized === 90 || normalized === 270 ? [height, width] : [width, height];
  const square = displayWidth === displayHeight;
  const boxWidth = square ? 480 : displayWidth > displayHeight ? 864 : 480;
  const boxHeight = square ? 480 : displayWidth > displayHeight ? 480 : 864;
  const scale = Math.min(1, boxWidth / displayWidth, boxHeight / displayHeight);
  const contentWidth = Math.max(2, Math.min(boxWidth, Math.round(displayWidth * scale / 2) * 2));
  const contentHeight = Math.max(2, Math.min(boxHeight, Math.round(displayHeight * scale / 2) * 2));
  return {
    width: Math.min(boxWidth, Math.max(32, Math.ceil(contentWidth / 32) * 32)),
    height: Math.min(boxHeight, Math.max(32, Math.ceil(contentHeight / 32) * 32)),
  };
}

export async function listDerivedMedia(signal?: AbortSignal): Promise<DerivedMedia[]> {
  const body = await jsonRequest("/api/derivations", { method: "GET", cache: "no-store", signal }) as { derivations?: unknown[] };
  return (Array.isArray(body.derivations) ? body.derivations : []).flatMap((receipt) => {
    const derived = remoteDerivationToResult(receipt);
    return derived ? [derived] : [];
  });
}

export async function deriveLibraryAsset(id: string, request: MediaDeriveRequest): Promise<DerivedMedia> {
  return deriveLibraryMedia({ type: "asset", asset_id: id }, request);
}

export async function deleteDerivedMedia(id: string): Promise<void> {
  await jsonRequest(`/api/derivations/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function updateDerivedMedia(id: string, pinned: boolean): Promise<DerivedMedia> {
  const body = await jsonRequest(`/api/derivations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pinned }),
  });
  const derived = remoteDerivationToResult(body);
  if (!derived) throw new Error("服务端未返回有效的派生结果");
  return derived;
}

export async function updateJobResult(id: string, pinned: boolean): Promise<void> {
  await jsonRequest(`/api/jobs/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pinned }),
  });
}

export async function saveDerivedMedia(id: string, displayName?: string, folderId?: string): Promise<LibraryAsset> {
  const body = await jsonRequest(`/api/derivations/${encodeURIComponent(id)}/assets`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...(displayName ? { display_name: displayName } : {}), ...(folderId ? { folder_id: folderId } : {}) }),
  });
  const asset = remoteAssetToLibraryItem((body as { asset?: unknown }).asset ?? body);
  if (!asset) throw new Error("服务端未返回有效的资产记录");
  return asset;
}
