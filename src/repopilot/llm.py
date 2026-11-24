"""Provider-neutral model boundary with OpenAI-compatible and Anthropic adapters."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .config import ModelSettings


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    tool_calls: tuple[dict[str, Any], ...] = ()
    finish_reason: str | None = None


class ModelClient(Protocol):
    def complete(
        self,
        messages: Sequence[ModelMessage],
        *,
        tools: Sequence[dict[str, Any]] = (),
        json_mode: bool = False,
    ) -> ModelResponse: ...


class ModelNotConfigured(RuntimeError):
    pass


class UnconfiguredModelClient:
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason or "未配置模型 Provider：请设置对应 API Key 后运行 repopilot providers"

    def complete(
        self,
        messages: Sequence[ModelMessage],
        *,
        tools: Sequence[dict[str, Any]] = (),
        json_mode: bool = False,
    ) -> ModelResponse:
        del messages, tools, json_mode
        raise ModelNotConfigured(self.reason)


class OpenAICompatibleModelClient:
    """Chat Completions adapter for OpenAI and any compatible endpoint.

    The SDK import is lazy so every offline test can run without the optional
    dependency. Tests may inject a compatible fake ``client``.
    """

    credential_env = "OPENAI_API_KEY"
    sdk_hint = "缺少 openai SDK，请运行 pip install -e .[agent]"

    def __init__(
        self,
        settings: ModelSettings | None = None,
        *,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or ModelSettings()
        if client is not None:
            self._client = client
            return

        resolved_key = (api_key or os.getenv(self.credential_env, "")).strip()
        if not resolved_key:
            raise ModelNotConfigured(f"缺少 {self.credential_env}")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ModelNotConfigured(self.sdk_hint) from exc
        options: dict[str, Any] = {
            "api_key": resolved_key,
            "timeout": self.settings.timeout_seconds,
        }
        if self.settings.base_url:
            options["base_url"] = self.settings.base_url
        self._client = OpenAI(**options)

    def extra_body(self) -> dict[str, Any]:
        return {}

    def complete(
        self,
        messages: Sequence[ModelMessage],
        *,
        tools: Sequence[dict[str, Any]] = (),
        json_mode: bool = False,
    ) -> ModelResponse:
        request: dict[str, Any] = {
            "model": self.settings.name,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "max_tokens": self.settings.max_tokens,
            "stream": False,
        }
        extra = self.extra_body()
        if extra:
            request["extra_body"] = extra
        if tools:
            request["tools"] = list(tools)
        if json_mode:
            request["response_format"] = {"type": "json_object"}

        completion = self._client.chat.completions.create(**request)
        if not completion.choices:
            raise ModelNotConfigured("模型 API 未返回 choices")
        choice = completion.choices[0]
        message = choice.message
        raw_calls = getattr(message, "tool_calls", None) or ()
        tool_calls = tuple(
            {
                "id": getattr(call, "id", ""),
                "name": call.function.name,
                "arguments": call.function.arguments,
            }
            for call in raw_calls
        )
        return ModelResponse(
            text=message.content or "",
            tool_calls=tool_calls,
            finish_reason=getattr(choice, "finish_reason", None),
        )


class DeepSeekModelClient(OpenAICompatibleModelClient):
    credential_env = "DEEPSEEK_API_KEY"

    def __init__(
        self,
        settings: ModelSettings | None = None,
        *,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        super().__init__(
            settings
            or ModelSettings(
                provider="deepseek",
                name="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
            ),
            api_key=api_key,
            client=client,
        )

    def extra_body(self) -> dict[str, Any]:
        return {"thinking": {"type": "enabled" if self.settings.thinking else "disabled"}}


class AnthropicModelClient:
    """Messages API adapter. ``json_mode`` is ignored: Claude has no JSON response format,
    so the JSON contract is enforced by prompt and by the caller's parser."""

    credential_env = "ANTHROPIC_API_KEY"
    sdk_hint = "缺少 anthropic SDK，请运行 pip install anthropic"

    def __init__(
        self,
        settings: ModelSettings | None = None,
        *,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or ModelSettings(
            provider="anthropic", name="claude-sonnet-5-5"
        )
        if client is not None:
            self._client = client
            return

        resolved_key = (api_key or os.getenv(self.credential_env, "")).strip()
        if not resolved_key:
            raise ModelNotConfigured(f"缺少 {self.credential_env}")
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ModelNotConfigured(self.sdk_hint) from exc
        options: dict[str, Any] = {
            "api_key": resolved_key,
            "timeout": self.settings.timeout_seconds,
        }
        if self.settings.base_url:
            options["base_url"] = self.settings.base_url
        self._client = Anthropic(**options)

    def complete(
        self,
        messages: Sequence[ModelMessage],
        *,
        tools: Sequence[dict[str, Any]] = (),
        json_mode: bool = False,
    ) -> ModelResponse:
        del json_mode
        system = "\n\n".join(
            message.content for message in messages if message.role == "system"
        ).strip()
        turns = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.role != "system"
        ]
        request: dict[str, Any] = {
            "model": self.settings.name,
            "max_tokens": self.settings.max_tokens,
            "messages": turns,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [_anthropic_tool(tool) for tool in tools]

        response = self._client.messages.create(**request)
        blocks = list(getattr(response, "content", ()) or ())
        text = "".join(
            getattr(block, "text", "")
            for block in blocks
            if getattr(block, "type", "") == "text"
        )
        tool_calls = tuple(
            {
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "arguments": json.dumps(getattr(block, "input", {}), ensure_ascii=False),
            }
            for block in blocks
            if getattr(block, "type", "") == "tool_use"
        )
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=getattr(response, "stop_reason", None),
        )


def _anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function", tool)
    return {
        "name": function.get("name", ""),
        "description": function.get("description", ""),
        "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
    }
