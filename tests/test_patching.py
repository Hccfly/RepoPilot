import difflib
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from repopilot.git import GitClient
from repopilot.patching import (
    PatchConflict,
    PatchPolicyError,
    PatchRunRecord,
    PatchStatus,
    UnifiedDiffPatchEngine,
)
from repopilot.persistence import JsonPatchRunStore, RunStoreError
from repopilot.policy import PermissionPolicy
from repopilot.workspace import RepositoryWorkspace, WorkspaceError


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _committed_repo(root: Path, files: dict[str, bytes]) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "RepoPilot Tests")
    _git(root, "config", "user.email", "repopilot@example.invalid")
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "fixture")


def _diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _record(
    engine: UnifiedDiffPatchEngine,
    patch: str,
) -> PatchRunRecord:
    proposal, baselines = engine.prepare(patch, "测试补丁")
    return PatchRunRecord(
        run_id=uuid4().hex,
        repository=str(engine.workspace.root),
        goal="测试目标",
        state={"phase": "wait_approval", "approval": "pending", "changed_files": []},
        proposal=proposal,
        baselines=baselines,
    )


class PatchEngineTests(unittest.TestCase):
    def test_apply_and_undo_restore_exact_bytes(self) -> None:
        original = b"alpha\nsecond line  \n"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _committed_repo(root, {"notes.txt": original})
            engine = UnifiedDiffPatchEngine(RepositoryWorkspace.discover(root), GitClient(root))
            patch = _diff("notes.txt", original.decode(), "beta\nsecond line  \n")
            record = _record(engine, patch)

            receipt = engine.apply(record)
            self.assertEqual(
                (root / "notes.txt").read_text(encoding="utf-8"),
                "beta\nsecond line  \n",
            )
            self.assertTrue(receipt.reverse_diff)
            self.assertEqual(record.status, PatchStatus.APPLIED)

            engine.undo(record)
            self.assertEqual((root / "notes.txt").read_bytes(), original)
            self.assertEqual(record.status, PatchStatus.UNDONE)

    def test_add_delete_and_undo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _committed_repo(root, {"keep.txt": b"keep\n", "delete.txt": b"old\n"})
            engine = UnifiedDiffPatchEngine(RepositoryWorkspace.discover(root), GitClient(root))
            add = "".join(
                difflib.unified_diff([], ["new\n"], fromfile="/dev/null", tofile="b/new.txt")
            )
            delete = "".join(
                difflib.unified_diff(["old\n"], [], fromfile="a/delete.txt", tofile="/dev/null")
            )
            record = _record(engine, add + delete)

            engine.apply(record)
            self.assertEqual((root / "new.txt").read_text(encoding="utf-8"), "new\n")
            self.assertFalse((root / "delete.txt").exists())

            engine.undo(record)
            self.assertFalse((root / "new.txt").exists())
            self.assertEqual((root / "delete.txt").read_bytes(), b"old\n")

    def test_changed_after_proposal_is_a_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _committed_repo(root, {"app.txt": b"old\n"})
            engine = UnifiedDiffPatchEngine(RepositoryWorkspace.discover(root), GitClient(root))
            record = _record(engine, _diff("app.txt", "old\n", "agent\n"))
            (root / "app.txt").write_bytes(b"user\n")

            with self.assertRaises(PatchConflict):
                engine.apply(record)
            self.assertEqual((root / "app.txt").read_bytes(), b"user\n")

    def test_dirty_tracked_and_untracked_targets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _committed_repo(root, {"tracked.txt": b"old\n"})
            engine = UnifiedDiffPatchEngine(RepositoryWorkspace.discover(root), GitClient(root))
            (root / "tracked.txt").write_bytes(b"user\n")
            with self.assertRaises(PatchPolicyError):
                engine.prepare(_diff("tracked.txt", "user\n", "agent\n"), "不能覆盖")

            (root / "new.txt").write_bytes(b"user\n")
            with self.assertRaises(PatchPolicyError):
                engine.prepare(_diff("new.txt", "user\n", "agent\n"), "不能覆盖")

    def test_initial_dirty_target_stays_protected_if_later_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _committed_repo(root, {"tracked.txt": b"old\n"})
            engine = UnifiedDiffPatchEngine(RepositoryWorkspace.discover(root), GitClient(root))
            (root / "tracked.txt").write_bytes(b"user\n")
            initial_dirty = engine.git.dirty_paths()
            (root / "tracked.txt").write_bytes(b"old\n")

            with self.assertRaises(PatchPolicyError):
                engine.prepare(
                    _diff("tracked.txt", "old\n", "agent\n"),
                    "任务开始时已有改动",
                    protected_dirty_paths=initial_dirty,
                )

    def test_invalid_multifile_patch_leaves_every_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            originals = {"one.txt": b"one\n", "two.txt": b"two\n"}
            _committed_repo(root, originals)
            engine = UnifiedDiffPatchEngine(RepositoryWorkspace.discover(root), GitClient(root))
            patch = _diff("one.txt", "one\n", "changed\n")
            patch += _diff("two.txt", "not-two\n", "changed\n")

            with self.assertRaises(PatchConflict):
                engine.prepare(patch, "第二个 hunk 冲突")
            self.assertEqual((root / "one.txt").read_bytes(), originals["one.txt"])
            self.assertEqual((root / "two.txt").read_bytes(), originals["two.txt"])

    def test_traversal_and_secret_targets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _committed_repo(root, {"safe.txt": b"safe\n"})
            engine = UnifiedDiffPatchEngine(RepositoryWorkspace.discover(root), GitClient(root))
            traversal = _diff("../outside.txt", "", "bad\n")
            secret = _diff(".env", "", "TOKEN=bad\n")

            with self.assertRaises(PatchPolicyError):
                engine.prepare(traversal, "越界")
            with self.assertRaises(WorkspaceError):
                engine.prepare(secret, "密钥")
            self.assertFalse((Path(temp) / "outside.txt").exists())
            self.assertFalse((root / ".env").exists())

    def test_cross_path_patch_is_rejected_as_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _committed_repo(root, {"old.txt": b"old\n"})
            engine = UnifiedDiffPatchEngine(RepositoryWorkspace.discover(root), GitClient(root))
            patch = "--- a/old.txt\n+++ b/new.txt\n@@ -1 +1 @@\n-old\n+new\n"

            with self.assertRaises(PatchPolicyError):
                engine.prepare(patch, "伪装重命名")


