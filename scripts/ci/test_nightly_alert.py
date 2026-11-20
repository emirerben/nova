"""nightly-alert.py tells a person when a scheduled CI or iOS run is red (KRI-1549).

The nightly `iOS` run was red 14 nights in a row and the nightly `CI` run 10
of 14 (2026-11-05..19) and nobody was told. The ways this alert can fail,
written before the code:

1. It fires outside the nightly: a PR, push or manual run opens the issue, or a
   green run of a feature branch closes the real one.
2. It is skipped on exactly the nights that matter: an upstream job fails or
   is cancelled and the alert job, lacking `always()`, never starts.
3. It runs before some job has finished (that job is not in its `needs`), so a
   late failure is missed and the night is called green.
4. It calls a red night green: a cancelled or timed-out job (the KRI-1379
   shards cancelled at their time limit) is not counted.
5. It calls a green night red: skipped jobs (selection) or the alert's own
   in-progress job count as failures.
6. The second red night opens a duplicate instead of commenting on the open
   issue.
7. CI's green night closes iOS's issue (both share one label).
8. The first red night ever fails because the label does not exist yet.
9. A flaky test that passed on retry is named as failing, or a test that never
   passed is left out.
10. A night where a whole suite fails makes the body longer than GitHub
    accepts (65,536 characters), the call fails and nobody is told.
11. The text says "last night" although the run started at noon the next day;
    it must name the run's start time and commit.
12. A log that cannot be fetched (expired, API error) crashes the alert instead
    of it still naming the failed jobs.
13. Assigning a person fails (an organisation owner, a removed collaborator)
    and so no issue is created at all.
14. A re-run attempt is judged by an earlier attempt's jobs.
15. The runner's gh refuses to print a log that holds terminal colour codes
    (every runner log does), so no test is ever named. Found by the first
    drill on 2026-11-20; the local gh printed the same logs.
"""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import unittest

SCRIPT = Path(__file__).resolve().with_name("nightly-alert.py")
SPEC = importlib.util.spec_from_file_location("nightly_alert", SCRIPT)
nightly_alert = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nightly_alert)

WORKFLOWS = SCRIPT.parents[2] / ".github/workflows"
REPO = "owner/repo"

# Lines copied from the red 2026-11-19 nightlies (iOS run 44518781625 shard 2/5,
# CI run 44506039771 screens leg), with their runner timestamps.
UI_SHARD_LOG = """\
2026-11-19T12:34:32.8713370Z Test Case '-[KriaUITests.CreationUITests testCleanUpSpeechApprovalShowsTheRenderStageAndKeepsTheConversationInView]' failed (29.177 seconds).
2026-11-19T12:35:05.5712200Z Test Case '-[KriaUITests.CreationUITests testCleanUpSpeechApprovalShowsTheRenderStageAndKeepsTheConversationInView]' failed (32.764 seconds).
2026-11-19T12:50:01.0000000Z Test Case '-[KriaUITests.CreationUITests testEditorChatDraftPersistsWhenReturningToTheMainChat]' failed (40.100 seconds).
2026-11-19T13:31:50.0000000Z Test Case '-[KriaUITests.CreationUITests testEditorChatDraftPersistsWhenReturningToTheMainChat]' passed (31.200 seconds).
2026-11-19T13:31:55.7395450Z UI coverage mismatch: missing=[], not passed=['KriaUITests/CreationUITests/testCleanUpSpeechApprovalShowsTheRenderStageAndKeepsTheConversationInView'], unexpected=[]
2026-11-19T13:31:55.7398090Z Flaky (passed on retry): KriaUITests/CreationUITests/testEditorChatDraftPersistsWhenReturningToTheMainChat (passed after 2 attempts)
2026-11-19T13:31:55.8267970Z ##[error]Process completed with exit code 1.
"""
SCREENS_LOG = """\
2026-11-19T11:15:31.7583790Z Test Case '-[KriaUITests.AccountPrivacyUITests testExcludedFontSurvivesPickerReturnSaveAndReopen]' passed (62.378 seconds).
2026-11-19T11:16:48.6553280Z Test Case '-[KriaUITests.AccountPrivacyUITests testFinishedEditLearningConsentAcceptAndDisableKeepsApprovedPreference]' failed (76.917 seconds).
2026-11-19T11:21:42.7854000Z ** TEST EXECUTE FAILED **
2026-11-19T11:21:54.3562030Z ##[error]Process completed with exit code 65.
"""
PYTEST_LOG = """\
2026-11-12T10:01:00.0000000Z FAILED tests/tasks/test_generative_build.py::test_variant_order - AssertionError: assert 1 == 2
2026-11-12T10:01:00.1000000Z ERROR tests/routes/test_me_jobs.py::test_list - RuntimeError: db gone
2026-11-12T10:01:00.2000000Z ========= 1 failed, 1 error, 812 passed in 301.2s =========
"""


