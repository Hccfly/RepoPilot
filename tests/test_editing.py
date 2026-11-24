import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from repopilot.cli import main
from repopilot.llm import ModelResponse
from repopilot.patching import PatchError, PatchStatus
from repopilot.persistence import JsonPatchRunStore
from repopilot.runtime import build_runtime


class _SequenceModel:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)

    def complete(self, messages, *, tools=(), json_mode=False):
        del messages, tools
        if not json_mode:
            raise AssertionError("P1 model calls must request JSON")
        return ModelResponse(self.responses.pop(0))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _create_repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "RepoPilot Tests")
    _git(root, "config", "user.email", "repopilot@example.invalid")
    (root / "app.py").write_text('def value():\n    return "old"\n', encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "fixture")


class _CountingModel:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete(self, messages, *, tools=(), json_mode=False):
        del messages, tools
        self.calls += 1
        return ModelResponse(self.responses.pop(0))


class EditingWorkflowTests(unittest.TestCase):
    def test_propose_diff_apply_undo_end_to_end(self) -> None:
        model = _SequenceModel(
            '{"queries":["return"],"files":["app.py"],"reason":"定位返回值"}',
            '{"summary":"更新返回值","steps":[{"description":"修改 value 返回值",'
            '"files":["app.py"],"verification":[]}],"risks":[]}',
            '{"unified_diff":"--- a/app.py\\n+++ b/app.py\\n@@ -1,2 +1,2 @@\\n '
            'def value():\\n-    return \\"old\\"\\n+    return \\"new\\"\\n",'
            '"rationale":"按计划更新返回值"}',
        )
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repo"
            ledger = base / "runs"
            _create_repo(root)
            runtime = build_runtime(root, model_client=model)
            store = JsonPatchRunStore(ledger)

            record = runtime.propose_task("把 value 返回值改为 new", store)
            self.assertEqual(record.status, PatchStatus.PROPOSED)
            self.assertEqual(
                (root / "app.py").read_text(encoding="utf-8"), 'def value():\n    return "old"\n'
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    main(["--runs-dir", str(ledger), "--store", "json", "diff", record.run_id]), 0
                )
            self.assertIn("--- a/app.py", output.getvalue())

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(["--runs-dir", str(ledger), "--store", "json", "apply", record.run_id]), 0
                )
            self.assertEqual(
                (root / "app.py").read_text(encoding="utf-8"), 'def value():\n    return "new"\n'
            )

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(["--runs-dir", str(ledger), "--store", "json", "undo", record.run_id]), 0
                )
            self.assertEqual(
                (root / "app.py").read_text(encoding="utf-8"), 'def value():\n    return "old"\n'
            )


    def test_plan_named_file_is_read_before_the_editor_edits_it(self) -> None:
        model = _SequenceModel(
            '{"queries":["value"],"files":["app.py"],"reason":"定位"}',
            '{"summary":"两处修改","steps":['
            '{"description":"改 app","files":["app.py"]},'
            '{"description":"改 other","files":["other.py"]}],"risks":[]}',
            '{"unified_diff":"--- a/other.py\\n+++ b/other.py\\n@@ -1,1 +1,2 @@\\n '
            'x = 1\\n+y = 2\\n","rationale":"按计划修改"}',
        )
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repo"
            ledger = base / "runs"
            _create_repo(root)
            (root / "other.py").write_text("x = 1\n", encoding="utf-8")
            _git(root, "add", "other.py")
            _git(root, "-c", "user.name=t", "-c", "user.email=t@e.i", "commit", "-qm", "other")
            runtime = build_runtime(root, model_client=model)
            store = JsonPatchRunStore(ledger)

            record = runtime.propose_task("把 value 返回值改为 new", store)
            remaining = (root / "other.py").read_text(encoding="utf-8")

        self.assertEqual(record.status, PatchStatus.PROPOSED)
        self.assertEqual(record.proposal.touched_files, ("other.py",))
        self.assertEqual(remaining, "x = 1\n")


    def test_empty_editor_diff_is_retried_once(self) -> None:
        edit = (
            '{"unified_diff":"--- a/app.py\\n+++ b/app.py\\n@@ -1,2 +1,2 @@\\n '
            'def value():\\n-    return \\"old\\"\\n+    return \\"new\\"\\n","rationale":"修改"}'
        )
        model = _CountingModel(
            '{"queries":["value"],"files":["app.py"],"reason":"定位"}',
            '{"summary":"改 value","steps":[{"description":"改",'
            '"files":["app.py"]}],"risks":[]}',
            '{"unified_diff":"","rationale":"看不出要改什么"}',
            edit,
        )
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repo"
            _create_repo(root)
            runtime = build_runtime(root, model_client=model)

            record = runtime.propose_task("把 value 返回值改为 new", JsonPatchRunStore(base / "runs"))

        self.assertEqual(record.status, PatchStatus.PROPOSED)
        self.assertEqual(model.calls, 4)

    def test_corrupt_diff_is_retried_once(self) -> None:
        corrupt = (
            '{"unified_diff":"--- a/app.py\\n+++ b/app.py\\n@@ -1,9 +1,9 @@\\n '
            'def value():\\n-    return \\"old\\"\\n+    return \\"new\\"\\n","rationale":"错的行数"}'
        )
        fixed = (
            '{"unified_diff":"--- a/app.py\\n+++ b/app.py\\n@@ -1,2 +1,2 @@\\n '
            'def value():\\n-    return \\"old\\"\\n+    return \\"new\\"\\n","rationale":"行数已修正"}'
        )
        model = _CountingModel(
            '{"queries":["value"],"files":["app.py"],"reason":"定位"}',
            '{"summary":"改 value","steps":[{"description":"改",'
            '"files":["app.py"]}],"risks":[]}',
            corrupt,
            fixed,
        )
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repo"
            _create_repo(root)
            runtime = build_runtime(root, model_client=model)

            record = runtime.propose_task(
                "把 value 返回值改为 new", JsonPatchRunStore(base / "runs")
            )

        self.assertEqual(record.status, PatchStatus.PROPOSED)
        self.assertEqual(record.proposal.rationale, "行数已修正")
        self.assertEqual(model.calls, 4)

    def test_empty_editor_diff_twice_reports_the_model_reason(self) -> None:
        empty = '{"unified_diff":"","rationale":"当前代码看起来没有问题"}'
        model = _CountingModel(
            '{"queries":["value"],"files":["app.py"],"reason":"定位"}',
            '{"summary":"改 value","steps":[{"description":"改",'
            '"files":["app.py"]}],"risks":[]}',
            empty,
            empty,
        )
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repo"
            _create_repo(root)
            runtime = build_runtime(root, model_client=model)

            with self.assertRaises(PatchError) as caught:
                runtime.propose_task("把 value 返回值改为 new", JsonPatchRunStore(base / "runs"))

        self.assertIn("当前代码看起来没有问题", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
