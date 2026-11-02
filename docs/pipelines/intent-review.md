# Intent review (KRI-1171)

After a creator's edit renders, nothing used to check the finished video against what they asked for. The intent review does, once per generation: it looks at a few frames of the published video with a vision model, names the asks that look unmet, tells the creator in the chat what it verified and what it could not, and, for two kinds of defect, prepares a fix in the normal editor that the creator approves or undoes.

Turning it on, canary steps, kill switches and rollback: [the rollout runbook](../runbooks/intent-review-rollout.md). Diagnosis, cost bounds and crash recovery: [the operating runbook](../runbooks/kria-intent-review.md).

## The invariant

**The model has no authority.** It returns verdicts about server-issued aliases (`R1`..`R12` for requirements, `f0`..`f5` for frames), a closed defect kind and a `high` / `medium` / `low` confidence. It returns no prose, no wording, no timing and no candidate. Everything that can change the creator's video or reach their chat is built by the server from the frozen approved inputs and closed copy tables, and a repair still needs the creator's Approve tap.

## Pipeline

```
render ready (observer)
  -> admission            one row per generation, tier + max_usd stamped      render_review_admission.py
  -> evidence             <= 6 JPEG frames of the byte-verified export        render_review_evidence.py
  -> paid review          one bounded vision call, stored before settled     intent_review_flow.py
  -> judgment             validate -> catalog -> disposition (pure)           intent_review_disposition.py
  -> repair (optional)    one reversible draft turn, creator approves         intent_review_repair_flow.py
  -> posting              receipt (+ one question) in the chat                intent_review_post.py
  -> settlement           the repaired render is reviewed once, parent closes intent_review_repair_flow.py
```

| Stage | Module | Contract |
| --- | --- | --- |
| Tier | `kria/intent_review_tier.py` | The only reader of the `KRIA_INTENT_REVIEW_*` flags. A child flag needs its parent; `_CODE_TIER_CEILING` is `repair`. Live flags can only lower the stamped tier. |
| Frozen inputs | `kria/render_review_context.py` | The approved request, draft, recipe and verified export identity. `review_model_received_visual_input` stays `False` here; the truth is `review.provenance.visual_input_received`, written only by the transport. |
| Transport | `agents/_openai_vision.py`, `services/bounded_media_review.py` | Frames as `input_image`, `store=False`, no retries, cost reserved before a frame leaves the process. The text adapter still rejects media. |
| Catalog | `kria/intent_review_catalog.py` | Issues at most 12 requirements (audio is never issued) and the candidates a repair may use, each only when an authority other than the draft disagrees with the draft. |
| Judgment | `kria/intent_review_disposition.py` | Any contract violation voids the whole review (`unavailable(invalid_output)`). Otherwise: repair, question, suggestion or no findings. |
| Repair | `kria/intent_review_repair.py`, `services/intent_review_repair_flow.py` | Compiled through the ordinary draft turn; `verify_intent_repair` refuses any change beyond the named fields. |
| Wording | `kria/intent_review_copy.py` | Every sentence through `say(en=, tr=)`. The honesty key: no "checked" sentence without `visual_input_received`. |
| Composition | `kria/intent_review_posts.py` | Pure: a stored review becomes the events the chat gets, or raises `UncheckedReview` and says nothing. |
| Posting | `services/intent_review_post.py` | Locks, guards, writes the events, records the decision. |

## What the creator sees

| Outcome | Chat |
| --- | --- |
| `no_findings` | One receipt: "I checked the finished video against your request. Everything I could see matches." If anything was withheld (audio) or could not be judged from stills, the headline names the limit instead. One `met` row per verified ask. |
| `suggestion` | One receipt: something may not match, nothing was changed. One `partial` row per doubtful ask; the optional fix is named only when it is still a sensible offer. |
| `needs_confirmation` | The suggestion receipt, then one `assistant_question` with the options "Change it" / "Keep it". A tap is an ordinary user message; the reviewer stages nothing. |
| `repair_staged` | The repair turn's own card (`draft_applied` with Undo, then `approval_requested`), then one receipt saying the fix is waiting for approval. Never a question. |
| `repair_verified` | One receipt: the fix worked, checked again; the fix line in the past tense; Undo still available. |
| `repair_unverified` | One receipt: the fix is in the new video but could not be confirmed; never says "checked". |
| `repair_reverted(worse)` | One receipt: the draft was undone, the video still shows the fix, ask to render the previous version. Never claims the old video is back. |
| `repair_reverted(worse_head_moved)` | One message: the creator's newer edits are untouched; the fix can be undone from its card. |
| everything else | Nothing. The deterministic post-render receipt stands. |

The events are the types old app builds already render: `assistant_response` (receipts), `assistant_question`, and the repair turn's `draft_applied` / `approval_requested`. A receipt row is a `RequirementReceipt` (`status` `met` or `partial`, the brief requirement's own id, `verification` and `stage` `checked`, stamped with the brief version and the render generation it judged), with `reply_lead` and `also_changed` next to it. No iOS change was needed. The reviewer's receipts carry `turn_value: "intent_review"` and are skipped by `GET brief`: the deterministic ledger alone decides what a requirement shows.

### When it posts, and when it stays quiet

An informational receipt is posted only if all of these are still true at the moment of writing, under the thread lock:

- the reviewed render is still the creator's current video (the input digest still holds);
- the creator has not spoken since the render, no turn is in flight and no other approval is pending (a new event would move the thread revision under a turn that is planning and throw it back to plan again);
- the review is still switched on for that creator.

Otherwise nothing is posted and the decision is recorded in `review.posting[<status>] = "skipped:<why>"`. A revert that undid the creator's draft is the one post that does not wait for a quiet thread: it explains a change that already happened.

Posting is idempotent per status: events carry the client id `intent-review:<review>:<status>:<receipt|question>`, the decision is recorded in `review.posting`, `receipt_event_id` / `question_event_id` hold the latest ids, and the posters serialise on the thread and then the review row (never waiting for the row while holding the thread). A crash between the terminal state and the post is repaired by the beat sweep, which posts recent unposted outcomes (30 minutes; 6 hours for a revert).

## States

```
evidence_ready -> reviewing -> no_findings | suggestion | needs_confirmation | repair_staged
                            -> unavailable | stale | exhausted | review_unknown     (silent)
repair_staged  -> repair_verified | repair_unverified | repair_reverted             (posted)
               -> repair_abandoned                                                  (silent)
```

A follow-up review of a repaired render (`parent_review_id` set) is capped at tier `review`, never repairs, never posts, and settles its parent when it reaches a terminal state.

## Tests

| Concern | Test |
| --- | --- |
| Copy, honesty key, iOS surface, TR/EN parity | `tests/kria/test_intent_review_copy.py` (pure) |
| Posting on the real thread: once, quiet-thread rules, crash sweep, settlement, brief isolation | `tests/integration/test_intent_review_posting_postgres.py` |
| Judged outcomes, repair, lifecycle | `tests/integration/test_intent_review_{outcome,repair,lifecycle}_postgres.py` |
| Structure of the model's replies | `tests/evals/test_intent_review_evals.py` (free replay; detection is proven only by the live qualification run) |

Run on a scratch database: `bash scripts/dev/test-db.sh`, then `cd src/apps/api && pytest tests/kria/test_intent_review_copy.py tests/integration/test_intent_review_posting_postgres.py`. The Linux-only frame decoder is qualified by `request-following-regressions.yml`.
