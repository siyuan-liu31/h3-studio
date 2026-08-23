import { CANVAS_DOCUMENT_VERSION, parseCanvasDocument, type CanvasDocumentV7 } from "./studio-document.ts";

export const CANVAS_WORKSPACE_VERSION = 1 as const;
export const CANVAS_WORKSPACE_STORAGE_KEY = "h3-studio-canvas-workspace-v1";
export const CANVAS_WORKSPACE_BACKUP_KEY = "h3-studio-canvas-workspace-v1-backup";

export type CanvasWorkspaceQuarantine = {
  index: number;
  canvasId?: string;
  title: string;
  reason: string;
  raw: unknown;
};

export type CanvasWorkspaceTab = {
  id: string;
  title: string;
  document: CanvasDocumentV7;
  updatedAt: number;
};

export type CanvasWorkspaceV1 = {
  version: typeof CANVAS_WORKSPACE_VERSION;
  activeCanvasId: string;
  canvases: CanvasWorkspaceTab[];
  quarantined?: CanvasWorkspaceQuarantine[];
};

export type CanvasWorkspaceParseResult = {
  ok: boolean;
  workspace?: CanvasWorkspaceV1;
  error?: string;
  issues?: CanvasWorkspaceQuarantine[];
};

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function safeTitle(value: unknown, index: number): string {
  const title = typeof value === "string" ? value.trim() : "";
  return title || `画布 ${index + 1}`;
}

export function createCanvasWorkspace(
  document: CanvasDocumentV7,
  createId: () => string = () => globalThis.crypto.randomUUID(),
  now: () => number = () => Date.now(),
): CanvasWorkspaceV1 {
  const id = createId();
  return {
    version: CANVAS_WORKSPACE_VERSION,
    activeCanvasId: id,
    canvases: [{ id, title: "画布 1", document, updatedAt: now() }],
  };
}

export function parseCanvasWorkspace(raw: string): CanvasWorkspaceParseResult {
  try {
    const parsed = recordValue(JSON.parse(raw));
    if (parsed.version !== CANVAS_WORKSPACE_VERSION || !Array.isArray(parsed.canvases)) {
      return { ok: false, error: "工作区版本不受支持" };
    }
    const issues: CanvasWorkspaceQuarantine[] = [];
    const seenIds = new Set<string>();
    const canvases = parsed.canvases.flatMap((value, index): CanvasWorkspaceTab[] => {
      const record = recordValue(value);
      const canvasId = typeof record.id === "string" && record.id ? record.id : undefined;
      const title = safeTitle(record.title, index);
      if (!canvasId) {
        issues.push({ index, title, reason: "画布缺少有效 ID", raw: value });
        return [];
      }
      if (seenIds.has(canvasId)) {
        issues.push({ index, canvasId, title, reason: "画布 ID 重复", raw: value });
        return [];
      }
      const documentRecord = recordValue(record.document);
      if (documentRecord.version !== CANVAS_DOCUMENT_VERSION || !Array.isArray(documentRecord.nodes) || !Array.isArray(documentRecord.edges)) {
        issues.push({ index, canvasId, title, reason: "画布文档结构损坏", raw: value });
        return [];
      }
      const restored = parseCanvasDocument(JSON.stringify(record.document));
      if (!restored.ok) {
        issues.push({ index, canvasId, title, reason: restored.error ?? "画布文档无法解析", raw: value });
        return [];
      }
      seenIds.add(canvasId);
      return [{
        id: canvasId,
        title,
        document: restored.document,
        updatedAt: Number.isFinite(Number(record.updatedAt)) ? Number(record.updatedAt) : 0,
      }];
    });
    const existingQuarantine = Array.isArray(parsed.quarantined) ? parsed.quarantined.flatMap((value): CanvasWorkspaceQuarantine[] => {
      const record = recordValue(value);
      if (!Number.isFinite(Number(record.index)) || typeof record.reason !== "string") return [];
      return [{ index: Number(record.index), ...(typeof record.canvasId === "string" ? { canvasId: record.canvasId } : {}), title: safeTitle(record.title, Number(record.index)), reason: record.reason, raw: record.raw }];
    }) : [];
    const quarantined = [...existingQuarantine, ...issues];
    if (!canvases.length) return { ok: false, error: "工作区没有可恢复的画布", issues: quarantined };
    const requestedActiveId = typeof parsed.activeCanvasId === "string" ? parsed.activeCanvasId : "";
    const activeCanvasId = canvases.some((canvas) => canvas.id === requestedActiveId) ? requestedActiveId : canvases[0].id;
    return { ok: true, workspace: { version: CANVAS_WORKSPACE_VERSION, activeCanvasId, canvases, ...(quarantined.length ? { quarantined } : {}) }, issues: quarantined };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "工作区解析失败" };
  }
}

