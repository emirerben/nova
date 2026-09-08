# Google Cloud cost controls

This runbook rolls out the £30/month control plane without exposing account IDs
or API keys in git. It does not authorize disabling billing, deleting projects,
or revoking credentials without the verification gates below.

## Monthly allocation

| Scope | Budget | Runtime environment |
|---|---:|---|
| Gemini production, including release canaries | £22 (canaries ≤£2) | `production` |
| Development, manual QA, and live evals | £2 | `development` |
| Omni experiments | £2 | `lab` |
| Cloud Storage | £4 | n/a |

Nova meters provider calls in USD with conservative headroom: $28 / $2.50 /
$2.50 / $2.50 for production, development, Omni, and release canaries. Google
Cloud budgets remain the authoritative GBP alerts.

## 1. Split projects and keys

Create dedicated, billing-enabled projects for `nova-gemini-prod`,
`nova-gemini-dev`, and `nova-omni-lab`. The reconciliation assumes these are
AI-only projects; do not place unrelated paid services in them.

Create one restricted Gemini API key in each project. Restrict every key to the
required Gemini/Vertex API and to its expected application surface. Store keys
only in the relevant secret store:

- Fly production API and workers: production key, `AI_USAGE_ENVIRONMENT=production`.
- Named live-eval GitHub environment and local manual QA: development key,
  `AI_USAGE_ENVIRONMENT=development`.
- Isolated Omni lab worker only: Omni key,
  `AI_USAGE_ENVIRONMENT=lab`; `OMNI_GENERATED_VIDEO_ENABLED` remains false in
  production.

Routine CI must have no Gemini secret. `.github/workflows/agent-evals.yml`
rejects a real key unless the named live workflow sets
`NOVA_PAID_AI_WORKFLOW=1`. Paid development calls also fail closed without
`usage_purpose`, `test_run_id`, estimated maximum cost, and reservation approval.
For direct non-production HTTP QA, these map to
`X-Nova-Usage-Purpose`, `X-Nova-Test-Run-Id`,
`X-Nova-Estimated-Max-Cost-Usd`, and `X-Nova-Reservation-Approved: true`.
Background render work cannot inherit HTTP headers, so use the process-wide
manual-QA envelope in section 5 for those flows. Production ignores test headers;
an internal release canary must instead send `X-Nova-Usage-Purpose: release_canary`
and `X-Nova-Release-Canary-Id`.

## 2. Budgets, email, and Pub/Sub

Copy `infra/gcp-cost-controls/terraform.tfvars.example` to an ignored
`terraform.tfvars`, replace every placeholder, and verify:

- the four project amounts are exactly £22, £2, £2, and £4;
- every project is assigned to the intended one of the two billing accounts
  (each account budget is derived from those assignments);
- the mailbox is actively monitored;
- every project number belongs to the declared billing account.

Then run:

```bash
cd infra/gcp-cost-controls
terraform init
terraform fmt -check
terraform validate
terraform plan -out=nova-cost-controls.tfplan
terraform show nova-cost-controls.tfplan
terraform apply nova-cost-controls.tfplan
```

The module creates correctly named account and project budgets. Every budget
notifies actual and forecast spend at 50%, 75%, 90%, and 100% through both the
monitored email channel and `nova-cost-alerts` Pub/Sub. It refuses a configuration
whose project total is not £30, whose project references an undeclared account,
or whose declared account has no allocation.

After apply, publish a test message to the topic and confirm the monitored
subscriber receives it. Cloud Billing Pub/Sub delivery is at least once, so
consumers must deduplicate by budget ID and threshold.

## 3. Standard Usage Cost export for both accounts

Terraform creates one protected EU dataset per account, but Google Cloud enables
the export from the Billing console:

1. Open each billing account separately.
2. Go to **Billing export → BigQuery export**.
3. Enable only **Standard usage cost data**.
4. Select the FinOps project and `all_billing_data` dataset declared for that
   account; save.
5. Do not enable Detailed, FOCUS, pricing, or CUD exports for this control plane.
6. Confirm `billing-export-bigquery@system.gserviceaccount.com` remains an owner
   of each dataset.

EU multi-region exports can backfill the current and previous month. Initial
catch-up can take up to five days. Keep reconciliation disabled until both tables
have a recent `export_time`.

Grant the Fly reconciliation identity BigQuery Job User on the query project,
BigQuery Data Viewer on both datasets, and Pub/Sub Publisher on the alert topic.
It does not need Billing Account Administrator.

Set the Fly secrets using the real values from both auto-created table names:

