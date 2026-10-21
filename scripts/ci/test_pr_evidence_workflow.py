"""Integration failure contracts for request-to-output PR evidence.

Written before wiring: a body edit must recheck without rerunning native CI;
candidate code must never execute in the independent evidence job; body text
must stay in the event file; missing baseline policy must fail that job; the
existing required lint check must cover code pushes; local checks must include
uncommitted files and reject missing evidence for affected changes.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_block(source, step_name):
    """Read one literal bash step without introducing a YAML dependency."""
    step = source.split(f"- name: {step_name}\n", 1)[1]
    script = step.split("        run: |\n", 1)[1]
    lines = []
    for line in script.splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line[10:] if line else "")
    return "\n".join(lines)


class EvidenceWorkflowTests(unittest.TestCase):
    def workflow(self):
        return (ROOT / ".github/workflows/pr-evidence.yml").read_text()

    # Hosted failure first: a synchronize event can carry yesterday's evidence
    # even after the PR body has been corrected for the current head. Fetch body
    # data at validation time, while rejecting a changed head/base branch, an
    # unexpected PR, malformed API data and read failures. Never evaluate body text.
    def current_event(self, workflow, *, changes=None, api_exit=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = {
                "repository": {"full_name": "example/nova"},
                "pull_request": {
                    "number": 42,
                    "head": {"sha": "b" * 40},
                    "base": {"sha": "a" * 40, "ref": "main"},
                    "body": "stale evidence",
                },
            }
            current = {
                "number": 42,
                "head": {"sha": "b" * 40},
                # Main may advance while this exact-head run is queued.
                "base": {"sha": "c" * 40, "ref": "main"},
                "body": "corrected evidence; $(touch injected); `touch injected`",
            }
            current.update(changes or {})
            (root / "event.json").write_text(json.dumps(event))
            (root / "response.json").write_text(json.dumps(current))
            gh = root / "gh"
            gh.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\nimport sys\n"
                "assert sys.argv[1:] == ['api', 'repos/example/nova/pulls/42']\n"
                f"sys.stdout.write(Path({str(root / 'response.json')!r}).read_text())\n"
                f"sys.exit({api_exit})\n"
            )
            gh.chmod(0o755)
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", run_block(workflow, "Read current PR evidence")],
                cwd=root,
                env=dict(
                    os.environ,
                    PATH=f"{root}:{os.environ['PATH']}",
                    GITHUB_EVENT_PATH=str(root / "event.json"),
                    GITHUB_REPOSITORY="example/nova",
                    PR_NUMBER="42",
                    HEAD_SHA="b" * 40,
                    RUNNER_TEMP=str(root),
                ),
                capture_output=True,
                text=True,
            )
            output = root / "pr-evidence-event.json"
            return (
                result,
                json.loads(output.read_text()) if output.exists() else None,
                (root / "injected").exists(),
            )

    def evidence_workflows(self):
        return [self.workflow(), (ROOT / ".github/workflows/ci.yml").read_text()]

    def test_current_body_replaces_stale_event_without_changing_immutable_base(self):
        for workflow in self.evidence_workflows():
            with self.subTest(workflow=workflow.splitlines()[0]):
                result, event, injected = self.current_event(workflow)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("corrected evidence", event["pull_request"]["body"])
                self.assertEqual(event["pull_request"]["base"]["sha"], "a" * 40)
                self.assertFalse(injected)

    def test_current_body_fails_closed_for_changed_head_or_pr_or_base_branch(self):
        for workflow in self.evidence_workflows():
            for change in (
                {"head": {"sha": "d" * 40}},
                {"number": 43},
                {"base": {"ref": "dev"}},
                {"body": None},
            ):
                with self.subTest(change=change):
                    result, event, injected = self.current_event(workflow, changes=change)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIsNone(event)
                    self.assertFalse(injected)

    def test_current_body_read_failure_never_uses_the_old_event(self):
        for workflow in self.evidence_workflows():
            result, event, _ = self.current_event(workflow, api_exit=42)
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(event)

    def test_body_edits_have_a_small_independent_read_only_check(self):
        workflow = self.workflow()
        self.assertIn("types: [opened, reopened, synchronize, edited, ready_for_review]", workflow)
        self.assertIn("branches: [main, dev]", workflow)
        self.assertIn("name: PR evidence", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("pull-requests: write", workflow)
        self.assertNotIn("github.event.pull_request.body", workflow)
        self.assertEqual(workflow.count("persist-credentials: false"), 2)
        self.assertIn("ref: ${{ github.event.pull_request.base.sha }}", workflow)
        self.assertIn("ref: ${{ github.event.pull_request.head.sha }}", workflow)
        self.assertIn('--event "$RUNNER_TEMP/pr-evidence-event.json"', workflow)
        main = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertNotIn("types: [opened, reopened, synchronize, edited", main)

    def check_bootstrap(self, base_files, head_files):
        """Execute the actual workflow shell against immutable local Git trees."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / ".pr-evidence-diff"
            candidate.mkdir()

            def git(*args):
                return subprocess.check_output(
                    ["git", *args], cwd=candidate, stderr=subprocess.PIPE, text=True
                ).strip()

            git("init", "-q")
            git("config", "user.name", "Evidence tests")
            git("config", "user.email", "evidence@example.invalid")
            (candidate / "README.md").write_text("fixture\n")
            for path, content in base_files.items():
                target = candidate / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            git("worktree", "add", "--detach", str(root / ".pr-evidence-policy"), base)
            for path, content in head_files.items():
                target = candidate / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            git("add", ".")
            git("commit", "--allow-empty", "-qm", "candidate")
            head = git("rev-parse", "HEAD")
            env = dict(
                os.environ,
                BASE_SHA=base,
                HEAD_SHA=head,
                GITHUB_EVENT_PATH="event.json",
                GITHUB_STEP_SUMMARY=str(root / "summary"),
                GITHUB_OUTPUT=str(root / "output"),
                POLICY_REPO=".pr-evidence-policy",
                DIFF_REPO=".pr-evidence-diff",
            )
            script = run_block(self.workflow(), "Resolve evidence policy")
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", script],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
            )
            return result

    def test_only_first_introduction_can_bootstrap(self):
        validator = "scripts/ci/pr-evidence.py"
        workflow = ".github/workflows/pr-evidence.yml"
        introduction = self.check_bootstrap(
            {}, {validator: "# new policy", workflow: "name: PR evidence"}
        )
        self.assertEqual(introduction.returncode, 0, introduction.stderr)
        self.assertIn("not active", introduction.stdout)
        incomplete = self.check_bootstrap({}, {validator: "# new policy"})
        self.assertNotEqual(incomplete.returncode, 0)
        broken_baseline = self.check_bootstrap(
            {workflow: "name: PR evidence"}, {validator: "# new policy"}
        )
        self.assertNotEqual(broken_baseline.returncode, 0)

    def test_independent_check_executes_only_the_baseline_policy(self):
        script = run_block(self.workflow(), "Validate request-to-output evidence")
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            for checkout, outcome in (
                (".pr-evidence-policy", "trusted"),
                (".pr-evidence-diff", "candidate"),
            ):
                validator = repo / checkout / "scripts/ci/pr-evidence.py"
                validator.parent.mkdir(parents=True)
                validator.write_text(
                    "from pathlib import Path\n"
                    f"Path({str(repo / 'executed')!r}).write_text({outcome!r})\n"
                )
            env = dict(
                os.environ,
                BASE_SHA="a" * 40,
                HEAD_SHA="b" * 40,
                GITHUB_EVENT_PATH="event.json",
                RUNNER_TEMP=str(repo),
            )
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", script],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((repo / "executed").read_text(), "trusted")
            (repo / "executed").unlink()
            (repo / ".pr-evidence-policy/scripts/ci/pr-evidence.py").unlink()
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", script],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((repo / "executed").exists())

    def test_existing_required_lint_gate_also_checks_evidence(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        lint = ci.split("  lint:\n", 1)[1].split("  test-api:\n", 1)[0]
        self.assertIn("needs: [changes, lint-suites]", lint)
        self.assertIn("scripts/ci/select-tests.py gate lint lint-suites", lint)
        self.assertIn("Validate request-to-output evidence", lint)
        self.assertIn("if: github.event_name == 'pull_request'", lint)
        self.assertIn('--event "$RUNNER_TEMP/pr-evidence-event.json"', lint)
        self.assertIn("initial policy bootstrap", lint)

    def test_preship_accepts_body_files_and_checks_working_changes(self):
        preship = (ROOT / "scripts/preship-check.sh").read_text()
        self.assertIn("--pr-body", preship)
        self.assertIn("PR_EVIDENCE_BODY", preship)
        self.assertIn(".dev/pr-body.md", preship)
        self.assertIn('python3 scripts/ci/pr-evidence.py --repo "$REPO"', preship)
        self.assertIn('--head HEAD --working --body-file "$PR_BODY_FILE"', preship)
        self.assertIn('fail "request-to-output evidence is missing or invalid', preship)


if __name__ == "__main__":
    unittest.main()