def job(job_id, name, conclusion, status="completed", failed_step=None):
    steps = [{"name": "Set up job", "conclusion": "success"}]
    if failed_step:
        steps.append({"name": failed_step, "conclusion": conclusion})
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "html_url": f"https://github.com/{REPO}/actions/runs/7/job/{job_id}",
        "steps": steps,
    }


def run(name="iOS", branch="main", attempt=1):
    return {
        "name": name,
        "event": "schedule",
        "head_branch": branch,
        "head_sha": "333a2a7118266c82b8d968e1cfa92a1e75b28665",
        "run_attempt": attempt,
        "run_started_at": "2026-11-19T11:53:09Z",
        "html_url": f"https://github.com/{REPO}/actions/runs/7",
    }


class FakeGh:
    """Answers the gh calls nightly-alert.py makes and records the writes."""

    def __init__(self, jobs, issues=(), logs=None, run_json=None, assign_fails=False, escape_guard=False):
        self.escape_guard = escape_guard
        self.jobs = jobs
        self.issues = list(issues)
        self.logs = logs or {}
        self.run_json = run_json or run()
        self.assign_fails = assign_fails
        self.calls = []
        self.writes = []

    def __call__(self, args, stdin=None):
        self.calls.append(args)
        if args[0] == "api":
            path = args[-1]
            if path == f"repos/{REPO}":
                return json.dumps({"default_branch": "main"})
            if path == f"repos/{REPO}/actions/runs/7":
                return json.dumps(self.run_json)
            match = re.fullmatch(rf"repos/{REPO}/actions/runs/7/attempts/(\d+)/jobs\?per_page=100&page=(\d+)", path)
            if match:
                jobs = self.jobs if match.group(2) == "1" else []
                return json.dumps({"total_count": len(self.jobs), "jobs": jobs, "attempt": int(match.group(1))})
            match = re.fullmatch(rf"repos/{REPO}/actions/jobs/(\d+)/logs", path)
            if match:
                log = self.logs.get(int(match.group(1)))
                if log is None:
                    raise subprocess.CalledProcessError(1, args, "", "HTTP 410: logs expired")
                if self.escape_guard and "\x1b[" in log and "--allow-escape-sequences" not in args:
                    raise subprocess.CalledProcessError(
                        1, args, "",
                        "the response contains terminal escape sequences; "
                        "pass --allow-escape-sequences to output it anyway\n",
                    )
                return log
            raise AssertionError(f"unexpected api call {path}")
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.issues)
        if args[:2] == ["issue", "create"] and self.assign_fails and "--assignee" in args:
            raise subprocess.CalledProcessError(1, args, "", "could not assign user")
        self.writes.append((args, stdin))
        if args[:2] == ["issue", "create"]:
            return f"https://github.com/{REPO}/issues/99\n"
        return ""

    def write(self, verb):
        return [(args, stdin) for args, stdin in self.writes if args[:2] == verb]


def alert(fake, env=None, argv=()):
    base = {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_RUN_ID": "7",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "nightly-alert",
        "NIGHTLY_ALERT_ASSIGNEES": "emirerben",
    }
    base.update(env or {})
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return nightly_alert.main(list(argv), env=base, gh=fake)


