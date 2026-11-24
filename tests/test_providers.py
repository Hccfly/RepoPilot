import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from repopilot.config import ModelSettings, load_config
from repopilot.llm import (
    AnthropicModelClient,
    DeepSeekModelClient,
    ModelMessage,
    ModelNotConfigured,
    ModelResponse,
    OpenAICompatibleModelClient,
)
from repopilot.providers import (
    ENTRY_POINT_GROUP,
    ProviderError,
    ProviderRegistry,
    builtin_providers,
)


class _FakeCompletions:
    def __init__(self) -> None:
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        message = SimpleNamespace(content='{"ok":true}', tool_calls=[])
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice])


def _openai_sdk() -> tuple[SimpleNamespace, _FakeCompletions]:
    completions = _FakeCompletions()
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


class _EchoClient:
    def __init__(self, settings: ModelSettings, api_key: str) -> None:
        self.settings = settings
        self.api_key = api_key

    def complete(self, messages, *, tools=(), json_mode=False) -> ModelResponse:
        del messages, tools, json_mode
        return ModelResponse("{}")


class _EchoProvider:
    """Minimal third-party style provider used to prove the plugin contract."""

    def __init__(self, name: str = "echo", requirement: str | None = None) -> None:
        self.name = name
        self.default_model = "echo-1"
        self.default_base_url = "https://echo.invalid/v1"
        self.credential_env = "ECHO_API_KEY"
        self._requirement = requirement

    def normalize(self, settings: ModelSettings, environ) -> ModelSettings:
        del environ
        return replace(
            settings,
            name=settings.name or self.default_model,
            base_url=settings.base_url or self.default_base_url,
        )

    def requirement(self) -> str | None:
        return self._requirement

    def create(self, settings, *, api_key: str, client=None):
        del client
        return _EchoClient(settings, api_key)


class _FakeEntryPoint:
    def __init__(self, name: str, target) -> None:
        self.name = name
        self._target = target

    def load(self):
        if isinstance(self._target, BaseException):
            raise self._target
        return self._target


def _registry(*entry_points) -> ProviderRegistry:
    return ProviderRegistry.default(entry_points=lambda: tuple(entry_points))


class RegistryTests(unittest.TestCase):
    def test_builtin_providers_are_registered(self) -> None:
        self.assertEqual(_registry().names(), ("anthropic", "deepseek", "openai"))

    def test_unknown_provider_lists_available_names(self) -> None:
        with self.assertRaises(ProviderError) as caught:
            _registry().get("nope")
        self.assertIn("deepseek", str(caught.exception))

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = _registry()
        with self.assertRaises(ProviderError):
            registry.register(_EchoProvider(name="deepseek"))

    def test_invalid_provider_name_is_rejected(self) -> None:
        with self.assertRaises(ProviderError):
            _registry().register(_EchoProvider(name="bad name!"))


class EntryPointTests(unittest.TestCase):
    def test_entry_point_provider_is_registered(self) -> None:
        registry = _registry(_FakeEntryPoint("echo", _EchoProvider()))
        self.assertIn("echo", registry.names())
        self.assertEqual(registry.errors, ())

    def test_entry_point_can_return_a_class(self) -> None:
        registry = _registry(_FakeEntryPoint("echo", lambda: _EchoProvider()))
        self.assertIn("echo", registry.names())

    def test_broken_entry_point_is_reported_without_crashing(self) -> None:
        registry = _registry(_FakeEntryPoint("boom", RuntimeError("missing dependency")))
        self.assertEqual(registry.names(), ("anthropic", "deepseek", "openai"))
        self.assertIn("boom", " ".join(registry.errors))

    def test_entry_point_cannot_shadow_a_builtin(self) -> None:
        registry = _registry(_FakeEntryPoint("deepseek", _EchoProvider(name="deepseek")))
        self.assertEqual(registry.names(), ("anthropic", "deepseek", "openai"))
        self.assertTrue(registry.errors)
        self.assertIsInstance(registry.get("deepseek").create(
            ModelSettings(provider="deepseek", name="m"), api_key="k", client=_openai_sdk()[0]
        ), DeepSeekModelClient)


class BuildTests(unittest.TestCase):
    def _echo_registry(self) -> ProviderRegistry:
        return _registry(_FakeEntryPoint("echo", _EchoProvider()))

    def test_provider_defaults_fill_settings(self) -> None:
        client = self._echo_registry().build(
            ModelSettings(provider="echo"), environ={"ECHO_API_KEY": "secret"}
        )
        self.assertEqual(client.settings.name, "echo-1")
        self.assertEqual(client.settings.base_url, "https://echo.invalid/v1")
        self.assertEqual(client.api_key, "secret")

    def test_missing_credential_names_the_environment_variable(self) -> None:
        with self.assertRaises(ModelNotConfigured) as caught:
            self._echo_registry().build(ModelSettings(provider="echo"), environ={})
        self.assertIn("ECHO_API_KEY", str(caught.exception))

    def test_api_key_env_setting_overrides_the_provider_default(self) -> None:
        client = self._echo_registry().build(
            ModelSettings(provider="echo", api_key_env="CI_KEY"), environ={"CI_KEY": "from-ci"}
        )
        self.assertEqual(client.api_key, "from-ci")

    def test_missing_requirement_is_reported(self) -> None:
        registry = _registry(
            _FakeEntryPoint("needy", _EchoProvider(name="needy", requirement="缺少 needy SDK"))
        )
        with self.assertRaises(ModelNotConfigured) as caught:
            registry.build(ModelSettings(provider="needy"), environ={"ECHO_API_KEY": "k"})
        self.assertIn("needy SDK", str(caught.exception))

    def test_build_never_reads_the_process_environment_when_given_one(self) -> None:
        with (
            patch.dict(os.environ, {"ECHO_API_KEY": "process-secret"}, clear=True),
            self.assertRaises(ModelNotConfigured),
        ):
            self._echo_registry().build(ModelSettings(provider="echo"), environ={})

    def test_describe_reports_readiness_without_leaking_secrets(self) -> None:
        infos = {item.name: item for item in self._echo_registry().describe(
            environ={"ECHO_API_KEY": "super-secret"}
        )}
        self.assertTrue(infos["echo"].ready)
        self.assertNotIn("super-secret", repr(infos["echo"]))
        self.assertFalse(infos["deepseek"].ready)


