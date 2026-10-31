"""wait-for-jobs.py waits for sibling jobs and fails closed on anything but success."""

import importlib.util
from pathlib import Path
import subprocess
import unittest

SPEC = importlib.util.spec_from_file_location(
    "wait_for_jobs", Path(__file__).resolve().with_name("wait-for-jobs.py")
)
wait_for_jobs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wait_for_jobs)


def job(status, conclusion=None):
    return {"status": status, "conclusion": conclusion}


class Clock:
    def __init__(self):
        self.now = 0

    def sleep(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


class WaitForJobsTests(unittest.TestCase):
    def run_wait(self, snapshots, names=("journey-offline",), deadline=600):
        clock = Clock()
        frames = iter(snapshots)

        def fetch():
            frame = next(frames)
            if isinstance(frame, Exception):
                raise frame
            return frame

        wait_for_jobs.wait(list(names), fetch, sleep=clock.sleep, clock=clock, deadline=deadline)
        return clock.now

    def test_waits_until_every_named_job_succeeds(self):
        waited = self.run_wait(
            [
                {},
                {"journey-offline": job("queued")},
                {"journey-offline": job("in_progress"), "journey-library": job("completed", "success")},
                {"journey-offline": job("completed", "success"), "journey-library": job("completed", "success")},
            ],
            names=("journey-offline", "journey-library"),
        )
        self.assertEqual(waited, 3 * wait_for_jobs.POLL_SECONDS)

    def test_any_other_conclusion_fails_the_leg(self):
        for conclusion in ("failure", "cancelled", "skipped", "timed_out", None):
            with self.subTest(conclusion=conclusion):
                with self.assertRaises(SystemExit) as raised:
                    self.run_wait([{"journey-offline": job("completed", conclusion)}])
                self.assertIn("journey-offline", str(raised.exception))

    def test_a_job_that_never_finishes_hits_the_deadline(self):
        stuck = [{"journey-offline": job("in_progress")}] * 100
        with self.assertRaises(SystemExit) as raised:
            self.run_wait(stuck, deadline=60)
        self.assertIn("Gave up", str(raised.exception))

    def test_transient_api_errors_are_retried_not_trusted(self):
        waited = self.run_wait(
            [
                subprocess.CalledProcessError(1, "gh"),
                ValueError("bad json"),
                {"journey-offline": job("completed", "success")},
            ]
        )
        self.assertEqual(waited, 2 * wait_for_jobs.POLL_SECONDS)

    def test_requires_at_least_one_job_name(self):
        with self.assertRaises(SystemExit):
            wait_for_jobs.main(["", ""])


if __name__ == "__main__":
    unittest.main()
