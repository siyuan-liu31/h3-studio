import assert from "node:assert/strict";
import test from "node:test";

import {
  HISTORY_LIMIT,
  JOB_HISTORY_CACHE_TTL_MS,
  currentOriginApiUrl,
  formatJobElapsed,
  jobParameterRows,
  mergeJobHistory,
  mergeJobHistoryPage,
  mergeJobReceipt,
  parseJobHistoryCache,
  parseJobHistoryCacheEnvelope,
  rebaseStudioJobMedia,
  serializeJobHistoryCache,
  serverJobToStudioJob,
} from "../app/studio-history.ts";

function job(id, overrides = {}) {
  return {
    id,
    status: "completed",
    progress: 100,
    message: "生成完成",
    createdAt: `2026-08-19T00:00:${String(Number(id) || 0).padStart(2, "0")}.000Z`,
    ...overrides,
  };
}

test("job history updates an existing task in place and preserves its result", () => {
  const queued = job("42", { status: "queued", progress: 0, message: "排队中" });
  const unrelated = job("41", { media: "image" });
  const completed = job("42", {
    downloadUrl: "/api/download?id=42",
    previewUrl: "/api/view?id=42",
    parameters: { profile_id: "h3-fl-turbo", steps: 4 },
  });

  assert.deepEqual(mergeJobHistory([queued, unrelated], completed), [completed, unrelated]);
});

test("job history is newest-first and bounded without admitting id-less drafts", () => {
  const history = Array.from({ length: HISTORY_LIMIT + 5 }, (_, index) => job(String(index)));
  const bounded = mergeJobHistory(history, job("new"));

  assert.equal(bounded.length, HISTORY_LIMIT);
  assert.equal(bounded[0].id, "new");
  assert.deepEqual(mergeJobHistory(bounded, job(undefined)), bounded);
});

test("job history cache keeps a bounded searchable prompt and expires safely", () => {
  const now = Date.parse("2026-08-22T12:00:00Z");
  const cachedJob = job("cache-1", {
    previewUrl: "https://old-host.invalid/api/preview?id=cache-1",
    downloadUrl: "https://old-host.invalid/api/download?id=cache-1",
    prompt: "large prompt should not be stored in the fast cache",
    workflowEvidence: { large: "value" },
  });
  const encoded = serializeJobHistoryCache([cachedJob], now);
  assert.match(encoded, /large prompt/);
  assert.doesNotMatch(encoded, /workflowEvidence/);
  assert.equal(parseJobHistoryCache(encoded, now + 1_000)[0]?.previewUrl, "/api/preview?id=cache-1");
  assert.deepEqual(parseJobHistoryCache(encoded, now + JOB_HISTORY_CACHE_TTL_MS + 1), []);
  assert.deepEqual(parseJobHistoryCache("not-json", now), []);
});

test("a delayed list response cannot regress a completed cached result", () => {
  const completed = job("stable", {
    updatedAt: "2026-08-22T12:00:10Z",
    previewUrl: "/api/preview?id=stable",
  });
  const staleQueued = job("stable", {
    status: "queued",
    progress: 10,
    updatedAt: "2026-08-22T12:00:01Z",
    previewUrl: undefined,
  });
  const [merged] = mergeJobHistory([completed], staleQueued);
  assert.equal(merged.status, "completed");
  assert.equal(merged.previewUrl, "/api/preview?id=stable");
});

test("history cache is instance-scoped, bounded, and rejects future timestamps", () => {
  const now = Date.parse("2026-08-22T12:00:00Z");
  const encoded = serializeJobHistoryCache([job("remote")], now, "instance-a");
  assert.deepEqual(parseJobHistoryCacheEnvelope(encoded, now), {
    instanceId: "instance-a",
    jobs: [parseJobHistoryCache(encoded, now)[0]],
  });
  assert.deepEqual(parseJobHistoryCache(encoded, now - 61_000), []);
});

test("summary refresh preserves omitted local fields and terminal failures", () => {
  const local = job("stable-failure", {
    status: "failed",
    prompt: "keep this searchable prompt",
    updatedAt: "2026-08-22T12:00:10Z",
  });
  const stale = job("stable-failure", {
    status: "queued",
    prompt: undefined,
    updatedAt: "2026-08-22T12:00:01Z",
  });
  const [merged] = mergeJobHistory([local], stale);
  assert.equal(merged.status, "failed");
  assert.equal(merged.prompt, "keep this searchable prompt");
});

test("cursor pages append without displacing the stable first page", () => {
  const first = [job("new-2"), job("new-1")];
  const older = [job("old-2"), job("old-1")];
  assert.deepEqual(mergeJobHistoryPage(first, older, true).map((item) => item.id), ["new-2", "new-1", "old-2", "old-1"]);
  assert.deepEqual(mergeJobHistoryPage(first, [], true), first);
});