class BuiltinProviderTests(unittest.TestCase):
    def _provider(self, name: str, *, available: bool = True):
        return next(
            item
            for item in builtin_providers(module_available=lambda _: available)
            if item.name == name
        )

    def test_deepseek_provider_builds_the_deepseek_client(self) -> None:
        provider = self._provider("deepseek")
        settings = provider.normalize(ModelSettings(), {})
        self.assertEqual(settings.name, "deepseek-v4-flash")
        self.assertEqual(settings.base_url, "https://api.deepseek.com")
        sdk, completions = _openai_sdk()
        client = provider.create(settings, api_key="k", client=sdk)

        self.assertIsInstance(client, DeepSeekModelClient)
        client.complete([ModelMessage("user", "x")], json_mode=True)
        self.assertEqual(
            completions.request["extra_body"], {"thinking": {"type": "disabled"}}
        )

    def test_openai_provider_is_generic_and_omits_thinking(self) -> None:
        provider = self._provider("openai")
        settings = provider.normalize(ModelSettings(provider="openai"), {})
        self.assertEqual(settings.base_url, "https://api.openai.com/v1")
        self.assertEqual(settings.name, "")
        sdk, completions = _openai_sdk()
        settings = replace(settings, name="gpt-x")
        client = provider.create(settings, api_key="k", client=sdk)

        self.assertIsInstance(client, OpenAICompatibleModelClient)
        self.assertNotIsInstance(client, DeepSeekModelClient)
        client.complete([ModelMessage("user", "x")], json_mode=True)
        self.assertNotIn("extra_body", completions.request)

    def test_openai_provider_requires_an_explicit_model(self) -> None:
        registry = ProviderRegistry.default(
            entry_points=lambda: (),
            builtin=builtin_providers(module_available=lambda _: True),
        )
        with self.assertRaises(ModelNotConfigured) as caught:
            registry.build(ModelSettings(provider="openai"), environ={"OPENAI_API_KEY": "k"})
        self.assertIn("model", str(caught.exception).lower())

    def test_anthropic_provider_maps_system_prompt_and_text(self) -> None:
        provider = self._provider("anthropic")
        settings = provider.normalize(ModelSettings(provider="anthropic"), {})
        self.assertEqual(settings.name, "claude-sonnet-5-5")
        messages = SimpleNamespace()
        sdk = SimpleNamespace(messages=messages)
        messages.create = lambda **kwargs: (
            setattr(messages, "request", kwargs)
            or SimpleNamespace(
                content=[SimpleNamespace(type="text", text="hello")], stop_reason="end_turn"
            )
        )
        client = provider.create(settings, api_key="k", client=sdk)

        self.assertIsInstance(client, AnthropicModelClient)
        response = client.complete(
            [ModelMessage("system", "be strict"), ModelMessage("user", "hi")]
        )
        self.assertEqual(response.text, "hello")
        self.assertEqual(messages.request["system"], "be strict")
        self.assertEqual(messages.request["messages"], [{"role": "user", "content": "hi"}])

    def test_missing_sdk_is_reported_by_requirement(self) -> None:
        provider = self._provider("deepseek", available=False)
        self.assertIn("openai", provider.requirement())


class ConfigProviderTests(unittest.TestCase):
    def test_environment_override_follows_the_selected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "repopilot.toml").write_text(
                '[model]\nprovider = "openai"\nname = "from-file"\n', encoding="utf-8"
            )
            with patch.dict(
                os.environ,
                {"OPENAI_MODEL": "from-openai-env", "DEEPSEEK_MODEL": "ignored"},
                clear=True,
            ):
                config = load_config(root)
            self.assertEqual(config.model.provider, "openai")
            self.assertEqual(config.model.name, "from-openai-env")

    def test_entry_point_group_is_stable(self) -> None:
        self.assertEqual(ENTRY_POINT_GROUP, "repopilot.model_providers")


class RuntimeProviderTests(unittest.TestCase):
    def test_build_runtime_uses_the_selected_provider(self) -> None:
        import subprocess

        from repopilot.runtime import build_runtime

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            (root / "app.py").write_text("x = 1\n", encoding="utf-8")
            with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                runtime = build_runtime(root, model_provider="anthropic")
            self.assertEqual(runtime.config.model.provider, "anthropic")
            self.assertEqual(runtime.config.model.name, "claude-sonnet-5-5")


class CliProvidersTests(unittest.TestCase):
    def test_providers_command_lists_builtins(self) -> None:
        import contextlib
        import io

        from repopilot.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["providers"]), 0)
        text = output.getvalue()
        for name in ("deepseek", "openai", "anthropic"):
            self.assertIn(name, text)
        self.assertIn("DEEPSEEK_API_KEY", text)


if __name__ == "__main__":
    unittest.main()