```text
BILLING_EXPORT_TARGETS_JSON=[{"billing_account_id":"...","project_id":"...","dataset_id":"all_billing_data","table_id":"gcp_billing_export_v1_...","location":"EU"}, ...]
BILLING_PROJECT_ENVIRONMENT_MAP_JSON={"nova-gemini-prod":"production","nova-gemini-dev":"development","nova-omni-lab":"lab","nova-storage":"excluded"}
BILLING_RECONCILIATION_DELAY_DAYS=3
BILLING_RECONCILIATION_THRESHOLD_PCT=0.10
BILLING_RECONCILIATION_PUBSUB_TOPIC=projects/<finops-project>/topics/nova-cost-alerts
```

Replace `nova-storage` with the real Cloud Storage project ID. The explicit
`excluded` value proves its exported spend is expected but outside the AI ledger;
positive cost from any project absent from the map remains an actionable
`incomplete` result. The 06:00 UTC task converts each account's billing currency
to USD using the exported conversion rate, aggregates only explicitly mapped AI
projects, and compares them with settled reservations for the closed day. A difference greater
than 10% and at least $0.01 emits a Pub/Sub alert. Missing export data is recorded
as `incomplete`, not falsely reported as matched. The persisted ledger side also
breaks each environment total down by purpose and principal, so customer,
internal, live-eval, release-canary, and Omni spend remain independently auditable
even though Cloud Billing can only verify their shared project total.

## 4. Dark launch and enforcement

Deploy migrations `0101`–`0103` before enabling any writer. `0102` builds the
three indexes on the hot `agent_run` table concurrently and validates its new
foreign key without blocking ordinary writes for a full table scan; `0103`
serializes billing claims and media-reference writes around retention. Roll out
in this order:

1. Configure split keys, budgets, exports, IAM, and the alert subscriber.
2. Enable `BILLING_RECONCILIATION_ENABLED=true`; observe delayed rows for three
   days and resolve any persistent `incomplete` result.
3. Enable `AI_COST_CONTROL_ENABLED=true` in development and run one attributed
   replay/live smoke cycle below.
4. Enable it in the Omni lab and verify the explicit cost confirmation.
5. Enable it in production. Internal production calls must include
   `usage_purpose=release_canary` plus a release-canary ID; customer calls are
   attributed through the owning user/job/session.

At 80% of an environment budget, experiments, weekly smoke, manual QA, and
optional background analysis stop. At 90%, internal canaries and new Director
reviews stop. At 100%, every new paid call stops. The schema can represent a
scoped, expiring override, but this release ships no override CLI or endpoint;
do not insert one ad hoc. Cached responses remain available.

API callers receive stable failures: `429 ai_budget_exhausted` includes the
scope, reset time, and whether cached behavior is available; invalid attribution
returns `ai_cost_control_policy_rejected`; a ledger outage returns retryable
`503 ai_cost_control_unavailable`; and an uncertain provider outcome returns
non-retryable `503 ai_provider_outcome_unknown` so clients do not double-spend.

Clip analysis uses a 90-day PostgreSQL reuse tier keyed by creator, source
fingerprint, analyzer, model, prompt version, schema version, and filter hint.
The creator foreign key makes durable entries disappear with account deletion.
Redis is only a 24-hour hot copy: it is deliberately shorter because those keys
are creator-scoped but not indexed for an immediate owner-wide purge.

Application rollback is flag-first and schema-preserving: disable
`AI_COST_CONTROL_ENABLED`, `BILLING_RECONCILIATION_ENABLED`, and
`STORAGE_RETENTION_ENABLED`, then roll the application image back. Once any new
ledger, cache, reconciliation, or retention row has been written, do **not** run
`alembic downgrade 0100`; migration `0101` refuses to destroy that durable audit
data. Contract the schema only in a later, retention-aware migration.

## 5. Paid test policy

Structural evals remain replay-only:

```bash
cd src/apps/api
pytest tests/evals/ -v
```

Full live evals are permitted only when a prompt, model, provider integration, or
structured-output contract changes. The workflow requires a purpose, test-run
ID, maximum cost, explicit reservation approval, and has a hard $2 ceiling. The
weekly provider smoke selects one song-classifier fixture, caps the run at $0.20,
and stores the successful response as a replay artifact.

