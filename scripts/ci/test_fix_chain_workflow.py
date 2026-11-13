"""The Fix chain workflow step runs where Actions runs it: in a workspace that is
not a git repository (both checkouts use path: subdirectories).

Failure modes first:
- gh pr list with no --repo/GH_REPO fails in a non-git cwd; the gate then counted
  squash subjects only and let a fourth fix through. The stub gh fails exactly
  like that unless the repo is named.
- an index that cannot be fetched must fail the job, not pass on an empty index.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/fix-chain.yml"

GH_STUB = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
d = os.environ["STUB_DIR"]
if args[0] == "api":
    print(open(d + "/pr.json").read())
elif args[:2] == ["pr", "list"]:
    if os.environ.get("STUB_GH_DOWN"):
        sys.exit(1)
    if "--repo" not in args and not os.environ.get("GH_REPO"):
        sys.stderr.write("failed to run git: fatal: not a git repository\\n")
        sys.exit(1)
    print(open(d + "/index.json").read())
"""


def run_block():
    step = WORKFLOW.read_text().split("- name: Check fix chain\n", 1)[1]
    script = step.split("        run: |\n", 1)[1]
    lines = []
    for line in script.splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line[10:] if line else "")
    return "\n".join(lines)


class FixChainWorkflowTests(unittest.TestCase):
    def run_step(self, *, gh_down=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, stub, temp = root / "work", root / "stub", root / "tmp"
            for d in (work, stub, temp):
                d.mkdir()
            checker = ROOT / "scripts/ci/fix-chain-check.py"
            policy = work / ".fix-chain-policy/scripts/ci"
            policy.mkdir(parents=True)
            (policy / "fix-chain-check.py").write_text(checker.read_text())
            diff = work / ".fix-chain-diff"
            diff.mkdir()

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(diff), *args], text=True
                ).strip()

            git("init", "-q", "-b", "main")
            git("config", "user.email", "t@invalid.local")
            git("config", "user.name", "t")
            (diff / "f.txt").write_text("1\n")
            git("add", ".")
            git("commit", "-qm", "chore: base")
            base = git("rev-parse", "HEAD")
            (diff / "f.txt").write_text("2\n")
            git("commit", "-qam", "fix: fourth")
            head = git("rev-parse", "HEAD")

            (stub / "pr.json").write_text(json.dumps({
                "head": {"sha": head, "ref": "codex/kri-9-fourth"},
                "title": "fix: fourth",
                "body": "no list",
            }))
            (stub / "index.json").write_text(json.dumps([
                {"number": n, "title": f"fix: part {n}", "headRefName": "codex/kri-9-part"}
                for n in (1, 2, 3)
            ]))
            gh = stub / "gh"
            gh.write_text(GH_STUB)
            gh.chmod(0o755)
            env = {
                "PATH": f"{stub}:{os.environ['PATH']}",
                "STUB_DIR": str(stub),
                "RUNNER_TEMP": str(temp),
                "GITHUB_STEP_SUMMARY": str(root / "summary.md"),
                "GITHUB_REPOSITORY": "example/nova",
                "PR_NUMBER": "50",
                "BASE_SHA": base,
                "HEAD_SHA": head,
                "FIX_CHAIN_RETRY_SLEEP": "0",
            }
            if gh_down:
                env["STUB_GH_DOWN"] = "1"
            done = subprocess.run(
                ["bash", "-c", run_block()],
                cwd=work, env=env, capture_output=True, text=True,
            )
            return done.returncode, done.stdout + done.stderr

    def test_prior_fixes_known_only_by_branch_are_counted_outside_a_git_cwd(self):
        code, out = self.run_step()
        self.assertEqual(code, 1, out)
        self.assertIn("KRI-9 already has 3 prior fix PR(s)", out)

    def test_unfetchable_index_fails_the_job(self):
        code, out = self.run_step(gh_down=True)
        self.assertEqual(code, 1, out)
        self.assertIn("Could not fetch the merged-PR index", out)


if __name__ == "__main__":
    unittest.main()
