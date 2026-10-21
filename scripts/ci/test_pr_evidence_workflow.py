"""Integration failure contracts for request-to-output PR evidence.

Written before wiring: a body edit must recheck without rerunning native CI;
candidate code must never execute in the independent evidence job; body text
must stay in the event file; missing baseline policy must fail that job; the
existing required lint check must cover code pushes; local checks must include
uncommitted files and reject missing evidence for affected changes.
"""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest


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

    def test_body_edits_have_a_small_independent_read_only_check(self):
        workflow = self.workflow()
        self.assertIn(
            "types: [opened, reopened, synchronize, edited, ready_for_review]", workflow
        )
        self.assertIn("branches: [main, dev]", workflow)
        self.assertIn("name: PR evidence", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("write", workflow)
        self.assertNotIn("github.event.pull_request.body", workflow)
        self.assertEqual(workflow.count("persist-credentials: false"), 2)
        self.assertIn("ref: ${{ github.event.pull_request.base.sha }}", workflow)
        self.assertIn("ref: ${{ github.event.pull_request.head.sha }}", workflow)
        self.assertIn('--event "$GITHUB_EVENT_PATH"', workflow)
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
        self.assertIn('--event "$GITHUB_EVENT_PATH"', lint)
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
