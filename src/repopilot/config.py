"""Configuration loading with safe, dependency-free defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"


@dataclass(frozen=True, slots=True)
class WorkspaceSettings:
    max_file_bytes: int = 1_000_000
    max_search_results: int = 100
    max_inventory_files: int = 500
    max_context_files: int = 8
    max_context_chars: int = 6000
    max_parallel_reads: int = 4


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    command_timeout_seconds: float = 120.0
    max_repair_attempts: int = 1
    max_output_chars: int = 8000
    max_parallel_verifications: int = 2


@dataclass(frozen=True, slots=True)
class PermissionSettings:
    read: str = "allow"
    edit: str = "ask"
    verify: str = "ask"
    git_write: str = "deny"
    network: str = "deny"


@dataclass(frozen=True, slots=True)
class ModelSettings:
    provider: str = "deepseek"
    name: str = ""
    base_url: str = ""
    thinking: bool = False
    max_tokens: int = 8192
    timeout_seconds: float = 60.0
    api_key_env: str = ""


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    """Verification execution backend. ``command`` is a fixed argv prefix, never a shell string."""

    mode: str = "local"
    command: tuple[str, ...] = ()
    host: str = "127.0.0.1"
    port: int = 8765
    token_env: str = "REPOPILOT_SANDBOX_TOKEN"
    tenant: str = "default"
    tls: bool = False
    ca_file: str = ""
    image: str = ""
    network: str = "none"
    memory: str = ""
    cpus: str = ""
    user: str = ""
    pool_size: int = 0
    pull: str = "missing"


@dataclass(frozen=True, slots=True)
class AppConfig:
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    permissions: PermissionSettings = field(default_factory=PermissionSettings)
    model: ModelSettings = field(default_factory=ModelSettings)
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)


def resolve_env_path(explicit_path: Path | None = None) -> Path:
    """Resolve the RepoPilot credential file without inspecting a target repository's .env."""
    if explicit_path is not None:
        return explicit_path.expanduser().resolve()
    override = os.getenv("REPOPILOT_ENV_FILE", "").strip()
    return Path(override).expanduser().resolve() if override else _DEFAULT_ENV_PATH


def load_environment(explicit_path: Path | None = None) -> Path | None:
    """Load RepoPilot's .env without overriding explicit process variables."""
    env_path = resolve_env_path(explicit_path)
    if not env_path.is_file():
        return None
    load_dotenv(dotenv_path=env_path, override=False, verbose=False)
    return env_path


def load_config(
    repository: Path,
    explicit_path: Path | None = None,
    *,
    env_path: Path | None = None,
) -> AppConfig:
    """Load project TOML and non-secret DeepSeek environment overrides."""
    load_environment(env_path)
    config = AppConfig()
    candidates = (
        [explicit_path]
        if explicit_path
        else [
            repository / ".repopilot" / "config.toml",
            repository / "repopilot.toml",
        ]
    )
    path = next((item for item in candidates if item and item.is_file()), None)
    if path:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
        config = _from_mapping(raw)

    model = replace(
        config.model,
        name=os.getenv(f"{_env_prefix(config.model.provider)}_MODEL", "").strip()
        or config.model.name,
        base_url=os.getenv(f"{_env_prefix(config.model.provider)}_BASE_URL", "").strip()
        or config.model.base_url,
    )
    return replace(config, model=model)


def _env_prefix(provider: str) -> str:
    return provider.upper().replace("-", "_").replace(".", "_")


def _from_mapping(raw: dict[str, Any]) -> AppConfig:
    sandbox_raw = dict(raw.get("sandbox", {}))
    if "command" in sandbox_raw:
        sandbox_raw["command"] = tuple(str(item) for item in sandbox_raw["command"])
    return AppConfig(
        workspace=WorkspaceSettings(**raw.get("workspace", {})),
        execution=ExecutionSettings(**raw.get("execution", {})),
        permissions=PermissionSettings(**raw.get("permissions", {})),
        model=ModelSettings(**raw.get("model", {})),
        sandbox=SandboxSettings(**sandbox_raw),
    )
