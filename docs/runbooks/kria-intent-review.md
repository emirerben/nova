# Intent review (KRI-1171)

A finished, published generation is checked against what the creator asked for. This runbook covers the stages that exist today. Later stages (review agent, repair, receipts) add their own sections when they ship.

## What runs

1. **Admission** (`KRIA_INTENT_REVIEW_ENABLED`). The runtime observer admits one row per generation in `creator_generation_reviews`, stamped with a `tier` and, for a reviewing tier, a per-review `max_usd`. A creator outside `KRIA_INTENT_REVIEW_USER_IDS` is not admitted (`[]` admits nobody, `["*"]` everyone).
2. **Evidence** (`tasks.prepare_generation_review`). Up to six JPEG frames are decoded from the byte-verified export. Up to four frames are anchored on the middle of on-screen text spans (requested wording first, then the title, then other text) because uniform sampling misses short text. Rows end in `evidence_ready`.
3. **Paid review** (`KRIA_INTENT_REVIEW_PAID_ENABLED`, task `tasks.review_generation_intent`). One bounded call sends the frames as inline images to `gpt-6.1-sol` through `app/agents/_openai_vision.py`. The text adapter still rejects media, so no other path can send a frame.

## Flags

| Key | Default | Effect |
| --- | --- | --- |
| `KRIA_INTENT_REVIEW_ENABLED` | `false` | Admission and evidence. Also the kill switch for every stage. |
| `KRIA_INTENT_REVIEW_PAID_ENABLED` | `false` | Paid frame review. A no-op without `ENABLED`. |
| `KRIA_INTENT_REVIEW_REPAIR_ENABLED` | `false` | Repair staging (a later stage). A no-op without `PAID`. |
| `KRIA_INTENT_REVIEW_USER_IDS` | `[]` | `[]` admits nobody, `["*"]` everyone. |
| `KRIA_INTENT_REVIEW_MAX_USD` | `0.10` | Per-review ceiling (`0 < x <= 0.25`), stamped at admission. |
| `KRIA_INTENT_REVIEW_CANARY_ID` | empty | `release_canary_id` the ledger requires for internal accounts. |
| `AI_OPENAI_INTENT_REVIEW_BUDGET_USD` | `0` | Monthly capability budget. `0` rejects every reserve. |

## Tier and code ceiling

`app/kria/intent_review_tier.py` is the only code that reads the flags. A child flag does nothing without its parent, and `_CODE_TIER_CEILING` caps what any flag combination can switch on. Each PR in the stack raises it, so enabling a flag early cannot turn on an unfinished stage.

| Tier | Needs | Does |
| --- | --- | --- |
| `off` | not enabled or not admitted | nothing |
| `evidence` | `ENABLED` | admission and frames only |
| `review` | `PAID`, `OPENAI_API_KEY`, `AI_OPENAI_INTENT_REVIEW_BUDGET_USD > 0`, `AI_OPENAI_MONTHLY_BUDGET_USD > 0` | one paid frame review |
| `repair` | `REPAIR` | reserved for a later stage |

The tier is stamped at admission. Live flags can only lower it, never raise it. The legacy Gemini quality review is retired for a runtime-v2 thread only when that creator's tier is `review` or higher, so admitting evidence alone never leaves a generation unreviewed.

## Cost bounds

- Exactly one provider call per row. The ledger key is `logical_call_id(feature="intent_review", request_id="intent-review:<row id>")`, stored in `paid_call_key` (unique). There are zero provider retries.
- Cost is reserved before any frame is sent. The worst case is about $0.08: a 24,000 character prompt (about $0.016), six frames at 3,000 tokens (about $0.036) and 2,500 output tokens (about $0.025). If the estimate is above the row's stamped `max_usd` (default $0.10) the row is `exhausted(budget_review)` and nothing is reserved.
- Monthly cap: `AI_OPENAI_INTENT_REVIEW_BUDGET_USD`, a separate ledger capability inside `AI_OPENAI_MONTHLY_BUDGET_USD`. It is checked under the ledger's provider lock and counts reserved, started, unknown and settled spend. `0` rejects every reserve. The capability is always metered, even if `AI_COST_CONTROL_ENABLED` is off.
- Do not reuse `CREDIT_QUALITY_REVIEW_*`. Those flags gate the Anthropic online judge and pin one approved run id with a total cap of $2.
- An unknown provider outcome keeps its reservation as liability and is never retried. The ledger counts `unknown` spend against every cap above for the rest of the calendar month (the TTL does not release it, and `reconcile_unknown_liability` only handles development `live_eval` rows), so a burst of unknowns makes later reviews `exhausted(budget_monthly)` until the month rolls over or an operator corrects the ledger.
- Internal accounts (an active `internal_account_grants` row) must be booked to a canary. Set `KRIA_INTENT_REVIEW_CANARY_ID` to a `release_canary_id`; spend then also counts against `AI_RELEASE_CANARY_MONTHLY_BUDGET_USD`. With it empty, an internal account's row ends `unavailable(canary_unattributed)` and no call is made.
- Outside production the ledger rejects the call (`unavailable(paid_gate_closed)`) unless the manual-QA envelope (`AI_MANUAL_QA_*`) is approved.