Create a protected GitHub environment named `paid-ai-evals`, restrict deployment
to reviewed refs/default branch policy, and require a reviewer before manual-run
secrets are released. Create a second `paid-ai-smoke` environment restricted to
the default branch with no reviewer pause; scheduled workflows always execute the
workflow from that branch, so the weekly smoke can run unattended without granting
arbitrary refs the key. Store `GEMINI_API_KEY_DEV` and `DATABASE_URL_DEV` in both
environments as environment secrets rather than repository-wide secrets.
`DATABASE_URL_DEV` must be the persistent development database also used by manual
QA; never point this workflow at an ephemeral Actions Postgres instance. That one
ledger is the atomic authority for manual QA, live evals, and the weekly smoke, so
those paths cannot each mint a separate monthly allowance. Apply migrations to the
database through the normal deployment path before running the workflow; the job
only verifies that the database is at Alembic head. Paid Anthropic judging is
deliberately disabled in the live workflow because its spend is not metered by the
Google reservation ledger; use replay-mode judging separately.

Manual QA uses the development key. Because a render continues in background
workers after the browser request ends, approve one process-wide development
envelope rather than relying on HTTP headers alone:

```text
AI_USAGE_ENVIRONMENT=development
AI_COST_CONTROL_ENABLED=true
AI_MANUAL_QA_TEST_RUN_ID=manual-qa-<ticket-or-date>
AI_MANUAL_QA_MAX_COST_USD=0.50
AI_MANUAL_QA_RESERVATION_APPROVED=true
```

Use a unique run ID, choose a reviewed maximum no greater than $2, restart the
local API/worker, and clear `AI_MANUAL_QA_RESERVATION_APPROVED` immediately after
the session. Every foreground and background call shares that atomic run cap.
Partially supplied live-eval or smoke attribution still fails closed. A
production canary must be explicitly tagged per release and counts against the
dedicated canary allowance within the £22 production budget.

## 6. Quarantine the old shared project

This is intentionally a manual, reversible checkpoint until the final revoke:

1. Run the new production, development, live-eval, and Omni paths once and verify
   their reservations settle under the expected project/environment.
2. Confirm three delayed reconciliation days are `matched` or have an explained
   difference below 10%.
3. Search deployment and CI secret inventories for both old shared key IDs; no
   active workload may reference them.
4. Disable the Gemini API on the old shared project and observe production plus
   one development smoke for 24 hours.
5. Only then revoke its two shared keys. Record operator, timestamp, key IDs, and
   rollback owner in the change ticket. Never paste key values into the ticket.

If any workload fails during the observation window, re-enable the API, fix that
workload's project-specific secret, and restart the verification window. Do not
reintroduce a shared key.

## 7. Storage retention

Keep the EU multi-region bucket. Apply and verify the lifecycle policy:

```bash
gsutil lifecycle set infra/gcs-lifecycle.json gs://$STORAGE_BUCKET
python3 scripts/check_gcs_lifecycle_drift.py --bucket "$STORAGE_BUCKET"
```

New batch uploads land under `staging/<user>/batch/` and are promoted to an
owned job prefix only after the job row is committed. The Job stores every
source generation and deterministic destination before the first copy; a
two-minute maintenance reconciler resumes interrupted copies and publishes any
promoted-but-undispatched render. Repeatedly unrecoverable promotions become an
honest failed job at 23 hours, before the one-day staging lifecycle can remove
their only source. Authenticated generative browser uploads land
directly under `users/<user>/generative/`; each retains a
24-hour database cleanup receipt until the Job transaction atomically attaches
it. Explicit mobile-purpose and synthetic session uploads use 24-hour lifecycle
prefixes. Account deletion immediately purges owned media and also records a
delayed, verified owner-prefix sweep after signed upload capabilities and slow
in-flight PUTs have quiesced. Enable
`STORAGE_RETENTION_ENABLED=true` to create the first generation-pinned report.
The daily scanner processes `STORAGE_RETENTION_SCAN_JOBS` jobs per pass (100 by
default). After seven days, inspect and approve that exact manifest:

```bash
cd src/apps/api
MANIFEST_ID=replace-with-manifest-uuid
OPERATOR=replace-with-operator-name
python scripts/storage_retention.py report "$MANIFEST_ID"
python scripts/storage_retention.py approve "$MANIFEST_ID" --operator "$OPERATOR"
# To reject instead: python scripts/storage_retention.py reject "$MANIFEST_ID" --operator "$OPERATOR"
```

Verify the deployed Privacy Policy shows the September 8, 2026 retention terms,
then and only then enable `STORAGE_RETENTION_DELETE_ENABLED=true`.

The database-backed sweep preserves active/pinned references and publications,
warns at 83 inactive days, deletes inactive sources/editable bases at 90 days,
keeps the latest final and poster for 365 days, and removes superseded derivatives
after seven days. Every delete rechecks both the database reference and exact GCS
generation immediately before deletion.
