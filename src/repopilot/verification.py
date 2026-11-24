"""P2 verification whitelist, argv-only execution, and the VERIFY/REPAIR loop."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .concurrency import run_ordered
from .contracts import RunPhase, RunState, StopReason, VerificationResult
from .patching import PatchRunRecord, PatchStatus
from .persistence import PatchRunStore
from .policy import Action, Effect, PermissionPolicy
from .workflow import WorkflowEngine


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VerificationAction:
    """One whitelisted verification command. The argv tuple is fixed by code, never by the model."""

    name: str
    description: str
    project: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("verification argv must not be empty")


class VerificationRunner(Protocol):
    def run(self, action: VerificationAction) -> VerificationResult: ...


_SECRET_ENV_KEYS = ("DEEPSEEK_API_KEY",)
_PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "tox.ini")


def detect_projects(root: Path) -> tuple[str, ...]:
    """Detect the project kinds a repository declares, without executing anything from it."""
    root = Path(root)
    kinds: list[str] = []
    if _looks_like_python(root):
        kinds.append("python")
    if (root / "package.json").is_file():
        kinds.append("node")
    return tuple(kinds)


def _looks_like_python(root: Path) -> bool:
    if any((root / marker).is_file() for marker in _PYTHON_MARKERS):
        return True
    tests = root / "tests"
    return tests.is_dir() and any(tests.glob("*.py"))


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class VerificationWhitelist:
    """The closed set of verification actions available for one repository."""

    actions: tuple[VerificationAction, ...]

    def names(self) -> tuple[str, ...]:
        return tuple(action.name for action in self.actions)

    def resolve(self, name: str) -> VerificationAction:
        for action in self.actions:
            if action.name == name:
                return action
        available = ", ".join(self.names()) or "无"
        raise VerificationError(f"未知或不可用的验证动作: {name}（可用: {available}）")

    def __bool__(self) -> bool:
        return bool(self.actions)

    @classmethod
    def discover(
        cls,
        root: Path,
        *,
        python_executable: str = sys.executable,
        which: Callable[[str], str | None] = shutil.which,
        module_available: Callable[[str], bool] = _module_available,
    ) -> VerificationWhitelist:
        root = Path(root)
        projects = detect_projects(root)
        actions: list[VerificationAction] = []
        if "python" in projects:
            if module_available("pytest"):
                actions.append(
                    VerificationAction(
                        "pytest",
                        "运行 pytest 测试套件",
                        "python",
                        (python_executable, "-m", "pytest", "-q"),
                    )
                )
            if module_available("ruff"):
                actions.append(
                    VerificationAction(
                        "ruff",
                        "使用 ruff 做静态检查",
                        "python",
                        (python_executable, "-m", "ruff", "check", "."),
                    )
                )
        if "node" in projects:
            npm = which("npm")
            scripts = _node_scripts(root / "package.json")
            if npm and "test" in scripts:
                actions.append(
                    VerificationAction("npm-test", "运行 npm test", "node", (npm, "test"))
                )
            if npm and "lint" in scripts:
                actions.append(
                    VerificationAction(
                        "npm-lint", "运行 npm run lint", "node", (npm, "run", "lint")
                    )
                )
        return cls(tuple(actions))


def _node_scripts(package_json: Path) -> dict[str, str]:
    if not package_json.is_file():
        return {}
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items()}


@dataclass(frozen=True, slots=True)
class SubprocessVerificationRunner:
    """Runs a whitelisted argv in the workspace root. No shell, no interpolation.

    ``prefix`` lets a sandbox backend wrap the action (for example a container
    runtime). The action argv is always appended as separate arguments and is
    never merged into a shell string.
    """

    workspace_root: Path
    timeout_seconds: float = 120.0
    max_output_chars: int = 8000
    prefix: tuple[str, ...] = ()

    def run(self, action: VerificationAction) -> VerificationResult:
        started = time.monotonic()
        command = [*self._prefix_argv(), *action.argv]
        try:
            completed = subprocess.run(
                command,
                cwd=self.workspace_root,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                env=sanitized_env(),
            )
        except subprocess.TimeoutExpired as exc:
            stdout, stdout_truncated = clip_output(decode_output(exc.stdout), self.max_output_chars)
            stderr, stderr_truncated = clip_output(decode_output(exc.stderr), self.max_output_chars)
            return VerificationResult(
                command=action.argv,
                exit_code=-1,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                action=action.name,
                truncated=stdout_truncated or stderr_truncated,
                duration_seconds=round(time.monotonic() - started, 3),
            )
        except OSError as exc:
            raise VerificationError(f"无法执行验证命令 {action.name}: {exc}") from exc

        stdout, stdout_truncated = clip_output(decode_output(completed.stdout), self.max_output_chars)
        stderr, stderr_truncated = clip_output(decode_output(completed.stderr), self.max_output_chars)
        return VerificationResult(
            command=action.argv,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
            action=action.name,
            truncated=stdout_truncated or stderr_truncated,
            duration_seconds=round(time.monotonic() - started, 3),
        )


    def _prefix_argv(self) -> list[str]:
        return [part.replace("{workspace}", str(self.workspace_root)) for part in self.prefix]


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    run_id: str
    phase: RunPhase
    results: tuple[VerificationResult, ...]
    skipped: tuple[str, ...]
    passed: bool


@dataclass(slots=True)
class VerificationWorkflow:
    """Drives VERIFY, persists every result, and refuses to reach DONE on failure."""

    runner: VerificationRunner
    whitelist: VerificationWhitelist
    policy: PermissionPolicy
    store: PatchRunStore
    workflow: WorkflowEngine
    max_repair_attempts: int = 1
    max_parallel_verifications: int = 2

    def verify(
        self,
        run_id: str,
        *,
        actions: Sequence[str] = (),
        force: bool = False,
        approved: bool = True,
    ) -> VerificationOutcome:
        record = self.store.load(run_id)
        if record.status != PatchStatus.APPLIED:
            raise VerificationError("只有已应用补丁的任务才能验证")
        state = RunState.from_dict(record.state)
        if state.phase in (RunPhase.DONE, RunPhase.FAILED, RunPhase.CANCELLED):
            raise VerificationError(f"任务已结束: {state.phase.value}")

        selected = self._select(actions)
        latest = {item.action: item for item in state.verification if item.action}
        pending: list[VerificationAction] = []
        skipped: list[str] = []
        for action in selected:
            prior = latest.get(action.name)
            if prior is not None and prior.passed and not force:
                skipped.append(action.name)
            else:
                pending.append(action)

        results: list[VerificationResult] = []
        for action in pending:
            self._authorize(action, approved)

        prior = list(state.verification)
        buffered: list[VerificationResult | None] = [None] * len(pending)

        def record_result(index: int, result: VerificationResult) -> None:
            buffered[index] = result
            state.verification = [*prior, *(item for item in buffered if item is not None)]
            # Persist as actions settle so a crash cannot replay finished work.
            self._persist(record, state)

        results = run_ordered(
            self._run_action,
            pending,
            max_workers=self.max_parallel_verifications,
            on_result=record_result,
        )
        for result in results:
            latest[result.action] = result

        passed = all(latest[action.name].passed for action in selected)
        self._advance(state, selected, latest, passed)
        self._persist(record, state)
        return VerificationOutcome(run_id, state.phase, tuple(results), tuple(skipped), passed)

    def _select(self, actions: Sequence[str]) -> list[VerificationAction]:
        if not actions:
            if not self.whitelist:
                raise VerificationError("没有可用的验证动作；请确认项目类型或安装 pytest/ruff")
            return list(self.whitelist.actions)
        resolved: list[VerificationAction] = []
        for name in actions:
            action = self.whitelist.resolve(name)
            if action not in resolved:
                resolved.append(action)
        return resolved

    def _run_action(self, action: VerificationAction) -> VerificationResult:
        try:
            return self.runner.run(action)
        except VerificationError as exc:
            # An unrunnable action is a failed verification, not a crash of the batch.
            return VerificationResult(
                command=action.argv, action=action.name, exit_code=-1, stderr=str(exc)
            )

    def _authorize(self, action: VerificationAction, approved: bool) -> None:
        decision = self.policy.decide(Action.VERIFY, action.name)
        if decision.effect is Effect.DENY:
            raise VerificationError(decision.reason)
        if decision.effect is Effect.ASK and not approved:
            raise VerificationError("验证动作尚未获得明确审批")

    def _advance(
        self,
        state: RunState,
        selected: Sequence[VerificationAction],
        latest: dict[str, VerificationResult],
        passed: bool,
    ) -> None:
        if passed:
            if state.phase is RunPhase.REPAIR:
                self.workflow.transition(state, RunPhase.VERIFY)
            if state.phase is RunPhase.VERIFY:
                # P3: the read-only review node decides whether REVIEW becomes DONE.
                self.workflow.transition(state, RunPhase.REVIEW)
            state.error = None
            state.stop_reason = None
            return
        if state.phase is RunPhase.VERIFY:
            self.workflow.transition(state, RunPhase.REPAIR)
        failed = sorted({action.name for action in selected if not latest[action.name].passed})
        state.error = "验证失败: " + ", ".join(failed)
        if state.phase is RunPhase.REPAIR and state.repair_attempts >= self.max_repair_attempts:
            self.workflow.transition(state, RunPhase.FAILED)
            state.stop_reason = StopReason.REPAIR_BUDGET_EXHAUSTED
        else:
            state.stop_reason = StopReason.VERIFICATION_FAILED

    def _persist(self, record: PatchRunRecord, state: RunState) -> None:
        record.state = state.to_dict()
        self.store.save(record)


def sanitized_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _SECRET_ENV_KEYS:
        env.pop(key, None)
    return env


def decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def clip_output(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True
