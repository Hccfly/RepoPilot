"""P3 bounded repair: one Editor retry, restricted to the already approved files."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import WorkspaceSettings
from .contracts import RunPhase, RunState, StopReason
from .llm import ModelClient, ModelMessage
from .patching import PatchRunRecord, UnifiedDiffPatchEngine
from .persistence import PatchRunStore
from .policy import Action, Effect, PermissionPolicy
from .prompts import REPAIR_SYSTEM_PROMPT, repair_prompt
from .tools.base import ToolCall, ToolContext, ToolRegistry
from .workflow import WorkflowEngine


class RepairError(RuntimeError):
    pass


@dataclass(slots=True)
class RepairWorkflow:
    model: ModelClient
    tools: ToolRegistry
    tool_context: ToolContext
    settings: WorkspaceSettings
    patch_engine: UnifiedDiffPatchEngine
    policy: PermissionPolicy
    store: PatchRunStore
    workflow: WorkflowEngine
    max_attempts: int = 1

    def repair(self, run_id: str, *, approved: bool = True) -> PatchRunRecord:
        record = self.store.load(run_id)
        state = RunState.from_dict(record.state)
        if state.phase is not RunPhase.REPAIR:
            raise RepairError(f"只有 REPAIR 阶段的任务才能修复，当前为 {state.phase.value}")
        if state.repair_attempts >= self.max_attempts:
            self._exhaust(state, record)
            raise RepairError("修复重试预算已用尽")

        self._authorize(record.proposal.touched_files, approved)
        contexts = self._read_context(record.proposal.touched_files)
        failures = [
            {
                "action": item.action,
                "exit_code": item.exit_code,
                "timed_out": item.timed_out,
                "output": (item.stderr or item.stdout)[-2000:],
            }
            for item in state.verification
            if not item.passed
        ]
        if not failures:
            raise RepairError("没有失败的验证结果可供修复参考")

        try:
            response = self.model.complete(
                [
                    ModelMessage("system", REPAIR_SYSTEM_PROMPT),
                    ModelMessage(
                        "user",
                        repair_prompt(state.task.goal, record.goal, failures, contexts),
                    ),
                ],
                json_mode=True,
            )
        except Exception as exc:
            raise RepairError(f"Repair 模型调用失败: {type(exc).__name__}: {exc}") from exc

        payload = _parse_repair_response(response.text)
        proposal, baselines = self.patch_engine.prepare_repair(
            payload["unified_diff"],
            payload["rationale"],
            allowed_files=record.proposal.touched_files,
        )
        self.patch_engine.apply_repair(record, proposal, baselines)
        state.repair_attempts += 1
        self.workflow.transition(state, RunPhase.VERIFY)
        state.error = None
        record.state = state.to_dict()
        self.store.save(record)
        return record

    def _authorize(self, touched: tuple[str, ...], approved: bool) -> None:
        decision = self.policy.decide(Action.EDIT, ",".join(touched))
        if decision.effect is Effect.DENY:
            raise RepairError(decision.reason)
        if decision.effect is Effect.ASK and not approved:
            raise RepairError("修复补丁尚未获得明确审批")

    def _read_context(self, touched: tuple[str, ...]) -> list[dict[str, str]]:
        contexts: list[dict[str, str]] = []
        for path in touched:
            result = self.tools.execute(
                self.tool_context,
                ToolCall("read_file", {"path": path, "max_chars": self.settings.max_context_chars}),
            )
            contexts.append({"path": result.metadata["path"], "content": result.output})
        return contexts

    def _exhaust(self, state: RunState, record: PatchRunRecord) -> None:
        state.stop_reason = StopReason.REPAIR_BUDGET_EXHAUSTED
        self.workflow.transition(state, RunPhase.FAILED)
        record.state = state.to_dict()
        self.store.save(record)


def _parse_repair_response(text: str) -> dict[str, str]:
    stripped = text.strip()
    if not stripped:
        raise RepairError("Repair 返回空内容")
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = "\n".join(stripped.splitlines()[1:-1]).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise RepairError(f"Repair 返回的不是合法 JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise RepairError("Repair 必须返回 JSON 对象")
    unified_diff = payload.get("unified_diff")
    rationale = payload.get("rationale")
    if not isinstance(unified_diff, str) or not unified_diff.strip():
        raise RepairError("Repair.unified_diff 必须是非空字符串")
    if not isinstance(rationale, str) or not rationale.strip():
        raise RepairError("Repair.rationale 必须是非空字符串")
    return {"unified_diff": unified_diff, "rationale": rationale}
