import assert from "node:assert/strict";
import test from "node:test";

import { imageDimensions } from "../app/studio-config.ts";
import { remoteAssetToLibraryItem, remoteDerivationToResult } from "../app/studio-library.ts";

test("image quality and aspect ratio map to the exact requested dimensions", () => {
  const expected = {
    "1K/16:9": [1024, 576],
    "1K/9:16": [576, 1024],
    "1K/3:4": [768, 1024],
    "1K/1:1": [1024, 1024],
    "2K/16:9": [2048, 1152],
    "2K/9:16": [1152, 2048],
    "2K/3:4": [1536, 2048],
    "2K/1:1": [2048, 2048],
  };
  for (const [key, dimensions] of Object.entries(expected)) {
    const [quality, aspect] = key.split("/");
    assert.deepEqual(imageDimensions(quality, aspect), { width: dimensions[0], height: dimensions[1] });
  }
});

test("remote asset receipts become same-origin reusable library items", () => {
  const id = "a".repeat(32);
  assert.deepEqual(remoteAssetToLibraryItem({
    id,
    kind: "video",
    filename: "reference.mp4",
    content_url: "https://untrusted.example/leak",
    content_hash: "f".repeat(64),
    size: 1234,
    created_at: 1_700_000_000,
    media: { duration: 5.25, has_audio: true, reference_fps: 24 },
  }), {
    id,
    kind: "video",
    filename: "reference.mp4",
    contentUrl: `/api/assets/${id}/content`,
    thumbnailUrl: `/api/assets/${id}/thumbnail`,
    contentHash: "f".repeat(64),
    size: 1234,
    pinned: false,
    createdAt: "2023-11-14T22:13:20.000Z",
    media: { duration: 5.25, has_audio: true, reference_fps: 24 },
  });
});

test("asset library exposes content-level duplicate cleanup and batch deletion", async () => {
  const source = await import("node:fs/promises").then(({ readFile }) => readFile(new URL("../app/studio.tsx", import.meta.url), "utf8"));
  const start = source.indexOf("function AssetLibrary(");
  const end = source.indexOf("function ResultThumbnail(", start);
  const assetLibrary = source.slice(start, end);
  assert.match(assetLibrary, /seenHashes/);
  assert.match(assetLibrary, /item\.contentHash/);
  assert.match(assetLibrary, /选择重复项/);
  assert.match(assetLibrary, /Promise\.allSettled/);
  assert.match(assetLibrary, /删除所选/);
});

test("asset library rejects malformed ids and unsupported media kinds", () => {
  assert.equal(remoteAssetToLibraryItem({ id: "../escape", kind: "image", filename: "x.png" }), undefined);
  assert.equal(remoteAssetToLibraryItem({ id: "b".repeat(32), kind: "document", filename: "x.pdf" }), undefined);
});

test("derivation receipts become durable result entries without becoming assets", () => {
  const id = "d".repeat(32);
  const assetId = "e".repeat(32);
  assert.deepEqual(remoteDerivationToResult({
    id,
    kind: "image",
    display_name: "last-frame.jpg",
    preview_url: `/api/derivations/${id}/content`,
    download_url: `/api/derivations/${id}/download`,
    thumbnail_url: `/api/derivations/${id}/thumbnail`,
    created_at: 1_700_000_000,
    asset_id: assetId,
    stored_name: "must-not-leak.jpg",
    media: { width: 1920, height: 1080 },
  }), {
    id,
    kind: "image",
    displayName: "last-frame.jpg",
    contentUrl: `/api/derivations/${id}/content`,
    thumbnailUrl: `/api/derivations/${id}/thumbnail`,
    downloadUrl: `/api/derivations/${id}/download`,
    createdAt: "2023-11-14T22:13:20.000Z",
    assetId,
    pinned: false,
    media: { width: 1920, height: 1080 },
  });
  assert.equal(remoteDerivationToResult({ id: "../escape", kind: "video" }), undefined);
});

test("asset and derivation receipts preserve durable pin state", () => {
  const assetId = "1".repeat(32);
  const derivationId = "2".repeat(32);
  assert.equal(remoteAssetToLibraryItem({ id: assetId, kind: "image", filename: "pin.png", pinned: true })?.pinned, true);
  assert.equal(remoteDerivationToResult({ id: derivationId, kind: "video", display_name: "pin.mp4", pinned: true })?.pinned, true);
});

test("normalized video receipts preserve source fps for Director frame contracts", () => {
  const id = "c".repeat(32);
  const asset = remoteAssetToLibraryItem({
    id, kind: "video", filename: "30fps.mp4",
    media: { duration: 10, fps: 24, source_fps: 30, reference_fps: 24, frame_count: 300, normalized_to_24fps: true },
  });
  assert.deepEqual(asset?.media, { duration: 10, fps: 24, source_fps: 30, reference_fps: 24, frame_count: 300, normalized_to_24fps: true });
});