test("overlapping cursor pages update rows in place without reordering cached history", () => {
  const history = [job("new-2"), job("new-1"), job("old-2", { prompt: "cached" }), job("old-1")];
  const overlap = [job("old-2", { prompt: "server" }), job("old-1"), job("older")];
  const merged = mergeJobHistoryPage(history, overlap, true);
  assert.deepEqual(merged.map((item) => item.id), ["new-2", "new-1", "old-2", "old-1", "older"]);
  assert.equal(merged[2].prompt, "server");
});

test("authoritative cursor window removes deleted cached rows without discarding the older tail", () => {
  const at = (id, seconds) => job(id, { createdAt: new Date(seconds * 1000).toISOString() });
  const history = [at("prior", 50), at("gone", 40), at("page-b", 30), at("tail", 20)];
  const page = [at("page-b", 30), at("page-c", 25)];
  const merged = mergeJobHistoryPage(history, page, true, true, 100, "45:cursor-id");
  assert.deepEqual(merged.map((item) => item.id), ["prior", "page-b", "page-c", "tail"]);
  assert.deepEqual(mergeJobHistoryPage(history, page, true, false, 100, "45:cursor-id").map((item) => item.id), ["prior", "page-b", "page-c"]);
});

test("empty final cursor page removes the deleted cached tail", () => {
  const at = (id, seconds) => job(id, { createdAt: new Date(seconds * 1000).toISOString() });
  const history = [at("prior", 50), at("gone-a", 40), at("gone-b", 30)];
  assert.deepEqual(mergeJobHistoryPage(history, [], true, false, 100, "45:cursor-id").map((item) => item.id), ["prior"]);
});

test("authoritative cursor refresh retains pinned results outside the refreshed window", () => {
  const at = (id, seconds, overrides = {}) => job(id, { createdAt: new Date(seconds * 1000).toISOString(), ...overrides });
  const history = [at("prior", 50), at("pinned-old", 10, { pinned: true }), at("gone", 5)];
  const page = [at("page", 30)];
  assert.deepEqual(
    mergeJobHistoryPage(history, page, true, false, 100, "45:cursor-id").map((item) => item.id),
    ["prior", "pinned-old", "page"],
  );
});

test("authoritative first-page refresh removes deleted rows and clears an empty server", () => {
  const newest = job("newest", { createdAt: "2026-08-22T12:00:30Z" });
  const deleted = job("deleted", { createdAt: "2026-08-22T12:00:20Z" });
  const boundary = job("boundary", { createdAt: "2026-08-22T12:00:10Z" });
  const older = job("older", { createdAt: "2026-08-22T12:00:01Z" });
  assert.deepEqual(mergeJobHistoryPage([newest, deleted, boundary, older], [newest, boundary], false, true).map((item) => item.id), ["newest", "boundary", "older"]);
  assert.deepEqual(mergeJobHistoryPage([newest, deleted], [], false), []);
  assert.deepEqual(mergeJobHistoryPage([newest], [], true), [newest]);
});

test("video receipt exposes the resolved Base contract and explicitly reports no LoRA", () => {
  const rows = jobParameterRows({
    output_type: "video",
    profile_id: "h3-fl-base",
    profile_version: "1",
    sampling_mode: "base",
    width: 1344,
    height: 768,
    duration_actual: 5.1667,
    frames: 124,
    steps: 20,
    sampler: "res_multistep",
    scheduler: "simple",
    lora: null,
    lora_strength: 0,
    denoise: 0.8,
    seed: 7,
  });

  assert.deepEqual(Object.fromEntries(rows.map(({ label, value }) => [label, value])), {
    "Resolved Profile": "h3-fl-base@1",
    "模式": "base",
    "尺寸": "1344×768",
    "时长": "5.1667s / 124f",
    Steps: "20",
    "Sampler / Scheduler": "res_multistep / simple",
    LoRA: "未加载",
    "调度去噪比例（实验）": "0.8",
    Seed: "7",
  });
});

test("status hydration keeps durable list timestamps while refreshing execution fields", () => {
  const merged = mergeJobReceipt(
    { id: "job-time", created_at: 1_700_000_000, updated_at: 1_700_000_001, status: "queued" },
    { id: "job-time", status: "completed", preview_url: "/preview" },
  );
  const restored = serverJobToStudioJob(merged);
  assert.equal(restored?.status, "completed");
  assert.equal(restored?.previewUrl, "/preview");
  assert.equal(restored?.createdAt, "2023-11-14T22:13:20.000Z");
  assert.equal(restored?.updatedAt, "2023-11-14T22:13:21.000Z");
});

