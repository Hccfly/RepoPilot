"""Model provider plugins: builtin adapters, registry, and entry-point discovery.

Third-party packages extend RepoPilot by declaring an entry point in the
``repopilot.model_providers`` group whose target is a provider instance (or a
zero-argument factory returning one).
"""

from __future__ import annotations

import importlib.util
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from importlib import metadata
from typing import Any, Protocol

from .config import ModelSettings
from .llm import (
    AnthropicModelClient,
    DeepSeekModelClient,
    ModelClient,
    ModelNotConfigured,
    OpenAICompatibleModelClient,
)

ENTRY_POINT_GROUP = "repopilot.model_providers"
_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ProviderError(RuntimeError):
    pass


class ModelProvider(Protocol):
    name: str
    default_model: str
    default_base_url: str
    credential_env: str

    def normalize(self, settings: ModelSettings, environ: Mapping[str, str]) -> ModelSettings: ...

    def requirement(self) -> str | None:
        """Return a human-readable reason when the provider cannot be constructed."""

    def create(
        self,
        settings: ModelSettings,
        *,
        api_key: str,
        client: Any | None = None,
    ) -> ModelClient: ...


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    name: str
    model: str
    base_url: str
    credential_env: str
    ready: bool
    detail: str = ""


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class _BuiltinProvider:
    name = ""
    default_model = ""
    default_base_url = ""
    credential_env = ""
    sdk_module = "openai"
    sdk_hint = "缺少 openai SDK，请运行 pip install -e .[agent]"
    client_class: type = OpenAICompatibleModelClient
    requires_model = False

    def __init__(self, module_available: Callable[[str], bool] = _module_available) -> None:
        self._module_available = module_available

    def normalize(self, settings: ModelSettings, environ: Mapping[str, str]) -> ModelSettings:
        prefix = self.name.upper().replace("-", "_").replace(".", "_")
        return replace(
            settings,
            name=settings.name
            or (environ.get(f"{prefix}_MODEL") or "").strip()
            or self.default_model,
            base_url=settings.base_url
            or (environ.get(f"{prefix}_BASE_URL") or "").strip()
            or self.default_base_url,
        )

    def requirement(self) -> str | None:
        return None if self._module_available(self.sdk_module) else self.sdk_hint

    def create(
        self,
        settings: ModelSettings,
        *,
        api_key: str,
        client: Any | None = None,
    ) -> ModelClient:
        if self.requires_model and not settings.name:
            raise ModelNotConfigured(
                f"provider {self.name} 必须显式指定 model.name 或 "
                f"{self.name.upper()}_MODEL"
            )
        return self.client_class(settings, api_key=api_key, client=client)


class _DeepSeekProvider(_BuiltinProvider):
    name = "deepseek"
    default_model = "deepseek-v4-flash"
    default_base_url = "https://api.deepseek.com"
    credential_env = "DEEPSEEK_API_KEY"
    client_class = DeepSeekModelClient


class _OpenAIProvider(_BuiltinProvider):
    name = "openai"
    default_model = ""
    default_base_url = "https://api.openai.com/v1"
    credential_env = "OPENAI_API_KEY"
    client_class = OpenAICompatibleModelClient
    requires_model = True


class _AnthropicProvider(_BuiltinProvider):
    name = "anthropic"
    default_model = "claude-sonnet-5-5"
    default_base_url = ""
    credential_env = "ANTHROPIC_API_KEY"
    sdk_module = "anthropic"
    sdk_hint = "缺少 anthropic SDK，请运行 pip install anthropic"
    client_class = AnthropicModelClient


def builtin_providers(
    *, module_available: Callable[[str], bool] = _module_available
) -> tuple[ModelProvider, ...]:
    return (
        _DeepSeekProvider(module_available),
        _OpenAIProvider(module_available),
        _AnthropicProvider(module_available),
    )


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}
        self._errors: list[str] = []

    @classmethod
    def default(
        cls,
        *,
        entry_points: Callable[[], Iterable[Any]] | None = None,
        builtin: Iterable[ModelProvider] | None = None,
    ) -> ProviderRegistry:
        registry = cls()
        for provider in builtin if builtin is not None else builtin_providers():
            registry.register(provider)
        loader = entry_points or _installed_entry_points
        for entry in loader():
            registry._load_entry_point(entry)
        return registry

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(self._errors)

    def register(self, provider: ModelProvider, *, source: str = "builtin") -> None:
        name = getattr(provider, "name", "")
        if not isinstance(name, str) or not _PROVIDER_NAME.fullmatch(name):
            raise ProviderError(f"非法的 provider 名称: {name!r}（来源: {source}）")
        if name in self._providers:
            raise ProviderError(f"provider 已存在: {name}（来源: {source}）")
        self._providers[name] = provider

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def get(self, name: str) -> ModelProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "无"
            raise ProviderError(f"未知的模型 provider: {name}（可用: {available}）") from exc

    def normalize(
        self, settings: ModelSettings, *, environ: Mapping[str, str] | None = None
    ) -> ModelSettings:
        env = os.environ if environ is None else environ
        return self.get(settings.provider).normalize(settings, env)

    def describe(self, *, environ: Mapping[str, str] | None = None) -> tuple[ProviderInfo, ...]:
        env = os.environ if environ is None else environ
        infos: list[ProviderInfo] = []
        for name in self.names():
            provider = self._providers[name]
            settings = provider.normalize(ModelSettings(provider=name), env)
            env_name = settings.api_key_env or provider.credential_env
            missing = provider.requirement()
            has_key = bool((env.get(env_name) or "").strip())
            detail = missing or ("" if has_key else f"缺少 {env_name}")
            infos.append(
                ProviderInfo(
                    name=name,
                    model=settings.name,
                    base_url=settings.base_url,
                    credential_env=env_name,
                    ready=missing is None and has_key,
                    detail=detail,
                )
            )
        return tuple(infos)

    def build(
        self, settings: ModelSettings, *, environ: Mapping[str, str] | None = None
    ) -> ModelClient:
        env = os.environ if environ is None else environ
        provider = self.get(settings.provider)
        normalized = provider.normalize(settings, env)
        missing = provider.requirement()
        if missing is not None:
            raise ModelNotConfigured(missing)
        env_name = normalized.api_key_env or provider.credential_env
        api_key = (env.get(env_name) or "").strip()
        if not api_key:
            raise ModelNotConfigured(f"缺少 {env_name}（provider={provider.name}）")
        return provider.create(normalized, api_key=api_key)

    def _load_entry_point(self, entry: Any) -> None:
        source = f"entry point {getattr(entry, 'name', '?')}"
        try:
            loaded = entry.load()
            if isinstance(loaded, type) or (callable(loaded) and not hasattr(loaded, "name")):
                loaded = loaded()
            self.register(loaded, source=source)
        except Exception as exc:  # noqa: BLE001
            self._errors.append(f"{source}: {type(exc).__name__}: {exc}")


def _installed_entry_points() -> Iterable[Any]:
    return metadata.entry_points(group=ENTRY_POINT_GROUP)


def render_provider_table(infos: Iterable[ProviderInfo]) -> tuple[str, ...]:
    rows = [f"{'NAME':<12}{'MODEL':<24}{'CREDENTIAL':<20}READY"]
    for info in infos:
        model = info.model or "(需指定)"
        state = "yes" if info.ready else f"no — {info.detail}"
        rows.append(f"{info.name:<12}{model:<24}{info.credential_env:<20}{state}")
    return tuple(rows)
