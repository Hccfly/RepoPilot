"""Interactive REPL dashboard. Standard library only; every write still needs approval."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import ReviewDecision, RunPhase, RunState, VerificationResult
from .git import GitError
from .llm import ModelNotConfigured
from .patching import PatchError, PatchRunRecord
from .persistence import PatchRunStore, RunStoreError, open_patch_store
from .planning import PlanningError
from .providers import ProviderError, ProviderRegistry, render_provider_table
from .repair import RepairError
from .report import render_markdown, render_verification_history
from .review import ReviewError
from .runtime import RepoPilotRuntime, build_runtime
from .sandbox import SandboxError
from .verification import VerificationError
from .workspace import WorkspaceError

HELP_TEXT = """
可用命令（大小写不敏感，可加前导 /）:
  help | /help             显示本帮助
  quit | exit              退出
  clear                    清屏
  open <路径>               切换目标仓库
  doctor                   检查环境、Provider 与仓库
  providers                列出模型 Provider 与就绪状态
  list                     列出运行台账
  show <run_id>            显示阶段、文件与验证摘要
  diff <run_id>            显示已保存的 unified diff
  plan <目标>               只读探索并生成计划
  propose <目标>            生成计划并保存待审批补丁
  apply <run_id>           确认后应用补丁
  undo <run_id>            确认后逐字节撤销
  commit <run_id>          确认后只提交本次运行涉及的文件
  verify <run_id> [动作...]  运行白名单验证，失败不进入 DONE
  repair <run_id>          执行一次定向修复并重新验证
  review <run_id>          只读审查，决定是否进入 DONE
  report <run_id> [json]   输出运行报告

