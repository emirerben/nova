# Intent review (KRI-1171)

A finished, published generation is checked against what the creator asked for. This runbook covers the stages that exist today. Later stages (repair, receipts) add their own sections when they ship.

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
| `KRIA_INTENT_REVIEW_DAILY_LIMIT_PER_USER` | `20` | Paid reviews per creator per UTC day, counted in the cost ledger (`exhausted(budget_user_day)`). `0` blocks every review. |
| `KRIA_INTENT_REVIEW_EVIDENCE_QUEUE` | `celery` | Queue for frame extraction (see Queue placement). Production pins `autoplace-jobs` in `fly.toml`. |
| `AI_OPENAI_INTENT_REVIEW_BUDGET_USD` | `0` | Monthly capability budget. `0` rejects every reserve. |

## Tier and code ceiling

`app/kria/intent_review_tier.py` is the only place that decides a creator's tier (allowlist, parent flags, API key and budgets). `KRIA_INTENT_REVIEW_ENABLED` is also read directly as the global kill switch by admission, publishing, recovery and the legacy interlock. A child flag does nothing without its parent, and `_CODE_TIER_CEILING` caps what any flag combination can switch on. Each PR in the stack raises it, so enabling a flag early cannot turn on an unfinished stage.

| Tier | Needs | Does |
| --- | --- | --- |
| `off` | not enabled or not admitted | nothing |
| `evidence` | `ENABLED` | admission and frames only |
| `review` | `PAID`, `OPENAI_API_KEY`, `AI_OPENAI_INTENT_REVIEW_BUDGET_USD > 0`, `AI_OPENAI_MONTHLY_BUDGET_USD > 0` | one paid frame review, judged and stored in shadow (nothing is posted) |
| `repair` | `REPAIR` | reserved for a later stage; the code ceiling keeps it at `review` today |

The tier is stamped at admission. Live flags can only lower it, never raise it. The legacy Gemini quality review is retired for a runtime-v2 thread only when that creator's tier is `review` or higher and the thread's newest intent review has not ended without a verdict (`unavailable`, `exhausted`, `stale`, `review_unknown`). When it has, the next reconcile hands the generation back to the legacy review. The intent review judges the creator's request against the video; it does not replace the legacy review's mechanical repairs (caption legibility, speech cuts, transition fallback), so a creator whose review reports no findings is not covered for those.

## Cost bounds

- Exactly one provider call per row. The ledger key is `logical_call_id(feature="intent_review", request_id="intent-review:<row id>")`, stored in `paid_call_key` (unique). There are zero provider retries.
- Cost is reserved before any frame is sent. The worst case is about $0.08: a 24,000 character prompt (about $0.016), six frames at 3,000 tokens (about $0.036) and 2,500 output tokens (about $0.025). If the estimate is above the row's stamped `max_usd` (default $0.10) the row is `exhausted(budget_review)` and nothing is reserved.
- Monthly cap: `AI_OPENAI_INTENT_REVIEW_BUDGET_USD`, a separate ledger capability inside `AI_OPENAI_MONTHLY_BUDGET_USD`. It is checked under the ledger's provider lock and counts reserved, started, unknown and settled spend. `0` rejects every reserve. The capability is always metered, even if `AI_COST_CONTROL_ENABLED` is off.
- Reviews are booked as `optional_background`, so they stop at 80% of the environment's monthly cap and leave the last headroom to customer renders and plans (`exhausted(budget_monthly)`).
- Do not reuse `CREDIT_QUALITY_REVIEW_*`. Those flags gate the Anthropic online judge and pin one approved run id with a total cap of $2.
- An unknown provider outcome keeps its reservation as liability and is never retried. The ledger counts `unknown` spend against every cap above for the rest of the calendar month (the TTL does not release it, and `reconcile_unknown_liability` only handles development `live_eval` rows), so a burst of unknowns makes later reviews `exhausted(budget_monthly)` until the month rolls over or an operator corrects the ledger.
- Internal accounts (an active `internal_account_grants` row) must be booked to a canary. Set `KRIA_INTENT_REVIEW_CANARY_ID` to a `release_canary_id`; spend then also counts against `AI_RELEASE_CANARY_MONTHLY_BUDGET_USD`. With it empty, an internal account's row ends `unavailable(canary_unattributed)` and no call is made.
- Outside production the ledger rejects the call (`unavailable(paid_gate_closed)`) unless the manual-QA envelope (`AI_MANUAL_QA_*`) is approved.

