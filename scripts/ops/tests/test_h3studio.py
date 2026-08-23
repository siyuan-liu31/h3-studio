from __future__ import annotations

import io
import signal
import subprocess
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.h3studio import (
    ForegroundSupervisor,
    ManagerError,
    ProcessSpec,
    doctor_checks,
    load_env_file,
    main,
    start_plan,
)


class FakeProcess:
    next_pid = 10_000

    def __init__(self, polls: list[int | None] | None = None) -> None:
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.polls = list(polls or [None])
        self.returncode: int | None = None
        self.waited = False

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if len(self.polls) > 1:
            value = self.polls.pop(0)
        else:
            value = self.polls[0]
        if value is not None:
            self.returncode = value
        return value

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


def workspace(root: Path) -> None:
    for name in ("package.json", "server/__main__.py", "scripts/start.mjs", ".env.example"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_dependencies_without_exposing_key_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)

            def find(name: str) -> str:
                return f"/tools/{name}"

            def version(argv):
                return (0, "v22.13.1") if argv[0].endswith("node") else (0, "10.0")

            checks = doctor_checks(
                root=root,
                env={"H3_STUDIO_API_KEY": "never-print-this", "COMFY_URL": "http://127.0.0.1:6006"},
                find_executable=find,
                read_version=version,
            )
        self.assertFalse(any(check.status == "ERROR" for check in checks))
        rendered = "\n".join(f"{check.name}: {check.message}" for check in checks)
        self.assertNotIn("never-print-this", rendered)
        self.assertIn("value hidden", rendered)
        self.assertEqual(next(check.status for check in checks if check.name == "production build"), "WARN")

    def test_doctor_rejects_old_node_and_mismatched_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            checks = doctor_checks(
                root=root,
                env={"H3_STUDIO_API_KEY": "one", "H3_STUDIO_PROXY_API_KEY": "two"},
                find_executable=lambda name: f"/tools/{name}",
                read_version=lambda _argv: (0, "v20.0.0"),
            )
        errors = {check.name for check in checks if check.status == "ERROR"}
        self.assertIn("Node.js", errors)
        self.assertIn("API key wiring", errors)

    def test_env_file_is_data_not_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, ".env.local")
            path.write_text(
                "SAFE='literal value'\nUNICODE=\"/data/视频\"\nNO_EXEC=$(touch /tmp/should-not-run)\n",
                encoding="utf-8",
            )
            values = load_env_file(path)
        self.assertEqual(values["SAFE"], "literal value")
        self.assertEqual(values["UNICODE"], "/data/视频")
        self.assertEqual(values["NO_EXEC"], "$(touch /tmp/should-not-run)")


class PlanTests(unittest.TestCase):
    def args(self, **overrides) -> Namespace:
        values = {
            "host": None, "port": None, "internal_port": None,
            "api_host": None, "api_port": None,
        }
        values.update(overrides)
        return Namespace(**values)

    def test_start_plan_defaults_to_loopback_and_mirrors_hidden_key(self) -> None:
        specs, environment = start_plan(
            self.args(), env={"H3_STUDIO_API_KEY": "secret"}, find_executable=lambda _name: "/usr/bin/node",
        )
        self.assertEqual(environment["H3_STUDIO_HOST"], "127.0.0.1")
        self.assertEqual(environment["H3_STUDIO_WEB_HOST"], "127.0.0.1")
        self.assertEqual(environment["H3_STUDIO_API_PROXY"], "http://127.0.0.1:6020")
        self.assertEqual(environment["H3_STUDIO_PROXY_API_KEY"], "secret")
        self.assertEqual([spec.name for spec in specs], ["Python API", "production frontend"])

    def test_start_plan_rejects_port_collision_and_key_mismatch(self) -> None:
        with self.assertRaisesRegex(ManagerError, "ports must be different"):
            start_plan(self.args(port=6020), env={})
        with self.assertRaisesRegex(ManagerError, "differ"):
            start_plan(
                self.args(),
                env={"H3_STUDIO_API_KEY": "one", "H3_STUDIO_PROXY_API_KEY": "two"},
            )

    def test_dry_runs_do_not_spawn_or_print_secrets(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(
                ["start", "--dry-run"],
                base_env={"H3_STUDIO_API_KEY": "super-secret-value"},
            )
        self.assertEqual(code, 0)
        combined = output.getvalue() + errors.getvalue()
        self.assertIn("no process was started", combined)
        self.assertNotIn("super-secret-value", combined)

        called = False

        def forbidden(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("install dry-run executed a command")

        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["install", "--dry-run"], run_command=forbidden), 0)
        self.assertFalse(called)


class SupervisorTests(unittest.TestCase):
    def test_unexpected_child_exit_stops_and_reaps_sibling(self) -> None:
        api = FakeProcess([None, 0])
        web = FakeProcess([None])
        queue = [api, web]
        signals: list[tuple[int, int]] = []

        def popen(*_args, **kwargs):
            self.assertTrue(kwargs["start_new_session"])
            return queue.pop(0)

        def send(process, signum):
            signals.append((process.pid, signum))
            process.returncode = -signum

        supervisor = ForegroundSupervisor(popen=popen, signal_process=send, sleep=lambda _value: None)
        with redirect_stderr(io.StringIO()):
            code = supervisor.run(
                [ProcessSpec("api", ("python",)), ProcessSpec("web", ("node",))],
                cwd=Path.cwd(), env={},
            )
        self.assertEqual(code, 1)
        self.assertIn((web.pid, signal.SIGTERM), signals)
        self.assertTrue(api.waited)
        self.assertTrue(web.waited)

    def test_shutdown_escalates_only_process_that_ignores_term(self) -> None:
        graceful = FakeProcess([None])
        stubborn = FakeProcess([None])
        calls: list[tuple[int, int]] = []
        now = [0.0]

        def send(process, signum):
            calls.append((process.pid, signum))
            if process is graceful or signum == signal.SIGKILL:
                process.returncode = -signum

        def sleep(value):
            now[0] += value

        supervisor = ForegroundSupervisor(
            signal_process=send, monotonic=lambda: now[0], sleep=sleep, shutdown_timeout=0.1,
        )
        supervisor.processes = [
            (ProcessSpec("graceful", ("a",)), graceful),
            (ProcessSpec("stubborn", ("b",)), stubborn),
        ]
        supervisor.shutdown()
        self.assertIn((graceful.pid, signal.SIGTERM), calls)
        self.assertNotIn((graceful.pid, signal.SIGKILL), calls)
        self.assertIn((stubborn.pid, signal.SIGTERM), calls)
        self.assertIn((stubborn.pid, signal.SIGKILL), calls)

    def test_spawn_failure_cleans_up_already_started_process(self) -> None:
        first = FakeProcess([None])
        calls: list[tuple[int, int]] = []
        invocations = 0

        def popen(*_args, **_kwargs):
            nonlocal invocations
            invocations += 1
            if invocations == 1:
                return first
            raise OSError("not found")

        def send(process, signum):
            calls.append((process.pid, signum))
            process.returncode = -signum

        supervisor = ForegroundSupervisor(popen=popen, signal_process=send, sleep=lambda _value: None)
        with redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ManagerError, "cannot start web"):
                supervisor.run(
                    [ProcessSpec("api", ("python",)), ProcessSpec("web", ("node",))],
                    cwd=Path.cwd(), env={},
                )
        self.assertIn((first.pid, signal.SIGTERM), calls)
        self.assertTrue(first.waited)


if __name__ == "__main__":
    unittest.main()
