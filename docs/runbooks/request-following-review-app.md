# Local request-following review app

Start from the repository root:

```bash
python3 scripts/request-following-review.py --prod
```

The app opens at `http://127.0.0.1:4455`. It uses `ADMIN_PROD_API_KEY` from
`.env`, keeps the key in the local Python process, and requires no frontend build.
For a local API, use `--api-url http://127.0.0.1:8000` and `ADMIN_API_KEY`.
`--port` changes the app port; `--no-open` suppresses opening the browser.
The production API must have this change deployed first. An empty queue explains
whether collection is disabled or has no eligible sessions.

## Review a session

1. Select a session and read the creator's requests and corrections.
2. Inspect AI turn plans, execution receipts and available saved-edit metadata.
3. Select the creator messages supporting the expected outcome. Enter that outcome,
   your reviewer name and rationale. Save approval, rejection or a clarification need.
4. Export an approved intent packet to continue regression work privately.

An intent approval does not certify the final video or approve a fixture for git.
Failed turns are execution diagnostics, not proof that the AI misunderstood a request.
The app labels understanding and persisted execution as unassessed, and final video
as unverified. It does not download videos. Reviewer names are attributed human input;
the shared admin credential does not establish individual identity.

Import the exported packet into the existing private regression workflow:

```bash
python3 scripts/request-following-loop.py --store /private/tmp/nova-learning-evidence \
  import-queue --input /path/to/export.json
```

The store must be outside git and mode 700. Re-importing an existing version refuses
to overwrite its history. Imported intent retains its source pointers; promotion
review and all measured evidence start empty. Author a runnable incident and check
redaction before using the existing `review` and `promote` commands described in
[the learning-loop runbook](request-following-learning-loop.md). Source files and
exports contain private content: they never belong in git. Exports are local copies;
server revocation and expiry cannot erase copies already downloaded.

## Automatic collection

Collection is off until explicitly enabled for a cohort. Configure the API and
worker/beat consistently:

- `REQUEST_FOLLOWING_COLLECTION_ENABLED=true`
- `REQUEST_FOLLOWING_USER_IDS=["creator-uuid"]` and/or
  `REQUEST_FOLLOWING_THREAD_IDS=["thread-uuid"]`
- `REQUEST_FOLLOWING_HOLDOUT_THREAD_IDS=["unseen-thread-uuid"]`, if needed before capture

A configured creator or thread must also pass the existing training-consent/internal-
account-grant policy. Empty or malformed scopes capture nothing. No new grants are
created by this feature. Deployment and production activation need normal approval.

Celery Beat sweeps up to 25 threads every 15 minutes. A durable cursor wraps through
the cohort and catches later changes, including ones that did not update the thread
creation timestamp. A database lock prevents concurrent collector races. Each new
source version invalidates the previous version for review/export; unchanged versions
are not duplicated. Review saves also reject stale content and concurrent reviews.

Holdout membership is immutable and holdout bodies are never collected or exported.
Adding an already exposed thread to the holdout setting does not make it unseen.
Each read/review/export rechecks scope, current eligibility, deletion and expiry.
Revocation hides old captures immediately; a new grant cannot revive them. The collector
erases revoked bodies when encountered and expired bodies in batches of 100, even
while capture is disabled. Private bodies expire after 30 days; minimal metadata
preserves split and deduplication until the creator or thread is deleted.

Captures are bounded to 2,000 events, 800 turns, 50 linked jobs and 2 MB. Incomplete
captures cannot receive approval. Signed URLs are scrubbed. Available current saved
edits are captured, but this is not a complete historical record of every manual edit.

## Daily free regression reports

The **Request-following regressions** GitHub workflow runs daily at 05:17 UTC,
can be started manually, and runs for relevant pull requests. It executes the existing
incident corpus, request-following replay suite and real PostgreSQL queue journey.
It uses dummy provider keys, explicit replay mode and a disposable database.

The local app links to workflow runs. Each run publishes a readable summary and a
14-day artifact containing JUnit, test logs, `report.json`, `coverage.json` and recorded
replay KPIs. These reports contain committed fixture identities, not private captures.
They identify failing checks and coverage gaps; they do not automatically generate
fixes, mutate prompts or turn newly collected sessions into ground truth.

Run the same report locally with a migrated disposable `_test` database:

```bash
DATABASE_URL=postgresql://localhost/nova_learning_test \
  src/apps/api/.venv/bin/python scripts/request-following-regressions.py \
  --output .request-following-report
```

A failed test, runner error or absent JUnit cannot produce a passing report. Historical
replay KPIs retain their recorded-evidence limitations; daily success is not evidence
of live-provider quality or rendered-video correctness.
