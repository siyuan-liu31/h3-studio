#!/usr/bin/env python3
"""Dependency-light install, diagnosis, and foreground process management."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIN_PYTHON = (3, 11)
MIN_NODE = (22, 13)
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ManagerError(RuntimeError):
    """An actionable management command failure."""


@dataclass(frozen=True, slots=True)
class Check:
    status: str
    name: str
    message: str


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    name: str
    argv: tuple[str, ...]


def _version_command(argv: Sequence[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            list(argv), cwd=PROJECT_ROOT, capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    return result.returncode, (result.stdout or result.stderr).strip()


def _parsed_version(raw: str) -> tuple[int, int] | None:
    match = re.search(r"(?:^|\s)v?(\d+)\.(\d+)", raw)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _valid_url(raw: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(raw)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def doctor_checks(
    *,
    root: Path = PROJECT_ROOT,
    env: Mapping[str, str] | None = None,
    find_executable: Callable[[str], str | None] = shutil.which,
    read_version: Callable[[Sequence[str]], tuple[int, str]] = _version_command,
    check_comfy: bool = False,
    open_url: Callable[..., Any] = urllib.request.urlopen,
) -> list[Check]:
    """Return checks without printing environment values or changing the system."""
    environment = dict(os.environ if env is None else env)
    checks: list[Check] = []
    python_ok = sys.version_info[:2] >= MIN_PYTHON
    checks.append(Check(
        "OK" if python_ok else "ERROR", "Python",
        f"{sys.version_info.major}.{sys.version_info.minor}; requires >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
    ))

    node = find_executable("node")
    if not node:
        checks.append(Check("ERROR", "Node.js", "not found; install Node.js >= 22.13"))
    else:
        code, raw = read_version((node, "--version"))
        version = _parsed_version(raw) if code == 0 else None
        ok = version is not None and version >= MIN_NODE
        shown = ".".join(map(str, version)) if version else "unreadable version"
        checks.append(Check("OK" if ok else "ERROR", "Node.js", f"{shown}; requires >= 22.13"))

    npm = find_executable("npm")
    checks.append(Check("OK" if npm else "ERROR", "npm", "available" if npm else "not found with Node.js"))
    for command in ("ffmpeg", "ffprobe"):
        path = find_executable(command)
        checks.append(Check("OK" if path else "ERROR", command, "available" if path else "not found in PATH"))

    required_files = (
        "package.json", "server/__main__.py", "scripts/start.mjs", ".env.example",
    )
    missing = [name for name in required_files if not (root / name).is_file()]
    checks.append(Check(
        "ERROR" if missing else "OK", "workspace",
        f"missing: {', '.join(missing)}" if missing else "required project files present",
    ))
    installed = (root / "node_modules/vinext/dist/cli.js").is_file()
    built = (root / "dist/server/index.js").is_file()
    checks.append(Check("OK" if installed else "WARN", "frontend dependencies", "installed" if installed else "run the install command"))
    checks.append(Check("OK" if built else "WARN", "production build", "present" if built else "run the install command"))

    comfy_url = environment.get("COMFY_URL", "http://127.0.0.1:6006").rstrip("/")
    if not _valid_url(comfy_url):
        checks.append(Check("ERROR", "COMFY_URL", "must be an HTTP(S) URL without credentials, query, or fragment"))
    elif not check_comfy:
        checks.append(Check("OK", "COMFY_URL", "valid; connectivity not requested"))
    else:
        try:
            request = urllib.request.Request(f"{comfy_url}/system_stats", headers={"Accept": "application/json"})
            with open_url(request, timeout=3) as response:
                status = int(getattr(response, "status", 200))
            checks.append(Check("OK" if 200 <= status < 300 else "WARN", "ComfyUI", f"HTTP {status}"))
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
            checks.append(Check("WARN", "ComfyUI", f"unreachable ({type(error).__name__}); start it before generation"))

    api_key = environment.get("H3_STUDIO_API_KEY", "")
    proxy_key = environment.get("H3_STUDIO_PROXY_API_KEY", "")
    if api_key and proxy_key and api_key != proxy_key:
        checks.append(Check("ERROR", "API key wiring", "API and proxy key variables differ"))
    elif api_key:
        checks.append(Check("OK", "API key wiring", "configured; value hidden"))
    elif proxy_key:
        checks.append(Check("WARN", "API key wiring", "proxy key is set while API authentication is disabled; values hidden"))
    else:
        checks.append(Check("WARN", "API key wiring", "authentication disabled; use only behind loopback/SSH"))
    return checks


def _decode_env_value(raw: str, *, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        if len(value) < 2 or value[-1] != value[0]:
            raise ManagerError(f"environment file line {line_number} has an unterminated quote")
        if value[0] == '"':
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as error:
                raise ManagerError(f"environment file line {line_number} has invalid quoted text") from error
            if not isinstance(decoded, str):
                raise ManagerError(f"environment file line {line_number} value must be text")
            return decoded
        return value[1:-1]
    marker = value.find(" #")
    return value[:marker].rstrip() if marker >= 0 else value


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a conservative dotenv subset without executing shell syntax."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ManagerError(f"cannot read environment file {path}: {error}") from error
    values: dict[str, str] = {}
    for number, source in enumerate(lines, start=1):
        line = source.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ManagerError(f"environment file line {number} must be KEY=VALUE")
        name, raw = line.split("=", 1)
        name = name.strip()
        if ENV_NAME.fullmatch(name) is None:
            raise ManagerError(f"environment file line {number} has an invalid variable name")
        values[name] = _decode_env_value(raw, line_number=number)
    return values


def merged_environment(root: Path, explicit: Path | None, base: Mapping[str, str] | None = None) -> tuple[dict[str, str], Path | None]:
    environment = dict(os.environ if base is None else base)
    candidate = explicit or (root / ".env.local" if (root / ".env.local").is_file() else None)
    if candidate:
        for name, value in load_env_file(candidate).items():
            environment.setdefault(name, value)
    return environment, candidate


def _port(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ManagerError(f"{label} must be an integer from 1 to 65535") from error
    if isinstance(value, float) or not 1 <= parsed <= 65535:
        raise ManagerError(f"{label} must be an integer from 1 to 65535")
    return parsed


def _host(value: str, label: str) -> str:
    if not value or any(character.isspace() for character in value) or any(character in value for character in "/?#"):
        raise ManagerError(f"{label} must be a hostname or IP address")
    return value


def start_plan(
    args: argparse.Namespace,
    *,
    root: Path = PROJECT_ROOT,
    env: Mapping[str, str] | None = None,
    find_executable: Callable[[str], str | None] = shutil.which,
) -> tuple[list[ProcessSpec], dict[str, str]]:
    environment = dict(os.environ if env is None else env)
    public_port = _port(args.port or environment.get("PORT", "3013"), "public port")
    api_port = _port(args.api_port or environment.get("H3_STUDIO_PORT", "6020"), "API port")
    internal_port = _port(
        args.internal_port or environment.get("H3_STUDIO_INTERNAL_WEB_PORT", str(public_port + 1)),
        "internal frontend port",
    )
    if len({public_port, internal_port, api_port}) != 3:
        raise ManagerError("public, internal frontend, and API ports must be different")
    public_host = _host(args.host or environment.get("H3_STUDIO_WEB_HOST", "127.0.0.1"), "public host")
    api_host = _host(args.api_host or environment.get("H3_STUDIO_HOST", "127.0.0.1"), "API host")
    node = find_executable("node") or "node"

    api_key = environment.get("H3_STUDIO_API_KEY", "")
    proxy_key = environment.get("H3_STUDIO_PROXY_API_KEY", "")
    if api_key and proxy_key and api_key != proxy_key:
        raise ManagerError("H3_STUDIO_API_KEY and H3_STUDIO_PROXY_API_KEY differ")
    if api_key and not proxy_key:
        environment["H3_STUDIO_PROXY_API_KEY"] = api_key
    environment.update({
        "H3_STUDIO_HOST": api_host,
        "H3_STUDIO_PORT": str(api_port),
        "H3_STUDIO_WEB_HOST": public_host,
        "H3_STUDIO_INTERNAL_WEB_PORT": str(internal_port),
        "H3_STUDIO_API_PROXY": f"http://127.0.0.1:{api_port}",
        "PORT": str(public_port),
    })
    specs = [
        ProcessSpec("Python API", (sys.executable, "-m", "server")),
        ProcessSpec("production frontend", (node, "scripts/start.mjs", "--hostname", public_host, "--port", str(public_port))),
    ]
    return specs, environment


def _signal_process_group(process: subprocess.Popen[Any], signum: int) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            return
    else:
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            return


class ForegroundSupervisor:
    """Own all child process groups and stop the whole set when one exits."""

    def __init__(
        self,
        *,
        popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        signal_process: Callable[[subprocess.Popen[Any], int], None] = _signal_process_group,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        shutdown_timeout: float = 8,
    ) -> None:
        self.popen = popen
        self.signal_process = signal_process
        self.monotonic = monotonic
        self.sleep = sleep
        self.shutdown_timeout = shutdown_timeout
        self.processes: list[tuple[ProcessSpec, subprocess.Popen[Any]]] = []
        self.requested_signal: int | None = None
        self._termination_sent: set[int] = set()

    def _terminate_once(self, process: subprocess.Popen[Any]) -> None:
        if process.pid not in self._termination_sent and process.poll() is None:
            self._termination_sent.add(process.pid)
            self.signal_process(process, signal.SIGTERM)

    def _request_shutdown(self, signum: int, _frame: Any = None) -> None:
        if self.requested_signal is None:
            self.requested_signal = signum
            for _spec, process in self.processes:
                self._terminate_once(process)

    def shutdown(self) -> None:
        for _spec, process in self.processes:
            self._terminate_once(process)
        deadline = self.monotonic() + self.shutdown_timeout
        while any(process.poll() is None for _spec, process in self.processes) and self.monotonic() < deadline:
            self.sleep(0.05)
        for _spec, process in self.processes:
            if process.poll() is None:
                self.signal_process(process, signal.SIGKILL)
        for _spec, process in self.processes:
            try:
                process.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                continue

    def run(self, specs: Sequence[ProcessSpec], *, cwd: Path, env: Mapping[str, str]) -> int:
        old_handlers: dict[int, Any] = {}
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._request_shutdown)
            for spec in specs:
                print(f"[h3-studio] starting {spec.name}: {shlex.join(spec.argv)}", file=sys.stderr, flush=True)
                try:
                    process = self.popen(
                        list(spec.argv), cwd=cwd, env=dict(env),
                        start_new_session=os.name == "posix",
                    )
                except OSError as error:
                    raise ManagerError(f"cannot start {spec.name}: {error}") from error
                self.processes.append((spec, process))
            while self.requested_signal is None:
                for spec, process in self.processes:
                    code = process.poll()
                    if code is not None:
                        print(f"[h3-studio] {spec.name} exited unexpectedly ({code})", file=sys.stderr)
                        return code if code != 0 else 1
                self.sleep(0.2)
            return 128 + self.requested_signal
        finally:
            self.shutdown()
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)


def install_commands() -> list[tuple[str, ...]]:
    return [("npm", "ci"), ("npm", "run", "build")]


def _print_plan(specs: Sequence[ProcessSpec], environment: Mapping[str, str]) -> None:
    print("Dry run; no process was started.")
    for spec in specs:
        print(f"- {spec.name}: {shlex.join(spec.argv)}")
    print(f"- API authentication: {'configured (value hidden)' if environment.get('H3_STUDIO_API_KEY') else 'disabled'}")
    print("- binding defaults: API and web use loopback unless explicitly overridden")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Install, diagnose, and run H3 Studio")
    subcommands = command.add_subparsers(dest="command", required=True)
    doctor = subcommands.add_parser("doctor", help="check host dependencies and configuration")
    doctor.add_argument("--env-file", type=Path)
    doctor.add_argument("--check-comfy", action="store_true", help="perform a read-only ComfyUI connectivity check")

    install = subcommands.add_parser("install", help="install exact Node dependencies and build production assets")
    install.add_argument("--dry-run", action="store_true")

    start = subcommands.add_parser("start", help="run API and production frontend together in the foreground")
    start.add_argument("--env-file", type=Path)
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--host", help="public web bind host; defaults to 127.0.0.1")
    start.add_argument("--port", type=int, help="public web port; defaults to 3013")
    start.add_argument("--internal-port", type=int, help="private frontend port; defaults to public port + 1")
    start.add_argument("--api-host", help="Python API bind host; defaults to 127.0.0.1")
    start.add_argument("--api-port", type=int, help="Python API port; defaults to 6020")
    start.add_argument("--shutdown-timeout", type=float, default=8)
    return command


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = PROJECT_ROOT,
    base_env: Mapping[str, str] | None = None,
    run_command: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    supervisor_factory: Callable[..., ForegroundSupervisor] = ForegroundSupervisor,
) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "install":
            commands = install_commands()
            if args.dry_run:
                print("Dry run; no files or dependencies were changed.")
                for item in commands:
                    print(f"- {shlex.join(item)}")
                return 0
            if sys.version_info[:2] < MIN_PYTHON:
                raise ManagerError("Python >= 3.11 is required")
            for executable in ("node", "npm"):
                if shutil.which(executable) is None:
                    raise ManagerError(f"{executable} is not installed or not in PATH")
            for item in commands:
                print(f"[h3-studio] running {shlex.join(item)}", file=sys.stderr, flush=True)
                try:
                    result = run_command(list(item), cwd=root, check=False)
                except OSError as error:
                    raise ManagerError(f"cannot run {item[0]}: {error}") from error
                if result.returncode:
                    raise ManagerError(f"{shlex.join(item)} failed with exit code {result.returncode}")
            print("H3 Studio dependencies installed and production build completed.")
            return 0

        environment, env_path = merged_environment(root, args.env_file, base_env)
        if env_path:
            print(f"[h3-studio] loaded variable names from {env_path}; values hidden", file=sys.stderr)
        if args.command == "doctor":
            checks = doctor_checks(root=root, env=environment, check_comfy=args.check_comfy)
            for check in checks:
                print(f"[{check.status}] {check.name}: {check.message}")
            return 2 if any(check.status == "ERROR" for check in checks) else 0

        specs, child_environment = start_plan(args, root=root, env=environment)
        if args.shutdown_timeout <= 0:
            raise ManagerError("shutdown timeout must be positive")
        if args.dry_run:
            _print_plan(specs, child_environment)
            return 0
        blocking = [
            check.name for check in doctor_checks(root=root, env=child_environment)
            if check.status == "ERROR"
        ]
        if blocking:
            raise ManagerError(f"doctor found blocking issues ({', '.join(blocking)}); run the doctor command for details")
        missing = [
            path for path in (root / "node_modules/vinext/dist/cli.js", root / "dist/server/index.js")
            if not path.is_file()
        ]
        if missing:
            raise ManagerError("frontend dependencies/build are missing; run `python3 scripts/h3studio.py install`")
        supervisor = supervisor_factory(shutdown_timeout=args.shutdown_timeout)
        return supervisor.run(specs, cwd=root, env=child_environment)
    except ManagerError as error:
        print(f"H3 Studio error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
