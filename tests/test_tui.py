import tempfile
import unittest
from pathlib import Path

from repopilot.llm import ModelResponse
from repopilot.tui import TuiSession

_APP = 'def value():\n    return "old"\n'
_TEST = 'from app import value\n\n\ndef test_value():\n    assert value() == "new"\n'
_PYPROJECT = '[project]\nname = "demo"\nversion = "0.1.0"\n'

_EXPLORER = '{"queries":["value"],"files":["app.py"],"reason":"定位返回值"}'
_PLANNER = (
    '{"summary":"更新返回值","steps":[{"description":"修改 value",'
    '"files":["app.py"],"verification":[]}],"risks":[]}'
)
_EDITOR = (
    '{"unified_diff":"--- a/app.py\\n+++ b/app.py\\n@@ -1,2 +1,2 @@\\n '
    'def value():\\n-    return \\"old\\"\\n+    return \\"new\\"\\n","rationale":"按计划更新"}'
)
_REVIEW = '{"decision":"approve","summary":"与目标一致","findings":[]}'


class _SequenceModel:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)

    def complete(self, messages, *, tools=(), json_mode=False):
        del messages, tools
        if not json_mode:
            raise AssertionError("TUI flow expects JSON mode")
        return ModelResponse(self.responses.pop(0))


def _git(root: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _create_repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (root / "app.py").write_text(_APP, encoding="utf-8")
    (root / "test_app.py").write_text(_TEST, encoding="utf-8")
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


def _session(
    root: Path, *, lines=(), answer=None, runtime_factory=None
) -> tuple[TuiSession, list[str]]:
    output: list[str] = []
    queue = list(lines)

    def reader(prompt: str = "") -> str:
        del prompt
        if queue:
            return queue.pop(0)
        if answer is None:
            raise EOFError
        return answer

    session = TuiSession(
        repository=root,
        runs_dir=root.parent / "runs",
        store_kind="json",
        reader=reader,
        writer=output.append,
        color=False,
        runtime_factory=runtime_factory,
    )
    return session, output


class TuiCommandTests(unittest.TestCase):
    def test_help_lists_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session, output = _session(Path(temp))
            self.assertEqual(session.execute("/help"), 0)
            text = "\n".join(output)
            for command in ("propose", "apply", "verify", "review", "report"):
                self.assertIn(command, text)

    def test_unknown_command_reports_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session, output = _session(Path(temp))
            self.assertEqual(session.execute("nonsense"), 1)
            self.assertIn("未知命令", "\n".join(output))
            self.assertEqual(session.execute("help"), 0)

    def test_quit_stops_the_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session, output = _session(Path(temp), lines=["help", "quit", "help"])
            self.assertEqual(session.run(), 0)
            joined = "\n".join(output)
            self.assertEqual(joined.count("可用命令"), 1)

    def test_eof_stops_the_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session, _ = _session(Path(temp), lines=[])
            self.assertEqual(session.run(), 0)

    def test_providers_works_without_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session, output = _session(Path(temp))
            self.assertEqual(session.execute("providers"), 0)
            text = "\n".join(output)
            self.assertIn("deepseek", text)
            self.assertIn("DEEPSEEK_API_KEY", text)

    def test_blank_line_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session, output = _session(Path(temp))
            self.assertEqual(session.execute("   "), 0)
            self.assertEqual(output, [])

    def test_commit_is_wired_and_requires_a_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session, output = _session(Path(temp))
            self.assertEqual(session.execute("commit"), 1)
            self.assertIn("commit <run_id>", "\n".join(output))


class TuiFlowTests(unittest.TestCase):
    def test_apply_requires_confirmation(self) -> None:
        model = _SequenceModel(_EXPLORER, _PLANNER, _EDITOR)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            session, output = _session(
                root,
                answer="n",
                runtime_factory=lambda repo: _runtime(repo, model),
            )
            self.assertEqual(session.execute("propose 把 value 改成 new"), 0)
            run_id = session.store.list_records()[-1].run_id

            self.assertEqual(session.execute(f"apply {run_id}"), 0)
            self.assertIn("已取消", "\n".join(output))
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), _APP)

    def test_propose_apply_verify_review_end_to_end(self) -> None:
        model = _SequenceModel(_EXPLORER, _PLANNER, _EDITOR, _REVIEW)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            session, output = _session(
                root,
                answer="y",
                runtime_factory=lambda repo: _runtime(repo, model),
            )

            self.assertEqual(session.execute("propose 把 value 改成 new"), 0)
            record = session.store.list_records()[-1]
            run_id = record.run_id

            self.assertEqual(session.execute(f"show {run_id}"), 0)
            self.assertIn("wait_approval", "\n".join(output))

            self.assertEqual(session.execute(f"diff {run_id}"), 0)
            self.assertIn('+    return "new"', "\n".join(output))

            self.assertEqual(session.execute(f"apply {run_id}"), 0)
            self.assertEqual(
                (root / "app.py").read_text(encoding="utf-8"), 'def value():\n    return "new"\n'
            )

            self.assertEqual(session.execute(f"verify {run_id} pytest"), 0)
            self.assertEqual(session.execute(f"review {run_id}"), 0)
            self.assertEqual(session.execute(f"report {run_id}"), 0)

            state = session.store.load(run_id).state
            self.assertEqual(state["phase"], "done")
            joined = "\n".join(output)
            self.assertIn("[通过] pytest", joined)
            self.assertIn("RepoPilot 运行报告", joined)

    def test_commands_on_unknown_run_report_a_readable_error(self) -> None:
        model = _SequenceModel()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            session, output = _session(root, runtime_factory=lambda repo: _runtime(repo, model))
            self.assertEqual(session.execute("show " + "f" * 32), 1)
            self.assertIn("错误", "\n".join(output))

    def test_open_switches_repository(self) -> None:
        model = _SequenceModel()
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "one"
            second = Path(temp) / "two"
            _create_repo(first)
            _create_repo(second)
            session, output = _session(first, runtime_factory=lambda repo: _runtime(repo, model))
            self.assertEqual(session.execute(f"open {second}"), 0)
            self.assertEqual(session.runtime.workspace.root, second.resolve())
            self.assertIn(str(second.resolve()), "\n".join(output))


def _runtime(root: Path, model):
    from repopilot.runtime import build_runtime

    return build_runtime(root, model_client=model)


if __name__ == "__main__":
    unittest.main()
