# Local request-following review app

Open an existing private capture store from the repository root:

```bash
python3 scripts/request-following-review.py --store /private/tmp/nova-learning-evidence
```

The app opens at `http://127.0.0.1:4455` without a database, frontend build or
production deployment. The store must be outside git and mode 700. Only development
captures with valid eligibility receipts appear. Incomplete captures show their
missing evidence and cannot be approved.

Import an operator-verified capture with its point-in-time eligibility receipt:

```bash
python3 scripts/request-following-loop.py --store /private/tmp/nova-learning-evidence import --input /path/to/capture.json --split development --eligibility-file /path/to/receipt.json
```

Receipt fields are `source_sha256`, `thread_id`, `creator_id`, `eligibility_key`,
`checked_at`, `captured_at`, and `expires_at`. The key identifies the existing
`internal_grant:<uuid>` or `training_consent:<uuid>` checked by the trusted operator;
a receipt does not create consent. Expiry cannot exceed 30 days from capture.
Changed source, wrong ownership, missing receipts and expired evidence are hidden.
A replacement grant cannot revive a capture or extend its original expiry.

File mode shows the eligibility check time and cannot observe later server revocation.
Delete private copies and exports when consent is revoked; expiration hides them in
the app but does not erase their files. Never commit captures, receipts or exports.

After deploying the review API, `python3 scripts/request-following-review.py --prod`
opens the live queue and rechecks current eligibility. It uses `ADMIN_PROD_API_KEY`
from `.env` only in the local Python process. For a local API, use
`--api-url http://127.0.0.1:8000` and `ADMIN_API_KEY`. `--port` changes the app port;
`--no-open` suppresses browser opening.

## Review a session

1. Select a session and read the creator's requests and corrections.
2. Inspect AI turn plans, execution receipts and available saved-edit metadata.
3. Select the creator messages supporting the expected outcome. Enter that outcome,
   your reviewer name and rationale. Save approval, rejection or a clarification need.
4. Export an approved intent packet to continue regression work privately.

An intent approval does not certify the final video or approve a fixture for git.
Failed turns are execution diagnostics, not proof that the AI misunderstood a request.
The app labels understanding and persisted execution as unassessed. Live API mode can
play a generation-pinned current export and record a separate output review (below).
File-store mode still reviews intent only. Reviewer names are attributed human input;
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

Collection is off until explicitly enabled for the approved scope. Configure the API and
worker/beat consistently:

- `REQUEST_FOLLOWING_COLLECTION_ENABLED=true`
- `REQUEST_FOLLOWING_ALL_USERS=true` for every account with current recorded eligibility;
  alternatively leave it false and set `REQUEST_FOLLOWING_USER_IDS=["creator-uuid"]`
  and/or `REQUEST_FOLLOWING_THREAD_IDS=["thread-uuid"]`
- `REQUEST_FOLLOWING_HOLDOUT_THREAD_IDS=["unseen-thread-uuid"]`, if needed before capture

Every creator, including in all-users mode, must pass the existing training-consent/internal-
account-grant policy. Empty scopes capture nothing unless all-users mode is explicitly enabled. Malformed
IDs fail the whole scope closed, including all-users mode. No new grants are
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

### Prepared production activation

The user selected all app accounts. This is scope authorization, not a replacement
for missing consent records. After this PR is reviewed, merged and deployed through
the normal Fly Deploy workflow, the remaining production configuration action is:

```bash
fly secrets set --app nova-video \
  REQUEST_FOLLOWING_COLLECTION_ENABLED=true \
  REQUEST_FOLLOWING_ALL_USERS=true
```

That command changes production and restarts processes; inspect current configuration
before using it. Do not reapply an already active scope merely to verify it.
Verify API and worker use the deployed revision and flags, then inspect the first
collector summary. Ineligible accounts are skipped; never manufacture consent to
increase coverage. To pause new capture, set `REQUEST_FOLLOWING_COLLECTION_ENABLED=false`;
retention cleanup continues. Scope/holdout metadata stays intact.


## Review actual exports and connect them to the daily report

Live API mode offers the current finished export only when the thread, current plan
item, job owner and object path agree. The player uses a 15-minute URL pinned to the
stored object generation. A ready flag alone is not video proof. Missing objects,
changed sources and foreign paths never become playable review evidence.

1. Select the creator messages establishing the **final** request. Later corrections
   replace earlier instructions. Assistant claims that an edit is done are not proof.
2. Write one expectation per line, or correct the prepared checklist. Accepted wording
   can be understood using the preceding assistant proposal plus the creator's acceptance.
