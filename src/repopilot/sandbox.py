"""Standalone verification sandbox service and its client.

The wire protocol carries action *names*, never argv: a client cannot ask the
service to run an arbitrary command, only one of the actions the service
resolved from its own whitelist. Requests are routed to a tenant, which owns
one workspace, one whitelist and one token.
"""

from __future__ import annotations

import atexit
import hmac
import json
import os
import queue
import shutil
import socket
import socketserver
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .config import SandboxSettings
from .contracts import VerificationResult
from .sandbox_audit import AuditRecord, SandboxLedger
from .verification import (
    SubprocessVerificationRunner,
    VerificationAction,
    VerificationError,
    VerificationRunner,
    VerificationWhitelist,
    clip_output,
    decode_output,
    sanitized_env,
)

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = 900.0
DEFAULT_QUOTA_WINDOW_SECONDS = 3600
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class SandboxError(VerificationError):
    pass


@dataclass(slots=True)
class SandboxService:
    """Executes one whitelisted action per request. Client-supplied argv is never used."""

    workspace_root: Path
    whitelist: VerificationWhitelist
    token: str = ""
    max_timeout_seconds: float = MAX_TIMEOUT_SECONDS
    max_output_chars: int = 8000

    def execute(self, request: dict) -> dict:
        if not self.authorized(request):
            return {"ok": False, "error": "unauthorized"}
        name = request.get("action")
        if not isinstance(name, str) or not name.strip():
            return {"ok": False, "error": "action 必须是非空字符串"}
        try:
            action = self.whitelist.resolve(name.strip())
        except VerificationError as exc:
            return {"ok": False, "error": str(exc)}
        result = self._runner(self._timeout(request)).run(action)
        return {"ok": True, **result.to_dict()}

    def authorized(self, request: dict) -> bool:
        if not self.token:
            return True
        supplied = request.get("token")
        if not isinstance(supplied, str):
            return False
        return hmac.compare_digest(supplied, self.token)

    def _runner(self, timeout_seconds: float) -> SubprocessVerificationRunner:
        return SubprocessVerificationRunner(
            self.workspace_root,
            timeout_seconds=timeout_seconds,
            max_output_chars=self.max_output_chars,
        )

    def _timeout(self, request: dict) -> float:
        raw = request.get("timeout_seconds", self.max_timeout_seconds)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return self.max_timeout_seconds
        return max(1.0, min(value, self.max_timeout_seconds))


@dataclass(frozen=True, slots=True)
class Tenant:
    name: str
    service: SandboxService
    max_concurrency: int = 1
    requests_per_window: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tenant name must not be empty")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if self.requests_per_window < 0:
            raise ValueError("requests_per_window must be >= 0")


class TenantRegistry:
    def __init__(self, tenants: Iterable[Tenant]) -> None:
        self._tenants = {tenant.name: tenant for tenant in tenants}
        if not self._tenants:
            raise SandboxError("至少需要一个租户")

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tenants))

    def get(self, name: str) -> Tenant:
        try:
            return self._tenants[name]
        except KeyError:
            available = ", ".join(self.names())
            raise SandboxError(f"未知租户: {name}（可用: {available}）") from None


