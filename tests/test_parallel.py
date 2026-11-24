import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from repopilot.concurrency import run_ordered
from repopilot.config import PermissionSettings
from repopilot.contracts import (
    ApprovalDecision,
    RunPhase,
    RunState,
    TaskRequest,
    VerificationResult,
)
from repopilot.llm import ModelResponse
from repopilot.patching import PatchProposal, PatchRunRecord, PatchStatus
from repopilot.persistence import JsonPatchRunStore
from repopilot.policy import PermissionPolicy
from repopilot.runtime import build_runtime
from repopilot.verification import (
    VerificationAction,
    VerificationError,
    VerificationWhitelist,
    VerificationWorkflow,
)
from repopilot.workflow import WorkflowEngine

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"
_EXPLORER = '{"queries":["parse_input","return"],"files":["src/parser.py"],"reason":"定位实现"}'
_PLANNER = (
    '{"summary":"补充空输入处理","steps":[{"description":"在解析入口处理空输入",'
    '"files":["src/parser.py"],"verification":["pytest"]}],"risks":[]}'
)


class RunOrderedTests(unittest.TestCase):
    def test_results_follow_input_order_not_completion_order(self) -> None:
        delays = {0: 0.09, 1: 0.05, 2: 0.0}

        def work(item: int) -> int:
            time.sleep(delays[item])
            return item * 10

        self.assertEqual(run_ordered(work, [0, 1, 2], max_workers=3), [0, 10, 20])

    def test_single_worker_runs_sequentially(self) -> None:
        order: list[int] = []

        def work(item: int) -> int:
            order.append(item)
            return item

        run_ordered(work, [1, 2, 3], max_workers=1)
        self.assertEqual(order, [1, 2, 3])

    def test_first_failure_by_input_order_is_raised(self) -> None:
        def work(item: int) -> int:
            if item == 2:
                time.sleep(0.02)
                raise RuntimeError("second")
            if item == 1:
                raise ValueError("first")
            return item

        with self.assertRaises(ValueError):
            run_ordered(work, [0, 1, 2], max_workers=3)

    def test_on_result_sees_every_item_once(self) -> None:
        seen: list[tuple[int, int]] = []
        run_ordered(
            lambda item: item,
            [1, 2, 3],
            max_workers=3,
            on_result=lambda index, value: seen.append((index, value)),
        )
        self.assertEqual(sorted(seen), [(0, 1), (1, 2), (2, 3)])

    def test_empty_input_is_a_no_op(self) -> None:
        self.assertEqual(run_ordered(lambda item: item, [], max_workers=4), [])


class _SequenceModel:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)

    def complete(self, messages, *, tools=(), json_mode=False):
        del messages, tools
        return ModelResponse(self.responses.pop(0))


def _create_repo(target: Path) -> None:
    shutil.copytree(FIXTURE, target, dirs_exist_ok=True)
    subprocess.run(["git", "init", "-q", str(target)], check=True, capture_output=True)


def _plan(root: Path, max_parallel_reads: int) -> RunState:
    runtime = build_runtime(root, model_client=_SequenceModel(_EXPLORER, _PLANNER))
    runtime.config = replace(
        runtime.config,
        workspace=replace(
            runtime.config.workspace, max_parallel_reads=max_parallel_reads
        ),
    )
    return runtime.plan_task("修复解析器空输入异常")


class PlanningParallelismTests(unittest.TestCase):
    def test_parallel_exploration_matches_sequential_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            _create_repo(root)
            sequential = _plan(root, 1)
            parallel = _plan(root, 4)

        self.assertEqual(sequential.phase, RunPhase.WAIT_APPROVAL)
        self.assertEqual(parallel.phase, RunPhase.WAIT_APPROVAL)
        self.assertEqual(sequential.search_queries, parallel.search_queries)
        self.assertEqual(sequential.inspected_files, parallel.inspected_files)
        self.assertEqual(
            [asdict(item) for item in sequential.evidence],
            [asdict(item) for item in parallel.evidence],
        )


def _action(name: str, *argv: str) -> VerificationAction:
    return VerificationAction(name=name, description=name, project="test", argv=tuple(argv))


