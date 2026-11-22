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
| 2026-11-21 | Same three face-sampler tests | `api (1)` on main's push run 44902004702, the first main run after #2436 | Caught regression (environment) | Same cause. | Same fix. |
| 2026-11-21 | `scripts/ios/phone-text-style-e2e.py`: "The actual sampler must detect the supplied face" | `journey-offline` on main's push run 44905057478 | Caught regression (environment) | Same cause: the face sampler had no cascade classifier in that job's `cv2`. | Same fix. |
| 2026-11-21 | `tests/integration/test_actual_export_verification.py::test_frame_profile_never_accepts_a_shifted_or_wrong_picture[handheld25-1-frame-late]`: the verifier answered `checked` for a one-frame-late export | 1 of 60 API shard jobs in the database experiment (run 44921274730, `ram r4 s0`); 2 of 480 repeats of the test alone (run 44927118911); 0 of 25 on a laptop | Caught regression (product) | `app/services/actual_export_verification.py` fails a shifted window only when every sample needed its next frame. A 30 fps proxy of 25 fps footage holds every sixth picture for two frames; a sample on a held frame passes as "displayed", so the rule never fires. The test's rate-capped proxy encode differs run to run, and in CI its next-frame scores sit at 0.948-0.952, across the 0.95 bar: when four clear it, the late export is accepted. With a sharp proxy it is accepted in 5 of 6 timing phases, every run (branch `codex/kri-1042-held-frame-shift`). | Fixed as `phone-simple-cut-v6` (KRI-831): a sample that matches both frames no longer counts as in place. The test was not changed. Replayed on 36 stored production windows (none flips) and on 720 generated windows on CI ([kri-831-late-window](kri-831-late-window.md)). |
| 2026-11-21 | `tests/kria/test_intent_review_catalog_postgres.py::test_the_real_frozen_packet_issues_its_one_requirement`: `assert 'ignored' == 'completed'` | `api (4)` on #2448's run 44937463977 (a hooks-only change) | Leak | The test borrows `approved_publication` and `paid` from files that hold the shared intent-review lock for all their tests, but did not hold it itself. pytest builds fixtures in argument order, so `approved_publication` wrote a dispatched execution before `paid` took the lock, and `test_review_sweep_is_time_boxed...` on another worker (same database) settled that execution first. | The test takes the lock through `usefixtures`, which runs before its arguments. `tests/test_intent_review_lock_order.py` fails any test that borrows a locked file's fixtures without it. The structural fix is one database per xdist worker, which removes the lock. |

## Not test failures, but found by the same work

- 2026-11-21: `tests/tasks/test_kria_pinned_r5_ci_wiring.py::test_x1_the_screens_leg_runs_every_ui_test_the_journey_names` fails on any branch that removes jobs from `ci.yml`. It failed in every repeat of the shard experiment, whose throwaway branch reduces `ci.yml` to one job. Expected there; not a defect.
