import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from repopilot.cli import main
from repopilot.llm import ModelResponse
from repopilot.patching import PatchStatus
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


if __name__ == "__main__":
    unittest.main()
