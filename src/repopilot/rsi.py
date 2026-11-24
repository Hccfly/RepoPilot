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
from .contracts import RunPhase
from .git import GitClient, GitError
from .grader import score_facts
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
_BLOCKED_SOURCE = ("src/repopilot/benchmark.py", "src/repopilot/grader.py")

_RUNNER = r"""
import json
import sys
from pathlib import Path

from repopilot.benchmark import Scenario, run_scenario_facts
from repopilot.grader import fingerprint

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
facts = [run_scenario_facts(Scenario.from_dict(item)) for item in payload]
sys.stdout.write(
    json.dumps(
        {"facts": facts, "grader_fingerprint": fingerprint()}, ensure_ascii=False
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
class EvaluationReport:
    repository: str
    patch: str | None
    scenarios: tuple[ScenarioDelta, ...]
    grader_fingerprint: str = ""

    @property
    def baseline_mean(self) -> float:
        return _mean(item.baseline for item in self.scenarios)

    @property
    def candidate_mean(self) -> float:
        return _mean(item.candidate for item in self.scenarios)

    @property
    def regression(self) -> bool:
        return self.candidate_mean < self.baseline_mean

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
    expect_grader: str | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Run the pristine source once and pin the grader fingerprint."""
    root = Path(repository).expanduser().resolve()
    source = root / "src"
    if not (source / "repopilot").is_dir():
        raise RsiError(f"目标仓库缺少 src/repopilot: {root}")
    scores, fingerprint = _run_scenarios(source, root, tuple(scenarios))
    if expect_grader and fingerprint != expect_grader:
        raise RsiError(
            "受信 grader 指纹不匹配："
            f"期望 {expect_grader}，实际 {fingerprint or '（无法计算）'}"
        )
    return scores, fingerprint


def evaluate_repository(
    repository: str | Path,
    *,
    scenarios: Sequence[Scenario] | None = None,
    patch: str | None = None,
    out_dir: str | Path | None = None,
    expect_grader: str | None = None,
    baseline: tuple[dict[str, dict[str, Any]], str] | None = None,
) -> EvaluationReport:
    root = Path(repository).expanduser().resolve()
    source = root / "src"
    if not (source / "repopilot").is_dir():
        raise RsiError(f"目标仓库缺少 src/repopilot: {root}")
    chosen = tuple(scenarios) if scenarios else BUILTIN_SCENARIOS
    if patch:
        try:
            assert_rsi_scope(parse_touched_files(patch))
        except PatchError as exc:
            raise RsiError(f"候选补丁无法解析: {exc}") from exc

    if baseline is None:
        baseline_scores, baseline_fingerprint = baseline_snapshot(
            root, chosen, expect_grader=expect_grader
        )
    else:
        baseline_scores, baseline_fingerprint = baseline
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
        candidate, candidate_fingerprint = _run_scenarios(
            candidate_source, root, chosen
        )
    finally:
        if workdir is not None:
            workdir.cleanup()
    if patch and candidate_fingerprint != baseline_fingerprint:
        candidate = _failed_outcomes(
            chosen, "候选修改了评测栈（grader/benchmark 指纹变化），拒绝评分"
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
    report = EvaluationReport(str(root), patch, deltas, baseline_fingerprint)

    if out_dir is not None:
        output = Path(out_dir).expanduser()
        output.mkdir(parents=True, exist_ok=True)
        (output / "scorecard.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (output / "scorecard.md").write_text(render_scorecard(report), encoding="utf-8")
    return report


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
    lines.append("")
    lines.append("评分由固定评分器在候选之外计算；应用补丁前请人工审阅 diff。")
    return "\n".join(lines) + "\n"


def _run_scenarios(
    source_root: Path, cwd: Path, scenarios: Sequence[Scenario]
) -> tuple[dict[str, dict[str, Any]], str]:
    with tempfile.TemporaryDirectory() as temp:
        payload = Path(temp) / "scenarios.json"
        payload.write_text(
            json.dumps([scenario.to_dict() for scenario in scenarios], ensure_ascii=False),
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
            return _failed_outcomes(scenarios, f"{type(exc).__name__}: {exc}"), ""
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        return _failed_outcomes(scenarios, detail[-800:] or "评测子进程失败"), ""
    try:
        payload_data = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _failed_outcomes(scenarios, f"候选输出不是合法 JSON: {exc}"), ""
    if not isinstance(payload_data, dict):
        return _failed_outcomes(scenarios, "候选输出不是对象"), ""
    fingerprint = str(payload_data.get("grader_fingerprint") or "")
    return (
        _score_facts_payload(scenarios, payload_data.get("facts")),
        fingerprint,
    )


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
) -> tuple[RoundRecord, ...]:
    """Generate and score one candidate per round. Never applies anything."""
    if rounds < 1:
        raise RsiError("rounds 必须大于 0")
    root = Path(repository).expanduser().resolve()
    output = Path(out_dir or "artifacts/rsi").expanduser()
    output.mkdir(parents=True, exist_ok=True)
    history = RsiHistory(history_path or default_rsi_history_path(root))
    runtime = build_runtime(root, model_client=model_client)

    baseline = baseline_snapshot(root, (scenario,), expect_grader=expect_grader)
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
                root, scenarios=(scenario,), patch=diff, baseline=baseline
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
