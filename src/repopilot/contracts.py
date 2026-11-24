"""Domain contracts shared by the CLI, workflow, tools, and persistence layers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class RunPhase(StrEnum):
    CREATED = "created"
    DISCOVER = "discover"
    PLAN = "plan"
    WAIT_APPROVAL = "wait_approval"
    PATCH = "patch"
    VERIFY = "verify"
    REPAIR = "repair"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"


class StopReason(StrEnum):
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    REPAIR_BUDGET_EXHAUSTED = "repair_budget_exhausted"
    REVIEW_REQUESTED_CHANGES = "review_requested_changes"
    REVIEW_REJECTED = "review_rejected"
    MODEL_ERROR = "model_error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TaskRequest:
    goal: str
    repository: str

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("goal must not be empty")


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    source: str
    summary: str
    line: int | None = None


@dataclass(frozen=True, slots=True)
class PlanStep:
    description: str
    files: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChangePlan:
    summary: str
    steps: tuple[PlanStep, ...]
    risks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    action: str = ""
    truncated: bool = False
    duration_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> VerificationResult:
        return cls(
            command=tuple(str(item) for item in value.get("command", ())),
            exit_code=int(value.get("exit_code", 0)),
            stdout=str(value.get("stdout", "")),
            stderr=str(value.get("stderr", "")),
            timed_out=bool(value.get("timed_out", False)),
            action=str(value.get("action", "")),
            truncated=bool(value.get("truncated", False)),
            duration_seconds=float(value.get("duration_seconds", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    severity: str
    message: str
    file: str = ""
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReviewFinding:
        return cls(
            severity=str(value.get("severity", "medium")),
            message=str(value.get("message", "")),
            file=str(value.get("file", "")),
            line=int(value["line"]) if value.get("line") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class ReviewReport:
    decision: ReviewDecision
    summary: str
    findings: tuple[ReviewFinding, ...] = ()

    @property
    def blocks_completion(self) -> bool:
        return self.decision is not ReviewDecision.APPROVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "summary": self.summary,
            "findings": [item.to_dict() for item in self.findings],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReviewReport:
        return cls(
            decision=ReviewDecision(value.get("decision", ReviewDecision.APPROVE)),
            summary=str(value.get("summary", "")),
            findings=tuple(ReviewFinding.from_dict(item) for item in value.get("findings", ())),
        )


@dataclass(slots=True)
class RunState:
    task: TaskRequest
    run_id: str = field(default_factory=lambda: uuid4().hex)
    phase: RunPhase = RunPhase.CREATED
    approval: ApprovalDecision = ApprovalDecision.PENDING
    inventory: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    inspected_files: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    plan: ChangePlan | None = None
    changed_files: list[str] = field(default_factory=list)
    verification: list[VerificationResult] = field(default_factory=list)
    repair_attempts: int = 0
    review: ReviewReport | None = None
    stop_reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a checkpoint-safe JSON-compatible representation."""
        return _json_safe(asdict(self))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunState:
        """Rebuild a run state from a checkpoint without trusting field types."""
        task = value["task"]
        raw_plan = value.get("plan")
        plan = (
            ChangePlan(
                summary=str(raw_plan["summary"]),
                steps=tuple(
                    PlanStep(
                        description=str(step["description"]),
                        files=tuple(str(item) for item in step.get("files", ())),
                        verification=tuple(str(item) for item in step.get("verification", ())),
                    )
                    for step in raw_plan.get("steps", ())
                ),
                risks=tuple(str(item) for item in raw_plan.get("risks", ())),
            )
            if raw_plan
            else None
        )
        return cls(
            task=TaskRequest(goal=str(task["goal"]), repository=str(task["repository"])),
            run_id=str(value["run_id"]),
            phase=RunPhase(value.get("phase", RunPhase.CREATED)),
            approval=ApprovalDecision(value.get("approval", ApprovalDecision.PENDING)),
            inventory=[str(item) for item in value.get("inventory", ())],
            search_queries=[str(item) for item in value.get("search_queries", ())],
            inspected_files=[str(item) for item in value.get("inspected_files", ())],
            evidence=[
                Evidence(
                    kind=str(item["kind"]),
                    source=str(item["source"]),
                    summary=str(item["summary"]),
                    line=item.get("line"),
                )
                for item in value.get("evidence", ())
            ],
            plan=plan,
            changed_files=[str(item) for item in value.get("changed_files", ())],
            verification=[
                VerificationResult.from_dict(item)
                for item in value.get("verification", ())
            ],
            repair_attempts=int(value.get("repair_attempts", 0)),
            review=ReviewReport.from_dict(value["review"]) if value.get("review") else None,
            stop_reason=value.get("stop_reason"),
            error=value.get("error"),
        )


def _json_safe(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