写操作一律需要你输入 y 确认；TUI 不会绕过策略层。
""".strip()

_ANSI = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}
_HANDLED_ERRORS = (
    WorkspaceError,
    GitError,
    ModelNotConfigured,
    PatchError,
    RunStoreError,
    VerificationError,
    SandboxError,
    ProviderError,
    PlanningError,
    RepairError,
    ReviewError,
    OSError,
    ValueError,
)


@dataclass(slots=True)
class TuiSession:
    repository: Path
    config_path: Path | None = None
    provider: str | None = None
    runs_dir: str | Path | None = None
    store_kind: str = "sqlite"
    reader: Callable[[str], str] = input
    writer: Callable[[str], None] = print
    color: bool = field(
        default_factory=lambda: sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    )
    runtime_factory: Callable[[Path], RepoPilotRuntime] | None = None
    _runtime: RepoPilotRuntime | None = field(default=None, init=False)
    _store: PatchRunStore | None = field(default=None, init=False)
    _running: bool = field(default=True, init=False)

    @property
    def store(self) -> PatchRunStore:
        if self._store is None:
            self._store = open_patch_store(self.store_kind, self.runs_dir)
        return self._store

    @property
    def runtime(self) -> RepoPilotRuntime:
        if self._runtime is None:
            self._runtime = self._build(self.repository)
        return self._runtime

    def run(self) -> int:
        self._banner()
        while self._running:
            try:
                line = self.reader("repopilot> ")
            except (EOFError, KeyboardInterrupt):
                self.writer("")
                break
            self.execute(line)
        self.writer("再见。")
        return 0

    def execute(self, line: str) -> int:
        parts = line.strip().split(None, 1)
        if not parts:
            return 0
        verb = parts[0].lstrip("/").lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        handler = self._handlers().get(verb)
        if handler is None:
            return self._error(f"未知命令: {verb}（输入 /help 查看命令）")
        try:
            return handler(rest)
        except _HANDLED_ERRORS as exc:
            return self._error(f"{type(exc).__name__}: {exc}")

    def _handlers(self) -> dict[str, Callable[[str], int]]:
        return {
            "help": self._help,
            "?": self._help,
            "quit": self._quit,
            "exit": self._quit,
            "clear": self._clear,
            "open": self._open,
            "doctor": self._doctor,
            "providers": self._providers,
            "list": self._list,
            "show": self._show,
            "status": self._show,
            "diff": self._diff,
            "plan": self._plan,
            "propose": self._propose,
            "apply": self._apply,
            "undo": self._undo,
            "commit": self._commit,
            "verify": self._verify,
            "repair": self._repair,
            "review": self._review,
            "report": self._report,
        }

    def _help(self, rest: str = "") -> int:
        del rest
        self.writer(HELP_TEXT)
        return 0

    def _quit(self, rest: str = "") -> int:
        del rest
        self._running = False
        return 0

    def _clear(self, rest: str = "") -> int:
        del rest
        if self.color:
            self.writer("\x1b[2J\x1b[H")
        return 0

    def _open(self, rest: str) -> int:
        if not rest:
            raise ValueError("用法: open <仓库路径>")
        candidate = Path(rest).expanduser().resolve()
        runtime = self._build(candidate)
        self.repository = candidate
        self._runtime = runtime
        self.writer(
            self._styled(
                f"已切换到 {runtime.workspace.root}（{runtime.git.current_branch()}）", "green"
            )
        )
        return 0

    def _doctor(self, rest: str = "") -> int:
        del rest
        runtime = self.runtime
        self.writer(f"仓库: {runtime.workspace.root}")
        self.writer(f"分支: {runtime.git.current_branch()}")
        model = runtime.config.model
        self.writer(f"Provider: {model.provider} / {model.name or '(provider 默认)'}")
        for info in runtime.providers.describe():
            if info.name == model.provider:
                state = "ready" if info.ready else f"not ready — {info.detail}"
                self.writer(f"Provider 状态: {state}")
        self.writer(f"工具: {', '.join(runtime.tools.names())}")
        return 0

    def _providers(self, rest: str = "") -> int:
        del rest
        registry = ProviderRegistry.default()
        for line in render_provider_table(registry.describe()):
            self.writer(line)
        for error in registry.errors:
            self.writer(self._styled(f"插件加载失败: {error}", "yellow"))
        return 0

    def _list(self, rest: str = "") -> int:
        del rest
        records = self.store.list_records()
        if not records:
            self.writer("没有找到运行台账。")
            return 0
        for record in records:
            phase = str(record.state.get("phase", "?"))
            goal = record.goal if len(record.goal) <= 40 else record.goal[:39] + "…"
            self.writer(f"{record.run_id}  {phase:<14}{record.status:<10}{goal}")
        return 0

    def _show(self, rest: str) -> int:
        record = self.store.load(self._run_id(rest, "show <run_id>"))
        state = RunState.from_dict(record.state)
        passed = sum(1 for item in state.verification if item.passed)
        rows = [
            f"目标: {record.goal}",
            f"阶段: {state.phase.value}    停止原因: {state.stop_reason or '-'}",
            f"状态: {record.status}    审批: {state.approval.value}",
            f"文件: {', '.join(record.proposal.touched_files) or '-'}",
            f"验证: {len(state.verification)} 条（通过 {passed}）",
        ]
        if state.error:
            rows.append(f"错误: {state.error}")
        if record.receipt is not None:
            rows.append(
                f"补丁: {record.receipt.patch_hash[:12]}…  应用时间: {record.receipt.applied_at}"
            )
        rows.append(f"下一步: {_next_step(state.phase, record.run_id)}")
        self._panel(f"Run {record.run_id}", rows)
        return 0

    def _diff(self, rest: str) -> int:
        record = self.store.load(self._run_id(rest, "diff <run_id>"))
        self._panel(f"Run {record.run_id}", [record.proposal.rationale])
        for proposal in record.all_diffs:
            for line in proposal.unified_diff.splitlines():
                self.writer(self._diff_line(line))
        return 0

    def _plan(self, rest: str) -> int:
        goal = rest.strip()
        if not goal:
            raise ValueError("用法: plan <目标>")
        state = self.runtime.plan_task(goal)
        if state.phase is not RunPhase.WAIT_APPROVAL or state.plan is None:
            self.writer(self._styled(f"规划失败: {state.error}", "red"))
            return 1
        steps = [
            f"{index}. {step.description}"
            for index, step in enumerate(state.plan.steps, start=1)
        ]
        self._panel("计划", [f"Run: {state.run_id}", state.plan.summary, *steps])
        self.writer("下一步: propose <目标> 生成补丁")
        return 0

    def _propose(self, rest: str) -> int:
        goal = rest.strip()
        if not goal:
            raise ValueError("用法: propose <目标>")
        record = self.runtime.propose_task(goal, self.store)
        self._panel(
            f"Run {record.run_id}",
            [
                f"状态: {record.status}",
                f"文件: {', '.join(record.proposal.touched_files)}",
                f"说明: {record.proposal.rationale}",
            ],
        )
        self.writer(f"下一步: diff {record.run_id}  然后 apply {record.run_id}")
        return 0

    def _apply(self, rest: str) -> int:
        run_id = self._run_id(rest, "apply <run_id>")
        record = self.store.load(run_id)
        files = ", ".join(record.proposal.touched_files)
        if not self._confirm(f"应用补丁到 {files}?"):
            self.writer(self._styled("已取消。", "dim"))
            return 0
        applied = self.runtime.patch_lifecycle(self.store).apply(run_id, approved=True)
        assert applied.receipt is not None
        self.writer(self._styled(f"已应用: {', '.join(applied.receipt.touched_files)}", "green"))
        self.writer(f"下一步: verify {run_id}")
        return 0

    def _undo(self, rest: str) -> int:
        run_id = self._run_id(rest, "undo <run_id>")
        record = self.store.load(run_id)
        files = ", ".join(record.proposal.touched_files)
        if not self._confirm(f"撤销补丁并恢复 {files}?"):
            self.writer(self._styled("已取消。", "dim"))
            return 0
        undone = self.runtime.patch_lifecycle(self.store).undo(run_id, approved=True)
        self.writer(self._styled(f"已恢复: {', '.join(undone.proposal.touched_files)}", "green"))
        return 0

    def _commit(self, rest: str) -> int:
        run_id = self._run_id(rest, "commit <run_id>")
        record = self.store.load(run_id)
        files = ", ".join(record.proposal.touched_files)
        if not self._confirm(f"提交 {files} 到当前分支?"):
            self.writer(self._styled("已取消。", "dim"))
            return 0
        committed = self.runtime.patch_lifecycle(self.store).commit(run_id, approved=True)
        assert committed.commit is not None
        self.writer(
            self._styled(f"已提交 {committed.commit['sha'][:12]}: {files}", "green")
        )
        return 0

    def _verify(self, rest: str) -> int:
        parts = rest.split()
        if not parts:
            raise ValueError("用法: verify <run_id> [动作...]")
        run_id, actions = parts[0], tuple(parts[1:])
        outcome = self.runtime.verify_task(run_id, self.store, actions=actions)
        self.writer(f"阶段: {outcome.phase.value}")
        self._print_verifications(self.store.load(run_id))
        if outcome.phase is RunPhase.REVIEW:
            self.writer(self._styled(f"验证通过。下一步: review {run_id}", "green"))
            return 0
        self.writer(self._styled(f"验证失败。下一步: repair {run_id}", "red"))
        return 1

    def _repair(self, rest: str) -> int:
        run_id = self._run_id(rest, "repair <run_id>")
        self.runtime.repair_task(run_id, self.store)
        outcome = self.runtime.verify_task(run_id, self.store)
        self.writer(f"阶段: {outcome.phase.value}")
        self._print_verifications(self.store.load(run_id))
        if outcome.phase is RunPhase.REVIEW:
            self.writer(self._styled(f"修复后验证通过。下一步: review {run_id}", "green"))
            return 0
        self.writer(self._styled(f"修复后验证仍未通过: {run_id}", "red"))
        return 1

    def _review(self, rest: str) -> int:
        run_id = self._run_id(rest, "review <run_id>")
        report = self.runtime.review_task(run_id, self.store)
        self.writer(f"审查结论: {report.decision.value}")
        self.writer(f"摘要: {report.summary}")
        for finding in report.findings:
            location = f"{finding.file}:{finding.line}" if finding.line else (finding.file or "-")
            self.writer(f"  [{finding.severity}] {location} {finding.message}")
        return 0 if report.decision is ReviewDecision.APPROVE else 1

    def _report(self, rest: str) -> int:
        parts = rest.split()
        if not parts:
            raise ValueError("用法: report <run_id> [markdown|json]")
        run_id = parts[0]
        fmt = parts[1].lower() if len(parts) > 1 else "markdown"
        report = self.runtime.report_task(run_id, self.store)
        if fmt == "json":
            self.writer(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        elif fmt == "markdown":
            for line in render_markdown(report).rstrip().splitlines():
                self.writer(line)
        else:
            raise ValueError(f"未知的报告格式: {fmt}")
        return 0

    def _print_verifications(self, record: PatchRunRecord) -> None:
        history = record.state.get("verification") or []
        if not history:
            return
        self.writer("验证历史:")
        results = [VerificationResult.from_dict(item) for item in history]
        for line in render_verification_history(results):
            self.writer(f"  {line}")

    def _build(self, repository: Path) -> RepoPilotRuntime:
        if self.runtime_factory is not None:
            return self.runtime_factory(repository)
        return build_runtime(
            repository, config_path=self.config_path, model_provider=self.provider
        )

    def _banner(self) -> None:
        self.writer(self._styled("RepoPilot TUI", "bold"))
        try:
            runtime = self.runtime
        except _HANDLED_ERRORS as exc:
            self.writer(self._styled(f"当前目录不是可用仓库: {exc}", "yellow"))
            self.writer("用 /open <路径> 切换目标仓库。")
        else:
            self.writer(f"仓库: {runtime.workspace.root}（{runtime.git.current_branch()}）")
            model = runtime.config.model
            self.writer(f"模型: {model.provider} / {model.name or '(provider 默认)'}")
        self.writer("输入 /help 查看命令，/quit 退出。")

    def _confirm(self, prompt: str) -> bool:
        try:
            answer = self.reader(f"{prompt} [y/N] ")
        except (EOFError, KeyboardInterrupt):
            self.writer("")
            return False
        return answer.strip().lower() in {"y", "yes"}

    def _run_id(self, rest: str, usage: str) -> str:
        parts = rest.split()
        if not parts:
            raise ValueError(f"用法: {usage}")
        return parts[0]

    def _panel(self, title: str, rows: list[str]) -> None:
        self.writer(self._styled(f"── {title} ──", "cyan"))
        for row in rows:
            self.writer(f"  {row}")

    def _diff_line(self, line: str) -> str:
        if line.startswith(("+++", "---")):
            return self._styled(line, "bold")
        if line.startswith("@@"):
            return self._styled(line, "cyan")
        if line.startswith("+"):
            return self._styled(line, "green")
        if line.startswith("-"):
            return self._styled(line, "red")
        return line

    def _styled(self, text: str, style: str) -> str:
        if not self.color:
            return text
        return f"\x1b[{_ANSI[style]}m{text}\x1b[0m"

    def _error(self, message: str) -> int:
        self.writer(self._styled(f"错误: {message}", "red"))
        return 1


def _next_step(phase: RunPhase, run_id: str) -> str:
    mapping = {
        RunPhase.WAIT_APPROVAL: f"diff {run_id}，然后 apply {run_id}",
        RunPhase.VERIFY: f"verify {run_id}",
        RunPhase.REPAIR: f"repair {run_id}",
        RunPhase.REVIEW: f"review {run_id}",
        RunPhase.DONE: "已完成",
        RunPhase.FAILED: f"已失败，可查看 report {run_id}",
        RunPhase.CANCELLED: "已取消",
    }
    return mapping.get(phase, f"继续 {run_id}")


def run_tui(args) -> int:
    session = TuiSession(
        repository=Path(args.repo),
        config_path=Path(args.config) if args.config else None,
        provider=getattr(args, "provider", None),
        runs_dir=getattr(args, "runs_dir", None),
        store_kind=getattr(args, "store", "sqlite"),
    )
    return session.run()
