import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from repopilot.benchmark import BUILTIN_SCENARIOS, Scenario, run_benchmarks, score_run
from repopilot.contracts import (
    ApprovalDecision,
    ReviewDecision,
    RunPhase,
    RunState,
    TaskRequest,
    VerificationResult,
)
from repopilot.git import GitClient
from repopilot.llm import ModelResponse
from repopilot.patching import PatchProposal, PatchRunRecord, PatchStatus, UnifiedDiffPatchEngine
from repopilot.persistence import JsonPatchRunStore
from repopilot.repair import RepairError
from repopilot.repo_map import RepoMap
from repopilot.report import build_report, render_markdown
from repopilot.review import ReviewError
from repopilot.runtime import build_runtime
from repopilot.workflow import WorkflowEngine
from repopilot.workspace import RepositoryWorkspace

_APP = 'def value():\n    return "old"\n'
_INITIAL_DIFF = (
    '--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n'
    '-    return "old"\n+    return "new"\n'
)
_REPAIR_DIFF = (
    '--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n'
    '-    return "new"\n+    return "fixed"\n'
)


class _Model:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)

    def complete(self, messages, *, tools=(), json_mode=False):
        del messages, tools
        if not json_mode:
            raise AssertionError("P3 model calls must request JSON")
        return ModelResponse(self.responses.pop(0))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _create_repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    (root / "app.py").write_text(_APP, encoding="utf-8")
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


def _applied_run(
    temp: Path, *, phase: RunPhase = RunPhase.VERIFY
) -> tuple[Path, JsonPatchRunStore, PatchRunRecord]:
    root = temp / "repo"
    _create_repo(root)
    store = JsonPatchRunStore(temp / "runs")
    engine = UnifiedDiffPatchEngine(RepositoryWorkspace(root), GitClient(root))
    proposal, baselines = engine.prepare(_INITIAL_DIFF, "初始修改")
    state = RunState(task=TaskRequest(goal="修复返回值", repository=str(root)))
    state.phase = RunPhase.VERIFY
    state.approval = ApprovalDecision.APPROVED
    state.verification = [
        VerificationResult(command=("bench",), action="bench", exit_code=1, stdout="AssertionError")
    ]
    record = PatchRunRecord(
        run_id="c" * 32,
        repository=str(root),
        goal="修复返回值",
        state=state.to_dict(),
        proposal=proposal,
        baselines=baselines,
    )
    store.save(record)
    engine.apply(record)
    store.save(record)
    if phase is not RunPhase.VERIFY:
        moved = RunState.from_dict(record.state)
        WorkflowEngine().transition(moved, phase)
        record.state = moved.to_dict()
        store.save(record)
    return root, store, record


class RepoMapTests(unittest.TestCase):
    def test_python_symbols_and_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "calc.py").write_text(
                "import os\n\n\nclass Engine:\n    def run(self):\n        pass\n\n\n"
                "def add(a, b):\n    return a + b\n",
                encoding="utf-8",
            )
            _git(root, "init", "-q")
            repo_map = RepoMap(root, GitClient(root))
            outline = repo_map.build()[0]
            self.assertEqual(
                [symbol.name for symbol in outline.symbols], ["Engine", "run", "add"]
            )
            self.assertEqual(outline.symbols[1].parent, "Engine")
            self.assertIn("os", outline.imports)

    def test_impact_finds_importers_and_callers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (root / "app.py").write_text(
                "from calc import add\n\n\ndef total():\n    return add(1, 2)\n", encoding="utf-8"
            )
            (root / "other.py").write_text("def unrelated():\n    pass\n", encoding="utf-8")
            _git(root, "init", "-q")
            impacted = RepoMap(root, GitClient(root)).impact(("calc.py",))
            self.assertIn("app.py", impacted)
            self.assertNotIn("other.py", impacted)

    def test_script_outline_extracts_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index.js").write_text(
                "import x from './x';\nexport class Store {}\nexport function load() {}\n",
                encoding="utf-8",
            )
            _git(root, "init", "-q")
            outline = RepoMap(root, GitClient(root)).build()[0]
            self.assertEqual([symbol.name for symbol in outline.symbols], ["Store", "load"])
            self.assertIn("./x", outline.imports)