RED_IOS = [
    job(1, "changes / select", "success"),
    job(2, "ios-tests (native, macos-15, 2/5)", "failure", failed_step="Native UI tests (reuse compiled build)"),
    job(3, "ios-tests (native, macos-15, 3/5)", "cancelled", failed_step="Native UI tests (reuse compiled build)"),
    job(4, "ios-tests (contracts, ubuntu-latest)", "success"),
    job(5, "build-and-test", "failure", failed_step="Require selected iOS tests to pass"),
    job(6, "nightly-alert", None, status="in_progress"),
]
GREEN_IOS = [
    job(1, "changes / select", "success"),
    job(2, "ios-tests (native, macos-15, 2/5)", "success"),
    job(3, "ios-contracts", "skipped"),
    job(5, "build-and-test", "success"),
    job(6, "nightly-alert", None, status="in_progress"),
]
OPEN_IOS = {"number": 41, "title": "Nightly iOS run is red"}


class VerdictTests(unittest.TestCase):
    def test_cancelled_and_failed_jobs_are_red_and_the_alert_itself_is_not(self):  # 4, 5
        red, unfinished = nightly_alert.red_jobs(RED_IOS, "nightly-alert")
        self.assertEqual([j["id"] for j in red], [2, 3, 5])
        self.assertEqual(unfinished, [])

    def test_skipped_jobs_and_the_alert_job_leave_a_green_night_green(self):  # 5
        self.assertEqual(nightly_alert.red_jobs(GREEN_IOS, "nightly-alert"), ([], []))

    def test_a_job_still_running_is_never_called_green(self):  # 3
        jobs = GREEN_IOS + [job(8, "late-job", None, status="in_progress")]
        red, unfinished = nightly_alert.red_jobs(jobs, "nightly-alert")
        self.assertEqual(red, [])
        self.assertEqual([j["name"] for j in unfinished], ["late-job"])
        fake = FakeGh(jobs, issues=[OPEN_IOS])
        alert(fake)
        self.assertEqual(fake.write(["issue", "close"]), [])
        (args, body), = fake.write(["issue", "comment"])
        self.assertIn("late-job", body)

    def test_the_jobs_of_this_attempt_decide_a_re_run(self):  # 14
        fake = FakeGh(GREEN_IOS, run_json=run(attempt=2))
        alert(fake, env={"GITHUB_RUN_ATTEMPT": "2"})
        paths = [args[-1] for args in fake.calls if args[0] == "api"]
        self.assertIn(f"repos/{REPO}/actions/runs/7/attempts/2/jobs?per_page=100&page=1", paths)


class FailedTestTests(unittest.TestCase):
    def test_the_ui_coverage_line_names_the_test_that_never_passed_and_not_the_flaky_one(self):  # 9
        self.assertEqual(
            nightly_alert.failed_tests(UI_SHARD_LOG),
            ["KriaUITests/CreationUITests/testCleanUpSpeechApprovalShowsTheRenderStageAndKeepsTheConversationInView"],
        )

    def test_xctest_failures_without_a_later_pass_are_named(self):  # 9
        self.assertEqual(
            nightly_alert.failed_tests(SCREENS_LOG),
            ["KriaUITests/AccountPrivacyUITests/testFinishedEditLearningConsentAcceptAndDisableKeepsApprovedPreference"],
        )

    def test_missing_and_unexpected_ui_tests_are_named_with_what_went_wrong(self):  # 9
        log = (
            "2026-11-19T13:31:55.0Z UI coverage mismatch: missing=['KriaUITests/A/testGone'], "
            "not passed=[], unexpected=['KriaUITests/B/testExtra']\n"
        )
        self.assertEqual(
            nightly_alert.failed_tests(log),
            ["KriaUITests/A/testGone (did not run)", "KriaUITests/B/testExtra (ran but was not selected)"],
        )

    def test_colour_codes_and_the_logs_byte_order_mark_do_not_hide_a_test(self):  # 15
        log = (
            "\ufeff2026-11-20T15:01:20.6Z Current runner version: '2.338.2'\n"
            "2026-11-20T15:01:35.2Z \x1b[36;1mecho \"Test Case '-[KriaUITests.DrillUITests testInTheScript]' failed\"\x1b[0m\n"
            "2026-11-20T15:01:36.0Z Test Case '-[KriaUITests.DrillUITests testDeliberatelyRed]' failed (0.1 seconds).\n"
            "2026-11-20T15:01:36.1Z \x1b[31mTest Case '-[KriaUITests.DrillUITests testColoured]' failed (0.1 seconds).\x1b[0m\n"
        )
        self.assertEqual(
            nightly_alert.failed_tests(log),
            ["KriaUITests/DrillUITests/testDeliberatelyRed", "KriaUITests/DrillUITests/testColoured"],
        )

    def test_pytest_failures_and_errors_are_named(self):  # 9
        self.assertEqual(
            nightly_alert.failed_tests(PYTEST_LOG),
            [
                "tests/tasks/test_generative_build.py::test_variant_order",
                "tests/routes/test_me_jobs.py::test_list (error)",
            ],
        )