## Row lifecycle

`evidence_ready -> reviewing -> <terminal>`. Every transition is a compare-and-swap on `status` (and the lease token while `reviewing`).

| Terminal | Reasons |
| --- | --- |
| `unavailable` | `paid_disabled`, `paid_gate_closed`, `canary_unattributed`, `frame_hash_mismatch`, `media_corrupt`, `context_too_large`, `provider_rejected`, `invalid_output`, `nothing_verifiable`, `prompt_builder_unavailable`, `ledger_unavailable`, `disposition_unavailable` |
| `no_findings`, `suggestion`, `needs_confirmation` | `reviewed` (the server judged a valid reply) |
| `stale` | `generation_changed`, `ownership_changed` |
| `exhausted` | `deadline` (claimed more than 180 s after `evidence_ready_at`), `attempts`, `budget_review`, `budget_monthly` |
| `review_unknown` | `provider_unknown_outcome`, `response_lost` |

- The claim, the pre-send recheck and the final check each lock the authority graph and compare the input digest. A newer render, a draft or recipe change, or an ownership change makes the row stale. If the call was already sent, the ledger is still settled.
- The response is stored (`review.raw_output`, capped at 16 KB, plus provenance) before the ledger is settled. Provenance has the model, response id, usage, reserved and settled USD, latency and the hash of every frame sent. `review.provenance.visual_input_received` is true only when the transport got a completed response to the frames. `review_model_received_visual_input` on the packet and bundle stays `False`.
- No provider call, frame decoding or pipeline event runs while a row lock is held.
- A completed response is judged by the server (next section). `unavailable(disposition_unavailable)` now means only that the stored catalog could not be read back, an internal fault rather than anything the model said.

## The review agent

`nova.kria.intent_review` (`app/agents/intent_review.py`, prompt `prompts/intent_review.txt`, `prompt_version` `intent_review_v3`) is never run through `Agent.run` in production. `run_intent_review` renders its prompt, sends it through the bounded image transport under an explicit reservation, and parses the reply with the same code. The dispatcher still rejects media, so an accidental dispatcher call fails closed.

**What the model is given.** The creator's request (at most 2,000 characters, as one JSON string so nothing in it can forge structure), up to 12 issued requirements (`R1`..`R12`, kind, label, the creator's verbatim words), and the frame manifest (`f0`..`f5` with a time, and a mark for frames centred on a text span). No candidate, target text or edit is ever in the prompt. Audio requirements are never issued: they are recorded in `review.catalog.withheld` and no finding or receipt may mention them.

**What the model returns.** A closed schema sent as a strict JSON schema over exactly the issued aliases and sent frames: exactly one finding per issued requirement, each with a verdict (`met`, `unmet`, `unverifiable`; `unverifiable` is how the model declines), a `defect_kind` for unmet only, one to three evidence frames for unmet, and a `high`/`medium`/`low` confidence. There is no free text, so nothing the model says can become wording, timing or an edit.

**What the server does with it** (`app/kria/intent_review_disposition.py`, pure functions):

- Any violation voids the whole review as `unavailable(invalid_output)`: not JSON or truncated, an extra key, more than 12 findings, two findings for one requirement, an issued requirement with no finding at all (the schema cannot force coverage, so the server does: a reply that checks one requirement and goes quiet on the rest never counts as a clean pass), an alias or frame that was not issued or sent, an unmet finding with no frame, a defect kind on a verdict that is not unmet, or a defect kind that does not fit the requirement kind (a wording defect on footage). The call is still settled at actual usage, `review.invalid_output` names the first violation, and no finding is stored. A void review never lands on `no_findings`.
- A `met` verdict on a timing requirement is recorded as `unverifiable`: stills sample instants and cannot show how long anything stays on screen.
- A reply that judged nothing is `unavailable(nothing_verifiable)`. A catalog with no requirement the frames can judge makes no call and reserves nothing.
- `low` confidence is ignored (kept in `disposition.ignored`). `medium` only ever produces a suggestion. A repair needs `high`, a tier of `repair`, staging preconditions that held, and a catalog candidate that matches both the requirement and the defect kind. Until the repair stage ships, every would-be repair is stored as a suggestion with `why = tier_review`.
- Wrong footage (a `select` requirement) is always a suggestion and never a question or a repair. Wording the draft already carries is a render-fidelity suggestion plus a `render_fidelity` pipeline event for engineering triage. A wording defect with no candidate becomes at most one question (`needs_confirmation`); the rest become suggestions.
- Row status, highest wins: `repair_staged` > `needs_confirmation` > `suggestion` > `no_findings` (which needs at least one `met`).