class SandboxScheduler:
    """Bounds concurrent executions globally and per tenant."""

    def __init__(
        self,
        registry: TenantRegistry,
        *,
        max_total_concurrency: int = 4,
        queue_timeout_seconds: float = 5.0,
        ledger: SandboxLedger | None = None,
        quota_window_seconds: int = DEFAULT_QUOTA_WINDOW_SECONDS,
    ) -> None:
        self.registry = registry
        self.global_gate = threading.BoundedSemaphore(max(1, max_total_concurrency))
        self.queue_timeout_seconds = queue_timeout_seconds
        self.ledger = ledger
        self.quota_window_seconds = max(1, int(quota_window_seconds))
        self._gates = {
            name: threading.BoundedSemaphore(registry.get(name).max_concurrency)
            for name in registry.names()
        }

    @property
    def gates(self) -> dict[str, threading.BoundedSemaphore]:
        return self._gates

    def execute(self, request: dict, *, remote: str = "") -> dict:
        started = time.monotonic()
        try:
            response = self._dispatch(request)
        except Exception as exc:  # noqa: BLE001
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._audit(request, response, started, remote)
        return response

    def _dispatch(self, request: dict) -> dict:
        name = request.get("tenant", "default")
        if not isinstance(name, str) or not name.strip():
            return {"ok": False, "error": "tenant 必须是非空字符串"}
        try:
            tenant = self.registry.get(name.strip())
        except SandboxError as exc:
            return {"ok": False, "error": str(exc)}
        if not tenant.service.authorized(request):
            return {"ok": False, "error": "unauthorized"}
        if self.ledger is not None and tenant.requests_per_window > 0:
            decision = self.ledger.reserve(
                tenant.name, tenant.requests_per_window, self.quota_window_seconds
            )
            if not decision.allowed:
                return {
                    "ok": False,
                    "error": (
                        f"quota_exceeded: 已用 {decision.used}/{decision.limit}，"
                        f"{decision.reset_at} 重置"
                    ),
                }
        if not self.global_gate.acquire(timeout=self.queue_timeout_seconds):
            return {"ok": False, "error": "busy"}
        try:
            gate = self._gates[tenant.name]
            if not gate.acquire(timeout=self.queue_timeout_seconds):
                return {"ok": False, "error": "busy"}
            try:
                return tenant.service.execute(request)
            finally:
                gate.release()
        finally:
            self.global_gate.release()

    def _audit(
        self, request: dict, response: dict, started: float, remote: str
    ) -> None:
        if self.ledger is None:
            return
        tenant = request.get("tenant", "default")
        action = request.get("action")
        exit_code = response.get("exit_code")
        try:
            self.ledger.record(
                AuditRecord(
                    timestamp=datetime.now(UTC).isoformat(),
                    tenant=tenant if isinstance(tenant, str) else "",
                    action=action if isinstance(action, str) else "",
                    ok=bool(response.get("ok")),
                    error=str(response.get("error", "")),
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                    duration_seconds=round(time.monotonic() - started, 3),
                    remote=remote,
                )
            )
        except (OSError, sqlite3.Error) as exc:
            print(f"sandbox audit write failed: {exc}", file=sys.stderr)


class _SandboxHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        scheduler: SandboxScheduler = self.server.scheduler  # type: ignore[attr-defined]
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            self._reply({"ok": False, "error": "请求过大"})
            return
        if not raw.strip():
            self._reply({"ok": False, "error": "空请求"})
            return
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply({"ok": False, "error": "请求不是合法 JSON"})
            return
        if not isinstance(request, dict):
            self._reply({"ok": False, "error": "请求必须是 JSON 对象"})
            return
        try:
            response = scheduler.execute(request, remote=self.client_address[0])
        except Exception as exc:  # noqa: BLE001
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._reply(response)

    def _reply(self, payload: dict) -> None:
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")


class SandboxServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        scheduler: SandboxScheduler,
        *,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.scheduler = scheduler
        super().__init__(address, _SandboxHandler)
        if ssl_context is not None:
            self.socket = ssl_context.wrap_socket(self.socket, server_side=True)


def server_ssl_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    if not cert_file or not key_file:
        raise SandboxError("启用 TLS 需要同时提供 cert_file 与 key_file")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return context


def client_ssl_context(ca_file: str = "") -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=ca_file or None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def create_multi_tenant_server(
    tenants: Iterable[Tenant],
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    ssl_context: ssl.SSLContext | None = None,
    max_total_concurrency: int = 4,
    queue_timeout_seconds: float = 5.0,
    ledger: SandboxLedger | None = None,
    quota_window_seconds: int = DEFAULT_QUOTA_WINDOW_SECONDS,
) -> SandboxServer:
    registry = TenantRegistry(tenants)
    if host not in _LOOPBACK:
        for name in registry.names():
            if not registry.get(name).service.token:
                raise SandboxError(f"租户 {name} 在非回环地址 {host} 上必须配置 token")
    scheduler = SandboxScheduler(
        registry,
        max_total_concurrency=max_total_concurrency,
        queue_timeout_seconds=queue_timeout_seconds,
        ledger=ledger,
        quota_window_seconds=quota_window_seconds,
    )
    return SandboxServer((host, port), scheduler, ssl_context=ssl_context)


def create_server(
    repository: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = "",
    python_executable: str | None = None,
    max_timeout_seconds: float = MAX_TIMEOUT_SECONDS,
    max_output_chars: int = 8000,
    ssl_context: ssl.SSLContext | None = None,
    ledger: SandboxLedger | None = None,
) -> SandboxServer:
    root = Path(repository).expanduser().resolve()
    if not root.is_dir():
        raise SandboxError(f"仓库目录不存在: {root}")
    whitelist = VerificationWhitelist.discover(
        root, python_executable=python_executable or sys.executable
    )
    if not whitelist:
        raise SandboxError("没有可用的验证动作；请确认沙箱环境已安装 pytest/ruff")
    service = SandboxService(
        root,
        whitelist,
        token=token,
        max_timeout_seconds=max_timeout_seconds,
        max_output_chars=max_output_chars,
    )
    return create_multi_tenant_server(
        [Tenant("default", service)],
        host=host,
        port=port,
        ssl_context=ssl_context,
        ledger=ledger,
    )