class PatchLedgerTests(unittest.TestCase):
    def test_ledger_is_external_and_lifecycle_requires_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repo"
            ledger = base / "local-app-data" / "RepoPilot" / "runs"
            _committed_repo(root, {"app.txt": b"old\n"})
            engine = UnifiedDiffPatchEngine(RepositoryWorkspace.discover(root), GitClient(root))
            record = _record(engine, _diff("app.txt", "old\n", "new\n"))
            store = JsonPatchRunStore(ledger)
            store.save(record)

            from repopilot.editing import PatchLifecycleService

            service = PatchLifecycleService(engine, PermissionPolicy(), store)
            with self.assertRaises(PatchPolicyError):
                service.apply(record.run_id, approved=False)
            applied = service.apply(record.run_id, approved=True)
            self.assertEqual(applied.status, PatchStatus.APPLIED)
            self.assertTrue((ledger / f"{record.run_id}.json").is_file())
            self.assertFalse((root / ".repopilot").exists())

            undone = service.undo(record.run_id, approved=True)
            self.assertEqual(undone.status, PatchStatus.UNDONE)
            self.assertEqual((root / "app.txt").read_bytes(), b"old\n")

    def test_run_id_cannot_escape_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JsonPatchRunStore(temp)
            with self.assertRaises(RunStoreError):
                store.load("../outside")


if __name__ == "__main__":
    unittest.main()
