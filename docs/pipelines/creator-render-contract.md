# Creator render contract

KRI-470 closes the handoff between an approved creator edit and the artifact
that gets rendered. A strategy, brief, or chat request is intent; it is not
proof that a renderer obeyed that intent. The contract makes the hard parts of
the approved edit explicit, routes from those facts, and refuses to pin or
publish when the compiled output cannot prove them.

## Authority and lifecycle

The approval boundary projects the accepted `CreativeStrategy` and pinned brief
into a versioned `creator_render_requirements` object. It records the
`generation_id`, strategy and brief digests, explicit duration, audio source
IDs and camera-audio policy, voiceover requirement, exact text with role and
timing, and the resolved clip order. The object has its own integrity digest.
Duration explicitness is retained before default serialization, including an
explicit 24-second request; a default value must not be mistaken for an
approved target.

The execution path is:

```text
approved strategy + brief + media identities
                -> contract (immutable generation and digest)
                -> capability route
                -> existing compiler / EditRecipeV2
                -> compiled contract verifier
                -> pin only if all hard requirements pass
                -> native/cloud output evidence
                -> final publication guard
```

On phones, the mandatory pin verifier checks the recipe timeline, audible
track sources and mute coverage, text content and placement, and the visual
sequence. The final device publication check revalidates the contract, recipe
digest, generation, brief identity, and source identity while holding the job
lock. A retry receives a fresh attempt identity but retains the approved
authority. An editor revision is a per-variant contract; sibling variants and
retries must not inherit another variant's edits.

Replacement renders are staged and generation-fenced before they can replace a
published artifact. Cloud update/upsert boundaries return `False` on rejection
and preserve the last-good video and poster. Staged replacements need fresh
evidence before callers can retire the previous artifact; a later status-only
`ready` write rechecks that evidence and cannot revive a rejected attempt.

The contract marker in `all_candidates` is mandatory for new contracted work.
If the marker is present but the assembly contract is missing or invalid, the
job fails closed. Historical jobs without the marker keep the legacy behavior
for compatibility. Requirements are never reconstructed from generated output
or reread from mutable chat during a retry.

## What is hard and what is not

The core contract covers duration, audio, ordering, and exact text. Every
`CreativeStrategy` field path still has an explicit disposition in code (see
"Field matrix"), but fields outside this projection remain under their existing
capability or planning policy. A matrix entry is not evidence that the field is
enforced by every renderer.

### Field matrix

`FIELD_MATRIX` in `services/creator_render_contract.py` replaces the old flat
per-field note. It is keyed by field path, nested and with list fields marked
`[]` (`shot_labels[]`, `clip_intents[].op`, `montage_audio.source_media_ids[]`),
and each entry carries a disposition and the owning component:

| Disposition | Meaning |
| --- | --- |
| `supported` | The contract pins it and a verifier produces evidence (duration, audio source/policy, exact text, order, voiceover requirement). |
| `preference_only` | Taste; nothing is held to it. |
| `upstream_resolved` | Resolved or repaired before the contract (planner, capability policy, server resolvers); not re-verified. |
| `unsupported` | A creator can state it but no component proves it (a named licensed SFX). A request depending on it must not be treated as met. |

The required path set is derived by walking the `CreativeStrategy` pydantic
schema recursively (`schema_field_paths`). `test_every_strategy_path_is_assigned`
fails when any field or nested field is added, removed, or renamed without a
matrix entry, so new fields cannot ship unclassified.

### Adapter declarations

`ADAPTER_DECLARATIONS` (phone, in `creator_render_contract.py`) and
`CLOUD_ADAPTER_DECLARATIONS` (cloud, in `cloud_render_contract.py`) state, per
render path, which contract requirements it `consumes` (routes from and
verifies) and which it `declines`, with a typed reason. Every requirement
(`duration_s`, `require_voiceover`, `audio_source_ids`, `original_audio`,
`exact_texts`, `order_required`, `hidden_parts`, `unresolved`) must be consumed or
declined exactly once. Today only the phone speech montage routes from the audio, duration
and order requirements, and only the unified montage plans its windows out of a hidden part;
every other phone path is verified at pin time (`verify_phone_recipe`).

### Hidden parts (Leverkusen M2)

"The kids at the start of the second goal clip are another family's children, so don't show
them": a resolved `hide_part` clip intent (one clip, `part_edge` start/end, the creator's
`part_s`) is pinned as `hidden_parts` ({media_id, edge, seconds}). The source seconds no
frame may come from are the part plus `HIDDEN_EDGE_GUARD_S` (a frame guard for the
presentation-interval rule, plus half a second because the creator's number is an estimate;
`app/pipeline/hidden_parts.py`). The length planner and the montage planner slide every
movable window out of the part and cap the clip's capacity at what it may still show; an
approved or frozen window that would show it, or a part nobody settled, refuses the plan.
`verify_phone_recipe` refuses any recipe clip (picture, sound or overlay) whose own source
window reaches into a part, whatever writer compiled it; every cloud adapter declines it
(`capability_unavailable`) before rendering. The field is left out of the stored contract
when empty, so every other job is byte-identical; an older or rolled-back worker refuses a
job that hides a part (fails closed). A part with no clip, edge or seconds is a question for
the creator (`app/kria/hidden_parts.py`), and a re-draft keeps the head draft's part with
the most seconds asked. The cloud adapters consume exactly what their renderer
emits receipt evidence for (see "Cloud evidence"); the rest stay declined.
Behaviour tests drive the real entry points (`verify_phone_recipe`,
`check_phone_dispatch_contract`, `preflight_cloud_contract`,
`verify_cloud_variant`) and assert the declared reason, not the table.

## Typed declines

A refusal carries a `DeclineReason` and a `field_path` (matrix path, or
`brief:text` for a brief-only literal) on `CreatorRenderContractError` and
`CloudRenderContractError`:

| Reason | Meaning | Creator recovery (`tasks/kria_runtime.py`) |
| --- | --- | --- |
| `capability_unavailable` | The path can never honour or prove it. | Refusal naming the limit and a supported alternative. |
| `evidence_missing` | Supported, but the output did not demonstrate it. | Repair / retry, only where the evidence can appear on a re-run (cloud publication verification, `creator_render_contract_unverified`; cloud preflight). A phone `phone_plan_unsupported` stays deterministic: it asks, with the original copy plus the typed alternative. |
| `requirement_conflict` | Two approved requirements cannot both hold. | A question for the route-independent conflicts the clarification gate owns (see "Clarification gate"); otherwise the existing ask behaviour, with a typed alternative appended to phone copy. |
| `needs_choice` | Ambiguous until the creator decides (includes `unresolved`). | The same gate asks before approval; a leftover `unresolved` item is refused at draft time instead of at dispatch. |

The reason is persisted beside, never in place of, the existing failure strings:
`phone_plan_unsupported`, `creator_render_contract_unsupported` and
`creator_render_contract_unverified` are unchanged. Phone and cloud-preflight
declines land in `Job.assembly_plan["creator_decline"]`
(`{"decline_reason", "field_path"?, "alternative"?, "failure_reason"}`), stamped with
the failure code they belong to; recovery honours it only when it matches the
job's current failure code, and a new worker run or a successful finalization
clears it, so a later unrelated failure never inherits an old refusal. A cloud
publication decline lands on the failed variant next to `error_class`, and
recovery reads only the targeted variant's own decline. `unresolved` stays a tuple of
strings; it is reported as `needs_choice` without a field path.

## Clarification gate

An unresolved material choice never becomes an approved plan (KRI-476 / PR-C, flag
`kria_choice_questions_enabled`; off = the previous behaviour, `unresolved` still refuses
at dispatch). `services/choice_questions.py:collect_conflicts(strategy, brief,
media_snapshot, capability)` is the ONLY place that turns an ambiguity into a question.
It reads the strategy, the brief and the approved media snapshot, never chat text or a
render route, and returns typed `UnresolvedChoice` items (`kind`, `field_path`,
`requirement_ids`, `options`, `input_digest`) in a fixed priority order. One question per
turn.

**Evidence rule.** A question needs creator-authored timing or an authoritative answered
duration choice. Model field presence and a non-default value are not evidence.
`length_policy.creator_duration_s` stamps `target_duration_requested` only from that
proof; model-only targets remain derived suggestions, including an explicit model 24.

