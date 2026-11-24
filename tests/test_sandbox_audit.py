import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from repopilot.sandbox import (
    SandboxScheduler,
    SandboxServer,
    SandboxService,
    Tenant,
    TenantRegistry,
)
from repopilot.sandbox_audit import (
    AuditRecord,
    SandboxLedger,
    default_sandbox_db_path,
)
from repopilot.verification import VerificationAction, VerificationWhitelist

_TOKEN = "super-secret-token-value"


def _probe() -> VerificationAction:
    return VerificationAction(
        "probe", "probe", "test", (sys.executable, "-c", "print('ok')")
    )


def _scheduler(
    temp: str,
    *,
    token: str = "",
    limit: int = 0,
    window_seconds: int = 3600,
    ledger: SandboxLedger | None = None,
) -> SandboxScheduler:
    service = SandboxService(
        Path(temp), VerificationWhitelist((_probe(),)), token=token
    )
    tenant = Tenant("alpha", service, requests_per_window=limit)
    return SandboxScheduler(
        TenantRegistry([tenant]),
        ledger=ledger,
        quota_window_seconds=window_seconds,
    )


class AuditRecordTests(unittest.TestCase):
    def test_records_survive_a_new_ledger_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sandbox.db"
            ledger = SandboxLedger(path)
            ledger.record(
                AuditRecord(
                    timestamp="2026-01-01T00:00:00+00:00",
                    tenant="alpha",
                    action="pytest",
                    ok=True,
                    error="",
                    exit_code=0,
                    duration_seconds=1.5,
                    remote="127.0.0.1",
                )
            )

            reopened = SandboxLedger(path).recent(10)

        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened[0].tenant, "alpha")
        self.assertEqual(reopened[0].action, "pytest")
        self.assertTrue(reopened[0].ok)
        self.assertEqual(reopened[0].exit_code, 0)

    def test_recent_returns_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SandboxLedger(Path(temp) / "sandbox.db")
            for name in ("first", "second"):
                ledger.record(
                    AuditRecord("2026-01-01T00:00:00+00:00", "alpha", name, True, "", 0, 0.1, "")
                )
            rows = ledger.recent(10)

        self.assertEqual([row.action for row in rows], ["second", "first"])


class SchedulerAuditTests(unittest.TestCase):
    def test_success_and_failure_are_both_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SandboxLedger(Path(temp) / "sandbox.db")
            scheduler = _scheduler(temp, ledger=ledger)

            ok = scheduler.execute({"tenant": "alpha", "action": "probe"}, remote="10.0.0.5")
            bad = scheduler.execute({"tenant": "alpha", "action": "nope"}, remote="10.0.0.5")
            rows = ledger.recent(10)

        self.assertTrue(ok["ok"])
        self.assertFalse(bad["ok"])
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.action for row in rows], ["nope", "probe"])
        self.assertTrue(rows[1].ok)
        self.assertFalse(rows[0].ok)
        self.assertIn("未知或不可用的验证动作", rows[0].error)
        self.assertEqual(rows[1].remote, "10.0.0.5")
        self.assertEqual(rows[1].exit_code, 0)

    def test_unauthorized_requests_are_audited_without_the_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "sandbox.db"
            ledger = SandboxLedger(db)
            scheduler = _scheduler(temp, token=_TOKEN, ledger=ledger)

            response = scheduler.execute(
                {"tenant": "alpha", "action": "probe", "token": "wrong"}
            )

            self.assertEqual(response["error"], "unauthorized")
            rows = ledger.recent(10)
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0].ok)
            self.assertNotIn(_TOKEN, db.read_text(encoding="utf-8", errors="replace"))

    def test_unknown_tenant_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SandboxLedger(Path(temp) / "sandbox.db")
            scheduler = _scheduler(temp, ledger=ledger)

            scheduler.execute({"tenant": "ghost", "action": "probe"})
            rows = ledger.recent(10)

        self.assertEqual(rows[0].tenant, "ghost")
        self.assertIn("未知租户", rows[0].error)


