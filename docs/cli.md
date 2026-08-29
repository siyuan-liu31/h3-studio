# H3 Studio Go CLI (`h3ctl`)

`h3ctl` is the scriptable client for the existing H3 Studio Python API. It does not compile ComfyUI graphs or read server storage directly. Generation continues on the server when the CLI exits; reconnect with `job wait` using the returned job ID.

## Build

```bash
cd cli
go build -o h3ctl ./cmd/h3ctl
./h3ctl --help
```

The CLI defaults to `http://127.0.0.1:6020`. A one-shot direct connection can use `--server`. Named contexts support either a direct URL or a temporary SSH tunnel:

```bash
h3ctl context add local --server http://127.0.0.1:6020 --use
h3ctl context add dev --ssh-target h3-dev --remote-api-port 6020
h3ctl context test dev
```

An SSH context stores only its target, optional SSH port, and remote API port. Each API command starts a temporary local forward to remote `127.0.0.1:6020`, waits until it is ready, keeps it alive for the entire command (including a long `job wait`), then closes and reaps it. `context list`, `show`, `add`, `update`, `use`, and `remove` never start SSH; `context test` does.

Use an SSH config alias so a rented machine can change addresses without changing the H3 context:

```sshconfig
Host h3-dev
  HostName gpu.example.com
  User your-user
  Port 2222
  IdentityFile ~/.ssh/id_ed25519
```

After an address or port change, edit only `HostName`/`Port` above. Alternatively update stored connection settings with `h3ctl context update dev --ssh-target new-alias --ssh-port 12900`, then run `h3ctl context test dev`. Use `h3ctl context update dev --clear-ssh-port` to return to the `Port` selected by SSH config. An update with no connection flags is rejected, and `context add` never overwrites an existing name.

Passwords and identity files are never stored by `h3ctl`; standard SSH config, public-key authentication, and `ssh-agent` are recommended. SSH is always started with `-n`, so command JSON from `--spec -` or `--input -` remains exclusively owned by `h3ctl`; `--non-interactive` additionally adds `BatchMode=yes`, which is appropriate for Agents and fails instead of prompting for a password. `H3_STUDIO_URL` and `H3CTL_CONFIG` remain available for direct/default configuration.

Connection startup uses a private temporary SSH ControlMaster socket. The master still reads the named host from your SSH config (including HostName, User, IdentityFile, ProxyJump, and Port), but `h3ctl` uses `-N`/`SessionType=none` and command-line overrides to disable remote sessions, backgrounding, TTY allocation, and inherited forwards for this session. Its private check/forward/exit control calls ignore user SSH config and request exactly one CLI-owned local forward. `h3ctl` first checks that the authenticated master is alive, then asks that exact master to bind the local forward; a conflicting local port is reallocated and retried before any health request. Only after the forward command succeeds does it validate the strict H3 `/health` JSON contract, including an exact JSON content type and a 64 KiB response limit. Both normal (`200`, ComfyUI healthy) and H3 degraded (`503`) contracts are accepted, while redirects, unrelated listeners, trailing JSON/content, and wrong content types are rejected. Tunnel termination, process reaping, and private-directory cleanup are bounded and complete before a success envelope is printed. Cleanup failures return `ssh_cleanup_failed`; Ctrl-C during forwarding returns `interrupted`, and a forwarding startup deadline returns `ssh_start_timeout`. Platforms or SSH builds without ControlMaster support return an explicit SSH control/start failure instead of falling back to an ambiguous listener probe.

## Agent output contract

```bash
h3ctl --output json capability list
h3ctl job wait job:ID --output jsonl --timeout 2h
h3ctl operation schema generate.video --json
```

JSON commands emit one `h3ctl.output/v1` envelope to stdout. JSONL wait progress is emitted to stdout as event objects, beginning with `submitted` when generation has been accepted; human progress and diagnostics use stderr. Table mode renders scalar columns or key/value rows. Errors have stable `code`, `message`, `retryable`, optional HTTP `status`, and optional `details` fields.

Help, unknown command/subcommand handling, `workflow`, `completion`, `operation list`, and `operation schema` are local operations. They do not start the current SSH context. `asset copy` and `operation run asset.copy` open only their declared source and destination contexts.

Help is intentionally plaintext, including when `--json` or `--output jsonl` is present. `--` ends global flag extraction; every following token belongs to the selected command.

Exit codes are: `2` invalid usage, `3` authentication/authorization, `4` missing resource, `5` wait timeout, `6` failed generation, `7` cancelled generation, and `8` unsupported server contract. Other operational failures return `1`.

`--control-timeout` (legacy alias `--request-timeout`) defaults to 30 seconds for health, status, metadata and submission requests. `--transfer-timeout` and `--media-timeout` default to `0` (unlimited) so a large upload/download or ffmpeg derivation is not accidentally killed by the control-plane default. `job wait --timeout` and `generate ... --wait-timeout` separately limit the complete wait. Negative timeouts are rejected. `Ctrl-C` stops the local wait only; it never cancels the remote job. Cancellation requires `job cancel`.