def serve_sandbox(
    repository: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    token_env: str = "REPOPILOT_SANDBOX_TOKEN",
    tenant_specs: Sequence[str] = (),
    cert_file: str = "",
    key_file: str = "",
    max_total_concurrency: int = 4,
    audit_db: str = "",
    no_audit: bool = False,
    quota: int = 0,
    quota_window: int = DEFAULT_QUOTA_WINDOW_SECONDS,
) -> int:
    tenants = _build_tenants(repository, token_env, tenant_specs, quota)
    ssl_context = (
        server_ssl_context(cert_file, key_file) if (cert_file or key_file) else None
    )
    ledger = None if no_audit else SandboxLedger(audit_db or None)
    server = create_multi_tenant_server(
        tenants,
        host=host,
        port=port,
        ssl_context=ssl_context,
        max_total_concurrency=max_total_concurrency,
        ledger=ledger,
        quota_window_seconds=quota_window,
    )
    bound_host, bound_port = server.server_address[:2]
    scheme = "tls" if ssl_context is not None else "tcp"
    print(f"Sandbox service listening on {scheme}://{bound_host}:{bound_port}")
    print(f"审计: {'关闭' if ledger is None else ledger.path}")
    print(f"配额: {'不限' if quota <= 0 else f'{quota} 次/{quota_window}s 每租户'}")
    for name in server.scheduler.registry.names():
        service = server.scheduler.registry.get(name).service
        print(f"  {name}: {service.workspace_root}  动作: {', '.join(service.whitelist.names())}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 sandbox 服务…")
    finally:
        server.server_close()
    return 0


def _build_tenants(
    repository: str | Path, token_env: str, tenant_specs: Sequence[str], quota: int = 0
) -> tuple[Tenant, ...]:
    if not tenant_specs:
        root = Path(repository).expanduser().resolve()
        return (_tenant_from_path("default", root, token_env, quota),)
    tenants: list[Tenant] = []
    for spec in tenant_specs:
        name, separator, raw_path = spec.partition("=")
        if not separator or not name.strip() or not raw_path.strip():
            raise SandboxError(f"--tenant 需要 name=path 形式: {spec}")
        tenants.append(
            _tenant_from_path(
                name.strip(), Path(raw_path).expanduser().resolve(), token_env, quota
            )
        )
    return tuple(tenants)


def _tenant_from_path(name: str, root: Path, token_env: str, quota: int = 0) -> Tenant:
    if not root.is_dir():
        raise SandboxError(f"租户 {name} 的目录不存在: {root}")
    whitelist = VerificationWhitelist.discover(root)
    if not whitelist:
        raise SandboxError(f"租户 {name} 没有可用的验证动作")
    specific = os.environ.get(f"{token_env}_{name.upper()}", "").strip()
    token = specific or (
        os.environ.get(token_env, "").strip() if name == "default" else ""
    )
    return Tenant(
        name,
        SandboxService(root, whitelist, token=token),
        requests_per_window=quota,
    )


