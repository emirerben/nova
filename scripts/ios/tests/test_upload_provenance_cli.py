"""Offline CLI integration exercise with real Git and a fixture GitHub CLI."""

import json
import os
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
            git("commit", "--allow-empty", "-qm", "workflow")
            workflow = git("rev-parse", "HEAD")
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
        if a[1] == 'api': print(json.dumps(f['raw_job']))
        elif a[1:3] == ['run','list']: print(json.dumps(f['runs']))
        elif '--log' in a: print(f['logs'][a[3]])
        elif f['unallocated'] and a[3] == '2': print(json.dumps({'jobs':[{'databaseId':22,'name':'release','conclusion':'cancelled','completedAt':'2','steps':[]}]}))
        else: print(json.dumps({'jobs': [{'name':'release','conclusion':'success','completedAt':a[3], 'steps':f['steps']}]}))
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
                HEAD_SHA=head,
            )
            for name, sources, expected in [
                ("incident", [old], old + " false"),
                ("true duplicate", [head], head + " true"),
                ("out of order", [head, old], head + " true"),
                ("unknown", ["unknown"], None),
                ("unallocated confirmed", [old, "unknown"], old + " false"),
                ("unallocated unproven", [old, "unknown"], None),
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
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), expected, name)