3. Watch the actual export. Choose **Met**, **Partly met**, **Missed**, **Unsure**, or
   **Changed by me** for each requirement. The last option excludes intentional manual
   edits from AI failure counts. Judge taste only where the creator requested it.
4. Confirm viewing and, for concrete audio/caption judgements, listening. Approve the
   intent and save. Empty verdicts cannot be submitted. Uncertain and creator-edited
   instructions remain separate; neither is treated as success or failure.
5. Save and next advances only after a successful save. Failed saves keep the fields;
   drafts survive reload in this browser tab's session storage. A new source/review
   version does not silently inherit an old draft. Close the tab to clear its drafts.

The server binds the outcome to job, variant, generation, byte count and MD5 (when
available). It rechecks the source, eligibility and object at save and report time.
It cannot prove a reviewer actually watched every frame or listened; those are explicit
reviewer attestations. No semantic model runs. A recorded review label on a detail
response does not assert the object is still current; `/media` and the report revalidate.

### Prepare 10–20 cases for guided review

Use `--batch /private/path/proposals.json` in live API mode. The mode-600 file must
stay outside the repo. The manifest is an array of up to 20 objects containing `id`,
`source_sha256`, optional `title`, and `expectations` with `text`, `category` and
`source_pointers` (creator messages). Proposals apply only to the matching current
source hash and never prefill verdicts or approve intent. Keep raw captures, downloaded
videos and these proposed requirements private. A deep link `/?case=CASE_UUID` opens
a report reference without paging through the queue.

Prepared offline copies may be reviewed as **local drafts only**. They are not current
server evidence: recheck existing eligibility, source hash, review version and exact
export identity before publishing any draft. Do not refresh hashes or generations to
make an old verdict pass. A source/object conflict needs a fresh review.

### Read-only live report credential and rollout

Set a new random `REQUEST_FOLLOWING_REPORT_TOKEN` on the API and as the GitHub
repository secret of the same name. Use a secret manager/stdin; never commit it or
reuse `ADMIN_PROD_API_KEY`. Deploy through the approved **Fly Deploy** workflow.
No migration or collection-scope change is needed. Blank/mismatched report credentials
return 401; the report token cannot read conversations, obtain media or save reviews.

On schedule or manual dispatch **from main**, the workflow fetches
`GET /request-following-report` with `X-Report-Token`. Pull-request runs never receive
the credential or call production. Downloadable `reviewed-sessions.json` and
`reviewed-sessions.md` are added to the existing 14-day artifact and summary.
Missing configuration, stale/malformed responses and HTTP failure fail this step
visibly, while the existing offline report remains available.

The projection covers current eligible development captures with creator request
activity in the last 14 days. It includes collection/review coverage, five verdict
counts, evidence dates, stale/unavailable/invalid reviews, and partial/missed categories
ranked by affected sessions. The bounded scan says when more than 1,000 current captures
exist. No population success percentage is inferred from selected review cases.

The `delivery` projection keeps ready jobs, device export attempts, acknowledged
exports and verified exports separate from human-reviewed fulfillment. It includes
recent captured sessions that fail before a Job exists. A device export is verified
only when its current published attempt has a matching successful output report;
historical publication without that report is an acknowledgement, not verification.
Delayed or repeated completion callbacks cannot add another published attempt.

Only a complete current review with every requirement met increments
`fulfilled_reviewed_sessions`. Partial or missed reviews increment
`unfulfilled_reviewed_sessions`. Missing, stale, unclear and creator-changed reviews
remain in `unknown_fulfillment_sessions`. First-attempt fulfillment and Photos-save
acknowledgements are `null`: the retained records do not establish those facts.
This remains a selected-session report. Original-request retry lineage, accepted
revisions, costs and client/server version cohorts remain KRI-728 work.

Only controlled category/verdict vocabulary, numeric counts, timestamps and opaque
case UUIDs leave the private API. The client rebuilds this allowlisted schema before
writing CI artifacts: creator identities, exact request text, free-form notes, signed
URLs and storage paths never enter the report. Open the private case to inspect exact
wording and reproduce a miss before proposing a fix. Historical replay KPIs remain a
separate section and are not relabelled as today's production quality.

After rollout, save a real reviewed case and run the workflow from main. Download the
artifact and confirm that case/category appears, and that unreviewed/manual-change
requirements have not become AI failures. Revocation/source changes/replaced objects
must remove old verdicts from the next report. Removing the report token disables
report access; it does not disable collection or delete private reviews.
