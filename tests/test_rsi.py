import difflib
import shutil
import tempfile
import unittest
from pathlib import Path

_IGNORE_PYCACHE = shutil.ignore_patterns("__pycache__", "*.pyc")

from repopilot.benchmark import Scenario, run_scenario_facts
from repopilot.contracts import RunPhase
from repopilot.grader import GradeSpec, score_facts
from repopilot.rsi import (
    RsiError,
    _run_cases,
    assert_rsi_scope,
    ensure_improvable,
    evaluate_repository,
    render_scorecard,
)

REPO = Path(__file__).resolve().parent.parent

_APP = 'def value():\n    return "old"\n'
_EXPLORER = '{"queries":["value"],"files":["app.py"],"reason":"定位返回值"}'
_PLANNER = (
    '{"summary":"更新返回值","steps":[{"description":"修改 value",'
    '"files":["app.py"],"verification":[]}],"risks":[]}'
)
_EDITOR = (
    '{"unified_diff":"--- a/app.py\\n+++ b/app.py\\n@@ -1,2 +1,2 @@\\n '
    'def value():\\n-    return \\"old\\"\\n+    return \\"new\\"\\n","rationale":"按计划更新"}'
)
_APPROVE = '{"decision":"approve","summary":"与目标一致","findings":[]}'

_TINY = Scenario(
    name="tiny",
    description="最小回归场景",
    goal="把 value 改成 new",
    files={"app.py": _APP},
    responses=(_EXPLORER, _PLANNER, _EDITOR, _APPROVE),
    verify_outcomes=(True,),
    expected_files=("app.py",),
    expected_phase=RunPhase.DONE,
    expected_stop_reason="verified",
)

_NOOP_PATCH = (
    "--- /dev/null\n+++ b/src/repopilot/_rsi_probe.py\n@@ -0,0 +1,1 @@\n+X = 1\n"
)
_LLM_SOURCE = (REPO / "src" / "repopilot" / "llm.py").read_text(encoding="utf-8")
_BROKEN_PATCH = "".join(
    difflib.unified_diff(
        _LLM_SOURCE.splitlines(keepends=True),
        ("this is not valid python\n" + _LLM_SOURCE).splitlines(keepends=True),
        fromfile="a/src/repopilot/llm.py",
        tofile="b/src/repopilot/llm.py",
    )
)
_OUT_OF_SCOPE_PATCH = (
    "--- a/tests/test_rsi.py\n+++ b/tests/test_rsi.py\n@@ -1,1 +1,2 @@\n"
    " import unittest\n+CHEAT = True\n"
)


class RsiScopeTests(unittest.TestCase):
    def test_runtime_source_is_allowed(self) -> None:
        assert_rsi_scope(["src/repopilot/planning.py", "src/repopilot/verification.py"])

    def test_tests_are_rejected(self) -> None:
        with self.assertRaises(RsiError):
            assert_rsi_scope(["tests/test_rsi.py"])

    def test_benchmark_is_rejected(self) -> None:
        with self.assertRaises(RsiError):
            assert_rsi_scope(["src/repopilot/benchmark.py"])

    def test_docs_and_project_files_are_rejected(self) -> None:
        for path in ("docs/roadmap.md", "README.md", "pyproject.toml", "AGENTS.md"):
            with self.subTest(path=path), self.assertRaises(RsiError):
                assert_rsi_scope([path])

    def test_paths_outside_src_are_rejected(self) -> None:
        with self.assertRaises(RsiError):
            assert_rsi_scope(["src/other/module.py"])

    def test_empty_input_is_rejected(self) -> None:
        with self.assertRaises(RsiError):
            assert_rsi_scope([])


class ScenarioSerializationTests(unittest.TestCase):
    def test_round_trip_preserves_the_scenario(self) -> None:
        restored = Scenario.from_dict(_TINY.to_dict())
        self.assertEqual(restored, _TINY)


class GradingTests(unittest.TestCase):
    def test_expected_facts_score_full_marks(self) -> None:
        score = score_facts(
            _TINY.grade_spec(),
            touched=("app.py",),
            phase=RunPhase.DONE,
            stop_reason="verified",
        )
        self.assertEqual(score.score, 1.0)
        self.assertTrue(score.passed)

    def test_wrong_phase_and_extra_files_lower_the_score(self) -> None:
        score = score_facts(
            _TINY.grade_spec(),
            touched=("app.py", "extra.py"),
            phase=RunPhase.REPAIR,
            stop_reason="verification_failed",
        )
        self.assertLess(score.score, 0.75)
        self.assertFalse(score.passed)

    def test_missing_touched_files_are_penalised(self) -> None:
        score = score_facts(
            _TINY.grade_spec(), touched=(), phase=RunPhase.DONE, stop_reason="verified"
        )
        self.assertLess(score.score, 0.75)
        self.assertIn("缺少预期文件", " ".join(score.reasons))

    def test_expected_error_must_match_the_actual_failure(self) -> None:
        spec = GradeSpec(
            name="dirty",
            expected_files=("app.py",),
            expected_phase=RunPhase.WAIT_APPROVAL,
            expected_stop_reason="",
            expect_error=True,
            expected_error="已有改动",
        )
        matching = score_facts(
            spec,
            touched=(),
            phase=None,
            stop_reason="",
            error="补丁触及任务开始前已有改动的文件: app.py",
        )
        other = score_facts(
            spec, touched=(), phase=None, stop_reason="", error="patch does not apply"
        )
        self.assertTrue(matching.passed)
        self.assertFalse(other.passed)
        self.assertIn("不符合预期", " ".join(other.reasons))


