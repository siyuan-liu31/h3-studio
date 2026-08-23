# Cycle 4 development evidence — remote E2E CLI

Scope: `scripts/e2e/**` only. No application or server production code was changed, and no remote generation was started.

Implemented:

- Seven declarative scenarios: T2I, img2img, T2V, I2V, FL2VA, image-reference Ref2VA, and video-reference Ref2VA.
- Versioned JSON run manifest with paths relative to the manifest and a JSON Schema reference.
- Offline dry-run that makes no network requests; optional read-only capability resolution from a file or remote endpoint.
- Capability selection by output type, compiler, sampling mode, availability, optional profile id, version, and 64-character manifest digest.
- Streaming multipart image/video upload without loading the complete asset into memory.
- Canonical typed graph payload with reference roles and explicit profile id/version/digest.
- Durable job polling with terminal failure/timeout handling.
- Atomic output download, SHA-256 comparison, and `ffprobe` assertions for dimensions, duration, 24 fps, and H3 audio.
- Incremental JSON evidence report suitable for preserving real remote run IDs and hashes.

Local verification:

```text
python3 -m unittest discover -s scripts/e2e/tests -v
11 tests passed
```

Tests cover the complete scenario catalog, manifest validation, explicit profile identity, typed graph roles, offline dry-run, streaming multipart upload, authentication header, same-origin download enforcement, ffprobe success/failure, an orchestrated mocked capability→submit→poll→download→probe flow, and acceptance of all seven generated payloads by the real server parser.

Real cloud execution is intentionally deferred to the main agent, who controls GPU spend and the remote tunnel.
