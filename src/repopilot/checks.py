"""Behavioural checks for subsystems outside the agent workflow.

Sandbox, audit and quota are not part of propose -> apply -> verify -> review,
so they cannot be expressed as a :class:`repopilot.benchmark.Scenario`. A check
runs one concrete behaviour and reports a status; the trusted parent grades it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    description: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Check:
        params = value.get("params") or {}
        return cls(
            name=str(value["name"]),
            description=str(value.get("description", "")),
            kind=str(value["kind"]),
            params={str(key): item for key, item in params.items()},
        )


def run_check(check: Check) -> dict[str, Any]:
    """Execute one check and return ``{"check", "status", "detail"}``."""
    handler = _HANDLERS.get(check.kind)
    if handler is None:
        return _result(check, "fail", f"未知的检查类型: {check.kind}")
    try:
        return handler(check)
    except Exception as exc:  # noqa: BLE001
        return _result(check, "fail", f"{type(exc).__name__}: {exc}")


def _result(check: Check, status: str, detail: str = "") -> dict[str, Any]:
    return {"check": check.name, "status": status, "detail": detail}


def _sandbox_timeout(check: Check) -> dict[str, Any]:
    from .verification import SubprocessVerificationRunner, VerificationAction

    timeout = float(check.params.get("timeout_seconds", 0.5))
    with tempfile.TemporaryDirectory() as temp:
        runner = SubprocessVerificationRunner(Path(temp), timeout_seconds=timeout)
        action = VerificationAction(
            "sleep",
            "sleep",
            "check",
            (sys.executable, "-c", "import time; time.sleep(30)"),
        )
        result = runner.run(action)
    if not result.timed_out:
        return _result(check, "fail", "验证超时没有被报告为 timed_out")
    if result.passed:
        return _result(check, "fail", "超时结果不应判定为通过")
    if result.duration_seconds > 30:
        return _result(check, "fail", f"超时未及时终止: {result.duration_seconds:.1f}s")
    return _result(check, "ok", f"{result.duration_seconds:.2f}s 后终止")


def _sandbox_secret_isolation(check: Check) -> dict[str, Any]:
    from .verification import SubprocessVerificationRunner, VerificationAction

    key = "DEEPSEEK_API_KEY"
    marker = "check-secret-value"
    previous = os.environ.get(key)
    os.environ[key] = marker
    try:
        with tempfile.TemporaryDirectory() as temp:
            runner = SubprocessVerificationRunner(Path(temp), timeout_seconds=30)
            action = VerificationAction(
                "env",
                "env",
                "check",
                (sys.executable, "-c", "import os;print(os.environ.get('DEEPSEEK_API_KEY','absent'))"),
            )
            result = runner.run(action)
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous
    if marker in result.stdout:
        return _result(check, "fail", "验证子进程读到了 API Key")
    if "absent" not in result.stdout:
        return _result(check, "fail", f"意外的子进程输出: {result.stdout.strip()[:80]}")
    return _result(check, "ok", "子进程环境已剥离密钥")


def _docker_network_isolation(check: Check) -> dict[str, Any]:
    from .sandbox import DockerSandboxRunner
    from .verification import VerificationAction

    docker = shutil.which("docker")
    image = str(check.params.get("image", "python:3.12-slim"))
    if not docker:
        return _result(check, "skip", "docker 不可用")
    try:
        inspected = subprocess.run(
            [docker, "image", "inspect", image], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _result(check, "skip", f"无法检查镜像: {exc}")
    if inspected.returncode != 0:
        return _result(check, "skip", f"缺少本地镜像 {image}")
    with tempfile.TemporaryDirectory() as temp:
        runner = DockerSandboxRunner(Path(temp), image=image, timeout_seconds=60)
        action = VerificationAction(
            "net",
            "net",
            "check",
            (
                "python",
                "-c",
                "import socket;socket.create_connection(('1.1.1.1',80),timeout=5);print('online')",
            ),
        )
        result = runner.run(action)
    if "online" in result.stdout:
        return _result(check, "fail", "容器内可以访问外网，--network none 未生效")
    return _result(check, "ok", "容器内网络不可达")


def _audit_hides_token(check: Check) -> dict[str, Any]:
    from .sandbox import SandboxScheduler, SandboxService, Tenant, TenantRegistry
    from .sandbox_audit import SandboxLedger
    from .verification import VerificationAction, VerificationWhitelist

    token = "check-secret-token"
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        db = root / "sandbox.db"
        ledger = SandboxLedger(db)
        action = VerificationAction(
            "probe", "probe", "check", (sys.executable, "-c", "print('ok')")
        )
        service = SandboxService(root, VerificationWhitelist((action,)), token=token)
        scheduler = SandboxScheduler(
            TenantRegistry([Tenant("default", service)]), ledger=ledger
        )
        authorized = scheduler.execute(
            {"action": "probe", "token": token}, remote="127.0.0.1"
        )
        denied = scheduler.execute(
            {"action": "probe", "token": "wrong"}, remote="127.0.0.1"
        )
        blob = db.read_bytes()
        records = ledger.recent(5)

    if not authorized.get("ok"):
        return _result(check, "fail", "授权请求没有成功")
    if denied.get("ok"):
        return _result(check, "fail", "错误 token 竟然被放行")
    if len(records) < 2:
        return _result(check, "fail", "审计记录缺失")
    if token.encode("utf-8") in blob:
        return _result(check, "fail", "审计数据库中出现明文 token")
    return _result(check, "ok", f"{len(records)} 条审计记录，不含 token")


def _quota_window_rollover(check: Check) -> dict[str, Any]:
    from .sandbox_audit import SandboxLedger

    window = int(check.params.get("window_seconds", 60))
    with tempfile.TemporaryDirectory() as temp:
        ledger = SandboxLedger(Path(temp) / "sandbox.db")
        first = ledger.reserve("default", 1, window, now=100.0)
        blocked = ledger.reserve("default", 1, window, now=110.0)
        rolled = ledger.reserve("default", 1, window, now=200.0)

    if not first.allowed:
        return _result(check, "fail", "窗口内首次请求被拒")
    if blocked.allowed:
        return _result(check, "fail", "同一窗口内超过配额仍被放行")
    if not rolled.allowed:
        return _result(check, "fail", "窗口滚落后额度没有恢复")
    return _result(check, "ok", "配额按窗口滚动恢复")


_HANDLERS: dict[str, Callable[[Check], dict[str, Any]]] = {
    "sandbox_timeout": _sandbox_timeout,
    "sandbox_secret_isolation": _sandbox_secret_isolation,
    "docker_network_isolation": _docker_network_isolation,
    "audit_hides_token": _audit_hides_token,
    "quota_window_rollover": _quota_window_rollover,
}


BUILTIN_CHECKS: tuple[Check, ...] = (
    Check(
        name="sandbox_timeout_kills",
        description="验证超时会终止子进程并报告 timed_out",
        kind="sandbox_timeout",
    ),
    Check(
        name="sandbox_secret_isolation",
        description="验证子进程环境不含 API Key",
        kind="sandbox_secret_isolation",
    ),
    Check(
        name="docker_network_isolation",
        description="Docker 后端默认网络隔离（无 docker/镜像时跳过）",
        kind="docker_network_isolation",
    ),
    Check(
        name="audit_hides_token",
        description="审计记录落盘但从不包含 token",
        kind="audit_hides_token",
    ),
    Check(
        name="quota_window_rollover",
        description="配额按固定窗口滚动恢复",
        kind="quota_window_rollover",
    ),
)
