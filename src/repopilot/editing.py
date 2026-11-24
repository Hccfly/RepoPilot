"""P1 model-backed patch proposal and approved lifecycle orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import WorkspaceSettings
from .contracts import RunPhase, RunState
from .llm import ModelClient, ModelMessage
from .patching import (
    CommitError,
    PatchError,
    PatchPolicyError,
    PatchRunRecord,
    PatchStatus,
    UnifiedDiffPatchEngine,
    parse_touched_files,
)
from .persistence import PatchRunStore
from .policy import Action, Effect, PermissionPolicy
from .prompts import EDITOR_SYSTEM_PROMPT, editor_prompt
from .tools.base import ToolCall, ToolContext, ToolRegistry


@dataclass(slots=True)
class PatchProposalWorkflow:
    model: ModelClient
    tools: ToolRegistry
    tool_context: ToolContext
    settings: WorkspaceSettings
    patch_engine: UnifiedDiffPatchEngine
    store: PatchRunStore
    initial_dirty_paths: tuple[str, ...] = ()

    def propose(self, state: RunState) -> PatchRunRecord:
        if state.phase is not RunPhase.WAIT_APPROVAL or state.plan is None:
            raise PatchError("只有完成 P0 计划的任务才能生成补丁")
        contexts: list[dict[str, str]] = []
        for path in state.inspected_files:
            result = self.tools.execute(
                self.tool_context,
                ToolCall(
                    "read_file",
                    {"path": path, "max_chars": self.settings.max_context_chars},
                ),
            )
            contexts.append({"path": path, "content": result.output})

        try:
            response = self.model.complete(
                [
                    ModelMessage("system", EDITOR_SYSTEM_PROMPT),
                    ModelMessage(
                        "user",
                        editor_prompt(state.task.goal, state.to_dict()["plan"], contexts),
                    ),
                ],
                json_mode=True,
            )
        except Exception as exc:
            raise PatchError(f"Editor 模型调用失败: {type(exc).__name__}: {exc}") from exc
        payload = _parse_editor_response(response.text)
        touched = parse_touched_files(payload["unified_diff"])
        inspected = set(state.inspected_files)
        for relative in touched:
            target = self.tool_context.workspace.validate_write(relative)
            if target.exists() and relative not in inspected:
                raise PatchPolicyError(f"Editor 尝试修改未读取文件: {relative}")

        proposal, baselines = self.patch_engine.prepare(
            payload["unified_diff"],
            payload["rationale"],
            protected_dirty_paths=self.initial_dirty_paths,
        )
        record = PatchRunRecord(
            run_id=state.run_id,
            repository=str(self.tool_context.workspace.root),
            goal=state.task.goal,
            state=state.to_dict(),
            proposal=proposal,
            baselines=baselines,
            initial_dirty_paths=self.initial_dirty_paths,
        )
        self.store.save(record)
        return record


@dataclass(slots=True)
class PatchLifecycleService:
    patch_engine: UnifiedDiffPatchEngine
    policy: PermissionPolicy
    store: PatchRunStore

    def load(self, run_id: str) -> PatchRunRecord:
        record = self.store.load(run_id)
        expected = str(self.patch_engine.workspace.root)
        if record.repository != expected:
            raise PatchPolicyError(
                f"运行台账属于其他仓库: expected={expected}, actual={record.repository}"
            )
        return record

    def apply(self, run_id: str, *, approved: bool) -> PatchRunRecord:
        record = self.load(run_id)
        self._authorize(record, approved)
        self.patch_engine.apply(record)
        try:
            self.store.save(record)
        except Exception:
            self.patch_engine.undo(record)
            raise
        return record

    def undo(self, run_id: str, *, approved: bool) -> PatchRunRecord:
        record = self.load(run_id)
        self._authorize(record, approved)
        self.patch_engine.undo(record)
        self.store.save(record)
        return record

    def commit(
        self, run_id: str, *, approved: bool, message: str | None = None
    ) -> PatchRunRecord:
        """Commit only this run's files, and only for a verified, reviewed task."""
        record = self.load(run_id)
        self._authorize_git(approved)
        if record.status == PatchStatus.COMMITTED:
            raise CommitError("该运行已经提交过")
        if record.status != PatchStatus.APPLIED:
            raise CommitError("只有已应用的补丁才能提交")
        state = RunState.from_dict(record.state)
        if state.phase is not RunPhase.DONE:
            raise CommitError(
                f"只有完成验证和审查的任务才能提交，当前阶段 {state.phase.value}"
            )
        self.patch_engine.verify_applied_state(record)
        paths = record.proposal.touched_files
        text = (message or "").strip() or _commit_message(record, state)
        self.patch_engine.git.stage_paths(paths)
        sha = self.patch_engine.git.commit_paths(paths, text)
        record.commit = {
            "sha": sha,
            "message": text,
            "files": list(paths),
            "committed_at": datetime.now(UTC).isoformat(),
        }
        record.status = PatchStatus.COMMITTED
        self.store.save(record)
        return record

    def _authorize_git(self, approved: bool) -> None:
        decision = self.policy.decide(Action.GIT_WRITE, "git commit")
        if decision.effect is Effect.DENY:
            raise PatchPolicyError(
                f"{decision.reason}；如需启用请设置 [permissions] git_write = \"ask\""
            )
        if decision.effect is Effect.ASK and not approved:
            raise PatchPolicyError("该 Git 写操作尚未获得明确审批")

    def _authorize(self, record: PatchRunRecord, approved: bool) -> None:
        resource = ",".join(record.proposal.touched_files)
        decision = self.policy.decide(Action.EDIT, resource)
        if decision.effect is Effect.DENY:
            raise PatchPolicyError(decision.reason)
        if decision.effect is Effect.ASK and not approved:
            raise PatchPolicyError("该编辑操作尚未获得明确审批")


def _commit_message(record: PatchRunRecord, state: RunState) -> str:
    verified = [
        item.action or " ".join(item.command) for item in state.verification if item.passed
    ]
    lines = [
        f"repopilot: {record.goal}",
        "",
        f"Run: {record.run_id}",
        f"Files: {', '.join(record.proposal.touched_files)}",
    ]
    if verified:
        lines.append(f"Verified: {', '.join(dict.fromkeys(verified))}")
    return "\n".join(lines)


def _parse_editor_response(text: str) -> dict[str, str]:
    stripped = text.strip()
    if not stripped:
        raise PatchError("Editor 返回空内容")
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise PatchError(f"Editor 返回的不是合法 JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise PatchError("Editor 必须返回 JSON 对象")
    unified_diff = payload.get("unified_diff")
    rationale = payload.get("rationale")
    if not isinstance(unified_diff, str) or not unified_diff.strip():
        raise PatchError("Editor.unified_diff 必须是非空字符串")
    if not isinstance(rationale, str) or not rationale.strip():
        raise PatchError("Editor.rationale 必须是非空字符串")
    return {"unified_diff": unified_diff, "rationale": rationale}
