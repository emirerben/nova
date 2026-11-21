# CI failure log

Every CI test failure fixed during the CI speed work is recorded here and
classified, so that test deletions later rest on evidence:

- **Caught regression**: the test failed because something it guards was
  really wrong (code, environment or data). Keep the test.
- **Change detector**: the test failed only because the implementation moved
  while behaviour stayed correct. Candidates for deletion once the pattern is
  clear. Never authorization, privacy, migration or concurrency tests.
- **Leak**: the test failed because of state another test, the runner or the
  install left behind. Fix the leak; keep the test.

Started 2026-11-21. Review after two weeks (2026-12-05).

| Date | Test | Where it failed | Class | Cause | Fix |
| --- | --- | --- | --- | --- | --- |
| 2026-11-21 | `tests/pipeline/test_face_sampler_worker.py` (3 tests): `module 'cv2' has no attribute 'CascadeClassifier'` | 1 of 45 API shard jobs in the 3-vs-6 shard experiment (run 44903511580, `L6 r3 s0`); reproduced locally in 1 of 12 and 7 of 16 fresh installs | Caught regression (environment) | The lock adopted in #2436 installs three OpenCV distributions into one `cv2/` directory. uv unpacks them in parallel, so the last writer varies; only `opencv-contrib-python` has the classifier in OpenCV 5. The old one-at-a-time `pip install` always ended with contrib on top. | `.github/actions/api-env` rewrites `opencv-contrib-python` last and asserts the classifier exists before any test runs. The real fix is one OpenCV distribution in `pyproject.toml`; that changes the production image and is a separate change. |

## Not test failures, but found by the same work

- 2026-11-21: `tests/tasks/test_kria_pinned_r5_ci_wiring.py::test_x1_the_screens_leg_runs_every_ui_test_the_journey_names` fails on any branch that removes jobs from `ci.yml`. It failed in every repeat of the shard experiment, whose throwaway branch reduces `ci.yml` to one job. Expected there; not a defect.
