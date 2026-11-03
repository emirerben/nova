# Intent review rollout (KRI-1171)

How to turn the intent review on, watch it, and turn it off. What the review does and how it is built is in [the pipeline guide](../pipelines/intent-review.md); day-to-day diagnosis is in [the operating runbook](kria-intent-review.md). This page is only the switchboard.

Everything ships dark: every flag below defaults off, so merging changes nothing for any creator. Nothing here is applied by a deploy. Each change is an explicit `fly secrets set` on the shared app followed by a restart.

## What each switch does

| Key | Default | Switches on | Needs |
| --- | --- | --- | --- |
| `KRIA_INTENT_REVIEW_ENABLED` | `false` | Admission of a row per finished generation, and the frame evidence for it. No provider call. Also the kill switch for every stage. | `KRIA_INTENT_REVIEW_USER_IDS` |
| `KRIA_INTENT_REVIEW_PAID_ENABLED` | `false` | One paid look at up to six frames of the finished video, stored and judged by the server, and the chat receipt (and at most one question) that reports it. | `ENABLED`, `OPENAI_API_KEY`, `AI_OPENAI_INTENT_REVIEW_BUDGET_USD > 0`, `AI_OPENAI_MONTHLY_BUDGET_USD > 0` |
| `KRIA_INTENT_REVIEW_REPAIR_ENABLED` | `false` | Staging one reversible repair draft that the creator approves or undoes. Nothing renders without their Approve tap. | `PAID` |
| `KRIA_INTENT_REVIEW_USER_IDS` | `[]` | Who is admitted. `[]` admits nobody, `["*"]` everyone. | |
| `KRIA_INTENT_REVIEW_MAX_USD` | `0.10` | Per-review ceiling, `0 < x <= 0.25`, stamped on the row at admission. | |
| `KRIA_INTENT_REVIEW_CANARY_ID` | empty | The `release_canary_id` the ledger requires for an internal account. Empty: an internal account's review ends `unavailable(canary_unattributed)` and makes no call. | |
| `AI_OPENAI_INTENT_REVIEW_BUDGET_USD` | `0` | Monthly ledger capability, inside `AI_OPENAI_MONTHLY_BUDGET_USD`. `0` rejects every reserve. Target in production: `50`. | |

A child flag does nothing without its parent, and the flags are read in exactly one place (`app/kria/intent_review_tier.py`). The tier is stamped on a row when it is admitted and the live flags can only lower it, so turning a flag off takes effect on rows already waiting.

With every flag on, a creator reaches the repair tier. The tier is decided only by the flags, the allowlist, the API key and the budgets; there is no code-level brake.

The legacy Gemini quality review is retired for a runtime-v2 thread only once that creator's tier is `review` or higher. Admitting evidence alone never leaves a generation unreviewed. The legacy flags are off in production, so the retirement is a no-op there today.

## Before the first flag

1. The stack is merged and deployed with every flag off. Migrations `0130` (the row) and `0131` (the lifecycle) ran at release. `GET https://nova-video.fly.dev/health` is green.
2. Decide where `tasks.prepare_generation_review` runs, **before** `KRIA_INTENT_REVIEW_ENABLED` goes on in production. It downloads up to 256 MB and runs ffmpeg, and the only worker that consumes its `agent-control` queue today is `light` (1 GB, concurrency 2, embedded Beat), a machine class that has run out of memory on a full MP4 in `/tmp` before. `tasks.review_generation_intent` itself is light (the frames come from the row; nothing is downloaded).
3. Open the Linear issue `Rollout: KRIA_INTENT_REVIEW` in the project. It stays open until the flags are on and verified.
4. A native speaker has read the Turkish copy in `app/kria/intent_review_copy.py`. Not done at the time of writing. The copy this change added beyond the product spec's table (the lead of the staged-repair receipt and the revert wording that does not name a button) needs the same read.

## Order of changes

Apply on the API **and** worker. Never print a value; set budgets and ids first so a later flag finds them in place.

```bash
# 1. budgets and attribution, no behaviour change
fly secrets set --app nova-video \
  AI_OPENAI_INTENT_REVIEW_BUDGET_USD=50 \
  KRIA_INTENT_REVIEW_CANARY_ID=<release_canary_id> \
  KRIA_INTENT_REVIEW_USER_IDS='["<operator user id>"]'

# 2. admission and evidence, no provider call
fly secrets set KRIA_INTENT_REVIEW_ENABLED=true --app nova-video

# 3. paid review and chat receipts (only after the qualification run below)
fly secrets set KRIA_INTENT_REVIEW_PAID_ENABLED=true --app nova-video

# 4. repair staging (only after the wording and timing controls below)
fly secrets set KRIA_INTENT_REVIEW_REPAIR_ENABLED=true --app nova-video
```

After each step restart both process groups (`fly machine restart <id>` for the api and worker machines, as in [the creator agent rollout](creator-agent-rollout.md)) and confirm `fly status --app nova-video`.

Never reuse `CREDIT_QUALITY_REVIEW_*`. Those flags gate the Anthropic online judge and pin one approved run with a total cap of $2.

## Canary

1. **Evidence.** Step 2 with the operator account only. Make a few real renders and read the rows:
   ```sql
   SELECT id, status, reason, tier, evidence_ready_at, review_attempts
   FROM creator_generation_reviews ORDER BY created_at DESC LIMIT 20;
   ```
   Rows reach `evidence_ready` within a minute. `unavailable` with `artifact_or_decoder_unavailable` or `media_corrupt` is a decoder or storage problem, not a review problem.
