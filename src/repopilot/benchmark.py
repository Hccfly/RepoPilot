"""P3 deterministic benchmark harness.

Every scenario runs offline: the model is a scripted replayer and verification is
a stub runner, so results are reproducible without an API key or network access.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import RunPhase, RunState, VerificationResult
from .grader import GradeSpec, Score, score_facts
from .llm import ModelMessage, ModelResponse
from .patching import PatchError, PatchRunRecord
from .persistence import JsonPatchRunStore
from .runtime import RepoPilotRuntime, build_runtime
from .verification import VerificationAction, VerificationWhitelist, VerificationWorkflow


class ScriptedModel:
    """Deterministic ModelClient: replays canned JSON, asserts every call wants JSON."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls = 0

    def complete(
        self,
        messages: Sequence[ModelMessage],
        *,
        tools: Sequence[dict[str, Any]] = (),
        json_mode: bool = False,
    ) -> ModelResponse:
        del messages, tools
        if not json_mode:
            raise AssertionError("RepoPilot expects JSON mode from every model call")
        if not self.responses:
            raise AssertionError("scripted model ran out of responses")
        self.calls += 1
        return ModelResponse(self.responses.pop(0))


class ScriptedRunner:
    """Replays a fixed sequence of verification outcomes."""

    def __init__(self, outcomes: Sequence[bool]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def run(self, action: VerificationAction) -> VerificationResult:
        if not self.outcomes:
            raise AssertionError("scripted runner ran out of outcomes")
        self.calls += 1
        passed = self.outcomes.pop(0)
        return VerificationResult(
            command=action.argv,
            action=action.name,
            exit_code=0 if passed else 1,
            stdout="" if passed else "AssertionError: expected behavior is missing",
        )


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    goal: str
    files: dict[str, str]
    responses: tuple[str, ...]
    verify_outcomes: tuple[bool, ...]
    expected_files: tuple[str, ...]
    expected_phase: RunPhase
    expected_stop_reason: str
    max_repairs: int = 1
    dirty_files: tuple[str, ...] = ()
    expect_error: bool = False
    expected_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "goal": self.goal,
            "files": dict(self.files),
            "responses": list(self.responses),
            "verify_outcomes": list(self.verify_outcomes),
            "expected_files": list(self.expected_files),
            "expected_phase": self.expected_phase.value,
            "expected_stop_reason": self.expected_stop_reason,
            "max_repairs": self.max_repairs,
            "dirty_files": list(self.dirty_files),
            "expect_error": self.expect_error,
            "expected_error": self.expected_error,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Scenario:
        return cls(
            name=str(value["name"]),
            description=str(value.get("description", "")),
            goal=str(value["goal"]),
            files={str(key): str(item) for key, item in value.get("files", {}).items()},
            responses=tuple(str(item) for item in value.get("responses", ())),
            verify_outcomes=tuple(bool(item) for item in value.get("verify_outcomes", ())),
            expected_files=tuple(str(item) for item in value.get("expected_files", ())),
            expected_phase=RunPhase(value.get("expected_phase", RunPhase.DONE)),
            expected_stop_reason=str(value.get("expected_stop_reason", "")),
            max_repairs=int(value.get("max_repairs", 1)),
            dirty_files=tuple(str(item) for item in value.get("dirty_files", ())),
            expect_error=bool(value.get("expect_error", False)),
            expected_error=str(value.get("expected_error", "")),
        )

    def grade_spec(self) -> GradeSpec:
        return GradeSpec(
            name=self.name,
            expected_files=self.expected_files,
            expected_phase=self.expected_phase,
            expected_stop_reason=self.expected_stop_reason,
            expect_error=self.expect_error,
            expected_error=self.expected_error,
        )


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario: str
    score: Score
    phase: str
    stop_reason: str
    error: str | None = None
    touched: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.score.passed


def score_run(scenario: Scenario, record: PatchRunRecord) -> Score:
    state = RunState.from_dict(record.state)
    return score_facts(
        scenario.grade_spec(),
        touched=record.proposal.touched_files,
        phase=state.phase,
        stop_reason=state.stop_reason or "",
    )


def run_scenario_facts(scenario: Scenario) -> dict[str, Any]:
    """Run one scenario and report raw facts. This function never scores."""
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "repo"
        _create_repo(root, scenario.files, scenario.dirty_files)
        store = JsonPatchRunStore(Path(temp) / "runs")
        model = ScriptedModel(*scenario.responses)
        runtime = build_runtime(root, model_client=model)
        try:
            record = runtime.propose_task(scenario.goal, store)
            runtime.patch_lifecycle(store).apply(record.run_id, approved=True)
            _drive(runtime, store, scenario, record.run_id)
            final = store.load(record.run_id)
        except PatchError as exc:
            return {
                "scenario": scenario.name,
                "phase": "",
                "stop_reason": "",
                "error": str(exc),
                "touched": [],
            }
        state = RunState.from_dict(final.state)
        return {
            "scenario": scenario.name,
            "phase": state.phase.value,
            "stop_reason": state.stop_reason or "",
            "error": None,
            "touched": list(final.proposal.touched_files),
        }


def run_scenario(scenario: Scenario) -> ScenarioResult:
    facts = run_scenario_facts(scenario)
    raw_phase = facts["phase"]
    phase = RunPhase(raw_phase) if raw_phase else None
    score = score_facts(
        scenario.grade_spec(),
        touched=facts["touched"],
        phase=phase,
        stop_reason=str(facts["stop_reason"]),
        error=facts["error"],
    )
    return ScenarioResult(
        scenario.name,
        score,
        str(raw_phase),
        str(facts["stop_reason"]),
        facts["error"],
        tuple(facts["touched"]),
    )


def _drive(
    runtime: RepoPilotRuntime, store: JsonPatchRunStore, scenario: Scenario, run_id: str
) -> None:
    runner = ScriptedRunner(scenario.verify_outcomes)
    whitelist = VerificationWhitelist(
        (VerificationAction("bench", "基准验证", "test", ("bench",)),)
    )
    verifier = VerificationWorkflow(
        runner=runner,
        whitelist=whitelist,
        policy=runtime.policy,
        store=store,
        workflow=runtime.workflow,
        max_repair_attempts=runtime.config.execution.max_repair_attempts,
    )
    outcome = verifier.verify(run_id)
    repairs = 0
    while (
        not outcome.passed
        and outcome.phase is RunPhase.REPAIR
        and repairs < scenario.max_repairs
    ):
        runtime.repair_task(run_id, store)
        repairs += 1
        outcome = verifier.verify(run_id)
    if outcome.phase is RunPhase.REVIEW:
        runtime.review_task(run_id, store)


def run_benchmarks(scenarios: Sequence[Scenario] | None = None) -> tuple[ScenarioResult, ...]:
    return tuple(run_scenario(item) for item in (scenarios or BUILTIN_SCENARIOS))


def _create_repo(root: Path, files: dict[str, str], dirty: Sequence[str] = ()) -> None:
    root.mkdir(parents=True)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=RepoPilot Bench",
        "-c",
        "user.email=bench@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture",
    )
    for name in dirty:
        target = root / name
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8"
        )


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


