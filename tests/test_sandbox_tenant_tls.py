import contextlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from repopilot.sandbox import (
    RemoteSandboxRunner,
    SandboxError,
    SandboxScheduler,
    SandboxServer,
    SandboxService,
    Tenant,
    TenantRegistry,
    create_multi_tenant_server,
    server_ssl_context,
)
from repopilot.verification import VerificationAction, VerificationWhitelist


def _action(marker: str = "ok", *, sleep: float = 0.0) -> VerificationAction:
    code = f"import time; time.sleep({sleep}); print({marker!r})"
    return VerificationAction("probe", "probe", "test", (sys.executable, "-c", code))


def _tenant(
    name: str,
    root: Path,
    *,
    marker: str | None = None,
    token: str = "",
    concurrency: int = 1,
) -> Tenant:
    service = SandboxService(
        root,
        VerificationWhitelist((_action(marker or name),)),
        token=token,
        max_timeout_seconds=30,
    )
    return Tenant(name, service, max_concurrency=concurrency)


@contextlib.contextmanager
def _served(server: SandboxServer):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TenantRegistryTests(unittest.TestCase):
    def test_lists_and_resolves_tenants(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = TenantRegistry(
                [_tenant("alpha", root), _tenant("beta", root)]
            )
        self.assertEqual(registry.names(), ("alpha", "beta"))
        self.assertEqual(registry.get("beta").name, "beta")

    def test_unknown_tenant_lists_the_available_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registry = TenantRegistry([_tenant("alpha", Path(temp))])
        with self.assertRaises(SandboxError) as caught:
            registry.get("gamma")
        self.assertIn("alpha", str(caught.exception))

    def test_default_tenant_is_used_when_the_request_omits_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scheduler = SandboxScheduler(
                TenantRegistry([_tenant("default", root, marker="default-tenant")])
            )
            response = scheduler.execute({"action": "probe"})

        self.assertTrue(response["ok"])
        self.assertIn("default-tenant", response["stdout"])


class SchedulerTests(unittest.TestCase):
    def _scheduler(self, temp: str, **kwargs) -> SandboxScheduler:
        root = Path(temp)
        return SandboxScheduler(
            TenantRegistry(
                [
                    _tenant("alpha", root, marker="from-alpha"),
                    _tenant("beta", root, marker="from-beta", token="beta-secret"),
                ]
            ),
            **kwargs,
        )

    def test_routes_to_the_requested_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp)
            alpha = scheduler.execute({"tenant": "alpha", "action": "probe"})
            beta = scheduler.execute(
                {"tenant": "beta", "action": "probe", "token": "beta-secret"}
            )

        self.assertIn("from-alpha", alpha["stdout"])
        self.assertIn("from-beta", beta["stdout"])

    def test_unknown_tenant_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            response = self._scheduler(temp).execute(
                {"tenant": "nope", "action": "probe"}
            )
        self.assertFalse(response["ok"])
        self.assertIn("未知租户", response["error"])

    def test_tenant_tokens_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp)
            response = scheduler.execute(
                {"tenant": "beta", "action": "probe", "token": "wrong"}
            )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "unauthorized")

    def test_per_tenant_concurrency_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp, queue_timeout_seconds=0.1)
            gate = scheduler.gates["alpha"]
            self.assertTrue(gate.acquire(timeout=0))
            try:
                response = scheduler.execute({"tenant": "alpha", "action": "probe"})
                other = scheduler.execute({"tenant": "beta", "action": "probe", "token": "beta-secret"})
            finally:
                gate.release()

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "busy")
        self.assertTrue(other["ok"])

    def test_global_concurrency_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp, max_total_concurrency=1, queue_timeout_seconds=0.1)
            self.assertTrue(scheduler.global_gate.acquire(timeout=0))
            try:
                response = scheduler.execute({"tenant": "alpha", "action": "probe"})
            finally:
                scheduler.global_gate.release()
        self.assertEqual(response["error"], "busy")

    def test_capacity_is_released_after_a_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scheduler = self._scheduler(temp)
            first = scheduler.execute({"tenant": "alpha", "action": "probe"})
            second = scheduler.execute({"tenant": "alpha", "action": "probe"})
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])


class MultiTenantTransportTests(unittest.TestCase):
    def test_each_tenant_runs_in_its_own_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            left = base / "left"
            right = base / "right"
            left.mkdir()
            right.mkdir()
            (left / "marker.txt").write_text("left", encoding="utf-8")
            (right / "marker.txt").write_text("right", encoding="utf-8")

            def tenant(name: str, root: Path) -> Tenant:
                action = VerificationAction(
                    "probe",
                    "probe",
                    "test",
                    (
                        sys.executable,
                        "-c",
                        "import pathlib;print(pathlib.Path('marker.txt').read_text())",
                    ),
                )
                service = SandboxService(root, VerificationWhitelist((action,)))
                return Tenant(name, service)

            server = create_multi_tenant_server(
                [tenant("left", left), tenant("right", right)], host="127.0.0.1", port=0
            )
            with _served(server) as port:
                left_result = RemoteSandboxRunner(
                    "127.0.0.1", port, tenant="left", timeout_seconds=10
                ).run(_action("left"))
                right_result = RemoteSandboxRunner(
                    "127.0.0.1", port, tenant="right", timeout_seconds=10
                ).run(_action("right"))

        self.assertEqual(left_result.stdout.strip(), "left")
        self.assertEqual(right_result.stdout.strip(), "right")


@unittest.skipUnless(shutil.which("openssl"), "需要 openssl 生成测试证书")
class TlsTests(unittest.TestCase):
    def _certificate(self, directory: Path) -> tuple[Path, Path]:
        cert = directory / "cert.pem"
        key = directory / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        return cert, key

    def test_tls_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cert, key = self._certificate(base)
            server = SandboxServer(
                ("127.0.0.1", 0),
                SandboxScheduler(
                    TenantRegistry([_tenant("default", base, marker="secure")])
                ),
                ssl_context=server_ssl_context(str(cert), str(key)),
            )
            with _served(server) as port:
                result = RemoteSandboxRunner(
                    "127.0.0.1",
                    port,
                    timeout_seconds=10,
                    tls=True,
                    ca_file=str(cert),
                ).run(_action("secure"))

        self.assertEqual(result.exit_code, 0)
        self.assertIn("secure", result.stdout)

    def test_untrusted_certificate_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cert, key = self._certificate(base)
            server = SandboxServer(
                ("127.0.0.1", 0),
                SandboxScheduler(TenantRegistry([_tenant("default", base)])),
                ssl_context=server_ssl_context(str(cert), str(key)),
            )
            with _served(server) as port:
                runner = RemoteSandboxRunner(
                    "127.0.0.1", port, timeout_seconds=10, tls=True
                )
                with self.assertRaises(SandboxError):
                    runner.run(_action("default"))

    def test_plain_client_cannot_talk_to_a_tls_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cert, key = self._certificate(base)
            server = SandboxServer(
                ("127.0.0.1", 0),
                SandboxScheduler(TenantRegistry([_tenant("default", base)])),
                ssl_context=server_ssl_context(str(cert), str(key)),
            )
            with _served(server) as port:
                runner = RemoteSandboxRunner("127.0.0.1", port, timeout_seconds=5)
                with self.assertRaises(SandboxError):
                    runner.run(_action("default"))


if __name__ == "__main__":
    unittest.main()
