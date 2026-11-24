"""RepoPilot command-line adapter."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .benchmark import BUILTIN_SCENARIOS
from .ci import run_ci_plan, run_ci_propose, run_ci_verify
from .config import load_environment, resolve_env_path
from .contracts import ReviewDecision, RunPhase, VerificationResult
from .git import GitError
from .llm import ModelNotConfigured
from .patching import PatchError, PatchRunRecord
from .persistence import (
    JsonPatchRunStore,
    PatchRunStore,
    RunStoreError,
    SqlitePatchRunStore,
)
from .providers import ProviderError, ProviderRegistry, render_provider_table
from .report import render_markdown, render_verification_history
from .rsi import (
    RsiError,
    assert_rsi_scope,
    default_rsi_history_path,
    evaluate_repository,
    render_scorecard,
    run_rounds,
)
from .rsi_history import RsiHistory
from .runtime import RepoPilotRuntime, build_runtime
from .sandbox import SandboxError, serve_sandbox
from .sandbox_audit import SandboxLedger
from .tui import run_tui
from .verification import VerificationError
from .workspace import WorkspaceError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repopilot",
        description="Resumable, reviewable repository task agent CLI.",
    )
    parser.add_argument("--version", action="version", version=f"RepoPilot {__version__}")
    parser.add_argument("--config", help="Path to repopilot TOML configuration.")
    parser.add_argument("--provider", help="覆盖配置中的模型 Provider 名称。")
    parser.add_argument(
        "--runs-dir",
        help="Patch ledger directory (default: %%LOCALAPPDATA%%\\RepoPilot\\runs).",
    )
    parser.add_argument(
        "--store",
        choices=("sqlite", "json"),
        default="sqlite",
        help="Ledger backend: sqlite checkpoint store (default) or the P1 JSON ledger.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="Check local prerequisites and repository discovery."
    )
    doctor.add_argument("--repo", default=".", help="Path inside the target Git repository.")

    status = subparsers.add_parser(
        "status", help="Show repository status, or the checkpoint of one run."
    )
    status.add_argument("run_id", nargs="?", help="Optional run identifier to inspect instead.")
    status.add_argument("--repo", default=".", help="Path inside the target Git repository.")

    task = subparsers.add_parser("task", help="Create the initial state for a repository task.")
    task.add_argument("goal", help="Bug, issue, or feature request to work on.")
    task.add_argument("--repo", default=".", help="Path inside the target Git repository.")
    task.add_argument("--json", action="store_true", help="Print checkpoint-safe JSON.")

    plan = subparsers.add_parser("plan", help="Generate an evidence-backed read-only change plan.")
    plan.add_argument("goal", help="Bug, issue, or feature request to analyze.")
    plan.add_argument("--repo", default=".", help="Path inside the target Git repository.")
    plan.add_argument("--json", action="store_true", help="Print checkpoint-safe JSON.")

    propose = subparsers.add_parser(
        "propose", help="Plan the task and store a reviewable unified-diff proposal."
    )
    propose.add_argument("goal", help="Bug, issue, or feature request to implement.")
    propose.add_argument("--repo", default=".", help="Path inside the target Git repository.")

    show_diff = subparsers.add_parser("diff", help="Show a stored patch proposal.")
    show_diff.add_argument("run_id", help="Patch run identifier returned by propose.")

    apply = subparsers.add_parser("apply", help="Approve and apply a stored patch proposal.")
    apply.add_argument("run_id", help="Patch run identifier returned by propose.")

    undo = subparsers.add_parser("undo", help="Undo an applied patch exactly.")
    undo.add_argument("run_id", help="Patch run identifier returned by propose.")

    commit = subparsers.add_parser(
        "commit", help="Commit this run's files after separate approval (opt-in)."
    )
    commit.add_argument("run_id", help="Run identifier whose patch is applied and verified.")
    commit.add_argument(
        "--message", help="提交说明；默认由运行目标与验证证据生成。"
    )

    verify = subparsers.add_parser(
        "verify", help="Run whitelisted verification against an applied patch."
    )
    verify.add_argument("run_id", help="Run identifier whose patch has been applied.")
    verify.add_argument(
        "--action",
        dest="actions",
        action="append",
        default=[],
        help="Whitelisted verification action; repeat to select several.",
    )
    verify.add_argument(
        "--force", action="store_true", help="Re-run actions that already passed."
    )

    resume = subparsers.add_parser(
        "resume", help="Continue an interrupted run from its checkpoint."
    )
    resume.add_argument("run_id", help="Run identifier to resume.")

    repair = subparsers.add_parser(
        "repair", help="Apply one bounded repair patch and re-verify."
    )
    repair.add_argument("run_id", help="Run identifier stopped in REPAIR.")

    review = subparsers.add_parser("review", help="Run the read-only review node.")
    review.add_argument("run_id", help="Run identifier that passed verification.")

    report = subparsers.add_parser("report", help="Render the run report.")
    report.add_argument("run_id", help="Run identifier to report on.")
    report.add_argument("--format", choices=("markdown", "json"), default="markdown")

    listing = subparsers.add_parser("list", help="List stored runs and their phases.")

    subparsers.add_parser("providers", help="List model providers and their readiness.")

    tui = subparsers.add_parser("tui", help="Open the interactive terminal dashboard.")
    tui.add_argument("--repo", default=".", help="Path inside the target Git repository.")

    ci = subparsers.add_parser(
        "ci", help="Headless CI mode: write artifacts, exit with a stable code."
    )
    ci_commands = ci.add_subparsers(dest="ci_command", required=True)

    ci_plan = ci_commands.add_parser("plan", help="Generate a plan and write plan.json.")
    ci_plan.add_argument("goal")
    ci_plan.add_argument("--repo", default=".")
    ci_plan.add_argument("--out-dir", default="artifacts")

    ci_propose = ci_commands.add_parser(
        "propose", help="Generate a proposal and write patch.diff plus report.json."
    )
    ci_propose.add_argument("goal")
    ci_propose.add_argument("--repo", default=".")
    ci_propose.add_argument("--out-dir", default="artifacts")

    ci_verify = ci_commands.add_parser(
        "verify", help="Re-run verification and write report.json plus report.md."
    )
    ci_verify.add_argument("run_id")
    ci_verify.add_argument("--out-dir", default="artifacts")

    sandbox = subparsers.add_parser(
        "sandbox", help="Run a standalone verification sandbox service."
    )
    sandbox_commands = sandbox.add_subparsers(dest="sandbox_command", required=True)
    sandbox_serve = sandbox_commands.add_parser(
        "serve", help="Serve whitelisted verification actions over TCP."
    )
    sandbox_serve.add_argument("--repo", default=".")
    sandbox_serve.add_argument("--host", default="127.0.0.1")
    sandbox_serve.add_argument("--port", type=int, default=8765)
    sandbox_serve.add_argument("--token-env", default="REPOPILOT_SANDBOX_TOKEN")
    sandbox_serve.add_argument(
        "--tenant",
        dest="tenants",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="多租户模式：重复指定 name=path；省略则使用 --repo 作为 default 租户。",
    )
    sandbox_serve.add_argument("--cert", dest="cert_file", default="", help="TLS 证书链。")
    sandbox_serve.add_argument("--key", dest="key_file", default="", help="TLS 私钥。")
    sandbox_serve.add_argument(
        "--max-concurrency", type=int, default=4, help="全局并发上限。"
    )
    sandbox_serve.add_argument(
        "--audit-db", default="", help="审计数据库路径；默认位于 RepoPilot 数据目录。"
    )
    sandbox_serve.add_argument("--no-audit", action="store_true", help="关闭请求审计。")
    sandbox_serve.add_argument(
        "--quota", type=int, default=0, help="每租户每窗口允许的请求数；0 表示不限。"
    )
    sandbox_serve.add_argument(
        "--quota-window", type=int, default=3600, help="配额窗口秒数。"
    )

    sandbox_audit = sandbox_commands.add_parser(
        "audit", help="Show recent sandbox requests."
    )
    sandbox_audit.add_argument("--db", default="", help="审计数据库路径。")
    sandbox_audit.add_argument("--limit", type=int, default=20)

    sandbox_quota = sandbox_commands.add_parser(
        "quota", help="Show current quota windows."
    )
    sandbox_quota.add_argument("--db", default="", help="审计数据库路径。")
    sandbox_quota.add_argument("--window", type=int, default=3600)

    rsi = subparsers.add_parser(
        "rsi", help="Score RepoPilot against its own benchmark (human-gated)."
    )
    rsi_commands = rsi.add_subparsers(dest="rsi_command", required=True)

    rsi_eval = rsi_commands.add_parser(
        "eval", help="Score the current checkout against the benchmark."
    )
    rsi_eval.add_argument("--repo", default=".")
    rsi_eval.add_argument("--out", default="")
    rsi_eval.add_argument("--scenario", dest="scenarios", action="append", default=[])
    rsi_eval.add_argument(
        "--expect-grader", default="", help="固定受信 grader 指纹，不匹配则拒绝评测。"
    )

    rsi_compare = rsi_commands.add_parser(
        "compare", help="Score a candidate patch against the baseline."
    )
    rsi_compare.add_argument("--patch", required=True)
    rsi_compare.add_argument("--repo", default=".")
    rsi_compare.add_argument("--out", default="")
    rsi_compare.add_argument(
        "--scenario", dest="scenarios", action="append", default=[]
    )
    rsi_compare.add_argument("--expect-grader", default="")

    rsi_propose = rsi_commands.add_parser(
        "propose", help="Propose a fix for one failing scenario and score it."
    )
    rsi_propose.add_argument("--scenario", required=True)
    rsi_propose.add_argument("--repo", default=".")
    rsi_propose.add_argument("--out", default="artifacts/rsi")
    rsi_propose.add_argument("--expect-grader", default="")

    rsi_run = rsi_commands.add_parser(
        "run", help="Run several candidate rounds and keep a scorecard history."
    )
    rsi_run.add_argument("--scenario", required=True)
    rsi_run.add_argument("--rounds", type=int, default=3)
    rsi_run.add_argument("--repo", default=".")
    rsi_run.add_argument("--out", default="artifacts/rsi")
    rsi_run.add_argument("--history", default="")
    rsi_run.add_argument("--expect-grader", default="")

    rsi_show_history = rsi_commands.add_parser(
        "history", help="Show past RSI rounds."
    )
    rsi_show_history.add_argument("--repo", default=".")
    rsi_show_history.add_argument("--history", default="")
    rsi_show_history.add_argument("--limit", type=int, default=20)

    for command in (
        propose,
        show_diff,
        apply,
        undo,
        commit,
        verify,
        resume,
        repair,
        review,
        report,
        listing,
        tui,
    ):
        command.add_argument(
            "--runs-dir",
            default=argparse.SUPPRESS,
            help="Override the patch ledger directory.",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        load_environment()
        if args.command == "providers":
            return _providers()
        if args.command == "sandbox":
            return _sandbox(args)
        if args.command == "tui":
            return run_tui(args)
        store = _open_store(args)
        if args.command == "rsi":
            return _rsi(args, store)
        if args.command == "ci":
            return _ci(args, store)
        if args.command == "diff":
            return _diff(store.load(args.run_id))
        if args.command == "list":
            return _list(store)
        if args.command == "status" and args.run_id:
            return _run_status(store.load(args.run_id))
        if args.command in {"apply", "undo", "commit"}:
            record = store.load(args.run_id)
            runtime = build_runtime(
                record.repository,
                config_path=args.config,
                model_provider=args.provider,
            )
            service = runtime.patch_lifecycle(store)
            if args.command == "apply":
                return _apply(service.apply(args.run_id, approved=True))
            if args.command == "commit":
                return _commit(
                    service.commit(
                        args.run_id, approved=True, message=args.message
                    )
                )
            return _undo(service.undo(args.run_id, approved=True))
        if args.command in {"verify", "resume", "repair", "review", "report"}:
            record = store.load(args.run_id)
            runtime = build_runtime(
                record.repository,
                config_path=args.config,
                model_provider=args.provider,
            )
            if args.command == "verify":
                return _verify(runtime, store, args)
            if args.command == "repair":
                return _repair(runtime, store, args)
            if args.command == "review":
                return _review(runtime, store, args)
            if args.command == "report":
                return _report(runtime, store, args)
            return _resume(runtime, store, args)

        runtime = build_runtime(
            args.repo, config_path=args.config, model_provider=args.provider
        )
        if args.command == "doctor":
            return _doctor(runtime)
        if args.command == "status":
            return _status(runtime)
        if args.command == "task":
            return _task(runtime, args.goal, as_json=args.json)
        if args.command == "plan":
            return _plan(runtime, args.goal, as_json=args.json)
        if args.command == "propose":
            return _propose(runtime.propose_task(args.goal, store), store)
    except (
        WorkspaceError,
        GitError,
        ModelNotConfigured,
        PatchError,
        RunStoreError,
        VerificationError,
        SandboxError,
        ProviderError,
        RsiError,
        OSError,
        ValueError,
    ) as exc:
        print(f"RepoPilot error: {exc}", file=sys.stderr)
        return 2
    return 2


def _open_store(args: argparse.Namespace) -> PatchRunStore:
    runs_dir = getattr(args, "runs_dir", None)
    if getattr(args, "store", "sqlite") == "json":
        return JsonPatchRunStore(runs_dir)
    return SqlitePatchRunStore(runs_dir)


def _sandbox(args: argparse.Namespace) -> int:
    if args.sandbox_command == "audit":
        return _sandbox_audit(args)
    if args.sandbox_command == "quota":
        return _sandbox_quota(args)
    return serve_sandbox(
        args.repo,
        host=args.host,
        port=args.port,
        token_env=args.token_env,
        tenant_specs=tuple(args.tenants),
        cert_file=args.cert_file,
        key_file=args.key_file,
        max_total_concurrency=args.max_concurrency,
        audit_db=args.audit_db,
        no_audit=args.no_audit,
        quota=args.quota,
        quota_window=args.quota_window,
    )


def _sandbox_audit(args: argparse.Namespace) -> int:
    rows = SandboxLedger(args.db or None).recent(args.limit)
    if not rows:
        print("没有审计记录。")
        return 0
    for row in rows:
        result = "ok" if row.ok else "fail"
        exit_code = row.exit_code if row.exit_code is not None else "-"
        print(
            f"{row.timestamp}  {row.tenant:<12}  {row.action:<10}  {result:<5}"
            f"  exit={exit_code:<4}  {row.duration_seconds:.2f}s  {row.remote}"
            f"  {row.error[:80]}".rstrip()
        )
    return 0


def _sandbox_quota(args: argparse.Namespace) -> int:
    usage = SandboxLedger(args.db or None).usage(args.window)
    if not usage:
        print("当前窗口没有配额记录。")
        return 0
    for item in usage:
        print(f"{item.tenant:<12}  used={item.used}  reset_at={item.reset_at}")
    return 0


def _ci(args: argparse.Namespace, store: PatchRunStore) -> int:
    if args.ci_command == "verify":
        record = store.load(args.run_id)
        runtime = build_runtime(
            record.repository,
            config_path=args.config,
            model_provider=args.provider,
        )
        return run_ci_verify(runtime, store, args.run_id, args.out_dir)
    runtime = build_runtime(
        args.repo, config_path=args.config, model_provider=args.provider
    )
    if args.ci_command == "plan":
        return run_ci_plan(runtime, args.goal, args.out_dir)
    return run_ci_propose(runtime, store, args.goal, args.out_dir)


def _rsi(args: argparse.Namespace, store: PatchRunStore) -> int:
    if args.rsi_command == "history":
        return _rsi_show_history(args)
    scenarios = _rsi_scenarios(args.scenarios)
    expect_grader = args.expect_grader or None
    if args.rsi_command == "run":
        return _rsi_run(args, store)
    if args.rsi_command == "eval":
        report = evaluate_repository(
            args.repo,
            scenarios=scenarios,
            out_dir=args.out or None,
            expect_grader=expect_grader,
        )
        print(render_scorecard(report), end="")
        return 0
    if args.rsi_command == "compare":
        patch = Path(args.patch).read_text(encoding="utf-8")
        report = evaluate_repository(
            args.repo,
            scenarios=scenarios,
            patch=patch,
            out_dir=args.out or None,
            expect_grader=expect_grader,
        )
        print(render_scorecard(report), end="")
        return 0
    return _rsi_propose(args, store)


def _rsi_run(args: argparse.Namespace, store: PatchRunStore) -> int:
    scenario = _scenario_by_name(args.scenario)
    history_path = (
        Path(args.history).expanduser()
        if args.history
        else default_rsi_history_path(args.repo)
    )
    records = run_rounds(
        args.repo,
        scenario=scenario,
        rounds=args.rounds,
        store=store,
        out_dir=args.out or None,
        history_path=history_path,
        expect_grader=args.expect_grader or None,
    )
    for item in records:
        score = "-" if item.score is None else f"{item.score:.2f}"
        reasons = "；".join(item.reasons)
        print(
            f"第 {item.index} 轮  {item.status:<10}score={score:<6}"
            f"best={item.best_score:.2f}  {item.run_id}  {reasons}".rstrip()
        )
    if records:
        print(f"最佳分数: {records[-1].best_score:.2f}")
    print(f"历史: {history_path}")
    print("未应用任何补丁；审阅 round-*.diff 后对目标 run 执行 apply。")
    return 0


def _rsi_show_history(args: argparse.Namespace) -> int:
    path = (
        Path(args.history).expanduser()
        if args.history
        else default_rsi_history_path(args.repo)
    )
    rounds = RsiHistory(path).rounds(args.limit)
    if not rounds:
        print(f"没有 RSI 历史: {path}")
        return 0
    for item in rounds:
        score = "-" if item.score is None else f"{item.score:.2f}"
        print(
            f"#{item.index:<3}{item.status:<10}{item.scenario:<20}"
            f"score={score:<6}best={item.best_score:.2f}  {item.run_id}  {item.patch_path}"
        )
    return 0


def _scenario_by_name(name: str):
    catalog = {scenario.name: scenario for scenario in BUILTIN_SCENARIOS}
    scenario = catalog.get(name)
    if scenario is None:
        available = ", ".join(sorted(catalog))
        raise RsiError(f"未知基准场景: {name}（可用: {available}）")
    return scenario


def _rsi_scenarios(names: list[str]):
    if not names:
        return None
    catalog = {scenario.name: scenario for scenario in BUILTIN_SCENARIOS}
    missing = [name for name in names if name not in catalog]
    if missing:
        available = ", ".join(sorted(catalog))
        raise RsiError(f"未知基准场景: {', '.join(missing)}（可用: {available}）")
    return tuple(catalog[name] for name in names)


def _rsi_propose(args: argparse.Namespace, store: PatchRunStore) -> int:
    scenario = _scenario_by_name(args.scenario)

    runtime = build_runtime(
        args.repo, config_path=args.config, model_provider=args.provider
    )
    goal = (
        f"RepoPilot 自改进：基准场景 {scenario.name} 未通过（{scenario.description}）。"
        f"期望阶段 {scenario.expected_phase.value}、停止原因 {scenario.expected_stop_reason}、"
        f"涉及文件 {', '.join(scenario.expected_files)}。"
        "请修改 RepoPilot 自身实现使该场景通过。"
    )
    record = runtime.propose_task(goal, store)
    assert_rsi_scope(record.proposal.touched_files)

    output = Path(args.out or "artifacts/rsi").expanduser()
    output.mkdir(parents=True, exist_ok=True)
    patch_path = output / "candidate.diff"
    patch_path.write_text(record.proposal.unified_diff, encoding="utf-8")

    report = evaluate_repository(
        args.repo,
        patch=record.proposal.unified_diff,
        out_dir=output,
        expect_grader=args.expect_grader or None,
    )
    print(f"Run: {record.run_id}")
    print(f"候选补丁: {patch_path}")
    print(render_scorecard(report), end="")
    print("补丁未应用。审阅 diff 后执行 repopilot apply。")
    return 0


def _providers() -> int:
    registry = ProviderRegistry.default()
    for line in render_provider_table(registry.describe()):
        print(line)
    for error in registry.errors:
        print(f"插件加载失败: {error}", file=sys.stderr)
    return 0


def _doctor(runtime: RepoPilotRuntime) -> int:
    print(f"RepoPilot: {__version__}")
    print(f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    print(f"Git: {shutil.which('git') or 'not found'}")
    print(f"Repository: {runtime.workspace.root}")
    print(f"Branch: {runtime.git.current_branch()}")
    print(f"Provider: {runtime.config.model.provider}")
    print(f"Model: {runtime.config.model.name or '(provider 默认)'}")
    print(f"Model endpoint: {runtime.config.model.base_url or '(SDK 默认)'}")
    print(f"Sandbox: {runtime.config.sandbox.mode}")
    for info in runtime.providers.describe():
        if info.name == runtime.config.model.provider:
            state = "ready" if info.ready else f"not ready — {info.detail}"
            print(f"Provider status: {state}")
    print(f"Environment file: {resolve_env_path()}")
    print(f"Registered tools: {', '.join(runtime.tools.names())}")
    return 0


def _propose(record: PatchRunRecord, store: JsonPatchRunStore) -> int:
    print(f"Run: {record.run_id}")
    print(f"Repository: {record.repository}")
    print(f"Status: {record.status}")
    print(f"涉及文件: {', '.join(record.proposal.touched_files)}")
    print(f"说明: {record.proposal.rationale}")
    print(f"台账: {store.root / (record.run_id + '.json')}")
    print(f"下一步: repopilot diff {record.run_id}")
    print(f"批准并应用: repopilot apply {record.run_id}")
    return 0


def _diff(record: PatchRunRecord) -> int:
    print(f"Run: {record.run_id}")
    print(f"Repository: {record.repository}")
    print(f"Status: {record.status}")
    print(f"说明: {record.proposal.rationale}")
    print(record.proposal.unified_diff, end="")
    return 0


def _apply(record: PatchRunRecord) -> int:
    assert record.receipt is not None
    print(f"Run: {record.run_id}")
    print(f"Status: {record.status}")
    print(f"已应用: {', '.join(record.receipt.touched_files)}")
    print(f"Patch SHA-256: {record.receipt.patch_hash}")
    print(f"下一步: repopilot verify {record.run_id}")
    return 0


def _verify(runtime: RepoPilotRuntime, store: PatchRunStore, args: argparse.Namespace) -> int:
    outcome = runtime.verify_task(
        args.run_id, store, actions=tuple(args.actions), force=args.force
    )
    record = store.load(args.run_id)
    print(f"Run: {record.run_id}")
    print(f"Phase: {outcome.phase.value}")
    _print_verifications(record)
    if outcome.phase is RunPhase.REVIEW:
        print(f"验证通过。下一步: repopilot review {record.run_id}")
        return 0
    print(f"验证失败: {record.state.get('error')}")
    return 1


def _repair(runtime: RepoPilotRuntime, store: PatchRunStore, args: argparse.Namespace) -> int:
    runtime.repair_task(args.run_id, store)
    outcome = runtime.verify_task(args.run_id, store)
    record = store.load(args.run_id)
    print(f"Run: {record.run_id}")
    print(f"Phase: {outcome.phase.value}")
    _print_verifications(record)
    if outcome.phase is RunPhase.REVIEW:
        print(f"修复后验证通过。下一步: repopilot review {record.run_id}")
        return 0
    print(f"修复后验证仍未通过: {record.state.get('error')}")
    return 1


def _review(runtime: RepoPilotRuntime, store: PatchRunStore, args: argparse.Namespace) -> int:
    report = runtime.review_task(args.run_id, store)
    record = store.load(args.run_id)
    print(f"Run: {record.run_id}")
    print(f"Phase: {record.state.get('phase')}")
    print(f"审查结论: {report.decision.value}")
    print(f"摘要: {report.summary}")
    for finding in report.findings:
        location = f"{finding.file}:{finding.line}" if finding.line else (finding.file or "-")
        print(f"  [{finding.severity}] {location} {finding.message}")
    return 0 if report.decision is ReviewDecision.APPROVE else 1


def _report(runtime: RepoPilotRuntime, store: PatchRunStore, args: argparse.Namespace) -> int:
    report = runtime.report_task(args.run_id, store)
    if args.format == "json":
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report), end="")
    return 0


def _resume(runtime: RepoPilotRuntime, store: PatchRunStore, args: argparse.Namespace) -> int:
    record, outcome = runtime.resume_task(args.run_id, store)
    phase = str(record.state.get("phase", "?"))
    print(f"Run: {record.run_id}")
    print(f"Phase: {phase}")
    if outcome is not None:
        _print_verifications(record)
    if phase == RunPhase.WAIT_APPROVAL.value:
        print(f"下一步: repopilot diff {record.run_id}")
        print(f"批准并应用: repopilot apply {record.run_id}")
    elif phase == RunPhase.REVIEW.value:
        print(f"下一步: repopilot review {record.run_id}")
    elif phase == RunPhase.REPAIR.value:
        print(f"下一步: repopilot repair {record.run_id}")
    elif phase == RunPhase.DONE.value:
        print("任务已完成。")
    elif phase in {RunPhase.FAILED.value, RunPhase.CANCELLED.value}:
        print(f"任务已结束，停止原因: {record.state.get('stop_reason')}")
    else:
        print("当前阶段没有可自动继续的步骤。")
    return 0 if phase not in {RunPhase.FAILED.value, RunPhase.CANCELLED.value} else 1


def _print_verifications(record: PatchRunRecord) -> None:
    history = record.state.get("verification") or []
    if not history:
        return
    print("验证历史:")
    results = [VerificationResult.from_dict(item) for item in history]
    for line in render_verification_history(results):
        print(f"  {line}")


def _list(store: PatchRunStore) -> int:
    records = store.list_records()
    if not records:
        print("没有找到运行台账。")
        return 0
    print(f"{'RUN':<34}{'PHASE':<14}{'STATUS':<10}GOAL")
    for record in records:
        phase = str(record.state.get("phase", "?"))
        goal = record.goal
        if len(goal) > 40:
            goal = goal[:39] + "…"
        print(f"{record.run_id:<34}{phase:<14}{record.status:<10}{goal}")
    return 0


def _run_status(record: PatchRunRecord) -> int:
    state = record.state
    print(f"Run: {record.run_id}")
    print(f"Repository: {record.repository}")
    print(f"Phase: {state.get('phase', '?')}")
    print(f"Status: {record.status}")
    print(f"Approval: {state.get('approval', '?')}")
    plan = state.get("plan")
    print(f"Plan: {plan.get('summary') if plan else '未生成'}")
    if record.receipt is not None:
        print(f"Patch SHA-256: {record.receipt.patch_hash}")
        print(f"Applied at: {record.receipt.applied_at}")
    history = state.get("verification") or []
    passed = sum(1 for item in history if VerificationResult.from_dict(item).passed)
    print(f"Verification: {len(history)} 条（通过 {passed}）")
    if state.get("error"):
        print(f"Error: {state['error']}")
    return 0


def _undo(record: PatchRunRecord) -> int:
    print(f"Run: {record.run_id}")
    print(f"Status: {record.status}")
    print(f"已逐字节恢复: {', '.join(record.proposal.touched_files)}")
    return 0


def _commit(record: PatchRunRecord) -> int:
    assert record.commit is not None
    print(f"Run: {record.run_id}")
    print(f"Status: {record.status}")
    print(f"提交: {record.commit['sha']}")
    print(f"文件: {', '.join(record.commit['files'])}")
    print(f"说明: {record.commit['message'].splitlines()[0]}")
    return 0


def _status(runtime: RepoPilotRuntime) -> int:
    entries = runtime.git.status_porcelain()
    print(f"Repository: {runtime.workspace.root}")
    print(f"Branch: {runtime.git.current_branch()}")
    print(f"Working tree entries: {len(entries)}")
    for entry in entries[:50]:
        print(f"  {entry}")
    if len(entries) > 50:
        print(f"  ... {len(entries) - 50} more")
    return 0


def _task(runtime: RepoPilotRuntime, goal: str, *, as_json: bool) -> int:
    state = runtime.start_task(goal)
    if as_json:
        print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Run: {state.run_id}")
        print(f"Repository: {state.task.repository}")
        print(f"Phase: {state.phase.value}")
        print(f"Goal: {state.task.goal}")
        print("Next: run `repopilot plan` to execute the read-only Explorer and Planner.")
    return 0


def _plan(runtime: RepoPilotRuntime, goal: str, *, as_json: bool) -> int:
    state = runtime.plan_task(goal)
    if as_json:
        print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
    elif state.phase is RunPhase.WAIT_APPROVAL and state.plan is not None:
        print(f"Run: {state.run_id}")
        print(f"Phase: {state.phase.value}")
        print(f"计划摘要: {state.plan.summary}")
        for index, step in enumerate(state.plan.steps, start=1):
            print(f"{index}. {step.description}")
            print(f"   文件: {', '.join(step.files)}")
            if step.verification:
                print(f"   建议验证: {', '.join(step.verification)}")
        if state.plan.risks:
            print("风险:")
            for risk in state.plan.risks:
                print(f"- {risk}")
        print(f"证据: {len(state.evidence)} 条；已读取文件: {', '.join(state.inspected_files)}")
        print("P0 为只读阶段，未修改文件。")
    else:
        print(f"规划失败: {state.error}", file=sys.stderr)
    return 0 if state.phase is RunPhase.WAIT_APPROVAL else 1


if __name__ == "__main__":
    raise SystemExit(main())
