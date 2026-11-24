import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from repopilot.benchmark import Scenario
from repopilot.contracts import RunPhase
from repopilot.llm import ModelResponse
from repopilot.persistence import JsonPatchRunStore
from repopilot.rsi import classify_round, run_rounds
from repopilot.rsi_history import RoundRecord, RsiHistory

REPO = Path(__file__).resolve().parent.parent
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def _scenario() -> Scenario:
    """Baseline scores 0.75: phase matches, stop reason does not."""
    return Scenario(
        name="round-tiny",
        description="最小多轮场景",
        goal="把 value 改成 new",
        files={"app.py": 'def value():\n    return "old"\n'},
        responses=(
            '{"queries":["value"],"files":["app.py"],"reason":"定位"}',
            (
                '{"summary":"更新","steps":[{"description":"改 value",'
                '"files":["app.py"],"verification":[]}],"risks":[]}'
            ),
            (
                '{"unified_diff":"--- a/app.py\\n+++ b/app.py\\n@@ -1,2 +1,2 @@\\n '
                'def value():\\n-    return \\"old\\"\\n+    return \\"new\\"\\n",'
                '"rationale":"x"}'
            ),
            '{"decision":"approve","summary":"ok","findings":[]}',
        ),
        verify_outcomes=(True,),
        expected_files=("app.py",),
        expected_phase=RunPhase.DONE,
        expected_stop_reason="never-happens",
    )


class _Model:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)

    def complete(self, messages, *, tools=(), json_mode=False):
        del messages, tools
        return ModelResponse(self.responses.pop(0))


def _explorer() -> str:
    return '{"queries":["class"],"files":["src/repopilot/rsi.py"],"reason":"范围"}'


_PLANNER = (
    '{"summary":"调整实现","steps":[{"description":"改 rsi",'
    '"files":["src/repopilot/rsi.py"],"verification":[]}],"risks":[]}'
)


def _editor(name: str) -> str:
    diff = (
        f"--- /dev/null\n+++ b/src/repopilot/{name}.py\n@@ -0,0 +1,1 @@\n+X = 1\n"
    )
    return json.dumps({"unified_diff": diff, "rationale": "probe"})


def _repo(temp: str) -> Path:
    root = Path(temp) / "repo"
    shutil.copytree(REPO / "src", root / "src", ignore=_IGNORE)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    return root


class HistoryTests(unittest.TestCase):
    def _record(self, index: int, *, score, best, status, digest="a" * 8) -> RoundRecord:
        return RoundRecord(
            index=index,
            run_id="r" * 32,
            timestamp="2026-01-01T00:00:00+00:00",
            scenario="round-tiny",
            patch_hash=digest,
            score=score,
            best_score=best,
            status=status,
            reasons=("原因",),
        )

    def test_append_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.jsonl"
            history = RsiHistory(path)
            history.append(self._record(1, score=0.5, best=0.5, status="improved"))
            history.append(self._record(2, score=0.4, best=0.5, status="rejected"))

            reloaded = RsiHistory(path).rounds()

        self.assertEqual([item.index for item in reloaded], [1, 2])
        self.assertEqual(reloaded[1].status, "rejected")
        self.assertEqual(reloaded[0].reasons, ("原因",))

    def test_best_score_and_known_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            history = RsiHistory(Path(temp) / "history.jsonl")
            history.append(self._record(1, score=0.7, best=0.7, status="improved", digest="aaa"))
            history.append(self._record(2, score=None, best=0.7, status="duplicate", digest="bbb"))

            self.assertEqual(history.best_score(), 0.7)
            self.assertEqual(history.patch_hashes(), frozenset({"aaa", "bbb"}))

    def test_corrupt_lines_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.jsonl"
            history = RsiHistory(path)
            history.append(self._record(1, score=0.5, best=0.5, status="improved"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write("{not json}\n")

            self.assertEqual(len(RsiHistory(path).rounds()), 1)


class ClassifyTests(unittest.TestCase):
    def test_improved_needs_a_strict_gain(self) -> None:
        self.assertEqual(classify_round(0.9, 0.8, duplicate=False), "improved")
        self.assertEqual(classify_round(0.8, 0.8, duplicate=False), "rejected")

    def test_duplicate_wins_over_score(self) -> None:
        self.assertEqual(classify_round(0.9, 0.8, duplicate=True), "duplicate")

    def test_missing_score_is_an_error(self) -> None:
        self.assertEqual(classify_round(None, 0.8, duplicate=False), "error")


class RunRoundsTests(unittest.TestCase):
    def test_two_rounds_are_recorded_and_source_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = _repo(temp)
            store = JsonPatchRunStore(Path(temp) / "runs")
            before = (root / "src" / "repopilot" / "rsi.py").read_bytes()
            model = _Model(
                _explorer(), _PLANNER, _editor("probe_one"),
                _explorer(), _PLANNER, _editor("probe_two"),
            )
            records = run_rounds(
                root,
                scenario=_scenario(),
                rounds=2,
                store=store,
                out_dir=Path(temp) / "artifacts",
                history_path=Path(temp) / "history.jsonl",
                model_client=model,
            )
            after = (root / "src" / "repopilot" / "rsi.py").read_bytes()
            history = RsiHistory(Path(temp) / "history.jsonl").rounds()

        self.assertEqual(len(records), 2)
        self.assertEqual([item.status for item in records], ["rejected", "rejected"])
        self.assertEqual(len(history), 2)
        self.assertEqual(before, after)
        self.assertFalse((root / "src" / "repopilot" / "probe_one.py").exists())
        self.assertEqual(records[0].score, 0.75)
        self.assertEqual(records[0].best_score, 0.75)

    def test_identical_candidates_are_marked_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = _repo(temp)
            store = JsonPatchRunStore(Path(temp) / "runs")
            model = _Model(
                _explorer(), _PLANNER, _editor("probe_one"),
                _explorer(), _PLANNER, _editor("probe_one"),
            )
            records = run_rounds(
                root,
                scenario=_scenario(),
                rounds=2,
                store=store,
                out_dir=Path(temp) / "artifacts",
                history_path=Path(temp) / "history.jsonl",
                model_client=model,
            )

        self.assertEqual([item.status for item in records], ["rejected", "duplicate"])
        self.assertIsNone(records[1].score)
        self.assertIn("与历史候选相同", " ".join(records[1].reasons))
        self.assertFalse((root / "src" / "repopilot" / "probe_one.py").exists())

    def test_a_broken_round_is_recorded_and_stops_the_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = _repo(temp)
            store = JsonPatchRunStore(Path(temp) / "runs")
            model = _Model("")
            records = run_rounds(
                root,
                scenario=_scenario(),
                rounds=3,
                store=store,
                out_dir=Path(temp) / "artifacts",
                history_path=Path(temp) / "history.jsonl",
                model_client=model,
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "error")
        self.assertIsNone(records[0].score)


class CliParserTests(unittest.TestCase):
    def test_run_and_history_are_wired(self) -> None:
        from repopilot.cli import _parser

        run = _parser().parse_args(
            ["rsi", "run", "--scenario", "bug_fix", "--rounds", "4"]
        )
        self.assertEqual(run.rsi_command, "run")
        self.assertEqual(run.rounds, 4)

        history = _parser().parse_args(["rsi", "history", "--limit", "5"])
        self.assertEqual(history.rsi_command, "history")
        self.assertEqual(history.limit, 5)


if __name__ == "__main__":
    unittest.main()