Redirects are never followed, so requests cannot silently move to a different origin. Generation submission uses the same payload and `request_id` to retry an ambiguous network disconnect; if recovery is exhausted, the error includes that `request_id`. After acceptance, wait or download errors include the `job_id`, `request_id`, and submission receipt needed to resume safely.

## Resource locators and transfers

Commands consistently accept:

```text
./frame.png
file:///absolute/frame.png
asset:ASSET_ID
job:JOB_ID#OUTPUT_INDEX
media:DERIVATION_ID
h3://CONTEXT/assets/ASSET_ID
```

Local generation inputs are uploaded first. Job and derivation inputs are materialized as internal server assets and do not appear in the user library until explicitly saved. A locator from another context must first be copied with `asset copy`; cross-machine transfers stream through an isolated local temporary file. Downloads use a unique same-directory `.part` file and an atomic commit. Non-force commits are atomic no-replace; `--force` atomically replaces only after a complete download, so an interrupted transfer preserves the old destination.

`file:` locators accept an empty authority or `localhost`; `file://localhost/path` means the local machine running `h3ctl`. Other file authorities are rejected instead of being mapped to local storage. Locator userinfo, query strings, and fragments are rejected (except the documented `job:ID#INDEX` output selector), and validation errors do not echo secret-bearing URIs. All server-created asset, job, derivation, project, and segment IDs are exactly 32 lowercase hexadecimal characters.

Directory upload is explicit and deterministic. Recursive traversal rejects symbolic links rather than following them:

```bash
h3ctl asset upload ./references --recursive \
  --include '*.png' --include '*.mp4' --output json
```

## Generation

Image:

```bash
h3ctl generate image \
  --prompt-file prompt.txt \
  --ref ./character.png \
  --profile auto --aspect-ratio 3:4 \
  --wait --download ./output.png
```

Video modes are explicit:

```bash
h3ctl generate video \
  --mode fl2v \
  --first-frame ./first.png \
  --last-frame asset:LAST_FRAME_ID \
  --prompt-file prompt.txt \
  --duration 10 \
  --wait --wait-timeout 2h \
  --download ./shot.mp4
```

Supported modes are `t2v`, `i2v`, `fl2v`, `r2v`, `v2v`, and `rv2v`. A bare `--ref LOCATOR` is always treated literally, so filenames and URIs containing `=` or `,` work without escaping. Structured roles use the explicit `json:` prefix, for example `--ref 'json:{"role":"identity","source":"asset:ID"}'`. `--ref-dir 'json:{"role":"reference","path":"./dir,with,commas"}'` is the recommended unambiguous directory form; legacy `role=reference,path=...` remains supported when the path contains no comma. Directory expansion uses stable order and enforces the six-reference boundary before submission. All three video entry points—typed flags, `--spec`, and `operation run generate.video`—default to `prompt_mode=preserve_tags_only`.

By default generation only submits and returns the durable job ID:

```bash
h3ctl generate video --mode t2v --prompt 'A sunrise' --json
h3ctl job wait job:ID --timeout 0 --output jsonl
h3ctl job download job:ID --to ./sunrise.mp4
```

Explicit profile IDs are resolved against `/api/capabilities`, and their current `profile_version` and `manifest_sha256` are submitted automatically. A generation `request_id` is always supplied for idempotency; pass `--request-id` to reuse one across submission retries.

## Frames and other atomic media operations

```bash
h3ctl media frame job:ID#0 --position first
h3ctl media frame job:ID#0 --position current --at 3.5
h3ctl media endpoints job:ID#0
h3ctl media trim asset:ID --start 1 --end 5
h3ctl media extract-audio asset:ID
h3ctl media remove-audio asset:ID
```

These call `/api/media/derive` and return derivation receipts. Use `media save` to promote a receipt into the asset library, or `media download` to download it locally.

## Atomic operations and future workflows

The human command tree and future workflow runner share reusable Go operations. Agents can discover them without scraping help text:

```bash
h3ctl operation list --json
h3ctl operation schema media.frame --json
h3ctl operation run media.frame --input request.json --json
```

`operation run` validates required fields, primitive types, enums, unknown fields, and integer values against the same schema returned by `operation schema`; it does not truncate numbers. Generation operation references accept the same local/file/asset/job/media locators as typed generation. `workflow` is reserved in v1 and returns an explicit `unsupported` error. A later resumable DAG engine can call the same operation service objects directly instead of spawning nested CLI processes. The raw ComfyUI graph for a completed generation remains available separately through `job workflow`.

## Current API boundaries

Commands map only to real server endpoints. Asset rename/folder/pin, job lifecycle, derivations, and long-video projects are supported. Operations for which the Python API has no contract return `unsupported`; the CLI never reports fabricated success. Shell completion and the resumable workflow runner are intentionally reserved for a later release.