def _applied_record(root: Path) -> PatchRunRecord:
    state = RunState(task=TaskRequest(goal="验证动作", repository=str(root)))
    state.phase = RunPhase.VERIFY
    state.approval = ApprovalDecision.APPROVED
    return PatchRunRecord(
        run_id="e" * 32,
        repository=str(root),
        goal="验证动作",
        state=state.to_dict(),
        proposal=PatchProposal(unified_diff="", touched_files=(), rationale="测试"),
        baselines=(),
        status=PatchStatus.APPLIED,
    )


def _workflow(root: Path, runner, *, parallel: int = 2):
    store = JsonPatchRunStore(root.parent / "runs")
    record = _applied_record(root)
    store.save(record)
    workflow = VerificationWorkflow(
        runner=runner,
        whitelist=VerificationWhitelist((_action("pytest", "pytest"), _action("ruff", "ruff"))),
        policy=PermissionPolicy(PermissionSettings(verify="allow")),
        store=store,
        workflow=WorkflowEngine(),
        max_parallel_verifications=parallel,
    )
    return store, record, workflow


class VerificationParallelismTests(unittest.TestCase):
    def _root(self, temp: str) -> Path:
        root = Path(temp) / "repo"
        root.mkdir()
        return root

    def test_actions_overlap_but_history_keeps_selected_order(self) -> None:
        barrier = threading.Barrier(2, timeout=5)
        broken: list[str] = []

        class _Runner:
            def run(self, action):
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    broken.append(action.name)
                return VerificationResult(
                    command=action.argv, action=action.name, exit_code=0
                )

        with tempfile.TemporaryDirectory() as temp:
            store, record, workflow = _workflow(self._root(temp), _Runner(), parallel=2)
            outcome = workflow.verify(record.run_id)
            history = store.load(record.run_id).state["verification"]

        self.assertEqual(broken, [])
        self.assertTrue(outcome.passed)
        self.assertEqual([item.action for item in outcome.results], ["pytest", "ruff"])
        self.assertEqual([item["action"] for item in history], ["pytest", "ruff"])

    def test_slow_first_action_still_records_in_selected_order(self) -> None:
        class _Runner:
            def run(self, action):
                time.sleep(0.08 if action.name == "pytest" else 0.01)
                return VerificationResult(
                    command=action.argv, action=action.name, exit_code=0
                )

        with tempfile.TemporaryDirectory() as temp:
            store, record, workflow = _workflow(self._root(temp), _Runner(), parallel=2)
            workflow.verify(record.run_id)
            history = store.load(record.run_id).state["verification"]

        self.assertEqual([item["action"] for item in history], ["pytest", "ruff"])

    def test_failure_in_one_action_does_not_stop_the_other(self) -> None:
        class _Runner:
            def run(self, action):
                failed = action.name == "ruff"
                return VerificationResult(
                    command=action.argv,
                    action=action.name,
                    exit_code=1 if failed else 0,
                    stderr="lint error" if failed else "",
                )

        with tempfile.TemporaryDirectory() as temp:
            store, record, workflow = _workflow(self._root(temp), _Runner(), parallel=2)
            outcome = workflow.verify(record.run_id)
            state = store.load(record.run_id).state

        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.phase, RunPhase.REPAIR)
        self.assertEqual(len(state["verification"]), 2)
        self.assertEqual([item["action"] for item in state["verification"]], ["pytest", "ruff"])

    def test_unrunnable_action_becomes_a_failed_result(self) -> None:
        class _Runner:
            def run(self, action):
                raise VerificationError("无法执行验证命令")

        with tempfile.TemporaryDirectory() as temp:
            store, record, workflow = _workflow(self._root(temp), _Runner(), parallel=2)
            outcome = workflow.verify(record.run_id)
            history = store.load(record.run_id).state["verification"]

        self.assertFalse(outcome.passed)
        self.assertEqual(history[0]["exit_code"], -1)
        self.assertIn("无法执行", history[0]["stderr"])

    def test_single_worker_path_still_works(self) -> None:
        class _Runner:
            def run(self, action):
                return VerificationResult(
                    command=action.argv, action=action.name, exit_code=0
                )

        with tempfile.TemporaryDirectory() as temp:
            _store, record, workflow = _workflow(self._root(temp), _Runner(), parallel=1)
            outcome = workflow.verify(record.run_id)

        self.assertTrue(outcome.passed)
        self.assertEqual([item.action for item in outcome.results], ["pytest", "ruff"])


if __name__ == "__main__":
    unittest.main()
