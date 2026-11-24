import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repopilot.config import load_config, load_environment
from repopilot.contracts import RunState, TaskRequest


class EnvironmentLoadingTests(unittest.TestCase):
    def test_dotenv_loads_key_without_putting_it_in_config_or_state(self) -> None:
        secret = "ds-test-secret-that-must-not-be-serialized"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_path = root / ".env"
            env_path.write_text(
                f"DEEPSEEK_API_KEY={secret}\nDEEPSEEK_MODEL=deepseek-v4-flash\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                loaded = load_environment(env_path)
                config = load_config(root, env_path=env_path)
                state = RunState(TaskRequest("检查配置", str(root)))

                self.assertEqual(loaded, env_path.resolve())
                self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), secret)
                self.assertNotIn(secret, repr(config))
                self.assertNotIn(secret, json.dumps(state.to_dict(), ensure_ascii=False))

    def test_process_environment_has_priority_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env_path = Path(temp) / ".env"
            env_path.write_text("DEEPSEEK_MODEL=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"DEEPSEEK_MODEL": "from-process"}, clear=True):
                config = load_config(Path(temp), env_path=env_path)

                self.assertEqual(config.model.name, "from-process")


if __name__ == "__main__":
    unittest.main()