**The gate depends on the model emitting the `timing` requirement.** `duration_vs_count` can only
compare a length the live brief carries. Main Creator prompt `2026-10-06-v44` emitted it for
"8 second ... all my clips" and "20 second ... 6 best clips" but dropped "60 second montage of all
my clips" (`brief_updates` empty), so a lost number meant a silent gate. Prompt `2026-10-07-v45`
(and `brief_extractor` `2026-10-08-v3`, which shares the brief section) tells the model to emit a
`timing` requirement with `duration_s` in the same turn for ANY stated length, alongside
`target_duration_s`, and never to invent one when none was stated. Verified by a live re-record
(2026-10-07, 6 Main Creator calls, real spend about $0.24): "60 second montage of all my clips"
now yields `timing{duration_s: 60}` (v44 returned nothing), "8 second" and "20 second" keep
their timing, and "chronological order" / the talk-to-camera request invent no length. One
recording per case, so treat it as evidence, not a guarantee. The same run showed two
side effects: the model now often drops the `select` requirement ("all my clips", "6 best
clips") and, on 12-clip montages, picks `archetype: day_vlog`, which EXEMPTS the length question
(the 8 s request no longer asks). These are observations of the older gate; KRI-492 replaces
those duration exemptions with the shared owner and capacity policy. The committed cassettes
(`tests/fixtures/agent_evals/main_creator/kri470_gate_*`, with `_v44` before-pictures) replay
structurally and ignore the prompt text; `tests/kria/test_creator_gate_live_cassettes.py` pins the
model-output -> gate chain, including the archetype exemption.

**Stated how-to details (KRI-522).** "Start with the blue video", "animated with typewriter",
"bottom left" and "placeholder location" used to live only in the brief's prose, so a later
turn (whose planner rebuilds `resolved_clip_intents` from the brief paraphrase) lost them and the
order receipt still read "met". They are now structured brief `facts` (Main Creator `2026-10-08-v48`,
`brief_extractor` `2026-10-08-v3`): order `first_clip` / `last_clip`, text `animation`
(`typewriter|fade|pop|slide`), `position` (`top_left`..`bottom_right`) and `placeholder`
(`app/schemas/text_style_intent.py`). `brief.apply_updates` keeps them across a same-kind,
same-scope `change` (the hook's wording arriving later). Brief-on turns also hand the clip-intent
planner the creator's own messages (`planner._load_raw_creator_request`), the only text a quote can
be checked against. The contract turns a stated first/last clip with no seated order intent into an
`unresolved` question (`clip_order_sequence.stated_anchors`); the order receipt is `partial` when
the plan record has no met first/last outcome. `unified_montage.plan_unified_montage` reads
animation / position / placeholder from the brief (else the strategy's `title_animation` /
`label_position`) into the snapshot, and `guided_story._text_elements` draws them
(`animation_phases` on the title, `LABEL_ANCHORS` for labels). Unset = byte-identical output.
Guards: `tests/services/test_request_following_kri522.py`, overlay fixture
`kri522_title_typewriter_and_corner_label.json`.

| Kind | Evidence required | Detector | Options (all executable today) | Persisted field | Exempt (never asks) |
| --- | --- | --- | --- | --- | --- |
| `duration_vs_count` | A live brief `timing` requirement with `duration_s` (quoted in the question) | The resolved included clips cannot each receive the 0.1 s floor within the creator target's ±10% tolerance | `extend` (N x floor, within 120 s), `fewer` (evenly spaced WITHIN the selection, never re-adding exclusions) | Authoritative duration proof or selected subset; pinned timing is superseded to match | Model-only lengths, unresolved subsets, or coverage that fits. Natural song/voice/take conflicts use the owner detector. |
| `length_vs_natural` / `source_coverage` | Creator timing plus source-backed capacity, or impossible included-source coverage | Shared owner, approved windows, source durations, portable minimum/cap, and pins | Only achievable durations or subsets; no unplaced lip-sync timing or unsupported song trim is invented | Durable scoped answer bound to the source/owner/input digest | Missing capacity waits for source-backed planning; compatible targets do not ask. |
| `order_basis` | A live brief `order` requirement, or `ordering_choice=chronological` | (a) a capture-order requirement (`capture_time`, `chronological`, `route`, `time`, `shot_order`) and some selected clip has no capture time (the contract's own condition); (b) a key-less or unknown-key rule ("clips 1, 2, 3 in that sequence") the contract cannot verify. A requirement that only pins a first/last clip (`first_clip`/`last_clip` facts, no `by`/`direction`/basis key) is a clip pin, not a rule: never asked (KRI-762). Upload order is `recommended` only when every first/last clip the creator named is placed (else `unordered` is) | `attachment_order` ("Use the order you added the clips"), `unordered` ("Continue without a fixed order"). A creator-typed sequence is NOT offered: nothing can receive one yet | `choice_answers[]`; the contract carries `order_basis="attachment_order"` + `order_ids`, or `order_required=false` | Rule (b) when the server already placed the sequence (a resolved `order` intent with assignments). A pin-only requirement the server could not place is not asked either: the contract names the clip ("I couldn't tell which clip \"…\" means", Turkish twin) and the creator's next message resolves it; "unordered" keeps placed pins live in the answered brief (`answered_brief`) |
| `text_placement` | A live brief dictated shot text | The literal appears on two or more draft shots | One `On shot N` option per matching shot | `choice_answers[]`; the contract sets that requirement's `shot_index` | A text with no matching label (a planner miss) |
| `title_text` | A live brief `text` requirement with `scope=title` and NO literal | The draft renders through the unified phone montage (`brief_checks.defers_to_unified_montage`: phone-proxy clips, enrolled account, montage-family format, non-voiceover audio) and the renderer would burn no title: `unified_montage.title_source_exists` (the SAME `_title` the render calls: `strategy.opening_title`, a title/global literal, or `title_from_facts`) is false. The detector needs `ChoiceCapability.creator_id`; callers that do not pass it never get this question | `no_title` ("Continue without a title", NOT marked recommended; a delegation such as "you decide" is not an answer and re-asks once with the explanation). Matched exactly after normalisation against a broad alias set (`no`, `none`, `skip`, `no title please`, `don't add a title`, `leave it off`, `başlık olmasın`, `başlıksız`, ...) and never by substring, so "No Plans" is a title, not an answer. Typed words are NOT an option, they become the literal through the normal brief extraction. We never offer to write the words (on-screen text is the creator's: KRI-255) | `choice_answers[]`; `answered_brief` supersedes the wordless title requirement in the pinned copy, so the render-time receipt no longer blocks; disclosed ("I'm leaving the title off...") | A title with words; `opening_title`; a global literal or brief facts to title from; any draft the unified montage does not render (cloud, voiceover, non-montage formats; their draft-time receipts judge it); no `creator_id`; a speech-excerpt draft (`strategy.montage_audio` preserving source audio with source ids, which becomes `contract.audio_source_ids` and takes `run_phone_speech_montage_job`: no title handling, never blocks on one). A legacy contract-less draft that the dispatcher sends to the speech lane anyway cannot be known at draft time |

Not questions: a rule no option can fix, a technical failure, and any combination no
renderer supports (typed declines above).

**One capture-order key set.** `clip_facts.CAPTURE_ORDER_KEYS` (`capture_time`,
`chronological`, `route`, `time`, `shot_order`) is the single constant the contract, the
montage planner (`unified_montage`) and the requirement receipts (`brief_checks`) all import.
The planner and receipts already rendered and judged `route`/`time`/`shot_order` as
capture-time order, but the contract treated them as unverifiable, so every such request was
refused at dispatch. Now they pin `order_basis="capture_time"` exactly like `chronological`.
This changes semantics for **contracted jobs only** (previously `unresolved`, refused at
dispatch); legacy unstamped jobs are untouched. With capture times there is no question;
without them the `order_basis` question applies. A key-less rule stays on the ask path.

**Where it runs.** `planner.plan_live_turn` runs the gate AFTER the media snapshot is
attached, on the same snapshot approval binds (`_plan_from_creator_output` runs before
it and would invent "missing capture dates"). `_complete_draft_turn` re-checks as a
backstop: with an open choice, or (for creators with a brief binding, the only ones whose
dispatch contract reads the brief) a contract that still has `unresolved` items, it
rolls back to a respond turn instead of minting an approvable draft. It says "your draft
is unchanged" only when a draft exists.

**Answers.** A tapped `choice_selection`, or a plain message that normalises (case,
punctuation, whitespace) to exactly one option key, label, list number or server alias
(`submit_turn` -> `match_open_choice`), is stored on the thread and replayed on every
later turn. A question is open until it is answered, replaced by a newer question, or the
assistant replies to a user turn taken after it (a text question or a draft; the per-event-type
table is `QUESTION_EVENT_EFFECT`). Asynchronous events (`generation_ready`, `assistant_review`,
`memory_updated`, `status_update`, `format_prompt`, `media_prompt`, `draft_applied`,
`draft_undone`, `assistant_error`, `assistant_render_failed`) never close it and never use up
an ask. Once it has been asked twice a later non-answer closes it too (a recovery that
restates it in words reopens it).
`resolve_choices` applies an answer only when the question's `input_digest` still equals
the digest of the current inputs, so a changed media set or brief length reopens just that
question and an old answer never answers a different one. **The server-owned answer
wins:** the chosen length / clip subset is written over whatever the model emitted that
turn (it may re-emit the old value or follow the option's label), and the matching brief
requirement is superseded in the PINNED copy (`answered_brief`; the thread's stored brief
keeps what the creator typed). Applied answers are `CreativeStrategy.choice_answers`
(server-owned: a model- or client-written value is discarded), link to the brief
requirement ids they resolve, are part of the `BriefBinding` digest when present, and are
disclosed in the draft summary.

**A later restatement supersedes the answer.** The answer wins on the turn(s) that follow
it (the model may echo the option's label or re-emit the old value). On a LATER turn, a new
user message that is not an answer and not a verbatim re-send of an earlier message, together
with a model plan that no longer carries the answered length/subset ("no, exactly 15 seconds
with all of them", "make it shorter"), drops the answer (kept as provenance only: it never
reaches the strategy or the pinned brief) and the gate asks once more, within the two-ask cap;
after the cap the plan goes through unchanged and the old answer is never silently
re-applied.

**Changing a closed answer by typing it (KRI-794).** With no question open, a plain message
that exactly names a DIFFERENT option of an already-answered gate question (its label, or for
the order basis a topical alias such as "no fixed order"; never a list number, an internal
key, "you choose" or the generic "fark etmez") becomes a server-derived `choice_selection`
on the latest question asked for that conflict, stored with `"reanswer": true`
(`match_answered_choice`); last answer wins. A client-sent selection for a closed question is
still `choice_selection_stale`. Naming the standing answer again stays a plain message unless
a render refusal of that choice came after it. On later turns a standing answer is disclosed
as kept ("As agreed earlier: … To change it, reply "…".", quoting the labels as the question
was asked, so the quoted words re-answer even after the chat's language changed) rather than
as a fresh promise.

**A refusal re-opens its choice once.** An unresolved ORDER line (`ORDER_BASIS_UNRESOLVED`)
raises with `field_path = "ordering_choice"`, which rides the typed decline onto the
`assistant_render_failed` event. When that refusal comes from a turn planned with the order
answer (a render approved BEFORE a typed re-answer is never blamed on the new one), the next
turn's gate asks the order question again (same digest, "I couldn't make your last video with
this choice.", "Continue without a fixed order" recommended instead of the refused option, so
"you choose" never picks it) instead of drafting the refused edit; past the ask limit the
answer keeps applying.

**Never a default.** Length and source-coverage conflicts get one card per unchanged
input digest. The pre-approval backstop keeps unanswered conflicts pending with a plain
explanation; it does not dispatch after the ask limit. Other choices retain their
two-card limit and existing receipt/backstop behavior. Only an explicit delegation ("you choose", "you decide", "up to you", "surprise me"; a bare "whatever" is not one)
picks the recommended option, recorded as `source="creator_delegated"` and disclosed.

`title_text` is the exception to "the plan goes through": a wordless title cannot render, so
once it is exhausted the pre-approval backstop (`_unresolved_choice_plan`) answers in words
("I won't guess, so I haven't made an edit yet ... reply "Continue without a title", or type the
words you want") and keeps the question open like `order_basis`; the render-time block below is
the last line, not the plan.

**Known limits.**

* A restatement is detected only for the length conflict (it compares the model's plan with
  the answered value); an order or text-placement answer is invalidated only by a digest
  change (different clips, different brief rule). A verbatim re-send of an earlier message
  never reopens an answer.
* An explicit sequence carried in `selected_media_ids` with a key-less order requirement
  (prod thread aed98bf6) is asked about for now: no renderer is proven to honour selection
  order, so it cannot be pinned as a verified basis. Corpus record
  `clarify-explicit-sequence-via-selection-asks-for-now` documents it; the route resolver
  (PR-D/F) should replace the question with a pinned order.
* `group_first` is not yet an explicit contract order basis: the arrangement is computed at
  render time (visual scatter, sequence intents), so pinning it needs the route resolver
  (PR-D).
* A `timing` requirement the model fails to emit keeps `duration_vs_count` silent ("30 clips in
  15 s" passes if the 15 never reached the brief), and a length question also needs a resolved
  clip set (a resolved `include` intent or a selection on the strategy): no include intent from the
  clip-request resolver means no count to compare. `montage_audio` and the other archetype
  exemptions in the table keep BOTH questions silent (including an archetype the model picks on
  its own for a plain "N second montage"); the KRI-469 route gap on those plans (cloud
  declines `montage_audio.source_media_ids[]`, phone renders `speech_montage`) is pinned in
  `test_montage_audio_route_shape_is_the_documented_kri469_gap` until the PR-H slice changes it.
* Questions render through the generic v1 `ChoiceQuestionCard` on iOS; web has no question
  card and shows the plain-text list.
* **The unified montage settles `text`/`order`/`timing` at render time**
  (`brief_checks.UNIFIED_SETTLED_KINDS`; `requirements_to_check_at_draft` skips them), and
  `_run_phone_unified_montage_job` BLOCKS the render when a bound brief has a checked receipt
  that is not met. A creator therefore only meets such a requirement after approving unless a
  gate question catches it earlier: `title_text` does for a wordless title (KRI-470); a wordless
  `text` requirement of another scope or an unmet `order` still blocks at render time. The block
  raises a bare `UnsupportedPhonePlan` ("Should I try again or simplify this request?") except
  for a wordless title, which is a typed `needs_choice` decline (`field_path=opening_title`,
  alternative: tell me the words or say "continue without a title") worded "I couldn't make the
  video yet: ..." because no video exists. With a second blocker the block stays untyped
  but its copy ends with the title's way forward. The narrated-alignment recovery at the second
  `ask_before_simplifying` site is still untyped (no receipt behind it).
* **The render-time block re-opens the `title_text` question.** The observer
  (`kria_runtime._blocked_title_question`) puts the same question (same `conflict` and
  `input_digest` as the draft-time one) on the `assistant_render_failed` event whenever the
  stored receipts carry the wordless-title reason and choice questions are on. That event type
  never closes an open question, so "continue without a title" (or any alias) is turned into a
  `choice_selection` server-side by the existing free-text matcher, the gate replays it from
  the thread events and `answered_brief` supersedes the requirement for the next render.
  Pinned end to end on Postgres (`tests/kria/test_title_block_answer_postgres.py`). Without
  stored receipts (binding mismatch) or with the flag off there is no question: the creator's
  reply is plain text.
* **Typed words after the block are NOT verified against the live brief extractor.** A reply
  that is not an option is never turned into one (deterministic). What it extracts to is the
  extractor's job: its prompt says `literal` is ONLY text the creator wrote out, so an
  instruction such as "make it fun" should arrive as a description with no literal and the
  title stays wordless (the gate re-asks, within the two-ask cap). The repo has no
  instruction-versus-title heuristic; if the extractor ever returned such a reply as a literal
  it would be burned verbatim. Known limit; verify with a live eval before relying on it.
* **A failed first render is not a render.** An item whose only job failed with no variant
  (`planner._job_never_rendered`) used to answer every follow-up with "I couldn't open your
  current edit to change it in place" (`no_ready_variant` is a guarded miss), so the typed
  words never reached the planner. It now records the unguarded `no_render` miss and skips the
  extract-first rendered-edit path: the follow-up is a normal re-plan (`has_render=False`). An
  in-flight (`render_in_flight`) or stale (`editor_state_stale`) target keeps its guarded reply.
  After the render-time block the `title_text` question is open (next bullet), so the reply
  is answered deterministically.
* A runtime-v2 render failure is worded once: `reconcile_render_state` holds back the generic
  "That render didn't finish" line only while runtime v2 is on AND a dispatched execution for that
  Job is younger than `creator_sessions.OBSERVER_FAILURE_WINDOW` (5 minutes; the observer normally
  posts within ~30 s). Past that, or with v2 off, or with no execution, the line is posted, so a
  failure is never lost when the observer never posts (that branch has already set the session
  to `failed`).
* `planner._FAILED_JOB_STATUSES` lists `matching_failed` / `no_labeled_tracks` too; they exist only
  on auto-music jobs (harmless here). #1460's `latest_job_failed` already keeps the bound
  extract-first path off a failed item; the `no_render` miss covers the copilot-first and
  editor-revision paths that still reach `_load_editor_target`.

## Stored-contract compatibility

`CreatorRenderContract` forbids extra fields and digests its full dump, so adding
a field would change the digest of every stored contract. `_digest` skips any
field registered in `_POST_V1_FIELD_DEFAULTS` while it holds its default, and
`STORED_V1_CONTRACT` in `tests/services/test_creator_render_contract.py` is a real
stored v1 contract that must keep reading. Register a new field there in the same
change that adds it.

## Shared length ownership (KRI-492)

The [36-pair phone/cloud audit](../reviews/kri-492-length-ownership.md) records each
format/audio combination, support conditions, natural source, exact selection,
verifier, and current qualification. `length_policy.py` declares one typed owner:
`creator`, `song`, `voiceover`, `take`, or `footage`. New format/audio combinations
require a declaration. A creator-named target uses ±10%; otherwise song placements
or a frozen music section, recorded narration, cleaned take, or selected best parts
provide the clock. Derived lengths remain notes, not creator proof.

An inclusive ceiling ("at most 10 seconds", "max 10 s", "en fazla 10 saniye", KRI-847) is not a
±10% target. The model records it on the `timing` requirement as
`facts.duration_bound = "at_most"` beside `duration_s` (absent or unknown = exact; a change
that sets `duration_s` without it drops it, as does an answered length card).
`length_policy.creator_duration_satisfied(..., "at_most")` is the exact check without its lower
side (`0 < actual <= N + 10%`): it can only relax the existing check, never tighten it, so a
wrong or stale fact cannot create a hard cap. It is used by the draft receipt ("you asked for
at most 10s"), the length plan (`target_duration_bound` on the envelope), the choice cards,
the approved music window and the render check ("You asked for at most 10 seconds; this edit
is 11.5 seconds."). Exclusive wording ("under N", "less than N", TR "N saniyenin altında") is
the strict whole-export cap of KRI-629 (`strict_duration_limit_s`), read from the description
and judged on the measured export including its ending; `duration_bound` never changes it. The
bound never rides on `CreatorRenderContract` (frozen schema, see above): dispatch writes it to
`assembly_plan["creator_length_bounds"][contract_digest]`, and `read_length_policy` returns it
as `state.requested_duration_bound`, so an exact job's stored keys are unchanged.

`length_planning.py` freezes exact best-parts windows once at server planning, with
source identities, capacity, deterministic ranking/fallback receipts, and generation.
Phone and cloud compilers read that envelope. The silent cap is 120 seconds for
footage-owned edits and 240 seconds when a voiceover or a talk-to-camera take owns the
length (`best_parts.edit_cap_s`, KRI-955); the included-source floor is 0.1 seconds; unused capacity is fairly redistributed.
Infeasible targets and coverage use the durable scoped choice gate before approval.
An authored assembly override keeps its own approved timing.

`creator_length_policy` lives beside the v1 contract and binds its generation and
digest, preserving the stored schema. Historical contracts without that state retain
legacy verification. New explicit-duration checks default to `log_only`; audio,
ordering, text, and recipe-integrity checks retain their independent requirements.
Historical jobs without an approved window envelope also retain their existing
matching and contiguous-slot behavior during regeneration. Source probes alone
do not authorize replanning their windows.
The approved check mode is frozen per generation. An editor rebind changes only its
variant's policy. Draft receipts cannot call a model target measured output.

### Replay and enforcement promotion

CI replays all complete captured successful jobs, capped at the latest 100. The
committed packet contains all 23 complete available snapshots. `base_sha` identifies
the actual comparison revision; `capture_base_sha` preserves original capture
provenance. To reproduce, export base outcomes using the harness against the archived
base API, then evaluate the candidate with `--baseline`, `--base-sha`, and
`--candidate-sha`. Missing replay inputs cannot count as passing. Minimized captures
prove structural behavior, not original visual/text equality.

Fly Deploy sets `CREATOR_DURATION_CHECK_REVISION` to the exact deployed SHA and
`CREATOR_DURATION_CHECK_MODE` to the selected mode. The verifier emits sanitized
`creator_length_check` JSON observations with mode, counts, timestamp, and revision;
no creator text or job identity is logged. The operator must independently verify
both API and worker revisions and the completeness of the uninterrupted log stream.
An event's revision fields alone do not establish fleet deployment.

After at least 24 actual continuous clean hours, collect the verified JSONL stream:

```sh
python scripts/ci/collect-length-checks.py --fleet-proof /private/tmp/fleet-proof.json \
  --journal /private/tmp/length-observations.json \
  --interval-start START_EPOCH --interval-end END_EPOCH --stream-complete < /private/tmp/fly-checks.jsonl
```

The fleet proof supplies `expected_candidate_sha`, matching `api_revision` and
`worker_revision`, `deployment_started_epoch_s`, a fresh `timestamp_epoch_s` (within
five minutes), and nonempty `log_source_provenance`. The collector validates this
operator-supplied proof; it does not authenticate Fly. Missing, malformed, stale,
disconnected, wrong-revision, empty, or declined observations remain pending. Its
journal contains only aggregate counts, intervals, and revisions.

Combine the journal's `clean_observations` with schema-version-1 promotion evidence:
base/candidate SHAs; complete successful replay counts bound to those SHAs; zero
newly blocking declines; deployment/clean-window timestamps; unique
`affected_supported_paths`; and one measured successful export per affected path.
Every canary records exact API/worker revisions, timestamp, duration, frame count,
command, app build, fixture SHA-256, and source provenance. Local synthetic exports
alone do not qualify as deployed canaries.

From the checkout matching the candidate SHA, prepare the immutable dispatch:

```sh
PYTHONPATH=src/apps/api src/apps/api/.venv/bin/python scripts/ci/prepare-length-promotion.py \
  --evidence /private/tmp/length-promotion.json --repo-root .
```

The tool emits a reviewable command; it does not dispatch. The repository's Fly
Deploy workflow rechecks main against `expected_sha` and validates all evidence
before setting `enforce`. Normal release approval is required. Insufficient real
observations keep KRI-492–499 open until promotion and deployed verification finish.

## Plan-authority stamp

`KRIA_PLAN_AUTHORITY_ENABLED` (default on) is read once, where a job's contract is
first stamped (`services/generative_jobs.py`, or `tasks/content_plan_build.py` when
dispatch is what first builds it), and persisted as
`Job.all_candidates["creator_plan_authority_version"] = 1` (a JSON key; no
migration). Workers branch on the stamp and never on the live flag, so a running
job cannot change behaviour; jobs without the stamp keep legacy behaviour. Only the
route stamp and the shadow route check read it so far (see "Route resolution"); nothing
renders differently because of it yet.

| Requirement | Phone evidence | Cloud evidence | Current rule |
| --- | --- | --- | --- |
| Duration | Compiled recipe duration within the accepted tolerance | Renderer measured duration (`actual_duration_s` or measured `duration_s`) | Compare actual output against the generation-frozen owner. There is no take-length exemption: missing owner receipts are `missing`, and only creator proof activates the ±10% target. |
| Recorded voice | Audible `VoiceoverRenderAsset` on an audio track | `narration_applied`, set only when the mixer reports the recording is in the output file | Guided and classic honour it; missing evidence declines |
| Camera audio (require / forbid) | Audible original assets, source IDs, and complete mute-window coverage | `source_audio_ids` + `source_audio_state` from the audio graph actually mixed | Guided honours and proves it; classic declines up front; named sources decline everywhere |
| Chronological order | Every selected clip has capture evidence; recipe picture sequence matches resolved IDs | `actual_clip_order` / `picture_timeline` from the rendered moments | Guided: gated before render, then verified; classic declines up front |
| Exact text | Normalized text layers, role/clip placement, and timing | `text_evidence` rows (role, text, window, media id) from the burned, pixel-checked layers | Guided honours and proves it; classic and slides decline |

Audio intent copied into a receipt or field such as
`source_audio_preserved` is not proof that camera audio survived the render.
The phone verifier needs actual recipe track/gain/mute evidence; the cloud
verifier needs the evidence's own `source_audio_ids`. Likewise, a loose list of
text strings cannot prove opening, closing, or per-clip placement: only rows
that carry a role and window, measured on the burned output, can.

### Cloud evidence (KRI-470 PR-E)

Two different things are declared per cloud adapter, and only one of them lifts a
preflight decline:

- **Honours** (`CLOUD_ADAPTER_DECLARATIONS[...].consumes`): the renderer follows the
  requirement by construction, or a cheap pre-render gate proves the plan already does.
  Only these are lifted at preflight, so a default contracted job never renders
  everything and then fails publication.
- **Evidences** (`CLOUD_EVIDENCES`): what the receipts can report. A superset; the
  post-render verifier checks it as the last line of defence, never the first.

| Requirement | `cloud_guided_story` | `cloud_classic` | `cloud_slides` |
| --- | --- | --- | --- |
| `duration_s` | honours, evidences | honours, evidences | declined (stills) |
| `require_voiceover` | honours (execution contract + pinned narration mix), evidences | honours (by archetype), evidences (**was declined: rendered then rejected**) | declined |
| `original_audio` | honours (`montage_audio` is in the snapshot) + **gated** (pre-render plan check), evidences | **declined** (matcher never reads `audio_strategy`; song variants replace camera audio, track-less ones keep it), evidences | declined |
| `order_required` | **gated** (pre-render plan gate), evidences | **declined** (greedy matcher never reads the contract), evidences | declined |
| `exact_texts` | honours typed copy (opening, closing, shot labels) + **gated** (pre-render plan check), evidences | declined | declined |
| `audio_source_ids` | declined (mixes every clip's sound) | declined | declined |
| `hidden_parts` | declined (no renderer keeps a part out or reports its windows) | declined | declined |

Before this PR every cloud adapter declined `exact_texts`, `audio_source_ids`,
`original_audio` and `order_required`, and classic voiceover/narrated jobs rendered
and were then rejected at publication for lack of a receipt.

**The guided pre-render gates.** The guided builder selects a coverage set and does not
order it by the contract (`ordering_choice` is never read by the proposal builder; fast
montage records `ordering_not_applied`), does not reconcile a recorded voice or a song
with camera audio, and has no field for brief-derived text. So after
`_guided_execution_plan` and before the attempt is claimed or any media is touched,
`check_guided_plan` runs three pure checks on the pinned plan, each a typed decline with
an alternative, persisted like other typed declines:

- **Order** (`check_guided_plan_order`): the collapsed (adjacent repeats merged) media order
  of `story_timeline` against `contract.order_ids` restricted to the media the plan uses.
  Because the proposal's own order is whatever the proposal builder chose, a contracted
  chronological order is declined whenever that order is not chronological: chronological
  order on cloud guided jobs is effectively unsupported until the route work (PR-D/F).
- **Camera audio** (`check_guided_plan_audio`): `original_audio == "require"` is declined
  when the plan has a narration, a mixed song, a creator song, a muting `montage_audio` or a
  pre-v6 compiler (`requirement_conflict` when the contract also requires the recorded
  voice); `"forbid"` is declined when the plan keeps source audio. Footage with no audio
  stream remains the one post-render case.
- **Exact text** (`check_guided_plan_text`): the plan's own text layers are checked with the
  verifier's role/window rules, assuming every layer renders. Guided honours the typed copy
  fields (opening title and hold, closing title, shot labels, per-clip montage labels) and
  the plan's generated captions/labels; a brief-derived literal with no snapshot field is
  declined before render.

The verifier applies the same order rule to the rendered order (`order_satisfied`), so a
gate that passed and a render that drifted is still refused.

**Where the evidence lives.** In a plain dict on the variant, `variant["cloud_evidence"]`
(`app/pipeline/cloud_render_evidence.py`), beside `render_receipt` and never inside the
strict `GuidedStoryRenderReceipt`: that model forbids unknown fields, so an older worker
reading a newer receipt during a rolling deploy or rollback would otherwise raise
`guided_story_receipt_mismatch`. The strict receipt is byte-compatible with before.
`verify_cloud_variant` reads the receipt and the sibling together.

| Evidence key | Meaning |
| --- | --- |
| `actual_clip_order` | Source media ids in output order (immediate repeats merged) |
| `picture_timeline` | `{media_id, start_s, end_s}` per segment of the output picture (guided only) |
| `source_audio_ids` / `source_audio_state` / `source_audio_reason` | Sources whose own sound is audible; `muted` carries why (`replaced_by_narration`, `replaced_by_music`, `not_preserved`, `level_zero`, `no_source_audio`) |
| `text_evidence` | `{role, text, start_s, end_s, media_id?}` per text layer measured visible (guided only) |
| `narration_applied` | The recording is in the output file (the voiceover mixer copies the video through on failure, so intent is never proof) |
| `actual_duration_s` | Probed length of the output |

`None`/absent means "this renderer did not produce it" and the verifier reports
`evidence_missing`; `[]` means "produced, and there was nothing". Evidence describes
one artifact and never outlives it: a new artifact without fresh evidence drops the old
(`_update_variant_entry`, the staged-merge helper and `_merge_finalized_variants`), a phone
export that replaces the cloud file drops it (`device-render/complete`), and a later artifact
with none is `evidence_missing`, not a pass on stale evidence. A guided text reburn
re-derives the text rows from the edited elements. A classic text reburn carries the existing
evidence over (a text burn changes neither picture order, camera audio, narration nor length)
only while the base is current (`base_video_stale` unset) and the variant already had
evidence. Receipts written before this change
have none and are never failed retroactively for requirements they never had to prove.

Evidence is emitted only for jobs that carry the contract marker
(`_cloud_evidence_context`), so a job without it gains no key on its variants. This is
narrower than "unchanged stored data": the marker is set whenever a creator strategy
exists, so every creator-flow job gains a `cloud_evidence` key. A legacy job is recognised on
the row already in hand and gains no key and no copy; the guided first render still costs
one extra read of the job row for the plan gate (the pinned plan loader's own read is not
shared). The context carries the approved
`gcs_path -> media_id` map. An unreadable job row at the guided gate logs
`guided_plan_gate_unreadable` (with the job id), the gate is skipped and the render proceeds
without evidence, which a contracted publication then refuses visibly.

How each adapter evidences a requirement and where it stops:

- **Guided story** (`guided_cloud_evidence`): order and picture windows come from the
  moments actually rendered; camera audio from the branches `_mux_guided_source_audio`
  mixed (replaced by a narration or a song when one is applied, 0 when the creator's
  level is 0); text from `burn_text_overlays_skia_with_evidence`'s per-element pixel
  check, with the role taken from the compiler's own element ids (`guided-title`
  opening, `guided-closing-title` closing, `clip-label-*` / `montage-text-*` per shot,
  bound to the segment they overlap). A closing title's planned end is compared with the
  measured duration using the renderer's own slack, `max(0.2, 0.04 * moments)`
  (`duration_tolerance_s`, shared with the guided duration check).
- **Classic** (`classic_cloud_evidence`): emitted by the montage/voiceover/day-vlog/
  single-hero renderer (`_process_generative_variant`) and the narrated renderer. Its
  slots are SOURCE-time windows, so classic reports order and camera audio only and
  never per-clip output timing (no `picture_timeline`). A collage (masonry) cut or a
  spliced carousel moment withholds order/audio evidence rather than guessing.
  Talking-head and subtitled renders emit no evidence, so `check_classic_archetype`
  refuses `require_voiceover` for them (`capability_unavailable`) once the archetype is
  known, before any variant renders. Order and camera audio are *reported* but declined
  up front (see the table).
- **Slides**: stills, no length, no voice, no camera audio, no order: everything stays
  declined with `capability_unavailable`.

Known limits: lanes that publish a new artifact without going through the renderers
above carry no evidence, so on a contract with objective requirements they stay refused
(`evidence_missing` or the in-place decline). For classic that is every reburn/edit lane:
narrated caption and bed-level reburns, camera-effect re-renders, retranscribe, the
overlay and sound-effect passes, and fast text reburns (`_update_variant_entry` drops the
receipt on a new artifact). Editor re-renders of a guided plan (timeline, revision,
orientation) re-run `render_execution_plan` and so re-emit evidence, but the pre-render
gates only run on the first render: revision re-renders are protected only by the
publication verifier.
Real-output check: an independent per-segment `ebur128` of the rendered file matches the
evidence's order and camera-audio claims (each IstRun clip has a distinct loudness
signature), a muted edit measures -70 LUFS, and a voiceover whose mix failed is a
copy-through of the footage with no voice, reported `narration_applied: false`.

Preflight takes the dispatched adapter (`cloud_adapter_for_job`); without a named
adapter it keeps the pre-PR conservative refusal, so nothing is lifted on an unknown
route. `verify_cloud_variant` picks the adapter from the variant
(`cloud_adapter_for_variant`).

## Prepared execution snapshot (KRI-722)

One immutable row per approved edit, `creator_prepared_executions` (migration 0125),
ties consent to everything that runs under it. Code: `app/kria/prepared_execution.py`.
Failure matrix: `docs/reviews/kri-722-prepared-execution.md`.

```text
 draft (immutable revision + snapshot_hash)
        |
 decide_approval: every gate passes ──> mint snapshot (same transaction)
        |     approval.prepared_execution_id = snapshot.id
        |     snapshot: draft id/revision/hash, approval id, readiness digest,
        |     brief version + digest, binding digest, route, sources[] (path, media id,
        |     original/analysis_proxy role, object generation, trim window),
        |     worker capabilities digest, per-decision digests with origin,
        |     compiled refs. No signed URLs or credentials (validator refuses them).
        v
 _claim_approval_dispatch: verify against live rows
        |     draft hash, binding digest, capabilities, source generation + lineage
        |     mismatch -> execution stale, code prepared_execution_stale /
        |                 prepared_execution_source_lineage / prepared_execution_unsupported
        |     ok -> new attempt_id; execution.result["prepared_execution"]
        v
 dispatch (strategy) or claim (editor): Job stamp
        |     all_candidates["creator_prepared_execution"] = {id, digest, schema_version}
        |     assembly_plan["creator_prepared_execution"] = {id, digest, attempt_id,
        |        approval_id, draft_id, draft_revision, generation_id, contract_digest,
        |        variant_id (editor only)}
        v
 device pin copies {id, attempt_id} into the variant record;
 publication refuses a record whose attempt is not the Job's current one
```

Rules:

- **Stamp, not flag.** `KRIA_PREPARED_EXECUTION_ENABLED` (default on) is read once,
  at approve. Workers branch on the stamp. Unstamped Jobs (historical, flag off)
  run the legacy path unchanged. The strict `CreatorRenderContract` model is not
  touched, so stored contract digests do not move. The contract digest is per
  attempt (it covers the Job's `creator_generation_id`), so it lives on the Job
  stamp, not in the snapshot.
- **Versions.** `SCHEMA_VERSION` and `COMPILER_VERSION` are refused when above the
  worker's own (`prepared_execution_unsupported`, recovery `refresh_replan`). An
  old worker without this code ignores the stamp (legacy), the same rolling-deploy
  exposure as the plan-authority stamp.
- **Retry (KRI-674).** The retry approval references its source attempt's snapshot.
  `claim_render_retry` re-verifies it, mints a new `attempt_id`, moves the Job stamp
  to it (`retry_of_attempt_id` names the previous one) and re-pins the recipe.
  No planner runs. A snapshot a dispatch claim already invalidated is refused.
- **Redelivery.** An accepted execution that already carries the
  `prepared_execution` record for the same snapshot (lost acknowledgement,
  outcome unknown) returns that record without re-fencing draft, binding,
  capabilities or sources. Its Job may already be rendering; a deploy or flag
  flip in between must not cancel it or invalidate the snapshot.
- **Receipts.** A stamped Job's unified-montage receipts read the approved brief.
  With a brief binding it comes from the Job's binding, checked against the
  snapshot's binding digest. Without one (creators outside the binding cohort) the
  snapshot pins the brief version current at approval (`brief_version` +
  `brief_digest`), and the reader loads exactly that `creative_brief_versions` row.
  Later chat cannot change what an accepted attempt reports. Unstamped Jobs, and
  snapshots approved with no brief at all, keep the latest-brief read.
- **Scoped decisions.** `decisions` is a map of per-key digests (`text`, `length`,
  `order`, `audio`, `direction`, `editor`, `shape`, `speech_cleanup`), each with an
  `origin` (`creator`, `creator_answer`, `model_default`, `system`) taken from
  provenance only. Strategy groups are planner output, so `model_default` even when
  a brief requirement asked for something inside them (field-level creator
  attribution is KRI-824). Editor drafts are `model_default` (Kria chat edits, undo
  baselines, comparisons) unless the payload is the creator's own editor state
  carried unchanged (`client_state_id` set, no `changes`, no text diff).
  Approval-card answers (`render_shape`, `speech_cleanup`) are `creator_answer`.
  `changed_decisions()` names the keys two snapshots disagree on (for KRI-723+).
- **Retention.** `prune_kria_drafts` keeps a draft body while a non-invalidated
  snapshot references it.
- **Cloud attempts** are fenced by `creator_generation_id` (each strategy dispatch
  mints a new Job and generation; the stamp records it). Cloud exact retry is KRI-723.

Not yet: concrete montage/speech/text/audio decisions still resolve inside the
render (KRI-723/724/725); the snapshot refuses drift but does not hold compiler output.

## Named moments of one take (KRI-723, first slice)

"Six moments from it, in this order: A, B, C ..." names actions, not windows. Six ordinal
energy windows in the right count and order are **not** evidence of six actions, so they never
read `met`.

- **Which moments.** `services/named_moments.named_moments`: resolved `then` order rows that
  name the edit's only video (one row listing them, or one row per moment). Rows naming only
  Visuals ride along. A moment count that disagrees with the named moments is not guessed at.
- **Evidence, no model call.** `ground()` matches the creator's words against stored
  `best_moments` / `understanding.notable_moments` (`start_s`, `end_s`, `description`) with the
  KRI-828 scorer (now shared). One best match = `grounded`; a tie (or two moments on one
  interval) = `ambiguous`; none = `unlocated`. Model prose, even with timestamps, never counts.
- **Ask before approval.** In `_gate_unresolved_choices`, an unlocated/ambiguous moment on a
  turn stamped `answers_moment_questions` (app protocol >= `moment_order_min_client_protocol`,
  **20** = the first iOS build with `MomentOrderCard`; `999` is the kill switch) returns `moment_order_question` instead of a draft,
  and keeps returning it until every moment is pointed at, skipped or left to Kria (a
  free-text reply never turns into guessed windows). Pins are dark with the question: an app
  that cannot answer keeps today's v1 windows byte-identical with an honest `not_located`
  receipt, so 999 is a real kill switch. A selected photo beside the take is never asked
  about (the length plan pins only all-video edits).
- **Answer.** `SubmitTurnBody.moment_order` (`point` / `skip` / `delegate` per moment, in the
  creator's final order). `runtime._validate_moment_order`: 409 `moment_order_stale` (old
  question, re-uploaded take), 404 `moment_order_media_not_found` (not this thread's take),
  422 `moment_order_invalid` / `moment_order_overlap`. The echo models read has no digits.
  `fold_moment_orders` re-reads it from thread events; the key is the creator's folded words on
  that file generation, so a reworded moment or re-uploaded take is re-asked.
- **Envelope v2.** `build_length_plan(pinned_moments=...)` gives each moment one window in the
  creator's order (a pointed window exactly as given; a grounded one centred on its evidence;
  "you pick" / unlocated from the strongest free moment) and records `moments[]` (+
  `skipped_moments`), whoever owns the length (a licensed-music section included). A pointed
  window ends at the take's end (the record holds what plays). Pins that cannot all be placed
  are a blocking `named_moments_unplaceable` conflict, never a fall-back to ordinal windows.
  Only such an envelope is v2; every other plan stays byte-identical v1. Readers use
  `supported_length_plan_version()`; `read_length_plan` refuses a v2 whose creator-pointed
  window was changed.
- **Execution and receipt.** The worker plays the envelope windows in order; Visual seating
  and freeze ("freeze the frame on the bite") use the located moments. `_moment_outcomes` is
  `met` only when the cut shows the evidenced interval; without evidence a row is `partial`
  `not_located` (a format limit: it never blocks the render, never claims the action).

Not in this slice: several takes interleaved, the user-song / lip-sync variants, text and
music frozen once. Proof: `tests/kria/test_kri723_moment_authority_m2.py`
(`KRI723_ARTIFACT=<dir>` writes `report.json`) and `tests/kria/test_kri723_moment_order_turns_postgres.py`.

**iOS card (protocol 20).** `MomentOrderCard` (`Kria/Features/MomentOrderCard.swift`, model in
`Kria/Core/MomentOrderQuestion.swift`) shows the moments in the creator's order (drag or arrows), and
per row: "Show where" (the KRI-717 source-window picker on this iPhone's original), "Not in my clips"
(`skip`) or "You pick" (`delegate`). A grounded row starts pointed at its evidence. The answer is
sent as `moment_order` with the server's neutral echo as the message. 404/422 keep the card and its
windows and show the server's reason on it; 409 `moment_order_stale` refreshes the thread, closes the
old card and says the newest one is the one to answer. Capability `moment_order_questions` (false at
999). Proof: `MomentOrderUITests` (XCUITest).

## Which spoken occurrence (KRI-724, phone Talking slice)

"Show MEROPE the first time **she** says Merope" on a one-take Talking video: the take's words
carry no speaker and there is no diarization (no paid model), so the first "Merope" anyone says
(Yasin's, off camera) is not evidence of hers. The creator is asked before approval.

- **Ask.** `choice_questions._speech_occurrence` (kind `speech_occurrence`, priority after
  `title_text`), only on a one-take `subtitled` / `talking_head` draft that is not a voiceover,
  with `SPEECH_OCCURRENCE_QUESTION_ENABLED` on. It asks about (a) a text-at-a-spoken-word ask
  that names someone else as the speaker, even when the word is said once, and (b) a named cut
  line said 2+ times with no ordinal (today `said_more_than_once`, nothing cut). Occurrences
  come from the take's timed sentences through the worker's own matchers
  (`phone_spoken_text.spoken_word_occurrences`, `phone_opening_cut.line_occurrences`), inside
  the start/end cut the draft will make (`estimate_take_window`). Options: up to five times as
  `0:40 “…Merope, it's a Colombian coffee…”`, "Every time" for a cut, and an explicit waiver
  ("Leave the text off" / "Don't cut it"). Nothing is recommended and "you choose" is not a
  delegation (only the creator knows whose voice). It is the existing `choice_question` card:
  no client or protocol change. The digest covers the media id and the occurrence times, so new
  media re-asks; two unanswered asks pass the plan through with KRI-791's honest `partial`.
- **Answer.** Option key `occ_<n>_of_<m>_t<centiseconds>` / `occ_all_of_<m>` / `waive`;
  `ChoiceAnswer.value` (same field as KRI-844's PR #1787) holds the word / line. Answers are
  server-owned (`resolve_choices` drops model-written ones) and frozen in
  `BriefBinding.choice_answers` at approval. The pin is read **only** from those answers
  (`opening_cut_ask.answered_occurrence_pins`), never from a brief fact. A waiver is an agreed
  simpler obligation: `answered_brief` supersedes the text ask, or drops the line from
  `cut_lines`.
- **Execution.** `opening_cut_ask.select_occurrence`: the ordinal when the render hears as many
  occurrences as were offered; otherwise the nearest one within `min(1 s, half the gap to its
  neighbours)` of the picked time. Text: on the cut timeline (the pin's source time remapped
  through the plan's removals); receipts `pinned_occurrence_missing` / `pinned_occurrence_cut`,
  never a fall-back to the first match. Cuts: that occurrence (or every one); a missing pick
  is `pinned_occurrence_missing`.
- **Receipts.** A placed pinned text is `met` (the creator named whose word it is); the draft
  says "I'll show it on the “Merope” you picked, at 0:40 in your take". A miss is `partial` with
  the reason.
- **Rollout.** Flag default `false`. Order: API + worker deploy first (workers ignore pins that
  never exist), then `fly secrets set SPEECH_OCCURRENCE_QUESTION_ENABLED=true` (api + worker) +
  restart. Off = the question is never asked; renders read the first match exactly as before.

Not in this slice: the recorded-voiceover / narrated slice (KRI-889), a quoted start/end line
that is not found (KRI-844), real speaker diarization, and physical-device export proof
(KRI-734). Proof: `tests/kria/test_kri724_speech_occurrence_replay.py` (`KRI724_REPORT=<file>`
writes the JSON), `tests/kria/test_kri724_speech_occurrence_turns_postgres.py`, and
`python -m app.cli.verify_take_window --take tests/fixtures/kri790_merope_t1_take.json
--answer occ_2 --spoken-text "MEROPE::Show MEROPE in big letters the first time she says
Merope"` (`.take-window-verify/report.json`; `--video` adds the frame at the placed time).

## Cloud adapters and the prepared snapshot (KRI-725, PR 1)

KRI-722 stamps every approved cloud Job, but until this PR only the device path read
the stamp after dispatch. The cloud adapters now enforce it in three places:

- **Worker start** (`generative_build._enter_prepared_attempt` ->
  `prepared_execution.verify_job_stamp`): one call in `_run_generative_job_impl`,
  right after `preflight_cloud_contract`, under the Job lock. It checks that the
  snapshot row exists, belongs to the Job's owner, parses at this worker's
  schema/compiler version, is not invalidated, and matches the stamp's digest and
  the Job's `creator_generation_id`. A refusal fails the Job with
  `failure_reason = prepared_execution_stale | prepared_execution_unsupported`
  before any ingest or model spend. It never rechecks live capabilities, so a
  deploy cannot cancel an accepted attempt (KRI-722 F10). One-variant rerenders
  (`regenerate_generative_variant`, `rebuild_slide_post_variant`) run the same
  check only when a Kria editor approval or exact retry re-pinned that variant.
  The creator's own later manual edits keep today's path.
- **Publication** (`_prepared_attempt_superseded`, called first in
  `_reject_unverified_cloud_variant` and in `_update_variant_entry`): inside a
  prepared attempt, a new playable output publishes only while that attempt is
  still the variant's `variant_fence`. A late completion of a superseded attempt
  becomes a failed variant (`prepared_execution_code = prepared_execution_superseded`,
  under the existing `creator_render_contract_unverified` class so every boundary
  keeps the last-good artifact). The attempt is held in a task-scoped contextvar
  that `_owned_job_task_fence` resets, so renderers cannot forget to attach it.
- **Guided dispatch** (`content_plan_build._story_matches_prepared_execution`): the
  recompiled story must match the snapshot's `compiled_refs.prepared_collection_story`
  digest minted at approve, not only the live proposal's own digest.

**Slides.** A slide-post approval pins every slide and sticker as a snapshot source
(`slide:i`, `sticker:i:id`, with object generation) and the draft digest as
`compiled_refs.slide_post`. The claim refuses a changed caption, order or photo.
Dispatch copies the approved draft onto the Job (`approved_slide_post`), and the
first render reads that copy instead of `PlanItem.slide_post`. It refuses a pinned
asset whose generation moved after dispatch. Non-slide snapshots gain no key, so
existing digests do not move. Kria's strategy policy does not offer `slides` today
(`format_availability` -> `policy_edit_format_unavailable`), so in production slide
posts still dispatch unstamped from the slides route. These fences apply as soon as
a Kria approval can reach the slides adapter.

No `COMPILER_VERSION` bump: a bump would invalidate every in-flight approval at
deploy. An old worker that renders a stamped slides Job falls back to the live read,
which is the same rolling-deploy exposure KRI-470/722 document.

### Prepared coverage matrix

`app/kria/prepared_coverage.py` keeps one row per render adapter. Each row names
where every approval-critical input is pinned, the worker and publication fences,
and the state of each decision key. A new adapter, or a key with no row, fails at
import. `gap:` rows name the ticket that owns the missing capability. They are
unmet demand, not support claims. `executed` means the decision is carried into the
pinned execution and fenced. It does not mean output proof (KRI-734). The guard test
`tests/kria/test_prepared_coverage_guard.py` keeps this table equal to the code:

<!-- prepared-coverage:start -->
| adapter | text | length | order | audio | direction | editor | shape | speech_cleanup |
|---|---|---|---|---|---|---|---|---|
| cloud_classic | gap:KRI-725 | executed | gap:KRI-725 | gap:KRI-725 | executed | executed | executed | executed |
| cloud_guided_story | executed | executed | executed | gap:KRI-725 | executed | executed | executed | executed |
| cloud_slides | executed | not_applicable | executed | not_applicable | executed | not_applicable | executed | not_applicable |
| phone_guided_unified_montage | executed | executed | executed | executed | executed | executed | executed | not_applicable |
| phone_narrated | executed | executed | executed | executed | executed | executed | executed | executed |
| phone_speech_montage | executed | executed | executed | executed | executed | executed | executed | executed |
| phone_subtitled | executed | executed | executed | executed | executed | executed | executed | executed |
| phone_voice_behind_footage | executed | executed | executed | executed | executed | executed | executed | executed |
| phone_voiceover_montage | executed | executed | executed | executed | executed | executed | executed | executed |
<!-- prepared-coverage:end -->

Evidence: `KRI725_ARTIFACT=.dev/kri-725 pytest tests/kria/test_kri725_cloud_prepared_postgres.py`
writes `report.json`, `slides_positive.mp4` and `frames.png`. Failure matrix:
`docs/reviews/kri-725-prepared-coverage.md`.

## Route resolution (KRI-470 PR-D)

`services/render_route.py:resolve_route` is the one function that maps an approved plan
to a render route. It is pure, and no string a creator or model wrote can reach it. Its
inputs are a **typed projection** (`RouteInputs`), not the contract or strategy objects:

- `ContractFacts`: booleans and enums lifted from the pinned contract (duration set, voice
  required, camera-audio sources present, `original_audio`, order required, unresolved) plus
  the matrix field path of the first exact text, derived from its *role*. Exact-text content,
  media ids and unresolved messages are not passed.
- `PlanFacts`: four enumerated strategy values the dispatchers branch on (`audio_strategy`,
  `song_sync`, `render_program`, and whether the guided-voiceover execution contract applies).
  Every other strategy field, including all prose (`rationale`, `intro_hook`, `story_structure`,
  titles, shot labels), is unreachable.
- the declared `edit_format` (coerced to the known vocabulary), whether a guided snapshot,
  a recording or a creator song is attached, the clip count, per-clip speech facts
  (`analysis.understanding.speech` from the approval snapshot; unknown when that block is
  absent, so legacy-shaped analyses never block a route), rollout capabilities (flags and the
  settings-aware `phone_render_supported_formats()` as plain values) and the platform.
- an opaque `contract_digest`, only echoed into the stamp and events.

Guards: a test pins the string-typed fields of the input dataclasses to an allowlist
(`test_the_resolver_inputs_are_a_typed_projection_without_prose`); the module may not name
request-text helpers; and `tests/services/test_render_route_invariance.py` plus the table
test rewrite **every** string (messages, `creator_request`, brief descriptions and literals,
strategy prose, exact-text content, unresolved messages, clip transcripts) for all incident
records and all `request_following` threads and assert the resolution is identical.

It returns a `RouteResolution`: a `Route`, a typed refusal (`reason` is a PR-A
`DeclineReason`, plus `field_path` and an `alternative`), or `needs_choice` (a typed
`choice_kind` plus `field_path`, a plain value PR-C's collector can wrap). Refusals come
from the adapter declarations: after a route is chosen, every pinned requirement its adapter
declares `capability_unavailable` or `requirement_conflict` refuses; `evidence_missing`
declines stay with the verifiers. `RouteResolution.drivers` lists the field paths the
decision read.

### Routes, plan conditions and the legacy gate each will retire

| Route | Platform | Plan conditions (in order) | Legacy branch it maps to | Legacy gate/heuristic PR-F retires |
| --- | --- | --- | --- | --- |
| `guided_story` | phone | an approved guided snapshot is attached | `_run_phone_guided_job` (first branch of the phone fork) | - |
| `speech_montage` | phone | montage family, no voice requirement, `contract.audio_source_ids` | `run_phone_speech_montage_job` (`required_speech`) | `speech_montage_possible` / `mentions_speech` raw-text gate (`phone_speech_montage_job.py`, `speech_montage_planning.py`, `render_shape.py`); fall-through to unified when the speech job returns False |
| `voice_behind_footage` | phone | montage family, no voice requirement, `contract.audio_source_ids` of exactly ONE clip, `plan.voice_mode == "continuous"` (KRI-479) | `run_phone_voice_behind_footage_job` (the dispatcher's `_voice_behind_footage_route` branch, STAMPED jobs only; unstamped jobs keep the speech lane line for line) | the speech lane's "I can't prove the confirmed picture order" refusal for this shape (the voice clip is not in `order_ids`) and its LLM excerpt planner / wrapping b-roll pool; on cloud the same plan is the existing `capability_unavailable` refusal with the iPhone alternative |
| `voiceover_montage` | phone | montage family + `contract.require_voiceover` | `_run_phone_voiceover_montage_job` | voiceover-file presence over the plan (the phone fork already reads the contract flag; `_run_phone_subtitled_job` still reads the file) |
| `unified_montage` | phone | montage family, none of the above | `_run_phone_unified_montage_job` -> `_run_phone_guided_job` | speech-coverage fallbacks to montage |
| `user_song_montage` / `lipsync_montage` | phone | montage family + `audio_strategy == "user_song"` (+ `song_sync == "lipsync"`) | unified montage entry with `candidates["user_song"]` | silent lip-sync -> background fallback; a stale song attachment steering the route |
| `subtitled` | phone, cloud | edit format `subtitled`, or narrated* with no voice requirement and one clip | `_run_phone_subtitled_job`; cloud `_resolve_archetype` | flag fallback to montage; self-narration clip-count rule |
| `talking_head` | phone, cloud | edit format `talking_head` (cloud), or narrated* with no voice requirement and 2+ clips | `_run_phone_subtitled_job` (multi-clip); cloud `_resolve_archetype` | footage-type promotion (`footage_type_bias`); speech-coverage fallbacks; `spine_too_short` |
| `narrated` | phone, cloud | narrated* + `contract.require_voiceover` | `_run_phone_narrated_job`; cloud `_resolve_archetype` | voiceover-file presence; `prefer_narrated_voiceover = job.mode == "content_plan"` |
| `guided_story` | cloud | a guided snapshot, and the plan's `render_program` is `guided` (or the guided-voiceover execution contract) | `_run_guided_story_job` | guided snapshot silently skipped (`guided_story_skipped_incompatible_intent`, `AudioLedGuidedConflict`) |
| `slides` | cloud | edit format `slides` | `_run_slide_post_job` | - |
| `montage` | cloud | montage, no voice requirement | `_resolve_archetype` -> `montage` | footage-type promotion; speech-coverage fallbacks; flag fallbacks |
| `voiceover` | cloud | montage family + `contract.require_voiceover` | `_resolve_archetype` -> `voiceover` | voiceover-file presence over the plan |
| `day_vlog`, `single_hero` | cloud | declared edit format, no voice requirement | `_resolve_archetype` | `insufficient_media` / flag fallbacks |

Typed refusals and choices the resolver adds on top (each mirrors a check the legacy
dispatcher already makes somewhere, or marks a silent downgrade): camera-audio sources
combined with a recorded voice or a creator song (`requirement_conflict`, mirrors
`check_phone_dispatch_contract`); `subtitled` / `talking_head` combined with a voice
requirement; a guided snapshot on an audio-led plan; a rollout flag that forbids the plan's
format; self-narration over clips that were all analysed and none speaks; a creator song on
cloud; `needs_choice` for an unresolved contract, a required voice with no recording
(`voiceover_recording`) and a creator-song plan with no song (`song_upload`).
`_creator_requests_narrated_treatment` is **not** a route input: it only decides narrated
storyboard text treatments from request prose, and PR-F replaces it with the approved text
requirements.

### Stamp and shadow mode

For jobs carrying `creator_plan_authority_version` (and only those) the resolved route is
recorded as a plain-dict **sibling key** on the job, never inside `CreatorRenderContract`:

```json
"creator_route": {"route": "speech_montage", "platform": "phone", "contract_digest": "<digest>"}
```

Why a sibling: the contract model is `extra="forbid"`, so any field added to it makes every
job stamped by new code unreadable by a still-running or rolled-back older worker (phone
dispatch, cloud preflight, the phone editor and device pinning all fail closed). A
golden schema (`tests/fixtures/creator_render_contract.schema.json`, checked by
`test_the_contract_model_schema_is_unchanged_so_old_workers_can_still_read_it`) keeps the
model byte-identical; only new code reads or writes `creator_route`, the same arrangement as
`cloud_evidence`.

The stamp is written ONCE, at dispatch (`tasks/content_plan_build.py`, right before the Job
is added), after every input is attached: the brief-bound contract, the creator song and the
guided snapshot. `build_generative_job` writes no route (its inputs are incomplete). A plan
that resolves to a refusal or an open choice gets no stamp, and re-stamping REMOVES a previous
stamp that no longer resolves. `read_route_stamp(assembly, contract_digest)` treats a stamp
whose `contract_digest` differs from the current contract (e.g. an editor `rebind`) as absent;
editor revisions never copy it. Every writer that rewrites `assembly_plan` for these jobs
spreads the existing keys (audited: the finalize/status/decline/speech-cleanup/editor-control
paths in `generative_build.py`, `creator_agent.py`, `generative_jobs.py`, `plan_items.py`,
`lyrics_preview_task.py`), so the key survives normal paths.

The dispatchers run the resolver in **shadow mode**: at each decision point
(`_shadow_route` in `tasks/generative_build.py`: the five phone branches, the cloud guided
entry, cloud slides and the cloud `_resolve_archetype` result) the legacy branch label is
written next to the branch, the resolver is run on the same persisted inputs and, when
they disagree (or the resolver refuses / needs a choice where legacy renders), one
`route_mismatch` event (stage `assembly`) is recorded through `record_pipeline_event`. The
legacy decision still renders. The check never raises (any fault is logged as
`route_shadow_check_failed`), reads nothing for unstamped jobs, and runs only where no
`FOR UPDATE` lock is held on the job row.

`route_mismatch` event data: `point` (`phone_dispatch`, `cloud_guided`, `cloud_slides`,
`cloud_archetype`), `platform`, `legacy_route`, `resolver_outcome` (`route` / `refusal` /
`needs_choice`), `resolver_route`, `decline_reason`, `field_path`, `choice_kind`,
`stamped_route` (the dispatch-time stamp; `null` when absent or stale), `contract_digest`
and `drivers` (field paths the resolver read). It carries no request text, URLs or ids.
Read it at `/admin/jobs/{id}` (pipeline trace, stage `assembly`) or with
`python scripts/admin.py --prod GET jobs/<id>/debug`. A redelivered task does not append an
identical event again (one JSONB containment probe on the job's trace, only when a mismatch is
about to be written; a failed probe records anyway).

### How PR-F uses it

PR-F flips one override at a time. The gate is `creator_render_contract.stamped_plan_contract`
(the dispatch-time `creator_plan_authority_version` stamp AND a pinned contract); an unstamped
job takes the legacy line byte for byte, and each flip has a stamped test and an unstamped
twin through the real dispatcher (`tests/tasks/test_route_override_flips.py`). A refusal is a
typed decline (`reason`, `field_path`, `alternative`, the same wording the resolver uses:
`render_route.ALT_*`), raised as `CloudRenderContractError` so the cloud handler persists it
(`creator_render_contract_unsupported` + `creator_decline`) and the phone fork maps it through
`_creator_decline_payload` (`phone_plan_unsupported`). What the creator then sees is decided by
the recovery observer in `tasks/kria_runtime.py`:

| Where the decline was persisted | `needs_choice`, `requirement_conflict`, `capability_unavailable` | `evidence_missing` |
| --- | --- | --- |
| Job level, cloud (`creator_render_contract_unsupported`: adapter preflight and every PR-F route decline, raised before anything renders) | `ask_user`, not retryable, the decline's own message plus its alternative | the same: a retry re-runs ingest and cannot add evidence |
| Variant level, cloud (`creator_render_contract_unverified`: publication check of a rendered artifact) | the same refusal copy | repair retry (PR-A): a re-run can produce the evidence |
| Phone (`phone_plan_unsupported`) | PR-A behaviour, alternative appended | deterministic, `ask_user` |

Only a TYPED decline changes the copy (an untyped failure keeps the generic retry text), so no
raw exception text reaches the creator. `tests/kria/test_cloud_typed_decline_recovery.py` runs the
real task entry, the real `_fail_job` and the real observer and asserts what the creator sees.

**Retired for stamped jobs (PR-F)**

| Override | What a stamped job does now | What the creator sees |
| --- | --- | --- |
| Phone subtitled worker recomputed the voice from the attached FILE (`_run_phone_subtitled_job`) | Reads `contract.require_voiceover`, like the dispatcher. A narrated format with a stray recording and no voice requirement renders self-narration | No more "No phone renderer is registered" failure |
| Cloud voice precedence: a file chose voiceover/narrated and skipped the guided snapshot (`_run_generative_job_impl`, `cloud_adapter_for_job`) | `plan_voiceover_path`: the file only counts when `require_voiceover`. Worker and preflight adapter selection read the same value. Required voice + no recording is `needs_choice` before any ingest or model spend | The approved montage/guided edit renders; or "record or upload your voice, or tell me to use music" |
| Rollout flags off (subtitled, talking_head, narrated, self-narration) fell back to a montage / voiceover (cloud) or raised an untyped ValueError (phone subtitled worker and the phone "no renderer for this format" dispatch) | `capability_unavailable`, the resolver's refusal verbatim, on both platforms | "Ask for a different format" etc. instead of a different kind of edit or a bare failure. Still untyped on phone: the multi-clip subtitled shape guard |
| `footage_type_bias` promoted a montage to `talking_head` | No promotion | The approved montage |
| `no_speech` / `spine_too_short` fallbacks and the mid-render `SpineExtractionError` degrade to montage | `evidence_missing`, a refusal with the alternative (no retry can add speech); marked speech-cleanup jobs keep their own `snapshot_mismatch` recovery first. No `route_mismatch` is recorded for the declined row | "I couldn't find clear speech..." with the alternative |
| Guided snapshot silently skipped to classic on an audio-led format; `validate_execution_binding` ValueError | `requirement_conflict` (same alternative as the resolver) / `evidence_missing` | A question or repair, not a different path |
| `speech_montage_possible` / `mentions_speech` raw-text gates | The phone dispatcher already routes a contracted job by `contract.audio_source_ids` (pinned by a twin test); the creation offer (`render_shape`) follows the typed `montage_audio` camera sources when `KRIA_PLAN_AUTHORITY_ENABLED` is on | The shape offer matches what the worker will do |

`_first_user_message` stays: it is a content-only language hint for landmark names in the
unified montage (and the pre-contract speech lane). `tests/test_raw_chat_text_readers.py` scans
`app/` for every reader of raw chat text (`creator_request`, first/latest user message, brief
`.text()` prose) and fails for one outside its categorised allow-list (agent prompt, plan time,
carry, content only, legacy unstamped). Carry and legacy modules are pinned per function, and each
legacy gate names a live (collected, not skipped or xfailed) test that proves plan-authority jobs
bypass it. Known limit: a reader that builds the name dynamically (getattr, string concatenation,
an alias) is not seen; the prose-taking helpers `speech_montage_possible`, `mentions_speech` and
`_creator_requests_narrated_treatment` are scanned by name.

**Still shadow / still raw-text (not decided by the owner; the resolver and the legacy dispatcher still differ and
`route_mismatch` keeps recording them)**

- `_creator_requests_narrated_treatment` (regexes over `creator_request` add intro / `PLAYER n` /
  score text to narrated storyboards) is **not** retired: it changes output, and
  `CreativeStrategy` has no typed carrier for players or scores, so dropping it for stamped jobs
  would silently remove treatments the creator asked for. Stamped and unstamped jobs behave the
  same until a typed treatment carrier exists (follow-up). The allow-list documents it as
  output-affecting, content-carrying.
- `prefer_narrated_voiceover = (job.mode == "content_plan")`: a content-plan montage with a
  required voice renders `narrated`; the resolver says `voiceover`. Product decision.
- `audio_strategy == "user_song"` with no attached song: resolver `needs_choice`, legacy renders a
  plain montage.
- Slides are checked after guided by the legacy dispatcher, before it by the resolver.
- Post-ingest facts the resolver cannot model: the `_MIN_SPINE_COVERAGE` threshold itself (the
  resolver only refuses when every clip was analysed with no speech; the worker measures coverage
  after ingest and declines `evidence_missing` below the threshold, which is a retired fallback, not
  a mismatch), `insufficient_media`, the lip-sync to background fallback, and the asset-only
  `AudioLedGuidedConflict`.
- A recorded voice on a `subtitled` edit (legacy renders subtitled; the verifier later declines
  `require_voiceover` as `evidence_missing`), a stale creator-song attachment, day-vlog and
  single-hero flags (they already fail visibly with their own typed policy errors, they never became
  a montage).
- The `kri469-voice-clip-ignored` and `kri481-song-audio-plays-twice` output xfails stay: they
  assert compiled-output facts the voice-behind-footage composer (PR-H) and KRI-481's phone export
  proof own, not a route.

**Reading `route_mismatch` after PR-F.** A stamped job that records a mismatch for one of the
"retired" rows above means a flip was bypassed (a regression, investigate). A mismatch for a
"still shadow" row is expected; use the event's `legacy_route` / `resolver_route` pair to decide
the next flip. The runbook note lives in `docs/runbooks/admin-job-debug.md`.

## Voice behind footage (KRI-479, PR-H: the first composable-tracks slice)

Design: `docs/designs/one-plan-voice-behind-footage.md` (sections 2, 3, 6, 7 are what this
slice builds). A stamped phone job whose approved plan is "one talk-to-camera clip is the
continuous voice, the other clips are the chronological picture, optional exact opening text,
an explicit or implied length" is **composed**, not planned: no LLM picks excerpts, no request
text is read, nothing wraps.

**Plan shape.** `CreativeStrategy.voice_mode` (`continuous` | `excerpts` | absent) is a
model-authored strategy field like `song_sync` (`SkipJsonSchema`: out of every derived JSON
schema, omitted when unset, so stored strategies and hashes are byte-identical when unused).
The Main Creator prompt teaches it only when the manifest advertises `phone_source_audio` AND
`Settings.voice_behind_footage_enabled` holds (`KRIA_PLAN_AUTHORITY_ENABLED` and
`SPEECH_EXCERPT_MONTAGE_ENABLED` both on: the route renders only for stamped jobs and needs the
camera-audio renderer), and says plainly when NOT to set `continuous` (best-quote requests,
talking-head / subtitled edits, several clips' sounds, no named clip, ambiguity; null is the safe
default). Prompt `2026-10-07-v47`; **the live re-record is pending an owner-approved spend**,
replay evals ignore the prompt text. `repair_creator_voice_mode` drops a stray value (no camera-audio
montage, voiceover/user-song, non-montage, or the route unavailable) silently and clears a
model-added day-vlog / single-hero shape next to a continuous voice (KRI-469's recorded strategy
had one); that clearing reaches the draft as a plan notice. The strategy key is read by new code
only: deploy API and workers together (an old pod ignores `voice_mode`, the same precedent as
`song_sync`). Absent or
`excerpts` = the speech-excerpt lane, unchanged. `FIELD_MATRIX`: `voice_mode` is
`upstream_resolved` (owner `render_contract:composition`): it changes nothing in the pinned
projection by itself; the route resolver reads it and dispatch derives the commitments below.

**Composition commitments (sibling key, never a contract field).**
`assembly_plan["creator_composition"] = {contract_digest, route, voice_picture, voice_span_s,
min_shot_s}`, written at dispatch right after the `creator_route` stamp
(`creator_render_contract.stamp_composition`), plan-authority jobs only, keyed by the contract
digest (`read_composition` reads a stale or malformed key as absent), removed again when the
strategy no longer implies it. `voice_picture="hidden"` is passed to `build_render_contract`
as an ARGUMENT (`composition=`): the voice clip is then left out of `order_ids` and the
selected set while `audio_source_ids` keeps it; with `composition=None` (every unstamped job)
the contract is byte-identical. `voice_span_s` is set only when the creator chose a silent
tail (the voice clip's length less the safety margin); `None` = the voice covers the whole
picture. `min_shot_s` is reserved for a creator-authored cadence (policy floor otherwise).
`CreatorRenderContract` gained no field (golden schema test unchanged).

**Composer.** `pipeline/phone_speech_montage_plan.py:compile_phone_voice_behind_footage_plan`
takes typed facts only (voice binding + `VoiceWindow`, ordered picture bindings, duration,
opening words + hold, floor, whether a silent tail is allowed); its signature has no request
text. One contiguous `voice` audio clip from timeline 0 (`volume` 1, fades), each picture clip
once in the given order on a `voice-footage` video track at `volume=0` (frame aligned, never
below the readable floor `MIN_READABLE_SHOT_S` 0.8 s unless the whole clip is shorter), the
opening title through the narrated title helpers, no music bed. `select_voice_window` picks the
voice span from word timings: from just before the first word, all the speech when it fits,
else the last sentence end inside the length when within `VOICE_TAIL_SLACK_S` (3 s) of it, else
a natural pause (a gap of 0.8 s or more) within it, else the last word (longer fade-out); a
cut is called "ending on a full sentence" only when the last word really ends one. The slack
is `voice_tail_slack_s(picture) = min(3 s, 15 % of the picture)`, one function shared by the
composer and the verifier. Lengths are computed in whole frames per clip exactly as the
composer allocates (`picture_frame_bounds`, `implicit_picture_duration`): a stated length
within 2 frames of the footage there is is clamped to it (inside the verifier's tolerance), any
other shortfall is a typed decline whose two numbers never read the same. Typed declines instead
of guessing: too many clips for the
length (`requirement_conflict`, alternative names the length that would fit), footage shorter
than the length (never looped), a voice shorter than the length without a chosen silent tail,
a clip without speech/audio, the voice clip among the picture clips.

**Worker entry.** `services/phone_speech_montage_job.py:run_phone_voice_behind_footage_job`
reads only the pinned contract, the commitments and typed strategy values. A length the creator
STATED (pinned in the contract, including a `voice_vs_duration` answer) is never touched: it
renders or declines, typed. A length nobody stated (the plan's own pick) is the voice's length
capped at the pick, EXTENDED up to what every clip needs to be seen (n x the 0.8 s floor, capped
by the speech and the footage; KRI-129: a model-picked number is never a reason to refuse) and
kept to the footage there is, with an adjustment either way,
transcribes the voice clip, composes, runs `validate_phone_pilot_recipe` and
`verify_phone_recipe(..., composition=)`, and pins the device request as the (non-editable)
`speech_montage` variant with `assembly_plan["speech_montage"]["route"] ==
"voice_behind_footage"`. `SPEECH_EXCERPT_MONTAGE_ENABLED=false` still stops it (no new flag;
rollback of the whole train is `KRIA_PLAN_AUTHORITY_ENABLED` for new jobs).

**Verifier (only when commitments are passed).** `voice_covers_timeline` /
`voice_window_contiguous` (one audible window from time zero up to the picture end or the
committed span, give or take `voice_tail_slack_s`; typed `audio_source_ids` decline),
`picture_shot_floor`, no text layer past the picture end, no soundtrack other than the approved voice (`requirement_conflict` on
`audio_strategy`), the hidden voice clip's picture on no video track, nothing past the picture
(`recipe.duration` is the max end over ALL tracks, so a long voice would silently stretch the
video), and the duration tolerance tightens from 10 % to `max(0.1 s, 1 frame)`.

**Conflict kinds added (this route only).** `which_voice` (several named or candidate speech
clips: one option per clip with its length and first words; the answer rewrites
`montage_audio.source_media_ids` to one id); `voice_vs_duration`: a voice (its SPEECH length when the clip's timed segments are known, else the
clip) shorter than an EXPLICIT length by more than 1 s asks `match_voice` ("End the edit when
your voice ends", rewrites the length) or `silent_tail` ("Keep the length, the last seconds play
without voice", commits `voice_span_s`, carries `target_duration_requested`), each only if the
clips can actually be cut to it; a voice over 60 s with NO stated length asks "how long", with
lengths computed from the real constraint: the smallest length every clip can be seen in (rounded
UP to a whole second, recommended, first), then up to two larger sensible values (30 / 45 / 60 s)
or all the footage, never more than the speech or the total footage allows; a longer voice with
a stated length is trimmed and disclosed after the render, never asked. The picture set (ids
and lengths) is part of the question's `input_digest`, so an answer does not survive a media
change. None of these is asked, and `duration_vs_count` keeps its legacy `montage_audio`
exemption, unless `ChoiceCapability.voice_route` (= `Settings.voice_behind_footage_enabled`)
holds. An unanswered `which_voice` after the maximum asks gets the same plain recovery message as
`order_basis` / `title_text`, never the excerpts lane. `duration_vs_count` now applies to this shape
(the voice clip is not counted; `fewer` keeps it selected). Priority:
`order_basis`, `which_voice`, `text_placement`, `title_text`, `duration_vs_count`,
`voice_vs_duration`. A `voice_mode` question is not implemented (the Creator sets it).
A `voice_vs_duration` answer counts as evidence of the length for the contract exactly like a
`duration_vs_count` answer.

**Disclosure.** The composer's `adjustments` (voice trimmed to the first N seconds and how it
ends, length extended or kept to the footage, a silent stretch over 1 s, clips that share a
capture time) are stored in `assembly_plan["speech_montage"]["adjustments"]` and reach the
creator in the finished-cut `assistant_review` reply (`kria_runtime._voice_behind_footage_note`,
the channel that already carries the creator-song note). They are NOT shown on the draft before
render: the trim and extension are decided at render time from the transcript. A resolver fault
on a stamped continuous-voice job is a retryable typed decline (`evidence_missing`), never the
excerpts lane.

**Proof.** Plan to recipe: `tests/pipeline/test_phone_voice_behind_footage_plan.py`,
`tests/services/test_creator_composition*.py`, `tests/services/test_phone_voice_behind_footage_job.py`,
`tests/tasks/test_voice_behind_footage_dispatch.py` (real dispatcher; stamped renders via the
composer, unstamped twin keeps the speech lane), the incident records
`voice-behind-footage-*` / `clarify-*voice*`. Export: `scripts/ios/phone-audio-parity.py
prepare OUT --cases voice_behind_footage` + `swift test --filter AudioParityFixtureTests` +
`compare OUT` (voice tones once for the whole span, picture clips silent, picture order, opening
text vs a no-text twin, length, loudness; negative controls for unmuted picture, a voice that
stops early and a wrapped picture), see `docs/runbooks/ios-development.md`.

**Known limits.** The editor cannot edit these variants on the server (same as
`speech_montage`: "edit its timeline in the app"); recompiling from the pure composer on a
text/length edit is a follow-up. Cloud declines the plan (`capability_unavailable`, iPhone
alternative). A request to show the speaker first ("speaker for the first N seconds, then
cut away") is NOT in this slice: the voice clip's own picture is hidden, so such a plan has no
route here. Only the opening text is composed; closing/per-clip/any text requirements fail
the verifier's `exact_texts` check (typed) rather than render without them. Capture-time ties
keep the snapshot order and are disclosed in the review reply, not asked. The `kri469-voice-clip-ignored` output
record stays xfail (owner now `KRI-512 / PR-H`, the re-base ticket): its recorded 30 s is the strategy's own pick
over 41 other clips, which cannot each be seen in 30 s; the new flow asks "how long" and offers
33 s (the least in which every clip is seen), 45 s and 60 s. What is missing to flip it is a
creator-confirmed length for that plan (no ticket is filed for it here).

## Routing and failure behavior

Routing consumes the typed contract rather than treating raw prompt wording as
a new source of truth. In the speech montage path, the selected camera-audio
IDs constrain transcription and planning. A planner refusal or unavailable
capability cannot silently fall through to an ordinary montage when the
contract requires speech. Unsupported combinations decline with a
creator-facing reason before pinning; missing evidence is a contract failure,
not an invitation to accept a one-off raw-text override.

The clips a job is dispatched with must be the clips the contract pins
(KRI-515). The contract filters the bound media snapshot by an explicit
`media_scope="selected"` pick, so `content_plan_build._creator_selected_clip_paths`
narrows `clip_paths` to that pick whenever the dispatch has no approved guided
proposal (every runtime-v2 approval): attachment order is kept, a named
`montage_audio.source_media_ids` clip always stays, and a pick that names no
attached clip keeps every clip. Proposal-backed guided jobs still hand the
worker every clip (the proposal owns media choice). Before this, a v2 montage
rendered a clip the Creator had dropped (a duplicate, "skip the X clip") and,
under an order rule, the phone verifier refused it: "This edit couldn't keep
the confirmed clip order" (prod thread 0b1f9556, stress kit M3). Guard:
`tests/tasks/test_selected_clip_dispatch.py`.

Cloud preflight declines unresolved order facts and every requirement the
dispatched adapter's declaration lists as declined (see "Cloud evidence"). For
consumed requirements, publication checks measured duration and the
renderer-produced receipt evidence.
Contracts with no objective requirements do not require receipts. A replacement
cannot reuse its predecessor's receipt or duration as proof of the new file.

The same fail-closed rule applies to fast in-place media passes. Overlay and
sound-effect mutations decline before overwriting a variant when its contract
contains objective requirements that the cloud path cannot re-verify. A staged
output must pass its own generation and receipt checks before publication; stale
workers return without touching newer work or the last-good artifact.

The final guard is therefore two-dimensional: native export checks still prove
the file and recipe are internally valid, while the creator contract proves
that the recipe was the approved one. Either side can refuse publication.

## Identity and editing rules

The contract binds the approved strategy and brief digests to a render
generation. Device records also retain the recipe digest and source identity;
publication rejects a changed recipe, stale generation, mismatched brief, or
unverified source. Deliberate editor changes mint a variant-specific revision
and are checked against that revision's authority. A stale root contract must
not be applied to an edited variant.

Legacy unmarked jobs remain readable and follow their previous path. New jobs
must carry the version marker and a valid contract; partial migration is
explicitly fail-closed so a missing contract cannot masquerade as an
unconstrained edit.

## Verification scope

The focused corpus covers the four core contract classes, wrong and missing
audio sources, partial mute coverage, misplaced text, reversed order, missing
chronology evidence, digest changes, retries, sibling editor revisions, and
legacy compatibility. Baseline tests were green while the original worker and
compiler still demonstrated silent violations; those overlapping baseline and
focused totals must not be added together. The final test counts and commands
belong to the release report.

The evidence uses synthetic fixtures with the real speech worker and native
recipe compiler. It does not claim production replay, iPhone export, native
pixel inspection, or listening to a rendered artifact. Cloud receipt limits are
intentional: exact text, camera-audio source identity/preservation, and order
are declined per adapter until that adapter's renderer emits proof that can
establish them (guided: exact text and camera audio, with order gated before render;
classic: recorded voice only; named audio sources: no cloud adapter).

Required speech cleanup also has a private winner stage. The accepted speech
snapshot and its upload generation are verified before the staged result is
swapped into the public variant; a missing, stale, or losing attempt cannot
publish an ordinary montage or overwrite the last-good result.

Text proof is based on rendered layers, not a flat string list. Multiline text
joins runs by baseline, karaoke lines join word runs with spaces, and every
required non-whitespace run needs non-transparent paint evidence. An invisible required word
is rejected unless an active karaoke highlight supplies the visible proof for
that word.

The existing `EditRecipeV2` already represents separate voice and picture
tracks, so a whole-track composer rewrite is deferred. Future fields must be
added with an explicit contract disposition, adapter evidence, and a failure
fixture before they become hard requirements.

Edit duration excludes the fixed, declared brand outro, matching
`finished_durations`: the device export probe verifies the recipe plus that
server-known tail. A picture order that includes a source used only as a speech
bed is unsupported; it is refused rather than silently dropping that source.

## Honest verdicts and phone output proof (KRI-470 PR-G)

What a "success" now has to survive on the phone side.

**What the phone verifier proves about audio.** `verify_phone_recipe` counts the separate
music bed (`audio.music_asset_id` at `music_volume > 0`) as an audible source, not just
track clips. The device plays that bed from source 0 on top of every audio-track clip, so:

- an asset audible through a track clip AND the bed is refused (`requirement_conflict`,
  `audio_strategy`; `doubled_soundtrack_assets` is the single check). This is the KRI-481
  double play, which a clip-only check could not see;
- a bed that names the camera's own file counts as camera audio (so `original_audio:
  forbid` / `audio_source_ids` apply to it);
- a bed with `music_volume == 0` is the silenced legacy reference and is not audible.

The contract has no music-policy field, so an unrequested music bed of a catalog track is
not refused by the contract (adding one is a route-resolver / composition decision). Two
other writers could have produced the same double play and were fixed: the speech-montage
compiler (music clips AND bed) and the authored editor timeline (a Save re-enabled the bed
for voiceover+music variants; the music level now lives on the clip: for a voiceover variant,
`mix` is the voice slider and the level is the compiler's `voiceover_music_gain(mix)`, for other
variants `mix` is the editor's music level).

**Order verdicts.** Where an authority can verify the order (a brief-binding cohort, i.e.
`build_receipts(include_unchecked=True)`, or a contract-stamped speech job:
`strict_order`), a brief `order` requirement is REQUIRED, exactly as `order_required`
pins it; legacy / unbound jobs keep the original softer verdicts byte for byte; only a requirement explicitly marked a preference (`facts.strength` of
`preference` / `optional`, or `facts.required: false`) may stay unchecked.
`brief_checks._check_order` therefore returns (strict jobs):

| Situation | Verdict |
| --- | --- |
| plan in the asked order | `met` |
| some clips had no capture time (honest fallback), some groups of a sequence landed | `partial` |
| plan in another order (attachment, song time, ...), or the creator's rule was not applied, or none of its groups landed | `not_possible` ("Couldn't") |
| a capture-order brief whose stated start/end clip is not where the creator put it (KRI-503, see below) | `not_possible` ("Couldn't"); unbound jobs unchanged |
| a rendered plan (unified / spoken-excerpt montage record) that recorded no order | `not_possible` |
| a draft (nothing rendered yet) with no recorded order; an optional preference | unchecked (judged when it renders / never) |
| a rule the checker has no key for, where another authority owns and verifies the order (a lip-sync montage's song placement, #1451; the contract's confirmed / editor / answered order ids) | unchecked (it is not judged here, and does not block) |

A `not_possible` receipt blocks a bound unified montage exactly as any other unmet
receipt does (`ask_before_simplifying`), so "Partly" order lines become "Couldn't" and two
shapes that used to render silently now ask first: an order rule the checker has no key
for (alongside one it can follow), and a render record with no order. The key set is the
shared `clip_facts.CAPTURE_ORDER_KEYS`. The spoken-excerpt montage job always records the
order it used (attachment included), so an unstamped brief that followed attachment order is
`met` rather than a false "couldn't confirm".

**Sequence rules on top of a capture order (KRI-503).** "Chronological order, starting with the
blue video" is two statements: a basis (filming time, the brief's `order` requirement) and a
seating of the clips the creator described (a resolved `order` clip intent with
`position` `first` / `last`, or none for "then ..."). The montage planner has always seated
those clips on top of the basis; the contract used to pin the PURE basis, so the plan that did
what the creator said was refused after rendering while the receipt read "met". Now:

* `services/clip_order_sequence.py` is the ONE seating rule (`sequence_rows`,
  `apply_sequence`): the planner (`unified_montage._apply_sequence`) and `build_render_contract`
  both call it, so they cannot drift. Leading groups come first, closing groups last, every
  other clip keeps its basis position, clips inside a group keep basis order, a clip named by
  two groups belongs to the first first / last group naming it, else the first group
  (`seat_owners`; KRI-1109: "the whole match" as one positionless group plus "ending on
  IMG_1043" ends on IMG_1043 however the planner listed the two, and is no conflict; one clip
  asked first AND last still is, KRI-764). `order_ids` is therefore `[start clip(s)] + [the rest by
  capture time] + [end clip(s)]`; `order_basis` stays `capture_time` (or `attachment_order`),
  so no consumer changes: `verify_phone_recipe`, the speech-montage b-roll order, cloud
  `check_guided_plan_order` / `order_satisfied` and the editor rebind all compare against
  `order_ids` exactly as before.
* The start clip comes from the server-resolved intents (`strategy.resolved_clip_intents`),
  never from the request text, and only for a job with a brief (the brief-binding cohort).
  The rule is applied only while `CLIP_INTENTS_ENABLED` is on, exactly like the planner, so
  the two agree after a flag flip. Jobs without a sequence intent (and every brief-less job)
  get a byte-identical contract and digest; `CreatorRenderContract` gained no field.
* `unresolved`: an `order` intent that is not `resolved` (it never reaches a draft, the
  resolver's own `needs_creator` clip question asks first) pins nothing and reports the
  intent's own question (else its words: "I couldn't tell which clips "the blue video" means");
  the draft backstop shows it as a plain message. A resolved intent whose clips are not in the
  edit pins nothing; the plan places nothing and the receipt says so.
* Receipt: `_check_order` now also reads which stated groups did NOT land, from the plan
  record's `intent_outcomes` (computed from the FINISHED order; each row carries a `code`
  so nothing re-parses messages): `misplaced` (a described first/last group is not where it was
  asked) and `absent` / `unresolved` (none of its clips are in the edit: "I found no clips
  for ..."). A `then` group wholly inside an earlier group, and a Visuals-only group, are
  NOT failures. A required order on a strict job is `not_possible` ("these aren't where you
  asked: the blue video (first)"). Unbound jobs keep today's verdict.
* No new draft-time question. A start clip the resolver cannot pin is already asked through the
  resolver's clip question (`needs_creator`); a second `order_start` question would have
  duplicated it. Known limits: a start clip that is the speaker of a spoken-excerpt montage
  is not in its b-roll list, so that job still asks "I can't prove the confirmed picture
  order"; its receipt carries no `intent_outcomes`, the verifier is the authority there. The
  native render program orders by `content_plan_build.order_paths_by_resolved_intents` (a
  third copy of the seating rule, not contract-checked). An explicit sequence of clips
  ("clips 1..8 in that sequence", KRI-491) is not a sequence rule here.

**A sequence rule with no basis order (KRI-510).** "End on the sip by the window" with no
filming or upload order asked for: the brief records an `order` requirement whose key is not a
capture key, and the resolver places the described clip (a resolved `order` intent,
`position: last`). The question gate does not ask about a placed rule
(`choice_questions._placed_sequence`), but the contract still listed every non-capture rule as
`unresolved`, so the planning preflight answered only "I can't verify this ordering rule from
the approved media." (prod thread 8e5e9930, 2026-10-07). Now:

* A described rule is PLACED when clip intents are on, every sequence row is `resolved`, and at
  least one row matched a clip in the media snapshot (a Visuals-only match is not placed: the
  planner seats clips only). `build_render_contract` skips a placed rule: not `unresolved`, not
  `order_required`.
* With a basis order (a capture-key requirement or an attachment answer alongside it) the
  KRI-503 seating pins it in `order_ids` as before. Without one there is no full order to pin,
  and `CreatorRenderContract` takes no new field (old workers must read every contract, see
  `test_the_contract_model_schema_is_unchanged_so_old_workers_can_still_read_it`). The proof is
  the brief receipt: `_check_order` reads where each described group landed
  (`intent_outcomes`), and a checked receipt that is not met blocks a bound unified montage
  before anything renders.
* A rule nothing placed (no intent, a `needs_creator` intent, a Visuals-only match, clip intents
  off) builds a byte-identical contract: the same unresolved message, still `order_required`,
  and the gate still asks its order question.
* Known limit: on routes that do not seat a described sequence (cloud guided story,
  spoken-excerpt montage) nothing in the contract checks a placed rule with no basis; the brief
  receipt is the only report. Pins: `tests/services/test_described_order_contract.py`, corpus
  record `kri510-order-end-on-clip-no-basis`.

**Last good artifact.** A contract refusal never replaces the last accepted artifact. At
the editor Save / `pin_device_request` the check runs before any mutation; at the retry
re-pin and at publication the refusal is written beside the intact state
(`record["contract_decline"]`: typed reason, field path, alternative, stage) and a record
still waiting on the phone moves to `needs_attention`. The variant's video, poster, URL
and `ok`, and the record's pinned request, receipts and published attempt are not touched.
Cloud publication is unchanged here.

**Refusal copy.** Recovery reads the recorded decline (`kria_runtime._device_contract_decline`)
and promises only what exists. It states what could not be confirmed, that the edit was NOT
applied, that the last good version is still available (or "Nothing was published." for a first
render), and a next step that works: for `evidence_missing`, "tell me to redo it and I'll make a
new version from what you already approved" (the brief persists across turns); for `needs_choice`
/ `requirement_conflict`, the typed alternative as the question; for `capability_unavailable`, the
way forward. Nothing re-runs a refused phone render and chat retry does not exist for device jobs,
so the execution error is `ask_user`, `retryable: false`. A refused FIRST render (no accepted
artifact) also fails the variant and job (`creator_render_contract_unverified`, typed decline
beside it) so it is visible and the reaper does not rescan it; an edit leaves everything alone.
Phone-side failures with no recorded refusal keep the "tap Retry on your iPhone" copy.

**Deploy note.** `verify_device_record_contract` re-runs `verify_phone_recipe`, so an already
pinned, contract-STAMPED record whose recipe plays one soundtrack twice (a song-lane or
speech-montage-with-music job compiled before these fixes) is refused from now on with a
permanent 409 on GET, asset downloads and uploads (and, on a refused publication, a typed
`contract_decline`). Before merging, check production for in-flight stamped phone jobs of those
two shapes (`_device_render_v1` records in `awaiting_device` / `syncing`) and let them finish or
re-render them first. Unstamped (legacy) records are not affected.

**Editor text roles.** An editor Save rebinds the contract's exact texts keeping every
requirement a text carried (the standard shape is `[opening X (2.0 s), any X]`; matched by element
id, else exact text) with its approved `duration_s`; an opening or closing text that the
edit moves out of its window is refused. Unknown elements stay `any`; a shot-scoped role
survives only while the timeline is untouched (it degrades to `any`, never to nothing).

**Real-export proof.** `scripts/ios/phone-audio-parity.py` (see
`docs/runbooks/ios-development.md`, "Phone export proof") exports a user-song montage
compiled by the real server compiler through the production exporter and measures the MP4:
the song once from its chosen window, no second copy from source time 0, camera audio
silent, cuts in order, the opening text present only in its window, and a negative control
(the bed switched back on) that must be detected. Recipe validation and file metadata are
not proof. A physical iPhone is still the end-of-train human check.

## Offline checks

### Measured phone export gate (KRI-734, first adapter)

The simple-cut phone adapter can additionally verify the candidate MP4's decoded
picture, audio and static text before publication. See the
[acceptance cases and coverage](../reviews/kri-734/implementation.md).
`actual_export_verification.verify_actual_export` compares sampled picture frames
and one-second PCM windows with the immutable full-timeline analysis proxies;
Tesseract reads the actual text pixels in the approved region and window. It
does not call a model or upload originals. The existing format/duration, creator
contract, source-generation and row/attempt fences still apply.

Enrollment requires `PHONE_ACTUAL_EXPORT_VERIFICATION_ENABLED=true` (every account;
there is no per-account list).
Keep it off until the full API/worker fleet contains the verifier and a protocol-12
client has passed its canary. A read-time `output_verification_required` hint makes
that client withhold the local candidate from playback/Save/Share until publication;
an older client gets 426 for an enrolled request. Historical unmarked requests
are not enrolled retroactively in picture/audio/OCR checks; all uploads still
must decode as local ISO media. Turning enrollment off does not waive checks for
already stamped requests, including exact retries. Roll back enrollment, not the
verifier code while stamped work remains outstanding.

Only a `checked` report tied to the exact request/digest, source-receipt digest,
reserved file SHA/size and storage generation can publish. Rejected, partial,
unsupported or missing evidence cannot replace the last accepted video/poster.
The report stays beside the rejected attempt. Completion retries cannot revive it;
an exact render retry needs existing consent and fresh checks. Failed replacement
receipts cannot borrow their previous ready variant to claim success.
Changed source receipts require a reviewed new edit; they cannot enter an exact
retry loop. A measured match with a stale request identity is recorded as rejected.

The first promoted shape is one contiguous original-video track, hard cuts, rate
1, identity transform/cover fit, static positioned text and original or muted
camera audio, up to 60 seconds/16 clips/four text layers. This does **not** promote
speech, music, animated text, effects, cloud or slides. Their existing guards remain;
KRI-722–725 and the remainder of KRI-734 own additional snapshot/adapter proof.
Picture checks are sampled; a checked report is not an assertion that every frame
or subjective creative choice was inspected. See report fields for exact coverage.

The OCR reader keeps exact text and punctuation matching at confidence >=40. It
tries the approved region's actual pixels and colour-isolated pixels at 2× and
native size: forced enlargement can attach background specks to large letters.
A native-size reading corrects that measurement without stripping punctuation or
painting expected copy. Each OCR call has a five-second timeout; the overall
verification deadline and picture/audio thresholds remain unchanged.

Nearby frames share bounded decoding batches without changing the sample clock,
comparison thresholds or OCR views. Exact-pixel controls cover fractional seeks,
variable frame timing, display rotation, nonzero timestamps and later source
windows. See the [production-hardware benchmark](../reviews/kri-734/performance.md)
for reproducible timings and raw results. The current shared-CPU API execution
path remains a rollout blocker under sustained load; worker-class benchmark
results do not change where completion runs or authorize enrollment.

Repeat a check without publication or model calls (private evidence inputs):

```sh
cd src/apps/api
python -m app.cli.verify_device_export --status request-status.json \
  --sources pinned-local-proxies.json --video export.mp4 --brand-tail standard \
  --report report.json
```

The local CLI trusts its operator-supplied inputs; production resolves and fences
the source receipts itself. Real-media regression commands:

```sh
bash scripts/check-api.sh test tests/integration/test_actual_export_verification.py tests/integration/test_actual_export_completion.py tests/routes/test_actual_export_publication.py tests/kria/test_runtime_phone_v2.py -q
```

### Existing contract checks

From the repository root:

```sh
bash scripts/check-api.sh test tests/services/test_creator_render_contract.py tests/services/test_creator_render_binding.py tests/services/test_cloud_render_contract.py tests/services/test_device_render_contract.py tests/services/test_phone_editor_render_contract.py tests/services/test_phone_speech_montage_job.py tests/services/test_speech_montage_planning.py tests/services/test_phone_music_bed_audible.py -q
bash scripts/check-api.sh test tests/kria/test_order_verdicts.py tests/routes/test_device_render_last_good.py tests/kria/test_runtime_phone_v2.py -q
bash scripts/check-api.sh test tests/tasks/test_unified_montage_dispatch.py tests/tasks/test_phone_format_matrix.py tests/tasks/test_generative_build.py tests/tasks/test_guided_story_build.py tests/routes/test_device_render.py tests/routes/test_phone_editor_commit.py -q
bash scripts/preship-check.sh
```

## Per-clip camera sound (KRI-674 live verification)

A named camera-audio source can mean ambient sound kept with its picture; it does
not necessarily mean spoken excerpts. Main Creator v61 can set `voice_mode` to
`per_clip` when the creator asks for each clip's own sound. This uses the ordinary
phone montage planner and keeps `audio_source_ids` intact. The compiled recipe
must prove exactly those sources are audible; missing, muted or extra audio still
fails verification. No speech detector or request-word matching chooses the lane.

The new value is omitted unless chosen, like the existing voice modes. Explicit
`continuous` and `excerpts`, and an absent historical mode, keep their established
interpretation. Workers honor `per_clip` only on plan-authority jobs. This ordinary
camera-sound mode does not need `SPEECH_EXCERPT_MONTAGE_ENABLED`; Main Creator only
learns it when plan authority and the phone source-audio capability are available.
The API and workers must deploy together; the strict pinned contract and native
recipe schemas do not change.

The route resolver and both worker entry points share the audio-mode decision in
`creator_render_contract`. A diagnostics import failure cannot become a dependency
of ordinary rendering. A saved guided picture plan still refuses a spoken-excerpt
requirement; it can retain named sources only with explicit per-clip audio intent.

Regression evidence crosses real parse/policy, contract projection and dispatch,
with renderer entries recorded, plus the existing compiled-recipe audio verifier.
The guards were red before the fix. Fresh Main Creator behavior and actual iPhone
export/retry/publication remain separate verification steps; these offline checks
are not a production-video pass. A failed pre-recipe job still needs a new version
and fresh approval; this change never rewrites an old approved render contract.
