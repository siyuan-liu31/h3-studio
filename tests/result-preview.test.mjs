import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { resolveResultPreviewTarget } from "../app/result-preview.ts";

const studioSource = readFileSync(new URL("../app/studio.tsx", import.meta.url), "utf8");
const globalsSource = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");

test("fresh output videos reset Chromium media state and recover transient first-load failures", () => {
  const component = studioSource.match(/function ResultVideo[\s\S]*?\n\}/)?.[0] ?? "";
  assert.match(component, /videoRef\.current\?\.load\(\)/);
  assert.match(component, /\[source, identity, attempt\]/);
  assert.match(component, /preload="metadata"/);
  assert.match(component, /onError=\{retryTransientFailure\}/);
  assert.match(component, /attempt >= 3/);
  assert.match(studioSource, /<ResultVideo key=/);
});

test("result cards keep their four actions in a compact two-column toolbar", () => {
  assert.match(globalsSource, /\.result-library-actions \{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(globalsSource, /\.result-library-actions a, \.result-library-actions button \{[^}]*min-height: 32px[^}]*white-space: nowrap/);
  assert.match(studioSource, /"＋ 添加到画布"/);
  assert.match(studioSource, /label="下载" startedLabel="已开始"/);
  assert.match(studioSource, /"删除结果"/);
});

test("result preview selects the Output actually connected to the matching generator", () => {
  const nodes = [
    { id: "video-a", kind: "video" },
    { id: "video-b", kind: "video" },
    { id: "output-a", kind: "output" },
    { id: "output-b", kind: "output" },
  ];
  const edges = [
    { source: "video-a", target: "output-a" },
    { source: "video-b", target: "output-b" },
  ];
  assert.deepEqual(resolveResultPreviewTarget(nodes, edges, { "video-a": "old", "video-b": "selected" }, "selected", "video"), {
    generatorId: "video-b",
    outputId: "output-b",
  });
});

test("result preview never selects an unrelated Output when the generator is disconnected", () => {
  const nodes = [
    { id: "video-a", kind: "video" },
    { id: "output-unrelated", kind: "output" },
    { id: "image-a", kind: "image" },
  ];
  const edges = [{ source: "image-a", target: "output-unrelated" }];
  assert.equal(resolveResultPreviewTarget(nodes, edges, {}, "selected", "video"), undefined);
});

test("an unattached result skips a disconnected first generator and uses the next valid path", () => {
  const nodes = [
    { id: "video-disconnected", kind: "video" },
    { id: "video-connected", kind: "video" },
    { id: "output-a", kind: "output" },
  ];
  const edges = [{ source: "video-connected", target: "output-a" }];
  assert.deepEqual(resolveResultPreviewTarget(nodes, edges, {}, "new-history-result", "video"), {
    generatorId: "video-connected",
    outputId: "output-a",
  });
});

test("an attached result never migrates to a different generator silently", () => {
  const nodes = [
    { id: "video-attached", kind: "video" },
    { id: "video-other", kind: "video" },
    { id: "output-a", kind: "output" },
  ];
  const edges = [{ source: "video-other", target: "output-a" }];
  assert.equal(resolveResultPreviewTarget(nodes, edges, { "video-attached": "selected" }, "selected", "video"), undefined);
});

test("result preview rejects canvases without a matching generator", () => {
  const nodes = [{ id: "output-a", kind: "output" }, { id: "image-a", kind: "image" }];
  assert.equal(resolveResultPreviewTarget(nodes, [], {}, "selected", "video"), undefined);
});

test("result preview rejects canvases without an Output node", () => {
  const nodes = [{ id: "video-a", kind: "video" }];
  assert.equal(resolveResultPreviewTarget(nodes, [], { "video-a": "selected" }, "selected", "video"), undefined);
});
