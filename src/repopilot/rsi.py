"""Human-gated recursive self-improvement: evaluate a candidate patch of RepoPilot itself.

The candidate runs in a subprocess with its own ``src`` on ``PYTHONPATH`` and only
reports raw run facts. Grading uses this module's pinned scorer, so a candidate
cannot raise its score by editing the benchmark's scoring code.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .benchmark import BUILTIN_SCENARIOS, Scenario, Score
from .checks import BUILTIN_CHECKS, Check
from .contracts import RunPhase
from .git import GitClient, GitError
from .grader import score_check, score_facts
from .patching import PatchError, parse_touched_files
from .persistence import PatchRunStore, default_run_store_root
from .rsi_history import (
    STATUS_DUPLICATE,
    STATUS_ERROR,
    STATUS_IMPROVED,
    STATUS_REJECTED,
    RoundRecord,
    RsiHistory,
)
from .runtime import build_runtime

EVALUATION_TIMEOUT_SECONDS = 1800
_BLOCKED_PREFIXES = ("tests/", "docs/", ".github/")
_BLOCKED_FILES = ("pyproject.toml", "README.md", "AGENTS.md")
_SCOPED_PREFIX = "src/repopilot/"
_BLOCKED_SOURCE = (
    "src/repopilot/benchmark.py",
    "src/repopilot/grader.py",
    "src/repopilot/checks.py",
)

_RUNNER = r"""
import json
import sys
from pathlib import Path

from repopilot.benchmark import Scenario, run_scenario_facts
from repopilot.checks import Check, run_check
from repopilot.grader import fingerprint

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
scenarios = [
    run_scenario_facts(Scenario.from_dict(item))
    for item in payload.get("scenarios", [])
]
checks = [run_check(Check.from_dict(item)) for item in payload.get("checks", [])]
sys.stdout.write(
    json.dumps(
        {
            "scenarios": scenarios,
            "checks": checks,
            "grader_fingerprint": fingerprint(),
        },
        ensure_ascii=False,
    )
)
"""


class RsiError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    scenario: str
    phase: RunPhase | None
    stop_reason: str
    touched: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ScenarioDelta:
    scenario: str
    baseline: float
    candidate: float
    passed: bool
    reasons: tuple[str, ...]

    @property
    def delta(self) -> float:
        return round(self.candidate - self.baseline, 3)


@dataclass(frozen=True, slots=True)
class CheckDelta:
    name: str
    baseline: str
    candidate: str
    passed: bool
    reasons: tuple[str, ...]
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    repository: str
    patch: str | None
    scenarios: tuple[ScenarioDelta, ...]
    grader_fingerprint: str = ""
    checks: tuple[CheckDelta, ...] = ()

    @property
    def baseline_mean(self) -> float:
        return _mean(self._values("baseline"))

    @property
    def candidate_mean(self) -> float:
        return _mean(self._values("candidate"))

    @property
    def regression(self) -> bool:
        return self.candidate_mean < self.baseline_mean

    def _values(self, side: str) -> list[float]:
        values = [
            getattr(item, side)
            for item in self.scenarios
        ]
        values += [
            1.0 if getattr(item, side) == "ok" else 0.0
            for item in self.checks
            if not item.skipped
        ]
        return values

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "patch": self.patch,
            "grader_fingerprint": self.grader_fingerprint,
            "baseline_mean": self.baseline_mean,
            "candidate_mean": self.candidate_mean,
            "regression": self.regression,
            "scenarios": [
                {
                    "scenario": item.scenario,
                    "baseline": item.baseline,
                    "candidate": item.candidate,
                    "delta": item.delta,
                    "passed": item.passed,
                    "reasons": list(item.reasons),
                }
                for item in self.scenarios
            ],
            "checks": [
                {
                    "check": item.name,
                    "baseline": item.baseline,
                    "candidate": item.candidate,
                    "passed": item.passed,
                    "skipped": item.skipped,
                    "reasons": list(item.reasons),
                }
                for item in self.checks
            ],
        }


def assert_rsi_scope(touched: Iterable[str]) -> None:
    """A self-improvement patch may only touch RepoPilot's runtime source."""
    paths = [str(path).replace("\\", "/") for path in touched]
    if not paths:
        raise RsiError("补丁没有涉及任何文件")
    for path in paths:
        if path in _BLOCKED_SOURCE or path in _BLOCKED_FILES:
            raise RsiError(f"RSI 不允许修改评测相关文件: {path}")
        if path.startswith(_BLOCKED_PREFIXES):
            raise RsiError(f"RSI 不允许修改测试或文档: {path}")
        if not path.startswith(_SCOPED_PREFIX):
            raise RsiError(f"RSI 只允许修改 {_SCOPED_PREFIX}**，收到: {path}")


