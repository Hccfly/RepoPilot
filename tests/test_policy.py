import unittest

from repopilot.config import PermissionSettings
from repopilot.policy import Action, Effect, PermissionPolicy


class PermissionPolicyTests(unittest.TestCase):
    def test_default_policy_is_read_only_without_prompt(self) -> None:
        policy = PermissionPolicy()

        self.assertEqual(policy.decide(Action.READ).effect, Effect.ALLOW)
        self.assertEqual(policy.decide(Action.EDIT).effect, Effect.ASK)
        self.assertEqual(policy.decide(Action.VERIFY).effect, Effect.ASK)
        self.assertEqual(policy.decide(Action.GIT_WRITE).effect, Effect.DENY)

    def test_destructive_git_remains_denied(self) -> None:
        policy = PermissionPolicy(PermissionSettings(git_write="allow"))

        decision = policy.decide(Action.GIT_WRITE, "git reset --hard HEAD~1")

        self.assertEqual(decision.effect, Effect.DENY)


if __name__ == "__main__":
    unittest.main()