class RepairWorkflowTests(unittest.TestCase):
    def test_repair_applies_bounded_patch_and_returns_to_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, store, record = _applied_run(Path(temp), phase=RunPhase.REPAIR)
            model = _Model(json.dumps({"unified_diff": _REPAIR_DIFF, "rationale": "修正返回值"}))
            runtime = build_runtime(root, model_client=model)

            repaired = runtime.repair_task(record.run_id, store)

            self.assertEqual(
                (root / "app.py").read_text(encoding="utf-8"), 'def value():\n    return "fixed"\n'
            )
            self.assertEqual(repaired.state["repair_attempts"], 1)
            self.assertEqual(repaired.state["phase"], "verify")
            self.assertEqual(len(repaired.repairs), 1)

    def test_repair_budget_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, store, record = _applied_run(Path(temp), phase=RunPhase.REPAIR)
            record.state["repair_attempts"] = 1
            store.save(record)
            runtime = build_runtime(root, model_client=_Model())

            with self.assertRaises(RepairError):
                runtime.repair_task(record.run_id, store)
            self.assertEqual(store.load(record.run_id).state["phase"], "failed")
            self.assertEqual(
                store.load(record.run_id).state["stop_reason"], "repair_budget_exhausted"
            )

    def test_repair_cannot_touch_files_outside_the_approved_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, store, record = _applied_run(Path(temp), phase=RunPhase.REPAIR)
            outside = "--- a/secret.py\n+++ b/secret.py\n@@ -0,0 +1 @@\n+x = 1\n"
            model = _Model(json.dumps({"unified_diff": outside, "rationale": "越界"}))
            runtime = build_runtime(root, model_client=model)

            with self.assertRaises(Exception) as caught:
                runtime.repair_task(record.run_id, store)
            self.assertIn("已批准范围", str(caught.exception))
            self.assertFalse((root / "secret.py").exists())

    def test_repair_keeps_exact_undo_of_the_original_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, store, record = _applied_run(Path(temp), phase=RunPhase.REPAIR)
            model = _Model(json.dumps({"unified_diff": _REPAIR_DIFF, "rationale": "修正"}))
            runtime = build_runtime(root, model_client=model)
            runtime.repair_task(record.run_id, store)

            runtime.patch_lifecycle(store).undo(record.run_id, approved=True)
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), _APP)


class ReviewWorkflowTests(unittest.TestCase):
    def _review(self, response: dict, *, attempts: int = 0):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root, store, record = _applied_run(Path(temp.name), phase=RunPhase.REVIEW)
        if attempts:
            record.state["repair_attempts"] = attempts
            store.save(record)
        runtime = build_runtime(root, model_client=_Model(json.dumps(response)))
        return runtime, store, record

    def test_approve_completes_the_run(self) -> None:
        runtime, store, record = self._review(
            {"decision": "approve", "summary": "与计划一致", "findings": []}
        )
        report = runtime.review_task(record.run_id, store)
        self.assertIs(report.decision, ReviewDecision.APPROVE)
        self.assertEqual(store.load(record.run_id).state["phase"], "done")
        self.assertEqual(store.load(record.run_id).state["stop_reason"], "verified")

    def test_request_changes_returns_to_repair(self) -> None:
        runtime, store, record = self._review(
            {
                "decision": "request_changes",
                "summary": "缺少边界处理",
                "findings": [
                    {"severity": "high", "file": "app.py", "line": 2, "message": "未处理空输入"}
                ],
            },
        )
        runtime.review_task(record.run_id, store)
        state = store.load(record.run_id).state
        self.assertEqual(state["phase"], "repair")
        self.assertEqual(state["stop_reason"], "review_requested_changes")
        self.assertEqual(state["review"]["findings"][0]["file"], "app.py")

    def test_applied_file_must_contain_the_finding_path(self) -> None:
        runtime, store, record = self._review(
            {
                "decision": "request_changes",
                "summary": "路径不实",
                "findings": [
                    {"severity": "high", "file": "ghost.py", "line": 1, "message": "不存在"}
                ],
            },
        )
        with self.assertRaises(ReviewError):
            runtime.review_task(record.run_id, store)

    def test_request_changes_without_budget_fails_the_run(self) -> None:
        runtime, store, record = self._review(
            {"decision": "reject", "summary": "方向错误", "findings": []},
            attempts=1,
        )
        runtime.review_task(record.run_id, store)
        state = store.load(record.run_id).state
        self.assertEqual(state["phase"], "failed")
        self.assertEqual(state["stop_reason"], "repair_budget_exhausted")


