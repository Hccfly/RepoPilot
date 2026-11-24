import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from repopilot.config import PermissionSettings
from repopilot.contracts import (
    ApprovalDecision,
    RunPhase,
    RunState,
    TaskRequest,
    VerificationResult,
)
from repopilot.editing import PatchLifecycleService
from repopilot.git import GitClient
from repopilot.patching import (
    CommitError,
    PatchConflict,
    PatchPolicyError,
    PatchRunRecord,
    PatchStatus,
    UnifiedDiffPatchEngine,
)
from repopilot.persistence import JsonPatchRunStore
from repopilot.policy import PermissionPolicy
from repopilot.report import build_report
from repopilot.workspace import RepositoryWorkspace

_APP = 'def value():\n    return "old"\n'
_DIFF = (
    '--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n'
    '-    return "old"\n+    return "new"\n'
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _create_repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    (root / "app.py").write_text(_APP, encoding="utf-8")
    (root / "user.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=RepoPilot Tests",
        "-c",
        "user.email=repopilot@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture",
    )


def _done_run(root: Path, store: JsonPatchRunStore, *, phase: RunPhase = RunPhase.DONE):
    engine = UnifiedDiffPatchEngine(RepositoryWorkspace(root), GitClient(root))
    proposal, baselines = engine.prepare(_DIFF, "更新返回值")
    state = RunState(task=TaskRequest(goal="修复返回值", repository=str(root)))
    state.phase = RunPhase.VERIFY
    state.approval = ApprovalDecision.APPROVED
    state.verification = [
        VerificationResult(command=("pytest",), action="pytest", exit_code=0)
    ]
    record = PatchRunRecord(
        run_id="a" * 32,
        repository=str(root),
        goal="修复返回值",
        state=state.to_dict(),
        proposal=proposal,
        baselines=baselines,
    )
    store.save(record)
    engine.apply(record)
    settled = RunState.from_dict(record.state)
    settled.phase = phase
    record.state = settled.to_dict()
    store.save(record)
    return record


def _service(root: Path, store: JsonPatchRunStore, *, git_write: str = "ask"):
    return PatchLifecycleService(
        UnifiedDiffPatchEngine(RepositoryWorkspace(root), GitClient(root)),
        PermissionPolicy(PermissionSettings(git_write=git_write)),
        store,
    )


def _count_commits(root: Path) -> int:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


class CommitPolicyTests(unittest.TestCase):
    def test_default_policy_denies_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            store = JsonPatchRunStore(Path(temp) / "runs")
            record = _done_run(root, store)
            service = _service(root, store, git_write="deny")

            with self.assertRaises(PatchPolicyError):
                service.commit(record.run_id, approved=True)
            self.assertEqual(_count_commits(root), 1)
            self.assertEqual(store.load(record.run_id).status, PatchStatus.APPLIED)

    def test_ask_requires_a_separate_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            store = JsonPatchRunStore(Path(temp) / "runs")
            record = _done_run(root, store)
            service = _service(root, store, git_write="ask")

            with self.assertRaises(PatchPolicyError):
                service.commit(record.run_id, approved=False)
            self.assertEqual(_count_commits(root), 1)


class CommitPreconditionTests(unittest.TestCase):
    def _setup(self, temp: str, *, phase: RunPhase = RunPhase.DONE):
        root = Path(temp) / "repo"
        _create_repo(root)
        store = JsonPatchRunStore(Path(temp) / "runs")
        record = _done_run(root, store, phase=phase)
        return root, store, record, _service(root, store)

    def test_only_done_runs_can_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _store, record, service = self._setup(temp, phase=RunPhase.REVIEW)
            with self.assertRaises(CommitError):
                service.commit(record.run_id, approved=True)
            self.assertEqual(_count_commits(root), 1)

    def test_second_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _store, record, service = self._setup(temp)
            service.commit(record.run_id, approved=True)
            with self.assertRaises(CommitError):
                service.commit(record.run_id, approved=True)
            self.assertEqual(_count_commits(root), 2)

    def test_workspace_change_after_apply_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _store, record, service = self._setup(temp)
            (root / "app.py").write_text('def value():\n    return "tampered"\n', encoding="utf-8")

            with self.assertRaises(PatchConflict):
                service.commit(record.run_id, approved=True)
            self.assertEqual(_count_commits(root), 1)

    def test_undo_after_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _store, record, service = self._setup(temp)
            service.commit(record.run_id, approved=True)

            with self.assertRaises(CommitError):
                service.undo(record.run_id, approved=True)

    def test_committed_status_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, store, record, service = self._setup(temp)
            committed = service.commit(record.run_id, approved=True)
            reloaded = store.load(record.run_id)

            self.assertEqual(committed.status, PatchStatus.COMMITTED)
            self.assertEqual(reloaded.status, PatchStatus.COMMITTED)
            self.assertEqual(reloaded.commit["sha"], committed.commit["sha"])


class CommitContentTests(unittest.TestCase):
    def _setup(self, temp: str):
        root = Path(temp) / "repo"
        _create_repo(root)
        store = JsonPatchRunStore(Path(temp) / "runs")
        record = _done_run(root, store)
        return root, store, record, _service(root, store)

    def test_commit_contains_only_run_files_and_leaves_user_index_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _store, record, service = self._setup(temp)
            (root / "user.py").write_text("x = 2\n", encoding="utf-8")
            _git(root, "add", "user.py")

            committed = service.commit(record.run_id, approved=True)

            git = GitClient(root)
            self.assertEqual(git.committed_paths(committed.commit["sha"]), ("app.py",))
            self.assertIn("user.py", git.staged_paths())
            self.assertNotIn("user.py", git.committed_paths(committed.commit["sha"]))

    def test_default_message_cites_goal_run_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _store, record, service = self._setup(temp)
            committed = service.commit(record.run_id, approved=True)
            message = committed.commit["message"]

            self.assertIn("修复返回值", message)
            self.assertIn(record.run_id, message)
            self.assertIn("pytest", message)

    def test_custom_message_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _store, record, service = self._setup(temp)
            committed = service.commit(
                record.run_id, approved=True, message="fix: 返回值"
            )
            self.assertEqual(committed.commit["message"], "fix: 返回值")

    def test_report_includes_the_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _root, _store, record, service = self._setup(temp)
            committed = service.commit(record.run_id, approved=True)
            payload = build_report(committed).to_dict()

            self.assertEqual(payload["commit"]["sha"], committed.commit["sha"])


class CommitCliTests(unittest.TestCase):
    def _repo_with_opt_in(self, temp: Path):
        root = temp / "repo"
        _create_repo(root)
        (root / "repopilot.toml").write_text(
            '[permissions]\ngit_write = "ask"\n', encoding="utf-8"
        )
        store = JsonPatchRunStore(temp / "runs")
        record = _done_run(root, store)
        return root, store, record

    def test_cli_commit_respects_the_opt_in(self) -> None:
        from repopilot.cli import main

        with tempfile.TemporaryDirectory() as temp:
            root, _store, record = self._repo_with_opt_in(Path(temp))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "--runs-dir",
                        str(Path(temp) / "runs"),
                        "--store",
                        "json",
                        "commit",
                        record.run_id,
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("提交:", output.getvalue())
            self.assertEqual(_count_commits(root), 2)

    def test_cli_commit_is_denied_by_default(self) -> None:
        from repopilot.cli import main

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            store = JsonPatchRunStore(Path(temp) / "runs")
            record = _done_run(root, store)

            code = main(
                [
                    "--runs-dir",
                    str(Path(temp) / "runs"),
                    "--store",
                    "json",
                    "commit",
                    record.run_id,
                ]
            )

            self.assertEqual(code, 2)
            self.assertEqual(_count_commits(root), 1)


if __name__ == "__main__":
    unittest.main()