class IssueTests(unittest.TestCase):
    def test_first_red_night_creates_the_label_then_one_assigned_issue(self):  # 8, 11
        fake = FakeGh(RED_IOS, logs={2: UI_SHARD_LOG, 5: "##[error]Process completed with exit code 1.\n"})
        alert(fake)
        verbs = [args[:2] for args, _ in fake.writes]
        self.assertEqual(verbs, [["label", "create"], ["issue", "create"]])
        (args, body), = fake.write(["issue", "create"])
        self.assertEqual(args[args.index("--title") + 1], "Nightly iOS run is red")
        self.assertEqual(args[args.index("--label") + 1], "nightly-red")
        self.assertEqual(args[args.index("--assignee") + 1], "emirerben")
        self.assertIn("2026-11-19 11:53 UTC", body)
        self.assertIn("333a2a711", body)
        self.assertIn(f"https://github.com/{REPO}/actions/runs/7", body)
        self.assertNotIn("last night", body.lower())
        self.assertIn("ios-tests (native, macos-15, 2/5)", body)
        self.assertIn("ios-tests (native, macos-15, 3/5)", body)
        self.assertIn("cancelled", body)
        self.assertIn("Native UI tests (reuse compiled build)", body)
        self.assertIn("testCleanUpSpeechApprovalShowsTheRenderStageAndKeepsTheConversationInView", body)
        self.assertNotIn("testEditorChatDraftPersistsWhenReturningToTheMainChat", body)

    def test_a_second_red_night_comments_on_the_open_issue(self):  # 6
        fake = FakeGh(RED_IOS, issues=[OPEN_IOS], logs={2: UI_SHARD_LOG})
        alert(fake)
        self.assertEqual(fake.write(["issue", "create"]), [])
        (args, body), = fake.write(["issue", "comment"])
        self.assertEqual(args[2], "41")
        self.assertIn("testCleanUpSpeechApprovalShowsTheRenderStageAndKeepsTheConversationInView", body)

    def test_a_green_night_closes_its_own_workflows_issue_only(self):  # 7, 11
        issues = [{"number": 40, "title": "Nightly CI run is red"}, OPEN_IOS]
        fake = FakeGh(GREEN_IOS, issues=issues)
        alert(fake)
        (args, _), = fake.write(["issue", "close"])
        self.assertEqual(args[2], "41")
        comment = args[args.index("--comment") + 1]
        self.assertIn("2026-11-19 11:53 UTC", comment)
        self.assertIn("333a2a711", comment)
        self.assertIn(f"https://github.com/{REPO}/actions/runs/7", comment)

    def test_a_green_night_with_no_open_issue_writes_nothing(self):
        fake = FakeGh(GREEN_IOS)
        alert(fake)
        self.assertEqual(fake.writes, [])

    def test_a_run_of_another_branch_never_touches_mains_issue(self):  # 1
        fake = FakeGh(GREEN_IOS, issues=[OPEN_IOS], run_json=run(branch="scratch/drill"))
        alert(fake)
        self.assertEqual(fake.writes, [])
        fake = FakeGh(RED_IOS, issues=[OPEN_IOS], run_json=run(branch="scratch/drill"))
        alert(fake)
        (args, _), = fake.write(["issue", "create"])
        self.assertEqual(args[args.index("--title") + 1], "Nightly iOS run is red (branch scratch/drill)")
        self.assertNotIn("--assignee", args)

    def test_an_unreadable_log_still_names_the_failed_jobs(self):  # 12
        fake = FakeGh(RED_IOS)  # every log fetch fails
        alert(fake)
        (_, body), = fake.write(["issue", "create"])
        self.assertIn("ios-tests (native, macos-15, 2/5)", body)
        self.assertIn("log could not be read", body)

    def test_a_gh_that_refuses_colour_codes_is_asked_again_to_print_the_log(self):  # 15
        coloured = "2026-11-20T15:01:35.2Z ##[group]Run \x1b[36;1mbash verify.sh\x1b[0m\n" + UI_SHARD_LOG
        fake = FakeGh(RED_IOS, logs={2: coloured}, escape_guard=True)
        alert(fake)
        (_, body), = fake.write(["issue", "create"])
        self.assertIn("testCleanUpSpeechApprovalShowsTheRenderStageAndKeepsTheConversationInView", body)
        self.assertNotIn("escape sequences", body)

    def test_an_assignee_github_refuses_does_not_stop_the_issue(self):  # 13
        fake = FakeGh(RED_IOS, logs={2: UI_SHARD_LOG}, assign_fails=True)
        alert(fake)
        (args, _), = fake.write(["issue", "create"])
        self.assertNotIn("--assignee", args)

    def test_a_whole_failing_suite_fits_in_one_issue(self):  # 10
        names = "".join(
            f"2026-11-19T12:00:00.0Z Test Case '-[KriaUITests.HugeUITests test{i:04d}{'X' * 150}]' failed (1.0 seconds).\n"
            for i in range(1500)
        )
        jobs = [job(n, f"ios-tests (native, macos-15, {n}/5)", "failure", failed_step="Native UI tests") for n in range(1, 6)]
        fake = FakeGh(jobs + [job(9, "nightly-alert", None, status="in_progress")], logs={n: names for n in range(1, 6)})
        alert(fake)
        (_, body), = fake.write(["issue", "create"])
        self.assertLess(len(body), 65_536)
        self.assertIn("more", body)
        for n in range(1, 6):
            self.assertIn(f"ios-tests (native, macos-15, {n}/5)", body)

    def test_dry_run_prints_and_writes_nothing(self):
        fake = FakeGh(RED_IOS, logs={2: UI_SHARD_LOG})
        alert(fake, argv=["--dry-run"])
        self.assertEqual(fake.writes, [])