export function updateCanvasWorkspaceDocument(
  workspace: CanvasWorkspaceV1,
  canvasId: string,
  document: CanvasDocumentV7,
  now: () => number = () => Date.now(),
): CanvasWorkspaceV1 {
  if (!workspace.canvases.some((canvas) => canvas.id === canvasId)) return workspace;
  return {
    ...workspace,
    activeCanvasId: canvasId,
    canvases: workspace.canvases.map((canvas) => canvas.id === canvasId ? { ...canvas, document, updatedAt: now() } : canvas),
  };
}

export function addCanvasWorkspaceTab(
  workspace: CanvasWorkspaceV1,
  document: CanvasDocumentV7,
  createId: () => string = () => globalThis.crypto.randomUUID(),
  now: () => number = () => Date.now(),
): CanvasWorkspaceV1 {
  const id = createId();
  const usedNumbers = new Set(workspace.canvases.map((canvas) => Number(canvas.title.match(/^画布 (\d+)$/)?.[1])).filter(Number.isFinite));
  let number = 1;
  while (usedNumbers.has(number)) number += 1;
  return {
    ...workspace,
    activeCanvasId: id,
    canvases: [...workspace.canvases, { id, title: `画布 ${number}`, document, updatedAt: now() }],
  };
}

export function renameCanvasWorkspaceTab(workspace: CanvasWorkspaceV1, canvasId: string, title: string): CanvasWorkspaceV1 {
  const nextTitle = title.trim();
  if (!nextTitle) return workspace;
  return { ...workspace, canvases: workspace.canvases.map((canvas) => canvas.id === canvasId ? { ...canvas, title: nextTitle } : canvas) };
}

export function removeCanvasWorkspaceTab(workspace: CanvasWorkspaceV1, canvasId: string): CanvasWorkspaceV1 {
  if (workspace.canvases.length <= 1) return workspace;
  const index = workspace.canvases.findIndex((canvas) => canvas.id === canvasId);
  if (index < 0) return workspace;
  const canvases = workspace.canvases.filter((canvas) => canvas.id !== canvasId);
  const activeCanvasId = workspace.activeCanvasId === canvasId
    ? canvases[Math.min(index, canvases.length - 1)].id
    : workspace.activeCanvasId;
  return { ...workspace, activeCanvasId, canvases };
}

export function serializeCanvasWorkspace(workspace: CanvasWorkspaceV1): string {
  return JSON.stringify(workspace);
}

export type CanvasWorkspaceStorage = Pick<Storage, "setItem">;

export function commitCanvasWorkspaceStorage(
  storage: CanvasWorkspaceStorage,
  workspaceRaw: string,
  recovery?: { key: string; value: string },
): { ok: true } | { ok: false; stage: "recovery" | "workspace" } {
  // The workspace key is the sole commit point. Recovery is written first so a
  // failure can never leave a newer workspace on disk while the UI reports that
  // the operation was not applied.
  if (recovery) {
    try { storage.setItem(recovery.key, recovery.value); }
    catch { return { ok: false, stage: "recovery" }; }
  }
  try { storage.setItem(CANVAS_WORKSPACE_STORAGE_KEY, workspaceRaw); }
  catch { return { ok: false, stage: "workspace" }; }
  return { ok: true };
}