test("result cards format user-perceived generation elapsed time", () => {
  assert.equal(formatJobElapsed("2026-08-21T00:00:00Z", "2026-08-21T00:00:42Z"), "耗时 42 秒");
  assert.equal(formatJobElapsed("2026-08-21T00:00:00Z", "2026-08-21T00:02:05Z"), "耗时 2 分 5 秒");
  assert.equal(formatJobElapsed("2026-08-21T00:00:00Z"), "耗时未知");
  assert.equal(formatJobElapsed("bad", "2026-08-21T00:00:00Z"), "耗时未知");
});

test("video and image receipts keep independent resolved profiles and sampling values", () => {
  const videoRows = jobParameterRows({
    output_type: "video",
    profile_id: "h3-ref-turbo",
    profile_version: "2",
    sampling_mode: "turbo4",
    steps: 4,
    sampler: "sa_solver",
    scheduler: "simple",
    lora: "h3_turbo.safetensors",
    lora_strength: 0.75,
  });
  const imageRows = jobParameterRows({
    output_type: "image",
    profile_id: "sdxl-image",
    profile_version: "3",
    mode: "image-to-image",
    steps: 28,
    sampler: "euler_ancestral",
    scheduler: "normal",
  });
  const values = (rows) => Object.fromEntries(rows.map(({ label, value }) => [label, value]));

  assert.equal(values(videoRows)["Resolved Profile"], "h3-ref-turbo@2");
  assert.equal(values(videoRows).Steps, "4");
  assert.equal(values(videoRows).LoRA, "h3_turbo.safetensors @ 0.75");
  assert.equal(values(imageRows)["Resolved Profile"], "sdxl-image@3");
  assert.equal(values(imageRows).Steps, "28");
  assert.equal(values(imageRows)["Sampler / Scheduler"], "euler_ancestral / normal");
  assert.equal(values(imageRows).LoRA, "—");
});

test("durable server receipts restore completed downloads across browsers", () => {
  const restored = serverJobToStudioJob({
    id: "remote-job",
    status: "completed",
    progress: 100,
    output_type: "video",
    created_at: 1_700_000_000,
    prompt: "A robot waves",
    parameters: { profile_id: "minimax-h3-fl2va-base", profile_version: "1.0", steps: 20 },
    outputs: [{ filename: "result.mp4" }],
    pinned: true,
  });
  assert.equal(restored?.id, "remote-job");
  assert.equal(restored?.media, "video");
  assert.equal(restored?.downloadUrl, "/api/download?id=remote-job&index=0");
  assert.equal(restored?.previewUrl, "/api/preview?id=remote-job&index=0");
  assert.equal(restored?.parameters?.steps, 20);
  assert.equal(restored?.createdAt, "2023-11-14T22:13:20.000Z");
  assert.equal(restored?.pinned, true);
});

test("durable receipts preserve exact workflow evidence for authenticated job export", () => {
  const sha = "a".repeat(64);
  const restored = serverJobToStudioJob({
    id: "workflow-job",
    status: "completed",
    output_type: "video",
    workflow_sha256: sha,
    workflow_evidence: { sha256: sha, director_mode: "v2v" },
  });
  assert.equal(restored?.workflowSha256, sha);
  assert.deepEqual(restored?.workflowEvidence, { sha256: sha, director_mode: "v2v" });
});

test("cloned-machine receipts rebase old absolute media links onto the active Studio origin", () => {
  assert.equal(currentOriginApiUrl("http://127.0.0.1:16020/api/download?id=job-1"), "/api/download?id=job-1");
  assert.equal(currentOriginApiUrl("https://old-host.invalid/preview?id=job-1"), "/preview?id=job-1");
  assert.equal(currentOriginApiUrl("https://untrusted.invalid/file.mp4", "/api/download?id=fallback"), "/api/download?id=fallback");
  const restored = serverJobToStudioJob({
    id: "cloned-job",
    status: "completed",
    output_type: "video",
    preview_url: "http://old-machine:3013/api/preview?id=cloned-job&index=0",
    thumbnail_url: "http://old-machine:3013/api/jobs/cloned-job/thumbnail?index=0",
    download_url: "http://old-machine:3013/api/download?id=cloned-job&index=0",
  });
  assert.equal(restored?.previewUrl, "/api/preview?id=cloned-job&index=0");
  assert.equal(restored?.thumbnailUrl, "/api/jobs/cloned-job/thumbnail?index=0");
  assert.equal(restored?.downloadUrl, "/api/download?id=cloned-job&index=0");
  const browserSaved = rebaseStudioJobMedia(job("saved-job", {
    previewUrl: "http://old-machine:3013/api/preview?id=saved-job",
    downloadUrl: "http://old-machine:3013/api/download?id=saved-job",
  }));
  assert.equal(browserSaved.previewUrl, "/api/preview?id=saved-job");
  assert.equal(browserSaved.downloadUrl, "/api/download?id=saved-job");
});
