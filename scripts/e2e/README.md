# H3 Studio remote API E2E

This standard-library Python tool exercises the deployed web/API boundary instead of importing server internals. It queries capabilities, resolves an available concrete profile, pins its `id`, `version`, and `manifest_sha256`, streams reference uploads, submits a canonical graph, polls the durable job, downloads the output, checks its SHA-256, and validates it with `ffprobe`.

Supported scenarios:

- `t2i`
- `img2img`
- `t2v`
- `i2v`
- `fl2va`
- `ref-image`
- `ref-video`

## Dry-run

Dry-run performs no network access unless `--fetch-capabilities` is explicitly present:

```bash
python3 -m scripts.e2e \
  --manifest scripts/e2e/example-manifest.json \
  --dry-run
```

To audit the exact version/digest-pinned request without contacting a server, save a prior `/api/capabilities` response and pass it with `--capabilities capabilities.json`.

## Remote execution

After replacing the example fixture paths with owned test media:

```bash
export H3_E2E_BASE_URL=http://127.0.0.1:16020
export H3_STUDIO_API_KEY='set-outside-shell-history'
python3 -m scripts.e2e \
  --manifest /absolute/path/to/run-manifest.json \
  --output-dir artifacts/e2e \
  --report artifacts/e2e/report.json
```

Use the frontend origin for `H3_E2E_BASE_URL`; its same-origin gateway injects the backend key when configured. Alternatively point directly at the backend and provide the key environment variable. The CLI never accepts or stores a literal key in a manifest.

For video, verification requires a positive duration, 24 fps, expected dimensions, and an audio stream. A generation run may request up to the exact final H3 grid point, `362 / 24` seconds (`15.083333...`, displayed as 15.08 seconds); the client then requires exactly 362 decoded frames. This generation-output limit does not relax the upload policy for user-supplied reference video or audio, which remains 15 seconds per clip and in total. Video runs may set `denoise` from `0.05` to `1` (default `1.0`); the client verifies both resolved job parameters and the actual `BasicScheduler` evidence. Video runs do not accept image CFG. Image verification requires a decodable visual stream and expected dimensions. Download SHA-256 is compared with server evidence when available.

The manifest schema is [manifest.schema.json](manifest.schema.json). Paths are resolved relative to the manifest file. `ref-video` may set `include_audio: true`; the uploaded clip must then contain an audio track and satisfy the server's reference-duration policy.

## Tests

```bash
python3 -m unittest discover -s scripts/e2e/tests -v
```