def workflow_jobs(text):
    """Each top-level job of a workflow, by id, as its own text."""
    jobs, name = {}, None
    for line in text.split("\njobs:\n", 1)[1].splitlines():
        match = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
        if match:
            name = match.group(1)
            jobs[name] = []
        elif name:
            jobs[name].append(line)
    return {name: "\n".join(lines) for name, lines in jobs.items()}


class WiringTests(unittest.TestCase):
    """Both scheduled workflows end in the alert, and only the nightly reports."""

    def test_each_nightly_runs_the_alert_last_on_schedule_only(self):  # 1, 2, 3
        for name in ("ci.yml", "ios.yml"):
            with self.subTest(workflow=name):
                text = (WORKFLOWS / name).read_text()
                self.assertRegex(text, r"\non:\n  schedule:\n")
                jobs = workflow_jobs(text)
                alert_job = jobs.pop("nightly-alert")
                self.assertIn("    if: always() && github.event_name == 'schedule'\n", alert_job + "\n")
                needs = re.search(r"^    needs: \[(.*)\]$", alert_job, re.M).group(1)
                self.assertEqual(sorted(needs.split(", ")), sorted(jobs))
                self.assertIn("      issues: write", alert_job)
                self.assertIn("      actions: read", alert_job)
                self.assertIn("run: python3 scripts/ci/nightly-alert.py", alert_job)
                self.assertNotIn("continue-on-error", alert_job)


if __name__ == "__main__":
    unittest.main()
