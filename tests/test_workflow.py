import unittest

from repopilot.contracts import RunPhase, RunState, TaskRequest
from repopilot.workflow import InvalidTransition, WorkflowEngine


class WorkflowTests(unittest.TestCase):
    def test_happy_path_prefix(self) -> None:
        state = RunState(TaskRequest(goal="fix parser", repository="D:/repo"))
        workflow = WorkflowEngine()

        workflow.transition(state, RunPhase.DISCOVER)
        workflow.transition(state, RunPhase.PLAN)
        workflow.transition(state, RunPhase.WAIT_APPROVAL)

        self.assertEqual(state.phase, RunPhase.WAIT_APPROVAL)

    def test_skipping_approval_is_rejected(self) -> None:
        state = RunState(TaskRequest(goal="fix parser", repository="D:/repo"))

        with self.assertRaises(InvalidTransition):
            WorkflowEngine().transition(state, RunPhase.PATCH)


if __name__ == "__main__":
    unittest.main()
