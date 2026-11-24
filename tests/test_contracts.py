import json
import unittest

from repopilot.contracts import RunPhase, RunState, TaskRequest


class RunStateTests(unittest.TestCase):
    def test_state_is_json_serializable(self) -> None:
        state = RunState(TaskRequest(goal="fix parser", repository="D:/repo"))
        state.phase = RunPhase.DISCOVER

        encoded = json.dumps(state.to_dict())

        self.assertIn('"discover"', encoded)
        self.assertIn('"fix parser"', encoded)

    def test_empty_goal_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TaskRequest(goal="   ", repository="D:/repo")


if __name__ == "__main__":
    unittest.main()
