import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from repopilot.llm import ModelResponse
from repopilot.runtime import build_runtime
from repopilot.tools import ToolCall


class _UnusedModel:
    def complete(self, messages, *, tools=(), json_mode=False):
        del messages, tools, json_mode
        return ModelResponse("{}")


class RepositoryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("rg") is None:
            self.skipTest("ripgrep is not installed")

    def test_inventory_and_search_honor_ignore_and_secret_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init", "-q", str(root)],
                check=True,
                capture_output=True,
            )
            (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (root / "visible.py").write_text("needle = True\n", encoding="utf-8")
            (root / "ignored.txt").write_text("needle\n", encoding="utf-8")
            (root / ".env").write_text("needle=secret\n", encoding="utf-8")
            dependency = root / "node_modules" / "package" / "index.js"
            dependency.parent.mkdir(parents=True)
            dependency.write_text("needle\n", encoding="utf-8")
            runtime = build_runtime(root, model_client=_UnusedModel())

            inventory = runtime.tools.execute(
                runtime.tool_context,
                ToolCall("repository_inventory", {"limit": 50}),
            )
            search = runtime.tools.execute(
                runtime.tool_context,
                ToolCall("search_code", {"query": "needle", "limit": 20}),
            )

            self.assertIn("visible.py", inventory.metadata["files"])
            self.assertNotIn("ignored.txt", inventory.metadata["files"])
            self.assertNotIn(".env", inventory.metadata["files"])
            self.assertNotIn("node_modules/package/index.js", inventory.metadata["files"])
            self.assertEqual([hit["path"] for hit in search.metadata["hits"]], ["visible.py"])

    def test_search_treats_the_query_as_a_literal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init", "-q", str(root)],
                check=True,
                capture_output=True,
            )
            (root / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n", encoding="utf-8"
            )
            runtime = build_runtime(root, model_client=_UnusedModel())

            for query in ("add(", "a + b"):
                with self.subTest(query=query):
                    search = runtime.tools.execute(
                        runtime.tool_context,
                        ToolCall("search_code", {"query": query, "limit": 20}),
                    )
                    self.assertIn(
                        "calc.py", [hit["path"] for hit in search.metadata["hits"]]
                    )

            # An invalid regex must not raise: the query is matched literally.
            unmatched = runtime.tools.execute(
                runtime.tool_context,
                ToolCall("search_code", {"query": "[", "limit": 20}),
            )
            self.assertEqual(unmatched.metadata["hits"], [])


if __name__ == "__main__":
    unittest.main()
