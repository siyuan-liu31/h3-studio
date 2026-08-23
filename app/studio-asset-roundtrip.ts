import type { AssetNode, CanvasDocumentV7, MediaKind } from "./studio-document.ts";

export type StudioAssetRoundTripInput = {
  media: MediaKind;
  fileName: string;
  localUrl: string;
  remoteId?: string;
  derivationId?: string;
  sourceJobId?: string;
  thumbnailUrl?: string;
  uploadState: "uploading" | "ready" | "error";
  role: string;
  mediaMeta?: Record<string, unknown>;
};

export function assetPayloadFromStudio(asset: StudioAssetRoundTripInput): AssetNode["asset"] {
  return {
    remoteId: asset.remoteId,
    fileName: asset.fileName,
    contentUrl: asset.remoteId ? `/api/assets/${asset.remoteId}/content` : asset.localUrl || undefined,
    thumbnailUrl: asset.thumbnailUrl,
    uploadState: asset.uploadState,
    media: asset.mediaMeta,
    role: asset.role,
    source: {
      kind: asset.derivationId ? "derivation" : asset.sourceJobId ? "job" : asset.remoteId ? "library" : "local",
      ...(!asset.remoteId && asset.localUrl ? { localUrl: asset.localUrl } : {}),
      ...(asset.sourceJobId ? { sourceJobId: asset.sourceJobId } : {}),
      ...(asset.derivationId ? { derivationId: asset.derivationId } : {}),
    },
  };
}

export function studioAssetFromDocument(node: AssetNode, document: CanvasDocumentV7, fallbackRole: string): StudioAssetRoundTripInput {
  const boundRole = document.nodes
    .flatMap((item) => item.kind === "video-generator" || item.kind === "image-generator" ? item.bindings : [])
    .find((binding) => binding.sourceNodeId === node.id)?.role;
  const source = node.asset.source;
  const remoteId = node.asset.remoteId;
  const encodedRemoteId = remoteId ? encodeURIComponent(remoteId) : undefined;
  const sourceJobId = source?.sourceJobId;
  const localSourceUrl = source?.localUrl ?? node.asset.contentUrl ?? "";
  return {
    media: node.mediaKind,
    fileName: node.asset.fileName,
    localUrl: remoteId
      ? `/api/assets/${encodedRemoteId}/content`
      : source?.derivationId
        ? localSourceUrl
      : sourceJobId
        ? localSourceUrl && !localSourceUrl.startsWith("blob:")
          ? localSourceUrl
          : `/api/preview?id=${encodeURIComponent(sourceJobId)}&index=0`
      : localSourceUrl,
    remoteId,
    derivationId: source?.derivationId,
    sourceJobId,
    thumbnailUrl: remoteId && node.mediaKind !== "audio"
      ? `/api/assets/${encodedRemoteId}/thumbnail`
      : sourceJobId ? `/api/jobs/${encodeURIComponent(sourceJobId)}/thumbnail?index=0` : node.asset.thumbnailUrl,
    uploadState: remoteId || sourceJobId ? "ready" : node.asset.uploadState,
    role: boundRole ?? node.asset.role ?? fallbackRole,
    mediaMeta: node.asset.media,
  };
}