def baseline_snapshot(
    repository: str | Path,
    scenarios: Sequence[Scenario],
    *,
    checks: Sequence[Check] | None = None,
    expect_grader: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    """Run the pristine source once and pin the grader fingerprint."""
    root = Path(repository).expanduser().resolve()
    source = root / "src"
    if not (source / "repopilot").is_dir():
        raise RsiError(f"目标仓库缺少 src/repopilot: {root}")
    scores, check_status, fingerprint = _run_cases(
        source, root, tuple(scenarios), tuple(checks if checks is not None else BUILTIN_CHECKS)
    )
    if expect_grader and fingerprint != expect_grader:
        raise RsiError(
            "受信 grader 指纹不匹配："
            f"期望 {expect_grader}，实际 {fingerprint or '（无法计算）'}"
        )
    return scores, check_status, fingerprint


def ensure_improvable(
    repository: str | Path,
    scenario: Scenario,
    *,
    checks: Sequence[Check] | None = None,
    expect_grader: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    """Refuse to generate candidates for a scenario that already scores 1.0."""
    snapshot = baseline_snapshot(
        repository, (scenario,), checks=checks, expect_grader=expect_grader
    )
    score = snapshot[0][scenario.name]["score"]
    if score >= 1.0:
        raise RsiError(
            f"基准场景 {scenario.name} 已经是 1.0，没有可改进项；"
            "请先引入一个真实失败的场景或行为"
        )
    return snapshot


def evaluate_repository(
    repository: str | Path,
    *,
    scenarios: Sequence[Scenario] | None = None,
    checks: Sequence[Check] | None = None,
    patch: str | None = None,
    out_dir: str | Path | None = None,
    expect_grader: str | None = None,
    baseline: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]
    | None = None,
) -> EvaluationReport:
    root = Path(repository).expanduser().resolve()
    source = root / "src"
    if not (source / "repopilot").is_dir():
        raise RsiError(f"目标仓库缺少 src/repopilot: {root}")
    chosen = tuple(scenarios) if scenarios else BUILTIN_SCENARIOS
    chosen_checks = tuple(checks) if checks is not None else BUILTIN_CHECKS
    if patch:
        try:
            assert_rsi_scope(parse_touched_files(patch))
        except PatchError as exc:
            raise RsiError(f"候选补丁无法解析: {exc}") from exc

    if baseline is None:
        baseline_scores, baseline_checks, baseline_fingerprint = baseline_snapshot(
            root, chosen, checks=chosen_checks, expect_grader=expect_grader
        )
    else:
        baseline_scores, baseline_checks, baseline_fingerprint = baseline
        if expect_grader and baseline_fingerprint != expect_grader:
            raise RsiError(
                "受信 grader 指纹不匹配："
                f"期望 {expect_grader}，实际 {baseline_fingerprint or '（无法计算）'}"
            )

    candidate_source = source
    workdir: tempfile.TemporaryDirectory | None = None
    if patch:
        workdir = tempfile.TemporaryDirectory()
        candidate_source = _prepare_candidate(source, patch, Path(workdir.name))
    try:
        candidate, candidate_checks, candidate_fingerprint = _run_cases(
            candidate_source, root, chosen, chosen_checks
        )
    finally:
        if workdir is not None:
            workdir.cleanup()
    if patch and candidate_fingerprint != baseline_fingerprint:
        candidate = _failed_outcomes(
            chosen, "候选修改了评测栈（grader/benchmark/checks 指纹变化），拒绝评分"
        )
        candidate_checks = _failed_check_status(
            chosen_checks, "候选修改了评测栈（grader/benchmark/checks 指纹变化）"
        )

    deltas = tuple(
        ScenarioDelta(
            scenario=scenario.name,
            baseline=baseline_scores[scenario.name]["score"],
            candidate=candidate[scenario.name]["score"],
            passed=candidate[scenario.name]["passed"],
            reasons=candidate[scenario.name]["reasons"],
        )
        for scenario in chosen
    )
    check_deltas = tuple(
        _check_delta(check, baseline_checks, candidate_checks) for check in chosen_checks
    )
    report = EvaluationReport(
        str(root), patch, deltas, baseline_fingerprint, check_deltas
    )

    if out_dir is not None:
        output = Path(out_dir).expanduser()
        output.mkdir(parents=True, exist_ok=True)
        (output / "scorecard.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (output / "scorecard.md").write_text(render_scorecard(report), encoding="utf-8")
    return report


def _check_delta(
    check: Check,
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
) -> CheckDelta:
    base = baseline.get(check.name, {"status": "skip", "detail": ""})
    cand = candidate.get(check.name, {"status": "fail", "detail": "候选没有返回该检查"})
    status = str(cand.get("status", "fail"))
    detail = str(cand.get("detail", ""))
    score = score_check(check.name, status=status, detail=detail)
    return CheckDelta(
        name=check.name,
        baseline=str(base.get("status", "skip")),
        candidate=status,
        passed=score.passed,
        reasons=score.reasons,
        skipped=status == "skip",
    )


def render_scorecard(report: EvaluationReport) -> str:
    lines = [
        "# RepoPilot 自我评测",
        "",
        f"- 仓库: `{report.repository}`",
        f"- 候选补丁: {'有' if report.patch else '无（仅基线）'}",
        (
            f"- 受信 grader: `{report.grader_fingerprint[:16]}…`"
            if report.grader_fingerprint
            else "- 受信 grader: （不可用）"
        ),
        f"- 基线均分: {report.baseline_mean}",
        f"- 候选均分: {report.candidate_mean}",
        f"- 结论: {'回退' if report.regression else '未回退'}",
        "",
        "| 场景 | 基线 | 候选 | Δ | 通过 | 原因 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.scenarios:
        reasons = "；".join(item.reasons) or "-"
        lines.append(
            f"| {item.scenario} | {item.baseline} | {item.candidate} | "
            f"{item.delta:+} | {'是' if item.passed else '否'} | {reasons} |"
        )
    if report.checks:
        lines.extend(
            ["", "| 检查 | 基线 | 候选 | 通过 | 原因 |", "| --- | --- | --- | --- | --- |"]
        )
        for check in report.checks:
            reasons = "；".join(check.reasons) or "-"
            state = "跳过" if check.skipped else ("是" if check.passed else "否")
            lines.append(
                f"| {check.name} | {check.baseline} | {check.candidate} | "
                f"{state} | {reasons} |"
            )
    lines.append("")
    lines.append("评分由固定评分器在候选之外计算；应用补丁前请人工审阅 diff。")
    return "\n".join(lines) + "\n"


def _run_cases(
    source_root: Path,
    cwd: Path,
    scenarios: Sequence[Scenario],
    checks: Sequence[Check],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    with tempfile.TemporaryDirectory() as temp:
        payload = Path(temp) / "cases.json"
        payload.write_text(
            json.dumps(
                {
                    "scenarios": [scenario.to_dict() for scenario in scenarios],
                    "checks": [check.to_dict() for check in checks],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(source_root)
        try:
            completed = subprocess.run(
                [sys.executable, "-c", _RUNNER, str(payload)],
                cwd=cwd,
                env=env,
                capture_output=True,
                timeout=EVALUATION_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _failure(scenarios, checks, f"{type(exc).__name__}: {exc}")
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        return _failure(scenarios, checks, detail[-800:] or "评测子进程失败")
    try:
        data = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _failure(scenarios, checks, f"候选输出不是合法 JSON: {exc}")
    if not isinstance(data, dict):
        return _failure(scenarios, checks, "候选输出不是对象")
    fingerprint = str(data.get("grader_fingerprint") or "")
    return (
        _score_facts_payload(scenarios, data.get("scenarios")),
        _check_status_map(checks, data.get("checks")),
        fingerprint,
    )


def _failure(
    scenarios: Sequence[Scenario], checks: Sequence[Check], error: str
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    return (
        _failed_outcomes(scenarios, error),
        _failed_check_status(checks, error),
        "",
    )


def _check_status_map(
    checks: Sequence[Check], payload: Any
) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, list):
        return _failed_check_status(checks, "候选没有返回检查结果")
    by_name = {
        str(item.get("check", "")): item for item in payload if isinstance(item, dict)
    }
    status: dict[str, dict[str, Any]] = {}
    for check in checks:
        item = by_name.get(check.name)
        if item is None:
            status[check.name] = {"status": "fail", "detail": "候选没有返回该检查"}
            continue
        value = str(item.get("status", "fail"))
        if value not in {"ok", "fail", "skip"}:
            value = "fail"
        status[check.name] = {"status": value, "detail": str(item.get("detail", ""))}
    return status


def _failed_check_status(
    checks: Sequence[Check], error: str
) -> dict[str, dict[str, Any]]:
    return {check.name: {"status": "fail", "detail": error} for check in checks}


def _score_facts_payload(
    scenarios: Sequence[Scenario], facts: Any
) -> dict[str, dict[str, Any]]:
    if not isinstance(facts, list):
        return _failed_outcomes(scenarios, "候选输出不是数组")
    by_name = {str(item.get("scenario", "")): item for item in facts if isinstance(item, dict)}
    scored: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        fact = by_name.get(scenario.name)
        if fact is None:
            scored[scenario.name] = _zero(scenario.name, "候选没有返回该场景的结果")
            continue
        raw_phase = fact.get("phase") or ""
        try:
            phase = RunPhase(raw_phase) if raw_phase else None
        except ValueError:
            scored[scenario.name] = _zero(scenario.name, "候选返回了非法的阶段值")
            continue
        error = fact.get("error")
        score: Score = score_facts(
            scenario.grade_spec(),
            touched=fact.get("touched") or (),
            phase=phase,
            stop_reason=str(fact.get("stop_reason") or ""),
            error=str(error) if error else None,
        )
        scored[scenario.name] = {
            "score": score.score,
            "passed": score.passed,
            "reasons": list(score.reasons),
        }
    return scored


def _failed_outcomes(
    scenarios: Sequence[Scenario], error: str
) -> dict[str, dict[str, Any]]:
    return {scenario.name: _zero(scenario.name, error) for scenario in scenarios}


def _zero(name: str, error: str) -> dict[str, Any]:
    return {"score": 0.0, "passed": False, "reasons": [error]}


def _prepare_candidate(source: Path, patch: str, workspace: Path) -> Path:
    candidate = workspace / "candidate"
    shutil.copytree(
        source,
        candidate / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    try:
        GitClient(candidate).apply_patch(patch)
    except GitError as exc:
        raise RsiError(f"候选补丁无法应用: {exc}") from exc
    return candidate / "src"


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return round(sum(items) / len(items), 3) if items else 0.0


def default_rsi_history_path(repository: str | Path) -> Path:
    override = os.getenv("REPOPILOT_RSI_HISTORY", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    root = Path(repository).expanduser().resolve()
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    return default_run_store_root() / f"rsi-{digest}.jsonl"


def classify_round(
    score: float | None, best_score: float, *, duplicate: bool
) -> str:
    if duplicate:
        return STATUS_DUPLICATE
    if score is None:
        return STATUS_ERROR
    return STATUS_IMPROVED if score > best_score else STATUS_REJECTED


def run_rounds(
    repository: str | Path,
    *,
    scenario: Scenario,
    rounds: int,
    store: PatchRunStore,
    out_dir: str | Path | None = None,
    history_path: str | Path | None = None,
    model_client=None,
    expect_grader: str | None = None,
    checks: Sequence[Check] | None = None,
) -> tuple[RoundRecord, ...]:
    """Generate and score one candidate per round. Never applies anything."""
    if rounds < 1:
        raise RsiError("rounds 必须大于 0")
    chosen_checks = tuple(checks) if checks is not None else BUILTIN_CHECKS
    root = Path(repository).expanduser().resolve()
    output = (
        Path(out_dir).expanduser()
        if out_dir is not None
        else root / "artifacts" / "rsi"
    )
    output.mkdir(parents=True, exist_ok=True)
    history = RsiHistory(history_path or default_rsi_history_path(root))
    runtime = build_runtime(root, model_client=model_client)

    baseline = ensure_improvable(
        root, scenario, checks=chosen_checks, expect_grader=expect_grader
    )
    best = baseline[0][scenario.name]["score"]
    seen = set(history.patch_hashes())
    records: list[RoundRecord] = []
    feedback = ""

    for index in range(1, rounds + 1):
        try:
            proposal = runtime.propose_task(
                _round_goal(scenario, index, feedback), store
            )
            assert_rsi_scope(proposal.proposal.touched_files)
        except (RsiError, PatchError) as exc:
            entry = RoundRecord(
                index=index,
                run_id="",
                timestamp=_now(),
                scenario=scenario.name,
                patch_hash="",
                score=None,
                best_score=best,
                status=STATUS_ERROR,
                reasons=(str(exc),),
            )
            history.append(entry)
            records.append(entry)
            break

        diff = proposal.proposal.unified_diff
        digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        patch_path = output / f"round-{index}.diff"
        patch_path.write_text(diff, encoding="utf-8")

        duplicate = digest in seen
        seen.add(digest)
        score: float | None = None
        reasons: tuple[str, ...] = ()
        if duplicate:
            reasons = ("与历史候选相同，跳过评测",)
        else:
            report = evaluate_repository(
                root,
                scenarios=(scenario,),
                checks=chosen_checks,
                patch=diff,
                baseline=baseline,
            )
            delta = report.scenarios[0]
            score = delta.candidate
            reasons = delta.reasons

        status = classify_round(score, best, duplicate=duplicate)
        if status == STATUS_IMPROVED and score is not None:
            best = score
        entry = RoundRecord(
            index=index,
            run_id=proposal.run_id,
            timestamp=_now(),
            scenario=scenario.name,
            patch_hash=digest,
            score=score,
            best_score=best,
            status=status,
            reasons=reasons,
            patch_path=str(patch_path),
        )
        history.append(entry)
        records.append(entry)
        feedback = "；".join(reasons)
        if best >= 1.0:
            break
    return tuple(records)


def _round_goal(scenario: Scenario, index: int, feedback: str) -> str:
    parts = [
        (
            f"RepoPilot 自改进（第 {index} 轮）：基准场景 {scenario.name} 未通过"
            f"（{scenario.description}）。"
        ),
        (
            f"期望阶段 {scenario.expected_phase.value}、停止原因 "
            f"{scenario.expected_stop_reason}、涉及文件 "
            f"{', '.join(scenario.expected_files)}。"
        ),
        "请修改 RepoPilot 自身实现使该场景通过，不要修改测试或评分代码。",
    ]
    if feedback:
        parts.append(f"上一轮评测反馈：{feedback}。请换一个思路。")
    return "".join(parts)


def _now() -> str:
    return datetime.now(UTC).isoformat()
