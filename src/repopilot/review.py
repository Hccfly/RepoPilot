"""P3 read-only review node. Its context is deliberately separate from the Editor's."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .contracts import ReviewDecision, ReviewFinding, ReviewReport, RunPhase, RunState, StopReason
from .llm import ModelClient, ModelMessage
from .patching import PatchRunRecord
from .persistence import PatchRunStore
from .prompts import REVIEWER_SYSTEM_PROMPT, reviewer_prompt
from .repo_map import RepoMap
from .workflow import WorkflowEngine


class ReviewError(RuntimeError):
    pass


_SEVERITIES = frozenset({"high", "medium", "low"})


@dataclass(slots=True)
class ReviewWorkflow:
    model: ModelClient
    store: PatchRunStore
    workflow: WorkflowEngine
    repo_map: RepoMap | None = None
    max_repair_attempts: int = 1

    def review(self, run_id: str) -> ReviewReport:
        record = self.store.load(run_id)
        state = RunState.from_dict(record.state)
        if state.phase is not RunPhase.REVIEW:
            raise ReviewError(f"只有 REVIEW 阶段的任务才能审查，当前为 {state.phase.value}")

        report = self._ask(record, state)
        state.review = report
        if report.blocks_completion:
            state.stop_reason = (
                StopReason.REVIEW_REJECTED
                if report.decision is ReviewDecision.REJECT
                else StopReason.REVIEW_REQUESTED_CHANGES
            )
            if state.repair_attempts >= self.max_repair_attempts:
                self.workflow.transition(state, RunPhase.FAILED)
                state.stop_reason = StopReason.REPAIR_BUDGET_EXHAUSTED
            else:
                self.workflow.transition(state, RunPhase.REPAIR)
        else:
            self.workflow.transition(state, RunPhase.DONE)
            state.stop_reason = StopReason.VERIFIED
            state.error = None
        self._persist(record, state)
        return report

    def _ask(self, record: PatchRunRecord, state: RunState) -> ReviewReport:
        diffs = "\n".join(item.unified_diff for item in record.all_diffs)
        evidence = [
            {"kind": item.kind, "source": item.source, "line": item.line, "summary": item.summary}
            for item in state.evidence
        ]
        verification = [item.to_dict() for item in state.verification]
        outline = ""
        if self.repo_map is not None:
            outline = self.repo_map.outline(record.proposal.touched_files)
        try:
            response = self.model.complete(
                [
                    ModelMessage("system", REVIEWER_SYSTEM_PROMPT),
                    ModelMessage(
                        "user",
                        reviewer_prompt(
                            state.task.goal,
                            record.state.get("plan") or {},
                            diffs,
                            evidence,
                            verification,
                            outline,
                        ),
                    ),
                ],
                json_mode=True,
            )
        except Exception as exc:
            raise ReviewError(f"Reviewer 模型调用失败: {type(exc).__name__}: {exc}") from exc
        return _parse_review(response.text, set(record.proposal.touched_files))

    def _persist(self, record: PatchRunRecord, state: RunState) -> None:
        record.state = state.to_dict()
        self.store.save(record)


def _parse_review(text: str, allowed_files: set[str]) -> ReviewReport:
    payload = _parse_json_object(text)
    raw_decision = payload.get("decision")
    try:
        decision = ReviewDecision(str(raw_decision))
    except ValueError as exc:
        raise ReviewError(f"Reviewer.decision 非法: {raw_decision!r}") from exc
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ReviewError("Reviewer.summary 必须是非空字符串")

    raw_findings = payload.get("findings", [])
    if not isinstance(raw_findings, list):
        raise ReviewError("Reviewer.findings 必须是数组")
    findings: list[ReviewFinding] = []
    for index, item in enumerate(raw_findings, start=1):
        if not isinstance(item, dict):
            raise ReviewError(f"Reviewer.findings[{index}] 必须是对象")
        message = item.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ReviewError(f"Reviewer.findings[{index}].message 必须是非空字符串")
        severity = str(item.get("severity", "medium")).lower()
        if severity not in _SEVERITIES:
            raise ReviewError(f"Reviewer.findings[{index}].severity 非法: {severity!r}")
        path = str(item.get("file", "")).replace("\\", "/")
        if path and path not in allowed_files:
            raise ReviewError(f"Reviewer 引用了补丁之外的文件: {path}")
        raw_line = item.get("line")
        line = int(raw_line) if isinstance(raw_line, int) and raw_line > 0 else None
        findings.append(ReviewFinding(severity, message.strip(), path, line))
    return ReviewReport(decision, summary.strip(), tuple(findings))


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ReviewError("Reviewer 返回空内容")
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = "\n".join(stripped.splitlines()[1:-1]).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"Reviewer 返回的不是合法 JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ReviewError("Reviewer 必须返回 JSON 对象")
    return payload
