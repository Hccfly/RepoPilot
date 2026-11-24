import unittest
from pathlib import Path

from repopilot.benchmark import Scenario
from repopilot.checks import BUILTIN_CHECKS, Check, run_check
from repopilot.contracts import RunPhase
from repopilot.grader import score_check
from repopilot.rsi import evaluate_repository

REPO = Path(__file__).resolve().parent.parent


def _check(kind: str, name: str = "probe") -> Check:
    return Check(name=name, description=name, kind=kind)


class CheckModelTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        check = Check("alpha", "desc", "sandbox_timeout", {"timeout_seconds": 1})
        self.assertEqual(Check.from_dict(check.to_dict()), check)

    def test_unknown_kind_fails_without_raising(self) -> None:
        result = run_check(_check("does-not-exist"))
        self.assertEqual(result["status"], "fail")
        self.assertIn("未知的检查类型", result["detail"])


class SandboxCheckTests(unittest.TestCase):
    def test_timeout_is_reported(self) -> None:
        result = run_check(_check("sandbox_timeout"))
        self.assertEqual(result["status"], "ok", result["detail"])

    def test_secret_is_stripped_from_the_child_environment(self) -> None:
        result = run_check(_check("sandbox_secret_isolation"))
        self.assertEqual(result["status"], "ok", result["detail"])

    def test_docker_check_never_fails_for_a_missing_environment(self) -> None:
        result = run_check(_check("docker_network_isolation"))
        self.assertIn(result["status"], {"ok", "skip"}, result["detail"])


class AuditAndQuotaCheckTests(unittest.TestCase):
    def test_audit_never_stores_the_token(self) -> None:
        result = run_check(_check("audit_hides_token"))
        self.assertEqual(result["status"], "ok", result["detail"])

    def test_quota_window_rolls_over(self) -> None:
        result = run_check(_check("quota_window_rollover"))
        self.assertEqual(result["status"], "ok", result["detail"])


class BuiltinChecksTests(unittest.TestCase):
    def test_every_builtin_check_is_green_or_skipped(self) -> None:
        for check in BUILTIN_CHECKS:
            with self.subTest(check=check.name):
                result = run_check(check)
                self.assertIn(result["status"], {"ok", "skip"}, result["detail"])


class CheckGradingTests(unittest.TestCase):
    def test_status_maps_to_a_score(self) -> None:
        self.assertEqual(score_check("a", status="ok").score, 1.0)
        self.assertEqual(score_check("a", status="fail", detail="boom").score, 0.0)
        skipped = score_check("a", status="skip", detail="no docker")
        self.assertTrue(skipped.passed)

    def test_report_includes_checks(self) -> None:
        scenario = Scenario(
            name="check-tiny",
            description="最小场景",
            goal="把 value 改成 new",
            files={"app.py": 'def value():\n    return "old"\n'},
            responses=(
                '{"queries":["value"],"files":["app.py"],"reason":"定位"}',
                (
                    '{"summary":"更新","steps":[{"description":"改 value",'
                    '"files":["app.py"],"verification":[]}],"risks":[]}'
                ),
                (
                    '{"unified_diff":"--- a/app.py\\n+++ b/app.py\\n@@ -1,2 +1,2 @@\\n '
                    'def value():\\n-    return \\"old\\"\\n+    return \\"new\\"\\n",'
                    '"rationale":"x"}'
                ),
                '{"decision":"approve","summary":"ok","findings":[]}',
            ),
            verify_outcomes=(True,),
            expected_files=("app.py",),
            expected_phase=RunPhase.DONE,
            expected_stop_reason="verified",
        )
        checks = (_check("quota_window_rollover", "quota"),)

        report = evaluate_repository(REPO, scenarios=[scenario], checks=checks)

        self.assertEqual(len(report.checks), 1)
        self.assertEqual(report.checks[0].name, "quota")
        self.assertEqual(report.checks[0].candidate, "ok")
        self.assertTrue(report.checks[0].passed)
        self.assertIn("checks", report.to_dict())


if __name__ == "__main__":
    unittest.main()
