import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from repopilot.config import SandboxSettings
from repopilot.contracts import VerificationResult
from repopilot.sandbox import (
    MAX_REQUEST_BYTES,
    RemoteSandboxRunner,
    SandboxError,
    SandboxScheduler,
    SandboxServer,
    SandboxService,
    Tenant,
    TenantRegistry,
    build_verification_runner,
    create_server,
)
from repopilot.verification import VerificationAction, VerificationWhitelist

_APP = 'def value():\n    return "old"\n'
_TEST = 'from app import value\n\n\ndef test_value():\n    assert value() == "old"\n'
_PYPROJECT = '[project]\nname = "demo"\nversion = "0.1.0"\n'


def _action(name: str, *argv: str) -> VerificationAction:
    return VerificationAction(name=name, description=name, project="test", argv=tuple(argv))


def _probe_action() -> VerificationAction:
    return _action("probe", sys.executable, "-c", "print('ok')")


def _service(
    *, token: str = "", actions: tuple[VerificationAction, ...] | None = None
) -> SandboxService:
    whitelist = VerificationWhitelist(actions or (_probe_action(),))
    return SandboxService(Path.cwd(), whitelist, token=token, max_timeout_seconds=30)


@contextlib.contextmanager
def _served(service: SandboxService):
    scheduler = SandboxScheduler(TenantRegistry([Tenant("default", service)]))
    server = SandboxServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _exchange(port: int, payload: dict) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        with connection.makefile("rb") as stream:
            line = stream.readline()
    return json.loads(line.decode("utf-8"))


def _exchange_raw(port: int, data: bytes) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(data)
        with connection.makefile("rb") as stream:
            line = stream.readline()
    return json.loads(line.decode("utf-8"))


class SandboxServiceTests(unittest.TestCase):
    def test_runs_a_whitelisted_action(self) -> None:
        response = _service().execute({"action": "probe"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["exit_code"], 0)
        self.assertIn("ok", response["stdout"])

    def test_unknown_action_is_rejected(self) -> None:
        response = _service().execute({"action": "rm-rf"})
        self.assertFalse(response["ok"])
        self.assertIn("未知或不可用的验证动作", response["error"])

    def test_missing_action_is_rejected(self) -> None:
        response = _service().execute({})
        self.assertFalse(response["ok"])
        self.assertIn("action", response["error"])

    def test_client_supplied_argv_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sentinel = Path(temp) / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")
            service = _service()
            response = service.execute(
                {"action": "probe", "argv": ["rm", "-rf", str(sentinel)]}
            )

            self.assertTrue(response["ok"])
            self.assertTrue(sentinel.exists())
            self.assertEqual(response["command"][-1], "print('ok')")

    def test_token_is_required_when_configured(self) -> None:
        service = _service(token="secret")
        self.assertFalse(service.execute({"action": "probe"})["ok"])
        self.assertFalse(service.execute({"action": "probe", "token": "wrong"})["ok"])
        self.assertTrue(service.execute({"action": "probe", "token": "secret"})["ok"])

    def test_timeout_is_clamped_to_the_server_maximum(self) -> None:
        service = _service()
        self.assertEqual(service._timeout({"timeout_seconds": 99999}), 30)
        self.assertEqual(service._timeout({"timeout_seconds": "bad"}), 30)
        self.assertEqual(service._timeout({"timeout_seconds": 2}), 2)


class SandboxTransportTests(unittest.TestCase):
    def test_round_trip_over_tcp(self) -> None:
        with _served(_service()) as port:
            response = _exchange(port, {"action": "probe"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["exit_code"], 0)

    def test_malformed_json_does_not_kill_the_service(self) -> None:
        with _served(_service()) as port:
            first = _exchange_raw(port, b"not-json\n")
            second = _exchange(port, {"action": "probe"})
        self.assertFalse(first["ok"])
        self.assertIn("合法 JSON", first["error"])
        self.assertTrue(second["ok"])

    def test_oversized_request_is_rejected(self) -> None:
        with _served(_service()) as port:
            response = _exchange_raw(port, b"{" + b" " * (MAX_REQUEST_BYTES + 10) + b"\n")
        self.assertFalse(response["ok"])
        self.assertIn("过大", response["error"])

    def test_unauthorized_request_is_rejected(self) -> None:
        with _served(_service(token="secret")) as port:
            response = _exchange(port, {"action": "probe"})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "unauthorized")


class RemoteRunnerTests(unittest.TestCase):
    def test_runner_maps_the_response(self) -> None:
        with _served(_service()) as port:
            runner = RemoteSandboxRunner("127.0.0.1", port, timeout_seconds=10)
            result = runner.run(_action("probe", "client-side", "argv"))

        self.assertIsInstance(result, VerificationResult)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.action, "probe")
        self.assertEqual(result.command, ("client-side", "argv"))
        self.assertTrue(result.passed)

    def test_runner_reports_server_errors(self) -> None:
        with _served(_service()) as port:
            runner = RemoteSandboxRunner("127.0.0.1", port, timeout_seconds=10)
            with self.assertRaises(SandboxError):
                runner.run(_action("missing", "missing"))

    def test_runner_reports_connection_failures(self) -> None:
        runner = RemoteSandboxRunner("127.0.0.1", 1, timeout_seconds=1)
        with self.assertRaises(SandboxError):
            runner.run(_action("probe", "probe"))

    def test_runner_sends_only_the_action_name(self) -> None:
        captured: list[dict] = []

        class _Capture(SandboxService):
            def execute(self, request: dict) -> dict:
                captured.append(request)
                return super().execute(request)

        with tempfile.TemporaryDirectory() as temp:
            service = _Capture(
                Path(temp), VerificationWhitelist((_probe_action(),)), max_timeout_seconds=30
            )
            with _served(service) as port:
                RemoteSandboxRunner("127.0.0.1", port, timeout_seconds=10).run(
                    _action("probe", "secret-arg")
                )

        self.assertEqual(
            set(captured[0]), {"action", "token", "timeout_seconds", "tenant"}
        )


class SandboxFactoryTests(unittest.TestCase):
    def test_remote_mode_returns_the_remote_runner(self) -> None:
        runner = build_verification_runner(
            SandboxSettings(mode="remote", port=9999),
            Path("."),
            timeout_seconds=5,
            max_output_chars=100,
        )
        self.assertIsInstance(runner, RemoteSandboxRunner)

    def test_non_loopback_without_token_is_refused(self) -> None:
        settings = SandboxSettings(mode="remote", host="10.0.0.1", port=9999)
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(SandboxError):
            build_verification_runner(
                settings, Path("."), timeout_seconds=5, max_output_chars=100
            )

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(SandboxError):
            build_verification_runner(
                SandboxSettings(mode="magic"),
                Path("."),
                timeout_seconds=5,
                max_output_chars=100,
            )


class SandboxEndToEndTests(unittest.TestCase):
    def test_real_pytest_runs_inside_the_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
            (root / "app.py").write_text(_APP, encoding="utf-8")
            (root / "test_app.py").write_text(_TEST, encoding="utf-8")
            server = create_server(root, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                service = server.scheduler.registry.get("default").service
                self.assertIn("pytest", service.whitelist.names())
                runner = RemoteSandboxRunner(
                    "127.0.0.1", server.server_address[1], timeout_seconds=60
                )
                result = runner.run(_action("pytest", "pytest"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertTrue(result.passed, result.stderr)


if __name__ == "__main__":
    unittest.main()