_APP = 'def value():\n    return "old"\n'
_OTHER = "x = 1\n"
_EXPLORER = '{"queries":["value"],"files":["app.py"],"reason":"定位返回值"}'
_PLANNER = (
    '{"summary":"更新返回值","steps":[{"description":"修改 value",'
    '"files":["app.py"],"verification":[]}],"risks":[]}'
)
_EDITOR = (
    '{"unified_diff":"--- a/app.py\\n+++ b/app.py\\n@@ -1,2 +1,2 @@\\n '
    'def value():\\n-    return \\"old\\"\\n+    return \\"new\\"\\n","rationale":"按计划更新"}'
)
_EDITOR_UNREAD = (
    '{"unified_diff":"--- a/other.py\\n+++ b/other.py\\n@@ -1,1 +1,2 @@\\n '
    'x = 1\\n+y = 2\\n","rationale":"顺手改未读取文件"}'
)
_REPAIR = (
    '{"unified_diff":"--- a/app.py\\n+++ b/app.py\\n@@ -1,2 +1,2 @@\\n '
    'def value():\\n-    return \\"new\\"\\n+    return \\"fixed\\"\\n","rationale":"修复返回值"}'
)
_REPAIR_OUT_OF_SCOPE = (
    '{"unified_diff":"--- a/other.py\\n+++ b/other.py\\n@@ -1,1 +1,2 @@\\n '
    'x = 1\\n+y = 2\\n","rationale":"越界修复"}'
)
_APPROVE = '{"decision":"approve","summary":"diff 与目标一致","findings":[]}'
_REQUEST_CHANGES = (
    '{"decision":"request_changes","summary":"缺少边界处理",'
    '"findings":[{"severity":"high","file":"app.py","line":2,"message":"未处理空输入"}]}'
)
_REJECT = '{"decision":"reject","summary":"方向完全错误","findings":[]}'