**Candidates** (`app/kria/intent_review_catalog.py`) are built from the frozen approved draft and are emitted only when an authority other than the draft disagrees with it, because a candidate that restores the draft's own value compiles to an empty diff:

| Candidate | Precondition | Auto |
| --- | --- | --- |
| `restore_exact_text` | no row mapped to the requirement carries the contract's creator-confirmed text (case-folded, whole words, accents kept), and the row to rewrite is one the requirement's scope names (the title, a clip's label) or holds nothing but those words. The target is the contract text byte for byte | yes |
| `fit_text_to_duration` | the element really outlives the video: it ends more than 0.1 s after the rendered duration (the duration the frame decoder probed), so text held to the very end is left alone. Only a `text_cut_off` finding can stage it. The new end is 0.3 s before the end; the window never starts earlier or ends later than approved and keeps at least 1 s | yes |
| `move_text_to_safe_anchor` | a title or text element with a named position. The server picks the anchor (bottom to top, top to bottom, middle to top) | no, suggestion only |

A requirement finds its rows by scope (`title`, `clip:<id>`) and otherwise by the creator's words as whole words, ignoring case and accents, the way the request-following receipts do: "Sale" is not in "Wholesale Day". A row found only by the creator's words that holds other words ("Day 3 in Istanbul" against the creator's "İstanbul") is never cut down to the creator's words; a wording claim there is a question. No candidate is emitted for pinned text, sequence fragments, captions, lyric lines, text bound to a clip when footage is selected or ordered, anything under an absolute sequence timing or a protected region, `preserve_editor_text` (the creator forbade touching text, so not even a restore), or (for retime and move) an explicit timing or placement ask. Draft kinds other than `editor` issue requirements but no candidates.

**Where to look.** `creator_generation_reviews.review` holds `catalog`, `raw_output` (at most 16 KB), `findings`, `disposition`, `provenance` and, for a void reply, `invalid_output`. The admin job-debug view shows the agent run (`nova.kria.intent_review`, outcome `terminal_schema` for a void reply) and the `intent_review` pipeline events. Shadow mode posts nothing: `receipt_event_id` and `question_event_id` stay empty.

**Replay evals** (`pytest tests/evals/test_intent_review_evals.py`, free): goldens for a correct edit, wrong footage, wrong wording, wording drift with no authority, a timing miss and an audio decoy; hostile fixtures for each way a reply is voided. Replay proves the structure and the server judgment, not that the model catches anything; the goldens' hand-written `raw_text` is replaced by recorded replies in the live qualification run.

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

`tasks.prepare_generation_review` downloads up to 256 MB and runs ffmpeg, so `KRIA_INTENT_REVIEW_EVIDENCE_QUEUE` routes it. `fly.toml` pins `autoplace-jobs`, consumed by the 4 GB worker; the code default `celery` keeps local `dev-auto.sh` working. It must never run on `agent-control` or `maintenance`, which the 1 GB `light` machine (Beat, concurrency 2) consumes, and `tests/test_render_review_routes.py` pins that. `tasks.review_generation_intent` is light (frames come from the row, nothing is downloaded) and routes to `agent-control`. Nothing needs re-routing before enabling `KRIA_INTENT_REVIEW_ENABLED`; the variable only has to reach the api and worker machines.

## Verification

- `cd src/apps/api && pytest tests/integration/test_intent_review_lifecycle_postgres.py tests/services/test_ai_cost_control_intent_review.py tests/services/test_openai_vision_transport.py tests/kria/test_intent_review_tier.py` on a scratch database (`bash scripts/dev/test-db.sh`). The OpenAI SDK object is the only fake.
- The Linux-only frame decoder is qualified by `request-following-regressions.yml`: `gh workflow run request-following-regressions.yml --ref <branch>`.
