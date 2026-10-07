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
`exact_texts`, `order_required`, `unresolved`) must be consumed or declined
exactly once. Today only the phone speech montage routes from the audio, duration
and order requirements; every other phone path is verified at pin time
(`verify_phone_recipe`). The cloud adapters consume exactly what their renderer
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

**Evidence rule.** A question needs something the CREATOR said, i.e. a live brief
requirement. The Creator model must always emit `target_duration_s` and the server stamps
`target_duration_requested` whenever it did, so neither is evidence of a stated length.

| Kind | Evidence required | Detector | Options (all executable today) | Persisted field | Exempt (never asks) |
| --- | --- | --- | --- | --- | --- |
| `duration_vs_count` | A live brief `timing` requirement with `duration_s` (quoted in the question) | N clips (the selection the draft carries when it has one, else the snapshot; minus clips outside a resolved `include` intent) cannot each get the readable-shot floor (`unified_montage.MIN_READABLE_SHOT_S`, 0.8 s) in that length | `extend` (the length N x floor needs, if within the 120 s cap), `fewer` (the clips that fit, evenly spaced WITHIN the selection, never re-adding an excluded clip); flash-cutting is never offered | `target_duration_s` + requested flag (extend), or `selected_media_ids` + `media_scope=selected` + the length (fewer); the pinned brief timing requirement is updated to match | Strategy-only lengths; non-montage formats; a live `select` requirement with no resolved subset; a selection whose clips fit; `montage_cadence`, `mixed_media_timing`, `montage_audio`, `archetype`, `execution_contract`; `audio_strategy` voiceover / user_song, `song_sync` (those planners never read the strategy length) |
| `order_basis` | A live brief `order` requirement, or `ordering_choice=chronological` | (a) a capture-order requirement (`capture_time`, `chronological`, `route`, `time`, `shot_order`) and some selected clip has no capture time (the contract's own condition); (b) a key-less or unknown-key rule ("clips 1, 2, 3 in that sequence") the contract cannot verify | `attachment_order` ("Use the order you added the clips"), `unordered` ("Continue without a fixed order"). A creator-typed sequence is NOT offered: nothing can receive one yet | `choice_answers[]`; the contract carries `order_basis="attachment_order"` + `order_ids`, or `order_required=false` | Rule (b) when the server already placed the sequence (a resolved `order` intent with assignments) |
| `text_placement` | A live brief dictated shot text | The literal appears on two or more draft shots | One `On shot N` option per matching shot | `choice_answers[]`; the contract sets that requirement's `shot_index` | A text with no matching label (a planner miss) |

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

**Never a default.** The same question is asked at most twice. After that nothing is
chosen for the creator and no requirement is rewritten: the plan goes through and the
receipts state what is unmet; an unverifiable order becomes ONE plain message quoting the
two ways forward. Only an explicit delegation ("you choose", "you decide", "up to you", "surprise me"; a bare "whatever" is not one)
picks the recommended option, recorded as `source="creator_delegated"` and disclosed.

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
* Questions render through the generic v1 `ChoiceQuestionCard` on iOS; web has no question
  card and shows the plain-text list.

## Stored-contract compatibility

`CreatorRenderContract` forbids extra fields and digests its full dump, so adding
a field would change the digest of every stored contract. `_digest` skips any
field registered in `_POST_V1_FIELD_DEFAULTS` while it holds its default, and
`STORED_V1_CONTRACT` in `tests/services/test_creator_render_contract.py` is a real
stored v1 contract that must keep reading. Register a new field there in the same
change that adds it.

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
| Duration | Compiled recipe duration within the accepted tolerance | Renderer measured duration (`actual_duration_s` or measured `duration_s`) | Compare actual output; never compare desired metadata |
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

PR-F flips one override at a time. For each row of the legacy-gate column: review the
`route_mismatch` events from a canary of stamped jobs, decide whether the resolver or the
legacy branch was right, then (for stamped jobs only) make the dispatcher obey the
resolver for that one case and turn the legacy downgrade into a typed refusal or a
repair/retry. Unstamped jobs keep the legacy branch, so each flip needs a stamped and an
unstamped twin test.

Differences between the resolver and the legacy dispatchers already known (the starting
worklist; the event log will add to it):

- `prefer_narrated_voiceover = job.mode == "content_plan"`: legacy renders `narrated` for a
  montage-family plan with a voiceover; the resolver says `voiceover`. Needs a product call.
- Voiceover-file presence vs the plan: cloud legacy lets an attached file choose
  voiceover/narrated and skip the guided snapshot; the resolver follows
  `contract.require_voiceover`. The resolver also asks (`voiceover_recording`) when the plan
  requires a voice and no file is attached, where legacy phone dispatch raises
  `needs_choice` through `check_phone_dispatch_contract` and cloud renders without it.
  `RouteInputs.voiceover_present` is the FILE; legacy phone routing uses the contract flag.
- Footage-type promotion (`footage_type_bias`): legacy turns a montage into `talking_head`.
- Rollout flags off (subtitled, talking_head, narrated, day_vlog, single_hero, self-narration):
  legacy silently falls back to montage; the resolver refuses with `capability_unavailable`.
- Speech-coverage fallback (`no_speech` -> montage): the resolver refuses only when every clip
  was analysed with a real `understanding.speech` block and none speaks.
- A guided snapshot on an audio-led or native plan: legacy skips it (or raises
  `AudioLedGuidedConflict`); the resolver returns `requirement_conflict`.
- A stale creator-song attachment (song attached, plan says library music): legacy renders
  `user_song_montage`; the resolver says `unified_montage`.
- `audio_strategy == "user_song"` with NO attached song: the resolver returns `needs_choice`
  (`song_upload`) where legacy silently renders a plain unified montage. A regression risk for
  PR-F: flipping this override changes what such jobs do.
- Ordering: legacy checks guided before slides; the resolver checks slides first. A slides plan
  never has a guided snapshot, so this differs only for inconsistent jobs.
- A recorded voice on a `subtitled` edit: legacy renders `subtitled`; the resolver refuses.
- Confirmed legacy defect the route label cannot show: `_run_phone_subtitled_job` recomputes
  `has_voiceover` from the file (`generative_build.py`, ~line 6000) while the dispatcher chose
  the branch from `contract.require_voiceover`. A narrated format with a file attached but no
  voice requirement reaches it and raises "No phone renderer is registered".
- Not modelled (post-ingest facts): `_MIN_SPINE_COVERAGE`, `spine_too_short`,
  `insufficient_media`, and the lip-sync to background fallback.

## Routing and failure behavior

Routing consumes the typed contract rather than treating raw prompt wording as
a new source of truth. In the speech montage path, the selected camera-audio
IDs constrain transcription and planning. A planner refusal or unavailable
capability cannot silently fall through to an ordinary montage when the
contract requires speech. Unsupported combinations decline with a
creator-facing reason before pinning; missing evidence is a contract failure,
not an invitation to accept a one-off raw-text override.

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
for voiceover+music variants; the creator's music level now lives on the clip).

**Order verdicts.** A brief `order` requirement is REQUIRED, exactly as `order_required`
pins it; only a requirement explicitly marked a preference (`facts.strength` of
`preference` / `optional`, or `facts.required: false`) may stay unchecked.
`brief_checks._check_order` therefore returns:

| Situation | Verdict |
| --- | --- |
| plan in the asked order | `met` |
| some clips had no capture time (honest fallback), some groups of a sequence landed | `partial` |
| plan in another order (attachment, song time, ...), or the creator's rule was not applied, or none of its groups landed | `not_possible` ("Couldn't") |
| a rendered plan (unified / spoken-excerpt montage record) that recorded no order | `not_possible` |
| a draft (nothing rendered yet) with no recorded order; an optional preference | unchecked (judged when it renders / never) |

A `not_possible` receipt blocks a bound unified montage exactly as any other unmet
receipt does (`ask_before_simplifying`), so "Partly" order lines become "Couldn't" and two
shapes that used to render silently now ask first: an order rule the checker has no key
for (alongside one it can follow), and a render record with no order. The key set is the
shared `clip_facts.CAPTURE_ORDER_KEYS`.

**Last good artifact.** A contract refusal never replaces the last accepted artifact. At
the editor Save / `pin_device_request` the check runs before any mutation; at the retry
re-pin and at publication the refusal is written beside the intact state
(`record["contract_decline"]`: typed reason, field path, alternative, stage) and a record
still waiting on the phone moves to `needs_attention`. The variant's video, poster, URL
and `ok`, and the record's pinned request, receipts and published attempt are not touched.
Cloud publication is unchanged here.

**Refusal to question or repair.** Recovery reads the recorded decline
(`kria_runtime._device_contract_decline`): `evidence_missing` (a clear instruction was
violated) is repaired from the approved request (`refresh_replan`, never "please restate");
`needs_choice` / `requirement_conflict` is the specific question carrying the typed
alternative; `capability_unavailable` is a refusal with the way forward. All say the last
good version is still available. Phone-side failures with no recorded refusal keep the
"tap Retry on your iPhone" copy.

**Editor text roles.** An editor Save rebinds the contract's exact texts keeping each
text's role (matched by element id, else exact text); an opening or closing text that the
edit moves out of its window is refused. Unknown elements stay `any`; a shot-scoped role
survives only while the timeline is untouched, and the saved text's own duration is the
creator's explicit edit, so only the role is enforced.

**Real-export proof.** `scripts/ios/phone-audio-parity.py` (see
`docs/runbooks/ios-development.md`, "Phone export proof") exports a user-song montage
compiled by the real server compiler through the production exporter and measures the MP4:
the song once from its chosen window, no second copy from source time 0, camera audio
silent, cuts in order, the opening text present only in its window, and a negative control
(the bed switched back on) that must be detected. Recipe validation and file metadata are
not proof. A physical iPhone is still the end-of-train human check.

## Offline checks

From the repository root:

```sh
bash scripts/check-api.sh test tests/services/test_creator_render_contract.py tests/services/test_creator_render_binding.py tests/services/test_cloud_render_contract.py tests/services/test_device_render_contract.py tests/services/test_phone_editor_render_contract.py tests/services/test_phone_speech_montage_job.py tests/services/test_speech_montage_planning.py tests/services/test_phone_music_bed_audible.py -q
bash scripts/check-api.sh test tests/kria/test_order_verdicts.py tests/routes/test_device_render_last_good.py tests/kria/test_runtime_phone_v2.py -q
bash scripts/check-api.sh test tests/tasks/test_unified_montage_dispatch.py tests/tasks/test_phone_format_matrix.py tests/tasks/test_generative_build.py tests/tasks/test_guided_story_build.py tests/routes/test_device_render.py tests/routes/test_phone_editor_commit.py -q
bash scripts/preship-check.sh
```