class ReportTests(unittest.TestCase):
    def test_markdown_cites_diff_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, store, record = _applied_run(Path(temp))
            report = build_report(store.load(record.run_id))
            text = render_markdown(report)

            self.assertIn("app.py", text)
            self.assertIn("| `app.py` | 1 | 1 |", text)
            self.assertIn("bench", text)
            self.assertIn("## 结论", text)

    def test_json_report_has_traceable_conclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, store, record = _applied_run(Path(temp))
            payload = build_report(store.load(record.run_id)).to_dict()

            self.assertEqual(payload["changed_files"], ["app.py"])
            self.assertEqual(payload["files"], [{"path": "app.py", "added": 1, "removed": 1}])
            self.assertTrue(payload["conclusions"])
            for line in payload["conclusions"]:
                self.assertTrue(line.startswith(("变更 ", "验证 ")), line)


class BenchmarkTests(unittest.TestCase):
    def test_builtin_scenarios_all_pass_offline(self) -> None:
        results = {item.scenario: item for item in run_benchmarks()}
        failing = [
            (key, item.score.reasons, item.error)
            for key, item in results.items()
            if not item.ok
        ]
        self.assertEqual(failing, [])
        self.assertEqual(len(results), len(BUILTIN_SCENARIOS))
        self.assertIsNotNone(results["dirty_worktree_conflict"].error)
        self.assertEqual(results["repair_recovery"].phase, "done")
        self.assertEqual(results["repair_recovery"].stop_reason, "verified")

    def test_unrelated_modification_lowers_the_score(self) -> None:
        scenario = Scenario(
            name="penalty",
            description="无关修改应扣分",
            goal="修复",
            files={},
            responses=(),
            verify_outcomes=(),
            expected_files=("app.py",),
            expected_phase=RunPhase.DONE,
            expected_stop_reason="verified",
        )
        clean = _record_for(("app.py",))
        noisy = _record_for(("app.py", "extra.py"))

        self.assertEqual(score_run(scenario, clean).score, 1.0)
        penalty = score_run(scenario, noisy)
        self.assertLess(penalty.score, 1.0)
        self.assertIn("extra.py", " ".join(penalty.reasons))


def _record_for(touched: tuple[str, ...]) -> PatchRunRecord:
    state = RunState(task=TaskRequest(goal="修复", repository="/tmp/repo"))
    state.phase = RunPhase.DONE
    state.stop_reason = "verified"
    return PatchRunRecord(
        run_id="d" * 32,
        repository="/tmp/repo",
        goal="修复",
        state=state.to_dict(),
        proposal=PatchProposal(unified_diff="", touched_files=touched, rationale="测试"),
        baselines=(),
        status=PatchStatus.UNDONE,
    )


class CliP3Tests(unittest.TestCase):
    def test_report_command_renders_markdown_and_json(self) -> None:
        from repopilot.cli import main

        with tempfile.TemporaryDirectory() as temp:
            _, _, record = _applied_run(Path(temp))
            base = [
                "--runs-dir",
                str(Path(temp) / "runs"),
                "--store",
                "json",
                "report",
                record.run_id,
            ]

            markdown = io.StringIO()
            with contextlib.redirect_stdout(markdown):
                self.assertEqual(main(base), 0)
            self.assertIn("RepoPilot 运行报告", markdown.getvalue())

            payload_out = io.StringIO()
            with contextlib.redirect_stdout(payload_out):
                self.assertEqual(main([*base, "--format", "json"]), 0)
            self.assertEqual(json.loads(payload_out.getvalue())["run_id"], record.run_id)


if __name__ == "__main__":
    unittest.main()
