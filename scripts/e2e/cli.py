"""Command-line entry point for remote H3 Studio E2E validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .client import ApiClient, E2EError
from .runner import execute_run
from .scenarios import SCENARIOS, dry_run_plan, load_manifest, resolve_profile


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Run reproducible H3 Studio remote API E2E scenarios")
    command.add_argument("--manifest", type=Path, help="versioned JSON scenario manifest")
    command.add_argument("--base-url", default=os.environ.get("H3_E2E_BASE_URL", "http://127.0.0.1:3013"))
    command.add_argument("--api-key-env", default="H3_STUDIO_API_KEY", help="environment variable containing the API key")
    command.add_argument("--output-dir", type=Path, default=Path("artifacts/e2e"))
    command.add_argument("--asset-root", type=Path, help="explicit directory allowed to contain manifest assets (defaults to manifest directory)")
    command.add_argument("--report", type=Path, help="write JSON evidence to this path")
    command.add_argument("--timeout", type=float, default=1800)
    command.add_argument("--interval", type=float, default=3)
    command.add_argument("--ffprobe", default="ffprobe")
    command.add_argument("--dry-run", action="store_true", help="print plans and never upload or generate")
    command.add_argument("--fetch-capabilities", action="store_true", help="allow the read-only capability query during dry-run")
    command.add_argument("--capabilities", type=Path, help="offline capabilities JSON used to resolve exact profile identity")
    command.add_argument("--list-scenarios", action="store_true")
    return command


def _read_capabilities(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read capabilities {path}: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("profiles"), list):
        raise ValueError("capabilities JSON must contain a profiles array")
    return value


def _write_report(path: Path | None, evidence: list[dict[str, Any]]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "runs": evidence}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    evidence: list[dict[str, Any]] = []
    if args.list_scenarios:
        for scenario in SCENARIOS.values():
            references = ", ".join(f"{slot.kind}:{slot.role}" for slot in scenario.references) or "none"
            print(f"{scenario.name:10} {scenario.description} [{references}]")
        return 0
    if not args.manifest:
        parser().error("--manifest is required unless --list-scenarios is used")
    try:
        runs = load_manifest(args.manifest.resolve(), asset_root=args.asset_root.resolve() if args.asset_root else None)
        api_key = os.environ.get(args.api_key_env, "")
        client = ApiClient(args.base_url, api_key=api_key)
        capabilities = _read_capabilities(args.capabilities) if args.capabilities else None
        if args.dry_run:
            if args.fetch_capabilities:
                capabilities = client.capabilities()
            plans = []
            for run in runs:
                profile = None
                if capabilities:
                    scenario = SCENARIOS[str(run["scenario"])]
                    profile = resolve_profile(
                        capabilities["profiles"], scenario,
                        sampling_mode=str(run.get("sampling_mode", "turbo4")),
                        profile_id=str(run.get("profile_id", "")),
                    )
                plans.append(dry_run_plan(run, profile))
            print(json.dumps({"version": 1, "plans": plans}, ensure_ascii=False, indent=2))
            return 0

        for run in runs:
            for path in run.get("assets", []):
                if not Path(path).is_file():
                    raise E2EError(f"asset does not exist: {path}")
            print(f"[{run['scenario']}] starting", file=sys.stderr, flush=True)
            record = execute_run(
                client, run, output_dir=args.output_dir,
                timeout=args.timeout, interval=args.interval, ffprobe_executable=args.ffprobe,
                on_status=lambda status, name=run["scenario"]: print(
                    f"[{name}] {status.get('status')} {status.get('progress', 0)}%", file=sys.stderr, flush=True,
                ),
            )
            evidence.append(record)
            _write_report(args.report, evidence)
            print(f"[{run['scenario']}] PASS {record['download']['path']}", file=sys.stderr, flush=True)
        print(json.dumps({"version": 1, "runs": evidence}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, E2EError) as error:
        partial = getattr(error, "partial_evidence", None)
        if isinstance(partial, dict):
            evidence.append(partial)
            _write_report(args.report, evidence)
        print(f"E2E ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
