"""Headless CI entrypoints: machine-readable artifacts and stable exit codes.

Nothing here applies a patch or asks for approval; CI prepares evidence and a
human (or a later explicit step) decides.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import RunPhase, VerificationResult
from .persistence import PatchRunStore
from .report import build_report, render_markdown, render_verification_history
from .runtime import RepoPilotRuntime


def run_ci_plan(runtime: RepoPilotRuntime, goal: str, out_dir: str | Path) -> int:
    output = _prepare(out_dir)
    state = runtime.plan_task(goal)
    _write_json(output / "plan.json", state.to_dict())
    if state.phase is not RunPhase.WAIT_APPROVAL or state.plan is None:
        print(f"规划失败: {state.error}")
        return 1
    print(f"Run: {state.run_id}")
    print(f"计划: {state.plan.summary}")
    for index, step in enumerate(state.plan.steps, start=1):
        print(f"  {index}. {step.description}")
    print(f"产物: {output / 'plan.json'}")
    return 0


def run_ci_propose(
    runtime: RepoPilotRuntime, store: PatchRunStore, goal: str, out_dir: str | Path
) -> int:
    output = _prepare(out_dir)
    record = runtime.propose_task(goal, store)
    (output / "patch.diff").write_text(record.proposal.unified_diff, encoding="utf-8")
    report = build_report(record)
    _write_json(output / "report.json", report.to_dict())
    (output / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(f"Run: {record.run_id}")
    print(f"文件: {', '.join(record.proposal.touched_files)}")
    print(f"产物: {output / 'patch.diff'}  {output / 'report.json'}")
    print("未应用任何改动；请审查后显式执行 apply。")
    return 0


def run_ci_verify(
    runtime: RepoPilotRuntime,
    store: PatchRunStore,
    run_id: str,
    out_dir: str | Path,
) -> int:
    output = _prepare(out_dir)
    outcome = runtime.verify_task(run_id, store)
    record = store.load(run_id)
    report = build_report(record)
    _write_json(output / "report.json", report.to_dict())
    (output / "report.md").write_text(render_markdown(report), encoding="utf-8")

    history = [
        VerificationResult.from_dict(item) for item in record.state.get("verification") or []
    ]
    print(f"Run: {run_id}")
    print(f"Phase: {outcome.phase.value}")
    for line in render_verification_history(history):
        print(f"  {line}")
    print(f"产物: {output / 'report.json'}")
    return 0 if outcome.passed else 1


def _prepare(out_dir: str | Path) -> Path:
    output = Path(out_dir).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    return output


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
