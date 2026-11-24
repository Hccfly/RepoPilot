import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from repopilot.cli import main
from repopilot.config import PermissionSettings
from repopilot.contracts import (
    ApprovalDecision,
    RunPhase,
    RunState,
    TaskRequest,
    VerificationResult,
)
from repopilot.patching import PatchProposal, PatchRunRecord, PatchStatus
from repopilot.persistence import JsonPatchRunStore, SqlitePatchRunStore
from repopilot.policy import PermissionPolicy
from repopilot.verification import (
    SubprocessVerificationRunner,
    VerificationAction,
    VerificationError,
    VerificationWhitelist,
    VerificationWorkflow,
    detect_projects,
)
from repopilot.workflow import WorkflowEngine


def _action(name: str, *argv: str) -> VerificationAction:
    return VerificationAction(name=name, description=name, project="test", argv=tuple(argv))


class _RecordingRunner:
    """Deterministic runner so workflow tests never depend on installed tools."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.failing = set(failing or ())

    def run(self, action: VerificationAction) -> VerificationResult:
        self.calls.append(action.name)
        failed = action.name in self.failing
        return VerificationResult(
            command=action.argv,
            action=action.name,
            exit_code=1 if failed else 0,
            stdout="boom" if failed else "ok",
        )


def _applied_record(root: Path, *, phase: RunPhase = RunPhase.VERIFY) -> PatchRunRecord:
    state = RunState(task=TaskRequest(goal="修复返回值", repository=str(root)))
    state.phase = phase
    state.approval = ApprovalDecision.APPROVED
    return PatchRunRecord(
        run_id="0" * 32,
        repository=str(root),
        goal="修复返回值",
        state=state.to_dict(),
        proposal=PatchProposal(unified_diff="", touched_files=(), rationale="测试夹具"),
        baselines=(),
        status=PatchStatus.APPLIED,
    )


class ProjectDetectionTests(unittest.TestCase):
    def test_detects_python_and_node_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
            (root / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
            self.assertEqual(detect_projects(root), ("python", "node"))

    def test_empty_directory_has_no_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(detect_projects(Path(temp)), ())

    def test_tests_package_with_python_files_detects_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            (root / "tests" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
            self.assertEqual(detect_projects(root), ("python",))


class WhitelistTests(unittest.TestCase):
    def test_node_actions_require_declared_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
            whitelist = VerificationWhitelist.discover(
                root,
                which=lambda name: "npm.cmd" if name == "npm" else None,
                module_available=lambda name: False,
            )
            self.assertEqual(whitelist.names(), ("npm-test",))

    def test_missing_package_manager_hides_node_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
            whitelist = VerificationWhitelist.discover(
                root, which=lambda name: None, module_available=lambda name: False
            )
            self.assertEqual(whitelist.names(), ())

    def test_unknown_action_is_rejected_with_available_names(self) -> None:
        whitelist = VerificationWhitelist((_action("pytest", "pytest"),))
        with self.assertRaises(VerificationError) as caught:
            whitelist.resolve("ruff")
        self.assertIn("pytest", str(caught.exception))

    def test_resolve_returns_matching_action(self) -> None:
        action = _action("pytest", sys.executable, "-m", "pytest")
        self.assertIs(VerificationWhitelist((action,)).resolve("pytest"), action)


class SubprocessRunnerTests(unittest.TestCase):
    def _runner(self, root: Path, **kwargs) -> SubprocessVerificationRunner:
        return SubprocessVerificationRunner(root, **kwargs)

    def test_captures_exit_code_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runner = self._runner(Path(temp), timeout_seconds=30)
            result = runner.run(
                _action("probe", sys.executable, "-c", "import sys;print('hello');sys.exit(3)")
            )
            self.assertEqual(result.exit_code, 3)
            self.assertIn("hello", result.stdout)
            self.assertFalse(result.timed_out)
            self.assertFalse(result.passed)

    def test_truncates_large_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runner = self._runner(Path(temp), timeout_seconds=30, max_output_chars=16)
            result = runner.run(_action("big", sys.executable, "-c", "print('x' * 500)"))
            self.assertTrue(result.truncated)
            self.assertLessEqual(len(result.stdout), 16)

    def test_timeout_is_reported_and_process_terminated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runner = self._runner(Path(temp), timeout_seconds=0.5)
            result = runner.run(
                _action("sleep", sys.executable, "-c", "import time; time.sleep(30)")
            )
            self.assertTrue(result.timed_out)
            self.assertFalse(result.passed)
            self.assertLess(result.duration_seconds, 15)

    def test_runs_in_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "marker.txt").write_text("present", encoding="utf-8")
            runner = self._runner(root, timeout_seconds=30)
            result = runner.run(
                _action(
                    "cwd",
                    sys.executable,
                    "-c",
                    "import pathlib;print(pathlib.Path('marker.txt').read_text())",
                )
            )
            self.assertIn("present", result.stdout)


class VerificationWorkflowTests(unittest.TestCase):
    def _build(self, root: Path, ledger: Path, runner: _RecordingRunner, policy=None):
        store = JsonPatchRunStore(ledger)
        record = _applied_record(root)
        store.save(record)
        workflow = VerificationWorkflow(
            runner=runner,
            whitelist=VerificationWhitelist((_action("pytest", "pytest"), _action("ruff", "ruff"))),
            policy=policy or PermissionPolicy(PermissionSettings(verify="ask")),
            store=store,
            workflow=WorkflowEngine(),
        )
        return store, record, workflow

    def test_failure_blocks_done(self) -> None:
        runner = _RecordingRunner({"ruff"})
        with tempfile.TemporaryDirectory() as temp:
            store, record, workflow = self._build(Path(temp), Path(temp) / "runs", runner)
            outcome = workflow.verify(record.run_id)
            self.assertFalse(outcome.passed)
            self.assertEqual(outcome.phase, RunPhase.REPAIR)
            reloaded = store.load(record.run_id)
            self.assertEqual(reloaded.state["phase"], "repair")
            self.assertEqual(len(reloaded.state["verification"]), 2)
            self.assertIsNotNone(reloaded.state["error"])

    def test_success_stops_at_review(self) -> None:
        runner = _RecordingRunner()
        with tempfile.TemporaryDirectory() as temp:
            store, record, workflow = self._build(Path(temp), Path(temp) / "runs", runner)
            outcome = workflow.verify(record.run_id)
            self.assertTrue(outcome.passed)
            self.assertEqual(outcome.phase, RunPhase.REVIEW)
            self.assertEqual(store.load(record.run_id).state["phase"], "review")

    def test_resume_skips_actions_that_already_passed(self) -> None:
        runner = _RecordingRunner({"ruff"})
        with tempfile.TemporaryDirectory() as temp:
            _, record, workflow = self._build(Path(temp), Path(temp) / "runs", runner)
            workflow.verify(record.run_id)
            self.assertEqual(runner.calls, ["pytest", "ruff"])

            runner.failing = set()
            outcome = workflow.verify(record.run_id)
            self.assertEqual(runner.calls, ["pytest", "ruff", "ruff"])
            self.assertEqual(outcome.skipped, ("pytest",))
            self.assertTrue(outcome.passed)

    def test_failure_after_exhausted_repair_budget_fails(self) -> None:
        runner = _RecordingRunner({"ruff"})
        with tempfile.TemporaryDirectory() as temp:
            store, record, workflow = self._build(Path(temp), Path(temp) / "runs", runner)
            record.state["repair_attempts"] = 1
            store.save(record)
            outcome = workflow.verify(record.run_id)
            self.assertFalse(outcome.passed)
            self.assertEqual(outcome.phase, RunPhase.FAILED)
            self.assertEqual(
                store.load(record.run_id).state["stop_reason"], "repair_budget_exhausted"
            )

    def test_policy_deny_blocks_execution(self) -> None:
        runner = _RecordingRunner()
        with tempfile.TemporaryDirectory() as temp:
            _, record, workflow = self._build(
                Path(temp),
                Path(temp) / "runs",
                runner,
                policy=PermissionPolicy(PermissionSettings(verify="deny")),
            )
            with self.assertRaises(VerificationError):
                workflow.verify(record.run_id)
            self.assertEqual(runner.calls, [])

    def test_policy_ask_requires_explicit_approval(self) -> None:
        runner = _RecordingRunner()
        with tempfile.TemporaryDirectory() as temp:
            _, record, workflow = self._build(Path(temp), Path(temp) / "runs", runner)
            with self.assertRaises(VerificationError):
                workflow.verify(record.run_id, approved=False)
            self.assertEqual(runner.calls, [])

    def test_only_applied_patches_can_be_verified(self) -> None:
        runner = _RecordingRunner()
        with tempfile.TemporaryDirectory() as temp:
            store, record, workflow = self._build(Path(temp), Path(temp) / "runs", runner)
            record.status = PatchStatus.PROPOSED
            store.save(record)
            with self.assertRaises(VerificationError):
                workflow.verify(record.run_id)
            self.assertEqual(runner.calls, [])

    def test_verification_history_survives_store_reload(self) -> None:
        runner = _RecordingRunner({"ruff"})
        with tempfile.TemporaryDirectory() as temp:
            _, record, workflow = self._build(Path(temp), Path(temp) / "runs", runner)
            workflow.verify(record.run_id)
            reopened = JsonPatchRunStore(Path(temp) / "runs")
            history = reopened.load(record.run_id).state["verification"]
            self.assertEqual([item["action"] for item in history], ["pytest", "ruff"])


class SqliteStoreTests(unittest.TestCase):
    def test_round_trip_preserves_state_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            ledger = Path(temp) / "runs"
            root.mkdir()
            record = _applied_record(root)
            record.state["verification"] = [
                VerificationResult(
                    command=("pytest",), action="pytest", exit_code=0, stdout="ok"
                ).to_dict()
            ]
            SqlitePatchRunStore(ledger).save(record)

            loaded = SqlitePatchRunStore(ledger).load(record.run_id)
            self.assertEqual(loaded.run_id, record.run_id)
            self.assertEqual(loaded.status, PatchStatus.APPLIED)
            self.assertEqual(loaded.state["phase"], "verify")
            self.assertEqual(loaded.state["verification"][0]["action"], "pytest")

    def test_list_returns_saved_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            ledger = Path(temp) / "runs"
            root.mkdir()
            record = _applied_record(root)
            store = SqlitePatchRunStore(ledger)
            store.save(record)
            self.assertEqual([item.run_id for item in store.list_records()], [record.run_id])

    def test_missing_record_reports_error(self) -> None:
        from repopilot.persistence import RunStoreError

        with tempfile.TemporaryDirectory() as temp, self.assertRaises(RunStoreError):
            SqlitePatchRunStore(Path(temp) / "runs").load("0" * 32)


class CliCheckpointTests(unittest.TestCase):
    def test_list_and_status_read_the_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            ledger = Path(temp) / "runs"
            root.mkdir()
            record = _applied_record(root)
            SqlitePatchRunStore(ledger).save(record)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--runs-dir", str(ledger), "list"]), 0)
            self.assertIn(record.run_id, output.getvalue())
            self.assertIn("verify", output.getvalue())

            detail = io.StringIO()
            with contextlib.redirect_stdout(detail):
                self.assertEqual(
                    main(["--runs-dir", str(ledger), "status", record.run_id]), 0
                )
            self.assertIn("verify", detail.getvalue())
            self.assertIn("applied", detail.getvalue())


if __name__ == "__main__":
    unittest.main()
