"""P3 machine-readable run report. Every conclusion cites a diff or a verification result."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import ReviewReport, RunPhase, RunState, VerificationResult
from .patching import PatchProposal, PatchRunRecord


@dataclass(frozen=True, slots=True)
class FileDiffSummary:
    path: str
    added: int
    removed: int

    def render(self) -> str:
        return f"{self.path} (+{self.added} -{self.removed})"


@dataclass(frozen=True, slots=True)
class RunReport:
    run_id: str
    repository: str
    goal: str
    phase: RunPhase
    stop_reason: str
    plan_summary: str
    changed_files: tuple[str, ...]
    files: tuple[FileDiffSummary, ...]
    verification: tuple[VerificationResult, ...]
    repair_attempts: int
    review: ReviewReport | None
    evidence_count: int
    commit: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repository": self.repository,
            "goal": self.goal,
            "phase": self.phase.value,
            "stop_reason": self.stop_reason,
            "plan": self.plan_summary,
            "changed_files": list(self.changed_files),
            "files": [
                {"path": item.path, "added": item.added, "removed": item.removed}
                for item in self.files
            ],
            "verification": [item.to_dict() for item in self.verification],
            "repair_attempts": self.repair_attempts,
            "review": self.review.to_dict() if self.review else None,
            "evidence_count": self.evidence_count,
            "commit": self.commit,
            "conclusions": list(self.conclusions()),
        }

    def conclusions(self) -> tuple[str, ...]:
        """Statements that are each traceable to a diff summary or a verification row."""
        items: list[str] = []
        for summary in self.files:
            items.append(f"变更 {summary.render()}")
        for result in self.verification:
            status = "通过" if result.passed else ("超时" if result.timed_out else "失败")
            name = result.action or " ".join(result.command)
            items.append(f"验证 {name} {status}（exit={result.exit_code}）")
        if self.review is not None:
            items.append(f"审查结论 {self.review.decision.value}: {self.review.summary}")
            for finding in self.review.findings:
                location = f"{finding.file}:{finding.line}" if finding.line else finding.file
                items.append(f"审查发现 [{finding.severity}] {location} {finding.message}")
        if self.commit is not None:
            items.append(f"提交 {self.commit.get('sha', '')}")
        return tuple(items)


def build_report(record: PatchRunRecord) -> RunReport:
    state = RunState.from_dict(record.state)
    plan_summary = state.plan.summary if state.plan else "未生成计划"
    return RunReport(
        run_id=record.run_id,
        repository=record.repository,
        goal=record.goal,
        phase=state.phase,
        stop_reason=state.stop_reason or _default_stop_reason(state.phase),
        plan_summary=plan_summary,
        changed_files=record.proposal.touched_files,
        files=summarize_diffs(record.all_diffs),
        verification=tuple(state.verification),
        repair_attempts=state.repair_attempts,
        review=state.review,
        evidence_count=len(state.evidence),
        commit=record.commit,
    )


def summarize_diffs(proposals: tuple[PatchProposal, ...]) -> tuple[FileDiffSummary, ...]:
    counts: dict[str, list[int]] = {}
    for proposal in proposals:
        current: str | None = None
        expects_header = False
        for line in proposal.unified_diff.splitlines():
            if line.startswith("--- "):
                expects_header = True
                continue
            if expects_header and line.startswith("+++ "):
                current = _header_path(line[4:])
                counts.setdefault(current, [0, 0])
                expects_header = False
                continue
            expects_header = False
            if current is None:
                continue
            if line.startswith("+"):
                counts[current][0] += 1
            elif line.startswith("-"):
                counts[current][1] += 1
    return tuple(
        FileDiffSummary(path, added, removed) for path, (added, removed) in counts.items()
    )


def render_markdown(report: RunReport) -> str:
    lines = [
        "# RepoPilot 运行报告",
        "",
        f"- 运行: `{report.run_id}`",
        f"- 仓库: `{report.repository}`",
        f"- 阶段: `{report.phase.value}`",
        f"- 停止原因: `{report.stop_reason}`",
        f"- 目标: {report.goal}",
        "",
        "## 计划",
        "",
        report.plan_summary,
        "",
        "## 变更",
        "",
        "| 文件 | 新增 | 删除 |",
        "| --- | --- | --- |",
    ]
    for summary in report.files:
        lines.append(f"| `{summary.path}` | {summary.added} | {summary.removed} |")
    if not report.files:
        lines.append("| 无 | 0 | 0 |")

    lines.extend(
        ["", "## 验证", "", "| 动作 | 结果 | 退出码 | 耗时(s) |", "| --- | --- | --- | --- |"]
    )
    for result in report.verification:
        status = "通过" if result.passed else ("超时" if result.timed_out else "失败")
        lines.append(
            f"| `{result.action or ' '.join(result.command)}` | {status} | "
            f"{result.exit_code} | {result.duration_seconds:.2f} |"
        )
    if not report.verification:
        lines.append("| 未执行 | - | - | - |")

    lines.extend(["", "## 审查", ""])
    if report.review is None:
        lines.append("未执行审查。")
    else:
        lines.append(f"- 结论: `{report.review.decision.value}`")
        lines.append(f"- 摘要: {report.review.summary}")
        for finding in report.review.findings:
            location = f"{finding.file}:{finding.line}" if finding.line else finding.file
            lines.append(f"- [{finding.severity}] `{location}` {finding.message}")

    if report.commit is not None:
        lines.extend(
            [
                "",
                "## 提交",
                "",
                f"- SHA: `{report.commit.get('sha', '')}`",
                f"- 文件: {', '.join(report.commit.get('files', []))}",
            ]
        )

    lines.extend(["", "## 结论", ""])
    lines.extend(f"- {item}" for item in report.conclusions())
    return "\n".join(lines) + "\n"


def render_verification_history(results: Sequence[VerificationResult]) -> tuple[str, ...]:
    lines: list[str] = []
    for result in results:
        label = "通过" if result.passed else ("超时" if result.timed_out else "失败")
        name = result.action or " ".join(result.command)
        lines.append(
            f"[{label}] {name} exit={result.exit_code} {result.duration_seconds:.2f}s"
        )
        if result.truncated:
            lines.append("输出已截断")
        if not result.passed:
            detail = (result.stderr or result.stdout).strip().splitlines()
            lines.extend(detail[-5:])
    return tuple(lines)


def _header_path(value: str) -> str:
    path = value.split("\t", 1)[0].strip().replace("\\", "/")
    return path[2:] if path.startswith(("a/", "b/")) else path


def _default_stop_reason(phase: RunPhase) -> str:
    if phase is RunPhase.DONE:
        return "verified"
    if phase is RunPhase.CANCELLED:
        return "cancelled"
    return "in_progress"
