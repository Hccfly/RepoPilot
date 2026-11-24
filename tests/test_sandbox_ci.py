import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from repopilot.ci import run_ci_plan, run_ci_propose, run_ci_verify
from repopilot.config import SandboxSettings
from repopilot.contracts import (
    ApprovalDecision,
    RunPhase,
    RunState,
    TaskRequest,
)
from repopilot.llm import ModelResponse
from repopilot.patching import PatchProposal, PatchRunRecord, PatchStatus
from repopilot.persistence import JsonPatchRunStore
from repopilot.runtime import build_runtime
from repopilot.sandbox import SandboxError, build_verification_runner
from repopilot.verification import (
    SubprocessVerificationRunner,
    VerificationAction,
)

_APP = 'def value():\n    return "old"\n'
_PASSING_TEST = 'from app import value\n\n\ndef test_value():\n    assert value() == "old"\n'
_FAILING_TEST = 'from app import value\n\n\ndef test_value():\n    assert value() == "new"\n'
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


class _SequenceModel:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)

    def complete(self, messages, *, tools=(), json_mode=False):
        del messages, tools
        return ModelResponse(self.responses.pop(0))


def _action(name: str, *argv: str) -> VerificationAction:
    return VerificationAction(name=name, description=name, project="test", argv=tuple(argv))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _create_repo(root: Path, *, test_source: str = _PASSING_TEST) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (root / "app.py").write_text(_APP, encoding="utf-8")
    (root / "test_app.py").write_text(test_source, encoding="utf-8")
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


class SandboxRunnerTests(unittest.TestCase):
    def test_prefix_keeps_action_arguments_separate(self) -> None:
        probe = "import json,sys;print(json.dumps(sys.argv[1:]))"
        with tempfile.TemporaryDirectory() as temp:
            runner = SubprocessVerificationRunner(
                Path(temp), prefix=(sys.executable, "-c", probe)
            )
            result = runner.run(_action("probe", "a b", '"quoted"'))

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(json.loads(result.stdout), ["a b", '"quoted"'])

    def test_workspace_placeholder_is_substituted_as_one_argument(self) -> None:
        probe = "import sys;print(sys.argv[1])"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runner = SubprocessVerificationRunner(
                root, prefix=(sys.executable, "-c", probe, "{workspace}")
            )
            result = runner.run(_action("cwd", "ignored"))

        self.assertEqual(Path(result.stdout.strip()), root)


class SandboxFactoryTests(unittest.TestCase):
    def _build(self, settings: SandboxSettings):
        return build_verification_runner(
            settings, Path("."), timeout_seconds=5, max_output_chars=100
        )

    def test_local_mode_has_no_prefix(self) -> None:
        self.assertEqual(self._build(SandboxSettings()).prefix, ())

    def test_command_mode_sets_the_prefix(self) -> None:
        runner = self._build(
            SandboxSettings(mode="command", command=("docker", "run", "--rm"))
        )
        self.assertEqual(runner.prefix, ("docker", "run", "--rm"))

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(SandboxError):
            self._build(SandboxSettings(mode="magic"))

    def test_empty_command_is_rejected(self) -> None:
        with self.assertRaises(SandboxError):
            self._build(SandboxSettings(mode="command"))


class CiProposeTests(unittest.TestCase):
    def test_propose_writes_artifacts_and_never_applies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            store = JsonPatchRunStore(Path(temp) / "runs")
            runtime = build_runtime(
                root, model_client=_SequenceModel(_EXPLORER, _PLANNER, _EDITOR)
            )
            out = Path(temp) / "artifacts"

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run_ci_propose(runtime, store, "把 value 改成 new", out)

            self.assertEqual(code, 0)
            self.assertIn('+    return "new"', (out / "patch.diff").read_text(encoding="utf-8"))
            payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["changed_files"], ["app.py"])
            self.assertIn("未应用任何改动", output.getvalue())
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), _APP)

    def test_plan_failure_returns_one_and_keeps_the_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            runtime = build_runtime(root, model_client=_SequenceModel(""))
            out = Path(temp) / "artifacts"

            with contextlib.redirect_stdout(io.StringIO()):
                code = run_ci_plan(runtime, "修复", out)

            self.assertEqual(code, 1)
            self.assertTrue((out / "plan.json").is_file())


class CiVerifyTests(unittest.TestCase):
    def _run(self, temp: str, *, test_source: str):
        root = Path(temp) / "repo"
        _create_repo(root, test_source=test_source)
        store = JsonPatchRunStore(Path(temp) / "runs")
        state = RunState(task=TaskRequest(goal="验证", repository=str(root)))
        state.phase = RunPhase.VERIFY
        state.approval = ApprovalDecision.APPROVED
        record = PatchRunRecord(
            run_id="b" * 32,
            repository=str(root),
            goal="验证",
            state=state.to_dict(),
            proposal=PatchProposal(unified_diff="", touched_files=("app.py",), rationale="t"),
            baselines=(),
            status=PatchStatus.APPLIED,
        )
        store.save(record)
        runtime = build_runtime(root, model_client=_SequenceModel())
        out = Path(temp) / "artifacts"
        with contextlib.redirect_stdout(io.StringIO()):
            code = run_ci_verify(runtime, store, record.run_id, out)
        state = store.load(record.run_id).state
        payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
        return code, state, payload, (out / "report.md").is_file()

    def test_passing_verification_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            code, state, payload, has_markdown = self._run(temp, test_source=_PASSING_TEST)

        self.assertEqual(code, 0)
        self.assertEqual(state["phase"], "review")
        self.assertEqual(payload["phase"], "review")
        self.assertTrue(has_markdown)

    def test_failing_verification_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            code, state, payload, _ = self._run(temp, test_source=_FAILING_TEST)

        self.assertEqual(code, 1)
        self.assertEqual(state["phase"], "repair")
        self.assertEqual(payload["stop_reason"], "verification_failed")


class CiParserTests(unittest.TestCase):
    def test_ci_subcommands_are_wired(self) -> None:
        from repopilot.cli import _parser

        args = _parser().parse_args(
            ["ci", "propose", "目标", "--repo", ".", "--out-dir", "artifacts"]
        )
        self.assertEqual(args.command, "ci")
        self.assertEqual(args.ci_command, "propose")
        self.assertEqual(args.goal, "目标")
        self.assertEqual(args.out_dir, "artifacts")


if __name__ == "__main__":
    unittest.main()
