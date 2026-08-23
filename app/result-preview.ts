export type ResultPreviewMedia = "image" | "video";

export type ResultPreviewNode = {
  id: string;
  kind: "asset" | "image" | "video" | "output";
};

export type ResultPreviewEdge = {
  source: string;
  target: string;
};

export type ResultPreviewTarget = {
  generatorId: string;
  outputId: string;
};

/**
 * Resolve only a real generator → Output path on the active canvas.
 * Preview must never create a hidden runtime or focus an unrelated Output.
 */
export function resolveResultPreviewTarget(
  nodes: readonly ResultPreviewNode[],
  edges: readonly ResultPreviewEdge[],
  jobIdsByGenerator: Readonly<Record<string, string | undefined>>,
  resultId: string | undefined,
  media: ResultPreviewMedia,
): ResultPreviewTarget | undefined {
  const attachedGenerator = resultId
    ? nodes.find((node) => node.kind === media && jobIdsByGenerator[node.id] === resultId)
    : undefined;
  const candidates = attachedGenerator ? [attachedGenerator] : nodes.filter((node) => node.kind === media);
  for (const generator of candidates) {
    const outputEdge = edges.find((edge) => edge.source === generator.id && nodes.some((node) => node.id === edge.target && node.kind === "output"));
    if (outputEdge) return { generatorId: generator.id, outputId: outputEdge.target };
  }
  return undefined;
}
