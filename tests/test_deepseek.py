import unittest
from types import SimpleNamespace

from repopilot.config import ModelSettings
from repopilot.llm import DeepSeekModelClient, ModelMessage


class _FakeCompletions:
    def __init__(self) -> None:
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        message = SimpleNamespace(content='{"ok":true}', tool_calls=[])
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice])


class DeepSeekModelClientTests(unittest.TestCase):
    def test_json_request_uses_deepseek_model_and_disables_thinking(self) -> None:
        completions = _FakeCompletions()
        sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        settings = ModelSettings(name="deepseek-v4-flash", thinking=False)
        client = DeepSeekModelClient(settings, client=sdk)

        response = client.complete(
            [ModelMessage("user", "output json")],
            json_mode=True,
        )

        self.assertEqual(response.text, '{"ok":true}')
        self.assertEqual(completions.request["model"], "deepseek-v4-flash")
        self.assertEqual(completions.request["response_format"], {"type": "json_object"})
        self.assertEqual(
            completions.request["extra_body"],
            {"thinking": {"type": "disabled"}},
        )


if __name__ == "__main__":
    unittest.main()
