"""Offline CLI integration exercise with real Git and a fixture GitHub CLI."""

import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


class ProvenanceCLITests(unittest.TestCase):
    def test_real_cli_and_git_history(self):
        resolver = Path(__file__).resolve().parents[1] / "upload_provenance.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", directory, *args], text=True
                ).strip()

            git("init", "-q")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            git("commit", "--allow-empty", "-qm", "uploaded")
            old = git("rev-parse", "HEAD")
            git("commit", "--allow-empty", "-qm", "candidate")
            head = git("rev-parse", "HEAD")
            policy = root / "scripts/ios/upload_provenance.py"
            policy.parent.mkdir(parents=True)
            policy.write_bytes(resolver.read_bytes())
            git("add", "scripts/ios/upload_provenance.py")
            git("commit", "-qm", "workflow")
            workflow = git("rev-parse", "HEAD")
            git("checkout", "-q", head)
            self.assertFalse(
                policy.exists(), "older binary source must predate the helper"
            )
            (root / ".git/FETCH_HEAD").write_text(workflow + "\n")
            fixture = root / "fixture.json"
            cli = root / "gh"
            cli.write_text(
                "#!"
                + sys.executable
                + "\n"
                + textwrap.dedent("""\
        import json, os, sys
        from pathlib import Path
        f = json.loads(Path(os.environ['FIXTURE']).read_text())
        a = sys.argv
        if a[1] == 'api': print(json.dumps(f['raw_responses'][a[2]]))
        elif a[1:3] == ['run','list']: print(json.dumps(f['runs']))
        elif '--log' in a: print(f['logs'][a[3]])
        elif f['jobless'] and a[3] == '2' and a[a.index('--attempt')+1] == str(f.get('empty_attempt',1)): print(json.dumps({'jobs':[]}))
        elif f['unallocated'] and a[3] == '2': print(json.dumps({'jobs':[{'databaseId':22,'name':'release','conclusion':'cancelled','completedAt':'2','steps':[]}]}))
        else: print(json.dumps({'jobs': f.get('job_override') or [{'name':'release','conclusion': f.get('job_conclusion','success'),'completedAt':a[3], 'steps':f['steps']}]}))
        """)
            )
            cli.chmod(0o755)

            def logs(sha):
                return "\n".join(
                    [
                        "release\tRun actions/checkout@v4\t2026-10-21T19:58:25Z [command]/usr/bin/git log -1 --format=%H",
                        f"release\tRun actions/checkout@v4\t2026-10-21T19:58:26Z {sha}",
                        "release\tUpload and distribute to external TestFlight\t2026-10-21T20:00:00Z [20:00:00]: Successfully uploaded the new binary to App Store Connect",
                    ]
                )

            data = {
                "runs": [
                    {
                        "databaseId": 1,
                        "status": "completed",
                        "attempt": 1,
                        "headSha": workflow,
                    }
                ],
                "logs": {"1": logs(old)},
                "steps": [
                    {"name": "Archive signed Release IPA", "conclusion": "success"},
                    {
                        "name": "Upload and distribute to external TestFlight",
                        "conclusion": "success",
                    },
                ],
            }
            env = dict(
                os.environ,
                PATH=directory + ":" + os.environ["PATH"],
                FIXTURE=str(fixture),
                GITHUB_RUN_ID="9",
                GITHUB_REPOSITORY="fixture/repo",
                GITHUB_SHA=workflow,
                RUNNER_TEMP=directory,
                HEAD_SHA=head,
            )
            for name, sources, expected in [
                ("incident", [old], old + " false"),
                ("true duplicate", [head], head + " true"),
                ("out of order", [head, old], head + " true"),
                ("unknown", ["unknown"], None),
                ("unallocated confirmed", [old, "unknown"], old + " false"),
                ("unallocated unproven", [old, "unknown"], None),
                ("no jobs cancelled", [old, "unknown"], old + " false"),
                ("no jobs wrong run", [old, "unknown"], None),
                ("no jobs wrong attempt", [old, "unknown"], None),
                ("no jobs wrong repo", [old, "unknown"], None),
                ("no jobs wrong workflow", [old, "unknown"], None),
                ("no jobs wrong branch", [old, "unknown"], None),
                ("no jobs active", [old, "unknown"], None),
                ("no jobs success", [old, "unknown"], None),
                ("no jobs missing status", [old, "unknown"], None),
                ("no jobs incomplete inventory", [old, "unknown"], None),
                ("no jobs missing total", [old, "unknown"], None),
                ("no jobs false total", [old, "unknown"], None),
                ("no jobs missing list", [old, "unknown"], None),
                ("no jobs nonempty inventory", [old, "unknown"], None),
                ("no jobs no prior upload", [old, "unknown"], None),
                ("no jobs rerun preserves upload", [old, head], head + " true"),
                ("cancelled after transfer", [old, head], head + " true"),
                ("ambiguous releases", [old], None),
                ("wrong job name", [old], None),
            ]:
                data["runs"] = [
                    {
                        "databaseId": i,
                        "status": "completed",
                        "attempt": 1,
                        "headSha": workflow,
                    }
                    for i in range(1, len(sources) + 1)
                ]
                data["logs"] = {str(i): logs(sha) for i, sha in enumerate(sources, 1)}
                data["unallocated"] = name.startswith("unallocated")
                data["raw_job"] = {
                    "status": "completed",
                    "conclusion": "cancelled",
                    "runner_id": 0,
                    "runner_name": "",
                    "runner_group_id": 0,
                    "runner_group_name": "",
                    "steps": [],
                }
                if name == "unallocated unproven":
                    del data["raw_job"]["runner_id"]
                data["jobless"] = name.startswith("no jobs")
                data["empty_attempt"] = (
                    2 if name == "no jobs rerun preserves upload" else 1
                )
                if data["empty_attempt"] == 2:
                    data["runs"][1]["attempt"] = 2
                data["job_conclusion"] = (
                    "cancelled" if name == "cancelled after transfer" else "success"
                )
                data["job_override"] = None
                if name == "ambiguous releases":
                    data["job_override"] = [{"name": "release"}, {"name": "release"}]
                if name == "wrong job name":
                    data["job_override"] = [{"name": "other"}]
                if name == "no jobs no prior upload":
                    data["runs"] = data["runs"][1:]
                raw_attempt = {
                    "id": 2,
                    "run_attempt": data["empty_attempt"],
                    "status": "completed",
                    "conclusion": "cancelled",
                    "head_branch": "main",
                    "path": ".github/workflows/testflight.yml",
                    "repository": {"full_name": "fixture/repo"},
                }
                raw_jobs = {"total_count": 0, "jobs": []}
                changes = {
                    "no jobs wrong run": ("id", 3),
                    "no jobs wrong attempt": ("run_attempt", 2),
                    "no jobs wrong repo": ("repository", {"full_name": "other/repo"}),
                    "no jobs wrong workflow": ("path", ".github/workflows/other.yml"),
                    "no jobs wrong branch": ("head_branch", "feature"),
                    "no jobs active": ("status", "in_progress"),
                    "no jobs success": ("conclusion", "success"),
                }
                if name in changes:
                    key, value = changes[name]
                    raw_attempt[key] = value
                if name == "no jobs missing status":
                    del raw_attempt["status"]
                if name == "no jobs incomplete inventory":
                    raw_jobs["total_count"] = 1
                if name == "no jobs missing total":
                    del raw_jobs["total_count"]
                if name == "no jobs false total":
                    raw_jobs["total_count"] = False
                if name == "no jobs missing list":
                    del raw_jobs["jobs"]
                if name == "no jobs nonempty inventory":
                    raw_jobs["jobs"] = [{"id": 22, "name": "release"}]
                attempt_path = f"repos/fixture/repo/actions/runs/2/attempts/{data['empty_attempt']}"
                data["raw_responses"] = {
                    "repos/fixture/repo/actions/jobs/22": data["raw_job"],
                    attempt_path: raw_attempt,
                    f"{attempt_path}/jobs?per_page=100": raw_jobs,
                }
                fixture.write_text(json.dumps(data))
                result = subprocess.run(
                    [sys.executable, str(resolver)],
                    cwd=directory,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                if expected is None:
                    self.assertNotEqual(result.returncode, 0, name)
                    if name.startswith("no jobs"):
                        reason = (
                            "No verified upload in retained history"
                            if name == "no jobs no prior upload"
                            else "missing or ambiguous release job"
                        )
                        self.assertIn(reason, result.stderr, name)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), expected, name)

            # Execute the real workflow's policy loader from an old checkout.
            workflow_text = (
                resolver.parents[2] / ".github/workflows/testflight.yml"
            ).read_text()
            loader = re.search(
                r'(?m)^          git show "\$GITHUB_SHA:scripts/ios/upload_provenance.py"[^\n]*\n          provenance=[^\n]*\n          read -r base superseded[^\n]*',
                workflow_text,
            )
            self.assertIsNotNone(
                loader, "workflow must load policy from its own revision"
            )
            data["unallocated"] = False
            data["job_override"] = None
            data["job_conclusion"] = "success"
            data["runs"] = [
                {
                    "databaseId": 1,
                    "status": "completed",
                    "attempt": 1,
                    "headSha": workflow,
                }
            ]
            data["logs"] = {"1": logs(old)}
            fixture.write_text(json.dumps(data))
            script = (
                "set -euo pipefail\n"
                + textwrap.dedent(loader.group())
                + '\nprintf "%s %s\\n" "$base" "$superseded"\n'
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), old + " false")
            self.assertEqual(git("rev-parse", "HEAD"), head)
            self.assertFalse(policy.exists())