## Row lifecycle

`evidence_ready -> reviewing -> <terminal>`. Every transition is a compare-and-swap on `status` (and the lease token while `reviewing`).

| Terminal | Reasons |
| --- | --- |
| `unavailable` | `paid_disabled`, `paid_gate_closed`, `canary_unattributed`, `frame_hash_mismatch`, `media_corrupt`, `context_too_large`, `provider_rejected`, `invalid_output`, `prompt_builder_unavailable`, `ledger_unavailable`, `disposition_unavailable` |
| `stale` | `generation_changed`, `ownership_changed` |
| `exhausted` | `deadline` (claimed more than 180 s after `evidence_ready_at`), `attempts`, `budget_review`, `budget_monthly` |
| `review_unknown` | `provider_unknown_outcome`, `response_lost` |

- The claim, the pre-send recheck and the final check each lock the authority graph and compare the input digest. A newer render, a draft or recipe change, or an ownership change makes the row stale. If the call was already sent, the ledger is still settled.
- The response is stored (`review.raw_output`, capped at 16 KB, plus provenance) before the ledger is settled. Provenance has the model, response id, usage, reserved and settled USD, latency and the hash of every frame sent. `review.provenance.visual_input_received` is true only when the transport got a completed response to the frames. `review_model_received_visual_input` on the packet and bundle stays `False`.
- No provider call, frame decoding or pipeline event runs while a row lock is held.
- Until the review agent ships, a completed response ends `unavailable(disposition_unavailable)`: the text and provenance are kept, no findings are parsed and nothing is shown to the creator.

## Crash recovery

`recover_intent_reviews` runs on every beat (through `reconcile_generation_reviews`).

- `evidence_ready` rows at a reviewing tier older than 30 s are republished (task id `generation-review:<id>:intent`). Rows past the 180 s start deadline become `exhausted(deadline)`.
- `reviewing` rows with an expired lease are decided from the stored response and the ledger entry for `paid_call_key`:

| Ledger state | Result |
| --- | --- |
| response already stored | settle from the stored usage, then finish |
| none or `released` | back to `evidence_ready` (at most two claims), else `exhausted(attempts)` |
| `reserved` | release, then as above |
| `provider_started` | mark unknown, `review_unknown` |
| `unknown` | `review_unknown` |
| `settled` | `review_unknown(response_lost)` |

A `provider_started` reservation is never released except on a definite 4xx. The 180 s lease and 180 s start deadline mean a second claim after an expired lease is usually past the deadline.

## Kill switches

Apply with `fly secrets set -a nova-video ...` on api and worker, then restart both.

- `KRIA_INTENT_REVIEW_PAID_ENABLED=false`: rows not yet claimed become `unavailable(paid_disabled)`. Reviews in flight finish.
- `KRIA_INTENT_REVIEW_ENABLED=false`: no admission, no evidence, no review, and the legacy review is no longer retired.
- `AI_OPENAI_INTENT_REVIEW_BUDGET_USD=0`: every reserve is rejected and the tier drops back to `evidence`.

## Queue placement

`tasks.review_generation_intent` is light (frames come from the row, nothing is downloaded) and routes to `agent-control`. `tasks.prepare_generation_review` downloads up to 256 MB and runs ffmpeg. The `light` machine (1 GB, concurrency 2, embedded Beat) has run out of memory on that workload before, so decide where it runs before enabling `KRIA_INTENT_REVIEW_ENABLED` in production.

## Verification

- `cd src/apps/api && pytest tests/integration/test_intent_review_lifecycle_postgres.py tests/services/test_ai_cost_control_intent_review.py tests/services/test_openai_vision_transport.py tests/kria/test_intent_review_tier.py` on a scratch database (`bash scripts/dev/test-db.sh`). The OpenAI SDK object is the only fake.
- The Linux-only frame decoder is qualified by `request-following-regressions.yml`: `gh workflow run request-following-regressions.yml --ref <branch>`.