class QuotaTests(unittest.TestCase):
    def test_limit_blocks_further_requests_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "sandbox.db"
            ledger = SandboxLedger(db)
            first = ledger.reserve("alpha", 2, 3600, now=1000.0)
            second = ledger.reserve("alpha", 2, 3600, now=1000.0)

            reopened = SandboxLedger(db)
            third = reopened.reserve("alpha", 2, 3600, now=1000.0)

        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertFalse(third.allowed)
        self.assertEqual(third.used, 2)
        self.assertEqual(third.limit, 2)

    def test_rejected_requests_do_not_consume_quota(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SandboxLedger(Path(temp) / "sandbox.db")
            for _ in range(5):
                ledger.reserve("alpha", 1, 3600, now=1000.0)
            usage = ledger.usage(3600, now=1000.0)

        self.assertEqual(usage[0].used, 1)

    def test_window_rollover_restores_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SandboxLedger(Path(temp) / "sandbox.db")
            self.assertTrue(ledger.reserve("alpha", 1, 60, now=100.0).allowed)
            self.assertFalse(ledger.reserve("alpha", 1, 60, now=110.0).allowed)
            self.assertTrue(ledger.reserve("alpha", 1, 60, now=200.0).allowed)

    def test_zero_limit_is_unlimited(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SandboxLedger(Path(temp) / "sandbox.db")
            self.assertTrue(ledger.reserve("alpha", 0, 60, now=100.0).allowed)

    def test_tenants_have_separate_quotas(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SandboxLedger(Path(temp) / "sandbox.db")
            self.assertTrue(ledger.reserve("alpha", 1, 3600, now=100.0).allowed)
            self.assertFalse(ledger.reserve("alpha", 1, 3600, now=100.0).allowed)
            self.assertTrue(ledger.reserve("beta", 1, 3600, now=100.0).allowed)

    def test_scheduler_returns_quota_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SandboxLedger(Path(temp) / "sandbox.db")
            scheduler = _scheduler(temp, limit=1, ledger=ledger)

            first = scheduler.execute({"tenant": "alpha", "action": "probe"})
            second = scheduler.execute({"tenant": "alpha", "action": "probe"})
            rows = ledger.recent(10)

        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("quota_exceeded", second["error"])
        self.assertEqual(len(rows), 2)


class TransportAuditTests(unittest.TestCase):
    def test_remote_address_reaches_the_audit_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ledger = SandboxLedger(Path(temp) / "sandbox.db")
            scheduler = _scheduler(temp, ledger=ledger)
            server = SandboxServer(("127.0.0.1", 0), scheduler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.create_connection(
                    ("127.0.0.1", server.server_address[1]), timeout=10
                ) as connection:
                    connection.sendall(
                        json.dumps({"tenant": "alpha", "action": "probe"}).encode() + b"\n"
                    )
                    connection.makefile("rb").readline()
                rows = ledger.recent(10)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(rows[0].remote, "127.0.0.1")


class DefaultPathTests(unittest.TestCase):
    def test_environment_override_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "custom.db"
            with patch.dict(os.environ, {"REPOPILOT_SANDBOX_DB": str(target)}, clear=False):
                self.assertEqual(default_sandbox_db_path(), target.resolve())


class CliAuditTests(unittest.TestCase):
    def test_audit_command_prints_recent_rows(self) -> None:
        from repopilot.cli import main

        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "sandbox.db"
            ledger = SandboxLedger(db)
            ledger.record(
                AuditRecord("2026-01-01T00:00:00+00:00", "alpha", "pytest", False, "boom", 1, 0.2, "127.0.0.1")
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["sandbox", "audit", "--db", str(db), "--limit", "5"])
            quota_output = io.StringIO()
            with contextlib.redirect_stdout(quota_output):
                quota_code = main(["sandbox", "quota", "--db", str(db)])

        self.assertEqual(code, 0)
        self.assertIn("pytest", output.getvalue())
        self.assertIn("boom", output.getvalue())
        self.assertEqual(quota_code, 0)
        self.assertIn("alpha", quota_output.getvalue())


if __name__ == "__main__":
    unittest.main()