2. **Qualify the model on real frames.** Needs the owner's explicit approval: at most $2 per approved run id and $5 in total. This change authorises no spend and none has been run. The preregistration (model, cases, predicates, bars, decoys, stop rules, spend table, exact commands, what to inspect) is [the qualification](../reviews/intent-review/QUALIFICATION.md); the harness is `python -m app.cli.qualify_intent_review`, zero cost by default and refusing to call a provider without `--execute`, an approved run id and maximum, and a pinned preregistration digest. A three-call pilot gates the seven-call full run. Four controls on generated footage, each twice: a correct edit (no findings), wrong footage (flagged, no repair), a wording drift with nothing confirmed (a question, never a rewrite) and a title window that outlives the video (the one eligible fit), plus an in-frame injection decoy. Stop on any void reply, any provider outcome other than ok, or any call that settles above $0.10. The recorded replies replace the hand-written `raw_text` of the golden fixtures.
3. **Paid review.** Step 3, operator account only. Read the receipts in the app and the rows (`review` holds the catalog, the stored reply, the findings, the disposition and the provenance). Check the ledger:
   ```sql
   SELECT status, estimated_cost_usd, settled_cost_usd, usage_purpose, release_canary_id
   FROM ai_cost_reservations WHERE feature = 'intent_review' ORDER BY created_at DESC LIMIT 20;
   ```
4. **Repair.** Step 4, operator account only. Run a wording control and a timing control on a test project: the card appears with Approve and Undo, nothing renders until Approve, Undo restores the draft, and the repaired render is reviewed once and settled.
5. **Widen.** Set `KRIA_INTENT_REVIEW_USER_IDS='["*"]'` only when all of these hold over at least 20 operator reviews:
   - `unavailable(invalid_output)` at most 5 %;
   - `review_unknown` at most 2 %;
   - zero repairs touching a protected field;
   - p95 settled cost at most $0.06;
   - no receipt that claimed a check the review did not make.

## Watching it

- `creator_generation_reviews.status` / `reason` counts, per day. Terminal statuses: `unavailable`, `stale`, `exhausted`, `review_unknown`, `no_findings`, `suggestion`, `needs_confirmation`, `repair_verified`, `repair_unverified`, `repair_reverted`, `repair_abandoned`.
- What was shown: `receipt_event_id` and `question_event_id` on the row, `review.posting` (`posted`, or `skipped:<why>` with `why` one of `stale`, `creator_moved_on`, `thread_busy`, `tier_off`, `thread_changed`, `unchecked`, `nothing_to_say`). A high rate of `creator_moved_on` is expected: creators often react before the review lands, and then the deterministic post-render receipt stands alone.
- The admin job-debug view (`/admin/jobs/{id}`): the agent run `nova.kria.intent_review` and the `intent_review` pipeline events (`outcome`, `posted`, `render_fidelity`).
- Held unknown liabilities count against every cap until the month rolls over, so a burst of `review_unknown` shows up as `exhausted(budget_monthly)` for later reviews. See the cost section of [the operating runbook](kria-intent-review.md).

## Turning it off

Each is a Fly secret on api and worker followed by a restart. None needs a code revert, and none touches a creator's video.

| To stop | Set | What happens to rows in flight |
| --- | --- | --- |
| New repairs | `KRIA_INTENT_REVIEW_REPAIR_ENABLED=false` | A repair already staged keeps waiting for the creator and still settles. A review that asked for a repair becomes a suggestion. |
| New paid calls and the receipts about them | `KRIA_INTENT_REVIEW_PAID_ENABLED=false` | Rows not yet claimed end `unavailable(paid_disabled)`; a review already sending finishes. Nothing more is posted for it. |
| Everything | `KRIA_INTENT_REVIEW_ENABLED=false` | No admission, no evidence, no review, no post. The legacy review is no longer retired. |
| Spend only | `AI_OPENAI_INTENT_REVIEW_BUDGET_USD=0` | Every reserve is rejected and the tier falls back to `evidence`. |

A revert of a worse repair is the one post that does not stop with the tier: it explains a change to the creator's draft that has already happened, so it is posted regardless.

## Rollback of the stack

Turn every flag off first. The merged code is inert with the flags off, so the PRs do not need reverting. If a revert is ever needed, revert top-down (receipts, repair, agent, transport) and leave the migrations: `0130` and `0131` only add a table and columns and nothing reads them with the flags off.

## Known limits at launch

- Whether the planner turns a plain "Render previous version" after a revert into a render has not been checked (it needs a live planner call), so the revert message says "ask me to render it" instead of naming a button.
- The one question a wording doubt asks offers "Change it to \"<the creator's words>\"" / "Keep it". The tap is an ordinary user message read by the planner, and the option carries the exact words so the planner has nothing to invent; that it resolves to the right element has not been checked live.
- Stills cannot show sound or motion. Audio asks are never reviewed, and a timing ask is never reported as met. A low-confidence `met` is treated like a low-confidence `unmet`: ignored. A review whose only passes are guesses is `unavailable(nothing_verifiable)` and posts nothing.
- Receipts are English or Turkish, from the thread's stored language; any other language reads English.