class GraderIsolationTests(unittest.TestCase):
    def test_runner_facts_carry_no_score(self) -> None:
        facts = run_scenario_facts(_TINY)
        self.assertNotIn("score", facts)
        self.assertEqual(facts["scenario"], "tiny")
        self.assertEqual(facts["phase"], "done")
        self.assertEqual(facts["touched"], ["app.py"])

    def test_grader_module_cannot_be_patched(self) -> None:
        with self.assertRaises(RsiError):
            assert_rsi_scope(["src/repopilot/grader.py"])

    def test_fingerprint_changes_when_the_stack_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "src"
            shutil.copytree(REPO / "src", candidate, ignore=_IGNORE_PYCACHE)
            grader_file = candidate / "repopilot" / "grader.py"
            grader_file.write_text(
                grader_file.read_text(encoding="utf-8") + "\n# loosened\n",
                encoding="utf-8",
            )
            _, _, baseline_fingerprint = _run_cases(REPO / "src", REPO, [_TINY], ())
            _, _, candidate_fingerprint = _run_cases(candidate, REPO, [_TINY], ())

        self.assertTrue(baseline_fingerprint)
        self.assertNotEqual(baseline_fingerprint, candidate_fingerprint)

    def test_expect_grader_pin_is_enforced(self) -> None:
        with self.assertRaises(RsiError):
            evaluate_repository(REPO, scenarios=[_TINY], checks=(), expect_grader="0" * 64)

    def test_a_passing_scenario_is_not_improvable(self) -> None:
        with self.assertRaises(RsiError) as caught:
            ensure_improvable(REPO, _TINY, checks=())
        self.assertIn("1.0", str(caught.exception))


class EvaluateRepositoryTests(unittest.TestCase):
    def test_baseline_without_patch_matches_candidate(self) -> None:
        report = evaluate_repository(REPO, scenarios=[_TINY], checks=())

        self.assertEqual(report.baseline_mean, 1.0)
        self.assertEqual(report.candidate_mean, 1.0)
        self.assertFalse(report.regression)
        self.assertEqual(report.scenarios[0].delta, 0.0)

    def test_harmless_patch_keeps_the_score(self) -> None:
        report = evaluate_repository(REPO, scenarios=[_TINY], checks=(), patch=_NOOP_PATCH)

        self.assertEqual(report.candidate_mean, 1.0)
        self.assertFalse(report.regression)

    def test_broken_patch_is_detected_as_a_regression(self) -> None:
        report = evaluate_repository(REPO, scenarios=[_TINY], checks=(), patch=_BROKEN_PATCH)

        self.assertEqual(report.candidate_mean, 0.0)
        self.assertTrue(report.regression)
        self.assertTrue(report.scenarios[0].reasons)

    def test_out_of_scope_patch_is_refused_before_evaluation(self) -> None:
        with self.assertRaises(RsiError):
            evaluate_repository(REPO, scenarios=[_TINY], checks=(), patch=_OUT_OF_SCOPE_PATCH)

    def test_scorecard_is_written_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report = evaluate_repository(REPO, scenarios=[_TINY], checks=(), out_dir=temp)
            markdown = (Path(temp) / "scorecard.md").read_text(encoding="utf-8")
            payload = (Path(temp) / "scorecard.json").read_text(encoding="utf-8")

        self.assertIn("RepoPilot 自我评测", markdown)
        self.assertIn("tiny", markdown)
        self.assertIn("受信 grader", markdown)
        self.assertIn('"candidate_mean"', payload)
        self.assertIn("grader_fingerprint", payload)
        self.assertEqual(len(report.grader_fingerprint), 64)
        self.assertIn("tiny", render_scorecard(report))


class CliParserTests(unittest.TestCase):
    def test_rsi_subcommands_are_wired(self) -> None:
        from repopilot.cli import _parser

        evaluate = _parser().parse_args(["rsi", "eval", "--repo", ".", "--out", "a"])
        self.assertEqual(evaluate.command, "rsi")
        self.assertEqual(evaluate.rsi_command, "eval")

        compare = _parser().parse_args(["rsi", "compare", "--patch", "p.diff"])
        self.assertEqual(compare.rsi_command, "compare")
        self.assertEqual(compare.patch, "p.diff")

        propose = _parser().parse_args(["rsi", "propose", "--scenario", "bug_fix"])
        self.assertEqual(propose.rsi_command, "propose")
        self.assertEqual(propose.scenario, "bug_fix")


if __name__ == "__main__":
    unittest.main()
