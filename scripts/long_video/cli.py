"""Command-line entry point for long-video remote acceptance."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Sequence

from scripts.e2e.client import E2EError

from .manifest import load_manifest
from .runner import dry_run_plan, execute_manifest, make_client


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Exercise the MiniMax H3 Video Studio long-video API and verify the merged output")
    command.add_argument("--manifest", type=Path, required=True)
    command.add_argument("--base-url", default=os.environ.get("H3_E2E_BASE_URL", "http://127.0.0.1:3013"))
    command.add_argument("--api-key-env", default="H3_E2E_API_KEY", help="environment variable containing the key; never a literal key")
    command.add_argument("--output-dir", type=Path, default=Path("artifacts/long-video"))
    command.add_argument("--timeout", type=float, default=3600, help="per-phase timeout in seconds")
    command.add_argument("--interval", type=float, default=3, help="poll interval in seconds")
    command.add_argument("--ffprobe", default="ffprobe")
    command.add_argument("--stop-after-index", type=int, help="exercise stop after this zero-based segment completes")
    command.add_argument("--dry-run", action="store_true", help="validate and print the call plan without network or filesystem writes")
    return command


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.timeout <= 0 or args.interval <= 0:
            raise ValueError("timeout and interval must be positive")
        if ENV_NAME.fullmatch(args.api_key_env) is None:
            raise ValueError("api-key-env must be an environment variable name")
        if args.dry_run:
            print(json.dumps(dry_run_plan(manifest, stop_after_index=args.stop_after_index), ensure_ascii=False, indent=2))
            return 0
        api_key = os.environ.get(args.api_key_env, "")
        client = make_client(args.base_url, api_key, min(args.timeout, 120))
        evidence = execute_manifest(
            client, manifest, output_dir=args.output_dir,
            timeout=args.timeout, interval=args.interval,
            ffprobe_executable=args.ffprobe, stop_after_index=args.stop_after_index,
        )
        print(json.dumps({
            "status": evidence["status"], "project_id": evidence["project_id"],
            "evidence_path": evidence["evidence_path"], "download": evidence.get("download"),
        }, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, E2EError, OSError) as error:
        print(f"long-video acceptance failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