@dataclass(frozen=True, slots=True)
class RemoteSandboxRunner:
    """Talks to a sandbox service. Only the action name is sent, never argv."""

    host: str
    port: int
    token: str = ""
    timeout_seconds: float = 120.0
    tenant: str = "default"
    tls: bool = False
    ca_file: str = ""
    server_hostname: str = ""

    def run(self, action: VerificationAction) -> VerificationResult:
        request = {
            "action": action.name,
            "token": self.token,
            "timeout_seconds": self.timeout_seconds,
            "tenant": self.tenant,
        }
        payload = self._exchange(request)
        if not payload.get("ok"):
            raise SandboxError(str(payload.get("error", "sandbox 执行失败")))
        result = VerificationResult.from_dict(payload)
        return replace(result, command=action.argv, action=action.name)

    def _exchange(self, request: dict) -> dict:
        try:
            connection = socket.create_connection(
                (self.host, self.port), timeout=self.timeout_seconds + 10
            )
            if self.tls:
                connection = client_ssl_context(self.ca_file).wrap_socket(
                    connection, server_hostname=self.server_hostname or self.host
                )
            with connection:
                connection.sendall(
                    json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
                )
                raw = _read_line(connection)
        except OSError as exc:
            raise SandboxError(
                f"无法连接 sandbox 服务 {self.host}:{self.port}: {exc}"
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxError("sandbox 返回的不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise SandboxError("sandbox 必须返回 JSON 对象")
        return payload


_CONTAINER_LABEL = "io.repopilot=1"
_PULL_POLICIES = frozenset({"never", "missing", "always"})
_DOCKER_WAIT_SECONDS = 120.0


def _validate_docker_options(
    *,
    workspace_root: Path,
    image: str,
    network: str,
    memory: str,
    cpus: str,
    user: str,
) -> None:
    if not image:
        raise SandboxError('[sandbox] mode = "docker" 时 image 不能为空')
    if not network:
        raise SandboxError("docker network 不能为空")
    for name, value in (
        ("image", image),
        ("network", network),
        ("memory", memory),
        ("cpus", cpus),
        ("user", user),
    ):
        if value.startswith("-"):
            raise SandboxError(f"docker {name} 不能以 - 开头: {value}")
    if not workspace_root.is_absolute():
        raise SandboxError(f"docker 模式需要绝对工作区路径: {workspace_root}")


def _docker_binary(explicit: str) -> str:
    binary = explicit or shutil.which("docker") or ""
    if not binary:
        raise SandboxError("找不到 docker 可执行文件")
    return binary


def ensure_image(docker_binary: str, image: str, *, policy: str = "missing") -> None:
    """Warm the local image cache. Never silent: failures surface as SandboxError."""
    if policy not in _PULL_POLICIES:
        raise SandboxError(
            f"未知的镜像拉取策略: {policy}（可用: never, missing, always）"
        )
    if policy == "never":
        return
    if policy == "missing":
        try:
            inspected = subprocess.run(
                [docker_binary, "image", "inspect", image],
                capture_output=True,
                check=False,
                timeout=_DOCKER_WAIT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxError(f"无法检查镜像 {image}: {exc}") from exc
        if inspected.returncode == 0:
            return
    try:
        pulled = subprocess.run(
            [docker_binary, "pull", image],
            capture_output=True,
            check=False,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxError(f"拉取镜像 {image} 失败: {exc}") from exc
    if pulled.returncode != 0:
        detail = decode_output(pulled.stderr).strip() or "docker pull 失败"
        raise SandboxError(f"拉取镜像 {image} 失败: {detail[-500:]}")


@dataclass(frozen=True, slots=True)
class DockerSandboxRunner:
    """Runs each action in a throwaway container with the workspace mounted at /work."""

    workspace_root: Path
    image: str
    network: str = "none"
    memory: str = ""
    cpus: str = ""
    user: str = ""
    timeout_seconds: float = 120.0
    max_output_chars: int = 8000
    docker: str = ""

    def __post_init__(self) -> None:
        _validate_docker_options(
            workspace_root=self.workspace_root,
            image=self.image,
            network=self.network,
            memory=self.memory,
            cpus=self.cpus,
            user=self.user,
        )

    def build_argv(self, action: VerificationAction, cidfile: Path) -> list[str]:
        argv = [self._docker_binary(), "run", "--rm", "--label", _CONTAINER_LABEL]
        argv.extend(["--network", self.network])
        if self.memory:
            argv.extend(["--memory", self.memory])
        if self.cpus:
            argv.extend(["--cpus", self.cpus])
        if self.user:
            argv.extend(["--user", self.user])
        argv.extend(["--cidfile", str(cidfile)])
        argv.extend(["-v", f"{self.workspace_root}:/work", "-w", "/work"])
        argv.append(self.image)
        argv.extend(action.argv)
        return argv

    def run(self, action: VerificationAction) -> VerificationResult:
        with tempfile.TemporaryDirectory() as temp:
            cidfile = Path(temp) / "container.cid"
            argv = self.build_argv(action, cidfile)
            started = time.monotonic()
            try:
                process = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=sanitized_env(),
                )
            except OSError as exc:
                raise SandboxError(f"无法启动 docker: {exc}") from exc
            timed_out = False
            try:
                out, err = process.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_container(cidfile)
                try:
                    out, err = process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    out, err = process.communicate()
            duration = round(time.monotonic() - started, 3)

        stdout, stdout_truncated = clip_output(
            decode_output(out), self.max_output_chars
        )
        stderr, stderr_truncated = clip_output(
            decode_output(err), self.max_output_chars
        )
        return VerificationResult(
            command=action.argv,
            exit_code=-1 if timed_out else process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            action=action.name,
            truncated=stdout_truncated or stderr_truncated,
            duration_seconds=duration,
        )

    def _kill_container(self, cidfile: Path) -> None:
        try:
            container_id = cidfile.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not container_id:
            return
        subprocess.run(
            [self._docker_binary(), "kill", container_id],
            capture_output=True,
            timeout=30,
            check=False,
        )

    def _docker_binary(self) -> str:
        return _docker_binary(self.docker)


class DockerContainerPool:
    """Keeps idle containers warm so container creation overlaps with other work.

    Every action still gets its own throwaway container: ``release`` destroys it
    and the background thread creates a replacement, so files written outside
    the mounted workspace never leak into the next action.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        image: str,
        network: str = "none",
        memory: str = "",
        cpus: str = "",
        user: str = "",
        size: int = 2,
        docker: str = "",
        pull: str = "missing",
        timeout_seconds: float = 120.0,
        max_output_chars: int = 8000,
    ) -> None:
        _validate_docker_options(
            workspace_root=workspace_root,
            image=image,
            network=network,
            memory=memory,
            cpus=cpus,
            user=user,
        )
        if size < 1:
            raise SandboxError("pool_size 必须大于 0")
        if pull not in _PULL_POLICIES:
            raise SandboxError(f"未知的镜像拉取策略: {pull}")
        self.workspace_root = workspace_root
        self.image = image
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.user = user
        self.size = size
        self.docker = docker
        self.pull = pull
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.label = f"io.repopilot.pool={uuid4().hex}"
        self._idle: queue.Queue[str] = queue.Queue()
        self._retired: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._created: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            ensure_image(self._docker_binary(), self.image, policy=self.pull)
            atexit.register(self.close)
            self._thread = threading.Thread(
                target=self._refill, name="repopilot-docker-pool", daemon=True
            )
            self._thread.start()

    def acquire(self, timeout: float = _DOCKER_WAIT_SECONDS) -> str:
        self.start()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return self._idle.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if self._startup_error is not None:
                    raise SandboxError(self._startup_error) from None
        raise SandboxError("容器池在超时前没有可用容器")

    def release(self, container_id: str) -> None:
        # Destruction happens on the refill thread so the caller never pays for it.
        self._retired.put(container_id)

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)
        self._drain_retired()
        while True:
            try:
                container_id = self._idle.get_nowait()
            except queue.Empty:
                break
            self._destroy(container_id)
        with self._lock:
            remaining = list(self._created)
        for container_id in remaining:
            self._destroy(container_id)

    def create_argv(self, cidfile: Path) -> list[str]:
        argv = [
            self._docker_binary(),
            "create",
            "--label",
            self.label,
            "--label",
            _CONTAINER_LABEL,
            "--network",
            self.network,
        ]
        if self.memory:
            argv.extend(["--memory", self.memory])
        if self.cpus:
            argv.extend(["--cpus", self.cpus])
        if self.user:
            argv.extend(["--user", self.user])
        argv.extend(["--cidfile", str(cidfile)])
        argv.extend(["-v", f"{self.workspace_root}:/work", "-w", "/work"])
        argv.extend([self.image, "sleep", "infinity"])
        return argv

    def exec_argv(self, container_id: str, action: VerificationAction) -> list[str]:
        return [self._docker_binary(), "exec", container_id, *action.argv]

    def _refill(self) -> None:
        while not self._stop.is_set():
            self._drain_retired()
            if self._idle.qsize() >= self.size:
                self._stop.wait(0.05)
                continue
            try:
                container_id = self._create_container()
            except SandboxError as exc:
                self._startup_error = str(exc)
                self._stop.wait(0.5)
                continue
            self._startup_error = None
            with self._lock:
                self._created.add(container_id)
            self._idle.put(container_id)

    def _drain_retired(self) -> None:
        while True:
            try:
                container_id = self._retired.get_nowait()
            except queue.Empty:
                return
            self._destroy(container_id)

    def _create_container(self) -> str:
        with tempfile.TemporaryDirectory() as temp:
            cidfile = Path(temp) / "container.cid"
            created = self._run_docker(self.create_argv(cidfile))
            if created.returncode != 0:
                detail = decode_output(created.stderr).strip() or "docker create 失败"
                raise SandboxError(detail[-500:])
            try:
                container_id = cidfile.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SandboxError(f"docker 未写出容器 ID: {exc}") from exc
        if not container_id:
            raise SandboxError("docker 返回了空的容器 ID")
        started = self._run_docker([self._docker_binary(), "start", container_id])
        if started.returncode != 0:
            self._run_docker([self._docker_binary(), "rm", "-f", container_id])
            detail = decode_output(started.stderr).strip() or "docker start 失败"
            raise SandboxError(detail[-500:])
        return container_id

    def _destroy(self, container_id: str) -> None:
        with self._lock:
            self._created.discard(container_id)
        self._run_docker([self._docker_binary(), "rm", "-f", container_id])

    def _run_docker(self, argv: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                check=False,
                timeout=_DOCKER_WAIT_SECONDS,
                env=sanitized_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxError(f"docker 调用失败: {exc}") from exc

    def _docker_binary(self) -> str:
        return _docker_binary(self.docker)


@dataclass(slots=True)
class DockerPooledSandboxRunner:
    pool: DockerContainerPool

    @property
    def pool_size(self) -> int:
        return self.pool.size

    @property
    def pull(self) -> str:
        return self.pool.pull

    def run(self, action: VerificationAction) -> VerificationResult:
        container_id = self.pool.acquire()
        try:
            return self._exec(action, container_id)
        finally:
            self.pool.release(container_id)

    def close(self) -> None:
        self.pool.close()

    def _exec(
        self, action: VerificationAction, container_id: str
    ) -> VerificationResult:
        argv = self.pool.exec_argv(container_id, action)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=sanitized_env(),
            )
        except OSError as exc:
            raise SandboxError(f"无法在容器内执行命令: {exc}") from exc
        timed_out = False
        try:
            out, err = process.communicate(timeout=self.pool.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            out, err = process.communicate()
        duration = round(time.monotonic() - started, 3)

        stdout, stdout_truncated = clip_output(
            decode_output(out), self.pool.max_output_chars
        )
        stderr, stderr_truncated = clip_output(
            decode_output(err), self.pool.max_output_chars
        )
        return VerificationResult(
            command=action.argv,
            exit_code=-1 if timed_out else process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            action=action.name,
            truncated=stdout_truncated or stderr_truncated,
            duration_seconds=duration,
        )


def _read_line(connection: socket.socket) -> bytes:
    buffer = bytearray()
    while len(buffer) <= MAX_RESPONSE_BYTES:
        chunk = connection.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)
        if chunk.endswith(b"\n"):
            break
    if len(buffer) > MAX_RESPONSE_BYTES:
        raise SandboxError("sandbox 响应过大")
    if not buffer:
        raise SandboxError("sandbox 未返回任何数据")
    return bytes(buffer)


def build_verification_runner(
    settings: SandboxSettings,
    workspace_root: Path,
    *,
    timeout_seconds: float,
    max_output_chars: int,
) -> VerificationRunner:
    if settings.mode == "local":
        return SubprocessVerificationRunner(
            workspace_root,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )
    if settings.mode == "command":
        if not settings.command:
            raise SandboxError('[sandbox] mode = "command" 时 command 不能为空')
        return SubprocessVerificationRunner(
            workspace_root,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            prefix=settings.command,
        )
    if settings.mode == "docker":
        if settings.pool_size > 0:
            return DockerPooledSandboxRunner(
                DockerContainerPool(
                    workspace_root,
                    image=settings.image,
                    network=settings.network or "none",
                    memory=settings.memory,
                    cpus=settings.cpus,
                    user=settings.user,
                    size=settings.pool_size,
                    pull=settings.pull,
                    timeout_seconds=timeout_seconds,
                    max_output_chars=max_output_chars,
                )
            )
        return DockerSandboxRunner(
            workspace_root,
            settings.image,
            network=settings.network or "none",
            memory=settings.memory,
            cpus=settings.cpus,
            user=settings.user,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )
    if settings.mode == "remote":
        token = os.environ.get(settings.token_env, "").strip()
        if not token and settings.host not in _LOOPBACK:
            raise SandboxError(f"远程沙箱需要环境变量 {settings.token_env}")
        return RemoteSandboxRunner(
            settings.host,
            settings.port,
            token=token,
            timeout_seconds=timeout_seconds,
            tenant=settings.tenant,
            tls=settings.tls,
            ca_file=settings.ca_file,
        )
    raise SandboxError(
        f"未知的沙箱模式: {settings.mode}（可用: local, command, docker, remote）"
    )
