# Account deletion after email confirmation

Release owner reported the generic server-error message after entering a delivered
confirmation code on 20 November 2026. The retained production log begins in the
tail of a foreign-key exception, so its exact constraint is unavailable.

The isolated Postgres reproduction creates an owner, library source, job and saved
JobClip, then calls the normal confirmation route. Before the fix, it fails on
job_clips_job_id_fkey. The route had captured clip paths for storage cleanup but
never removed clip rows before deleting jobs. A second reproduction with a
plan item pointing at the current job fails on plan_items_current_job_id_fkey.

Acceptance is a 204 response, absence of the user/job/clip rows, and retained
storage-cleanup manifests. Wrong or expired tokens must continue to fail before
erasure. Concurrent unseen children must continue to cause transaction rollback.

The fix removes only clip IDs captured under lock, before deleting the captured
jobs, and clears current-job links in the captured locked plans. No schema migration or client update is required.

Verified locally against a freshly migrated disposable Postgres database.
The regression fails before the change and passes after it. The media-library
erasure integration suite and existing account-route suite pass together, 23 tests.
Production deployment and a user-initiated retry remain pending. KRI-55 is the
historical account-lifecycle reference in the repository runbook. Linear lookup
returned no matching historical issue during this investigation.
