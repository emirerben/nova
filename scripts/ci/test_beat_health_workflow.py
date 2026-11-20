"""Contract for .github/workflows/beat-health.yml, the outside check that the
Celery Beat maintenance lane is alive.

Ways it can go wrong, written before the change (KRI-1553):

1. It depends on GitHub's schedule alone. GitHub ran the 15-minute schedule 3
   times on 2026-11-19, so a dead lane went unseen for about 9 hours.
2. A workflow it follows is renamed. `workflow_run` matches by name, so the
   trigger stops without any error.
3. It runs after pull-request CI, including forks, and gains something worth
   stealing or steering: a token permission, a checkout, a secret, or text from
   the triggering run pasted into the shell.
4. The schedule is dropped, so a quiet day has no check at all.
"""

from pathlib import Path
import re
import unittest

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


def workflow_name(file_name: str) -> str:
    match = re.search(r"^name: (.+)$", (WORKFLOWS / file_name).read_text(), re.MULTILINE)
    assert match is not None, file_name
    return match.group(1).strip().strip("\"'")


class BeatHealthWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (WORKFLOWS / "beat-health.yml").read_text()
        self.triggers, self.rest = self.workflow.split("\npermissions:", 1)
        self.triggers = self.triggers[self.triggers.index("\non:\n") :]

    def test_it_runs_after_every_ci_run_and_deploy_not_only_on_the_schedule(self):
        followed = re.search(
            r"^  workflow_run:\n    workflows: \[([^\]]+)\]\n    types: \[completed\]$",
            self.triggers,
            re.MULTILINE,
        )
        self.assertIsNotNone(followed, "beat-health must follow other workflows")
        names = [name.strip().strip("\"'") for name in followed.group(1).split(",")]
        self.assertEqual(
            sorted(names),
            sorted([workflow_name("ci.yml"), workflow_name("fly-deploy.yml")]),
        )

    def test_the_schedule_stays_as_the_check_for_a_quiet_day(self):
        self.assertRegex(self.triggers, r"(?m)^  schedule:\n(?:    #[^\n]*\n)*    - cron: ")
        self.assertIn("\n  workflow_dispatch:", self.triggers)

    def test_a_run_started_by_someone_elses_pull_request_has_nothing_to_take(self):
        self.assertTrue(self.rest.startswith(" {}\n"), "permissions must stay {}")
        code = "\n".join(
            line for line in self.rest.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("uses:", code)
        self.assertNotIn("secrets.", code)
        self.assertNotIn("${{", code)


if __name__ == "__main__":
    unittest.main()