BUILTIN_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="bug_fix",
        description="标准缺陷修复：提案、应用、验证通过、审查通过",
        goal="把 value 返回值改为 new",
        files={"app.py": _APP},
        responses=(_EXPLORER, _PLANNER, _EDITOR, _APPROVE),
        verify_outcomes=(True,),
        expected_files=("app.py",),
        expected_phase=RunPhase.DONE,
        expected_stop_reason="verified",
    ),
    Scenario(
        name="review_request_changes",
        description="审查节点基于 diff 提出 blocking finding",
        goal="把 value 返回值改为 new",
        files={"app.py": _APP},
        responses=(_EXPLORER, _PLANNER, _EDITOR, _REQUEST_CHANGES),
        verify_outcomes=(True,),
        expected_files=("app.py",),
        expected_phase=RunPhase.REPAIR,
        expected_stop_reason="review_requested_changes",
    ),
    Scenario(
        name="repair_recovery",
        description="首次验证失败，修复后被再次验证通过",
        goal="把 value 返回值改为 new",
        files={"app.py": _APP},
        responses=(_EXPLORER, _PLANNER, _EDITOR, _REPAIR, _APPROVE),
        verify_outcomes=(False, True),
        expected_files=("app.py",),
        expected_phase=RunPhase.DONE,
        expected_stop_reason="verified",
    ),
    Scenario(
        name="verification_timeout",
        description="验证超时后停在 REPAIR，不进入 DONE",
        goal="把 value 返回值改为 new",
        files={"app.py": _APP},
        responses=(_EXPLORER, _PLANNER, _EDITOR),
        verify_outcomes=(False,),
        expected_files=("app.py",),
        expected_phase=RunPhase.REPAIR,
        expected_stop_reason="verification_failed",
        max_repairs=0,
    ),
    Scenario(
        name="dirty_worktree_conflict",
        description="目标文件存在用户改动时拒绝生成补丁",
        goal="把 value 返回值改为 new",
        files={"app.py": _APP},
        responses=(_EXPLORER, _PLANNER, _EDITOR),
        verify_outcomes=(),
        expected_files=("app.py",),
        expected_phase=RunPhase.WAIT_APPROVAL,
        expected_stop_reason="",
        dirty_files=("app.py",),
        expect_error=True,
        expected_error="补丁触及任务开始前已有改动的文件",
    ),
    Scenario(
        name="review_reject",
        description="审查直接拒绝时退回 REPAIR，而不是进入 DONE",
        goal="把 value 返回值改为 new",
        files={"app.py": _APP},
        responses=(_EXPLORER, _PLANNER, _EDITOR, _REJECT),
        verify_outcomes=(True,),
        expected_files=("app.py",),
        expected_phase=RunPhase.REPAIR,
        expected_stop_reason="review_rejected",
    ),
    Scenario(
        name="repair_budget_exhausted",
        description="修复后仍然失败时用尽预算并进入 FAILED",
        goal="把 value 返回值改为 new",
        files={"app.py": _APP},
        responses=(_EXPLORER, _PLANNER, _EDITOR, _REPAIR),
        verify_outcomes=(False, False),
        expected_files=("app.py",),
        expected_phase=RunPhase.FAILED,
        expected_stop_reason="repair_budget_exhausted",
    ),
    Scenario(
        name="repair_out_of_scope",
        description="修复补丁越出已批准范围时整体拒绝",
        goal="把 value 返回值改为 new",
        files={"app.py": _APP, "other.py": _OTHER},
        responses=(_EXPLORER, _PLANNER, _EDITOR, _REPAIR_OUT_OF_SCOPE),
        verify_outcomes=(False,),
        expected_files=("app.py",),
        expected_phase=RunPhase.REPAIR,
        expected_stop_reason="",
        expect_error=True,
        expected_error="修复补丁越出已批准范围",
    ),
    Scenario(
        name="editor_unread_file",
        description="编辑节点修改未读取文件时被拒绝",
        goal="把 value 返回值改为 new",
        files={"app.py": _APP, "other.py": _OTHER},
        responses=(_EXPLORER, _PLANNER, _EDITOR_UNREAD),
        verify_outcomes=(),
        expected_files=("app.py",),
        expected_phase=RunPhase.WAIT_APPROVAL,
        expected_stop_reason="",
        expect_error=True,
        expected_error="Editor 尝试修改未读取文件",
    ),
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    results = run_benchmarks()
    print(f"{'SCENARIO':<26}{'SCORE':<8}{'PHASE':<14}{'STOP REASON':<28}REASONS")
    for result in results:
        reasons = "; ".join(result.score.reasons)
        print(f"{result.scenario:<26}{result.score.score:<8}{result.phase:<14}"
              f"{result.stop_reason:<28}{reasons}")
    failures = [item for item in results if not item.ok]
    print(f"\n{len(results) - len(failures)}/{len(results)} 通过")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
