import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repopilot.contracts import RunPhase
from repopilot.llm import ModelResponse
from repopilot.runtime import build_runtime

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


class _SequenceModel:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, *, tools=(), json_mode=False):
        self.calls.append((messages, tools, json_mode))
        return ModelResponse(self.responses.pop(0))


def _create_repo(target: Path) -> None:
    shutil.copytree(FIXTURE, target, dirs_exist_ok=True)
    subprocess.run(["git", "init", "-q", str(target)], check=True, capture_output=True)


class ReadOnlyPlanningTests(unittest.TestCase):
    def test_plan_uses_only_inspected_files_and_does_not_mutate_fixture(self) -> None:
        before = {
            path.relative_to(FIXTURE): path.read_bytes()
            for path in FIXTURE.rglob("*")
            if path.is_file()
        }
        model = _SequenceModel(
            '{"queries":["parse_input"],"files":["src/parser.py"],"reason":"定位实现"}',
            '{"summary":"补充空输入处理","steps":[{"description":"在解析入口处理空输入",'
            '"files":["src/parser.py"],"verification":["pytest tests/test_parser.py"]}],'
            '"risks":["需要确认空输入的预期返回值"]}',
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            runtime = build_runtime(root, model_client=model)
            with patch.object(
                socket, "create_connection", side_effect=AssertionError("network blocked")
            ):
                state = runtime.plan_task("修复解析器空输入异常")

        after = {
            path.relative_to(FIXTURE): path.read_bytes()
            for path in FIXTURE.rglob("*")
            if path.is_file()
        }
        self.assertEqual(state.phase, RunPhase.WAIT_APPROVAL)
        self.assertEqual(state.plan.steps[0].files, ("src/parser.py",))
        self.assertIn("src/parser.py", state.inspected_files)
        self.assertEqual(before, after)
        self.assertTrue(all(call[2] for call in model.calls))

    def test_empty_explorer_output_becomes_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            runtime = build_runtime(root, model_client=_SequenceModel(""))

            state = runtime.plan_task("修复解析器")

        self.assertEqual(state.phase, RunPhase.FAILED)
        self.assertIn("返回空内容", state.error)

    def test_invalid_explorer_json_becomes_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            runtime = build_runtime(root, model_client=_SequenceModel("not-json"))

            state = runtime.plan_task("修复解析器")

        self.assertEqual(state.phase, RunPhase.FAILED)
        self.assertIn("不是合法 JSON", state.error)

    def test_planner_cannot_reference_uninspected_file(self) -> None:
        model = _SequenceModel(
            '{"queries":["parse_input"],"files":["src/parser.py"],"reason":"定位实现"}',
            '{"summary":"计划","steps":[{"description":"修改未检查文件",'
            '"files":["src/unknown.py"],"verification":[]}],"risks":[]}',
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            runtime = build_runtime(root, model_client=model)

            state = runtime.plan_task("修复解析器")

        self.assertEqual(state.phase, RunPhase.FAILED)
        self.assertIn("未读取文件", state.error)


if __name__ == "__main__":
    unittest.main()
