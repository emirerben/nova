# Shared clip understanding record (KRI-127)

One open-vocabulary record per clip, written by the analyzer and read by every
agent. It exists so a new kind of creator request ("name the dish", "label each
city", "the pub videos") never needs a new field, enum or keyword list.

## Shape

`app/schemas/clip_understanding.py::ClipUnderstanding` — `subject`, `summary`,
`setting`, `activity`, `people{count, speaks_to_camera, note}`,
`speech{has_speech, to_camera, transcript}`, `on_screen_text`, `brands`,
`content_type`, `audio_type`, `notable_moments`. Everything is free text or a
plain flag. It is AI-written evidence and is never on-screen copy by itself.

## One writer, one reader

- **Writer:** `understanding_payload(meta, best_moments=...)` in
  `app/services/clip_understanding.py`. `autoplace._analyze_video` persists it
  under `PlanItemAsset.analysis["understanding"]` (plus a correctly named
  top-level `transcript`; the legacy `on_screen_text` key still holds the spoken
  transcript for videos and is kept for back-compat).
- **Reader:** `clip_record(analysis, kind=...)`. It never raises and projects
  new analyses, legacy video analyses (v<=7) and image analyses into the same
  record. The chat agent (`creator_sessions._chat_evidence`) and the edit
  planner (`edit_proposal_build` → `EditProposalMedia`) read clips only through
  it. Do not read `analysis[...]` keys directly in a new consumer.

Text fields are defanged (control characters, role markers, code fences) because
transcripts are third-party text that ends up in agent prompts.

## Invariants

- **`ClipMeta` field names.** Older modules read optional attributes off
  `ClipMeta` with `getattr(meta, "<name>", default)` for names it never had
  (`summary`, `brands`, `composition_note`, `content_type`, `audio_type`). A new
  field with one of those names silently switches that code path on with no
  flag. Such fields are named `clip_*` on `ClipMeta`. Guard:
  `tests/pipeline/test_clip_meta_dormant_readers.py`.
- **The record does not replace `matcher_clip_metas.detected_subject`.** That
  field still feeds the music matcher. Visual labels use the persisted clip
  record at their grounding boundary. Guard:
  `tests/pipeline/test_guided_story_clip_understanding.py`.
- **Versioning.** `ANALYSIS_VERSION = 8` is video-only; photos stay fresh at 6
  (`_MIN_FRESH_ANALYSIS_VERSION_BY_KIND`). Re-analysis is lazy. `clip_cache`
  invalidates through `CACHE_SCHEMA_VERSION` + the analyzer `prompt_version`.
- **Generative candidate cache.** `generative_build._clip_meta_from_cache`
  derives accepted fields from `ClipMeta`, preserving understanding on fast
  reburns. Legacy rows use dataclass defaults; unknown keys are ignored. Guard:
  `tests/tasks/test_generative_clip_cache.py`.
- **parse() threading.** New `ClipMetadataOutput` fields must be threaded
  through `ClipMetadataAgent.parse()` (`TestParseThreading`).

## Fixture

`tests/fixtures/kri126_thirty_clip_guided_story.json` carries a real
`understanding` block per clip, regenerated from the production clips with
`scripts/dev/regen_kri126_fixture.py` (read-only on prod; transcripts and brand
strings are redacted before anything is written).

## Open-vocabulary clip intents (`CLIP_INTENTS_ENABLED`, default `false`)

Generic `clip_intents` replace `sport_labels`, `context_label`,
`participant_labels`, and `score_labels` on `CreativeStrategy` (KRI-156).
`label_source="clip"` (the default, omitted in serialized legacy-compatible
intents) uses visual evidence. `label_source="transcript"` selects the pinned
narration materializer and requires `op="label"` plus `transcript_kind`:
`participant`, `score`, or `topic`. These kinds select existing deterministic
narration grammars; the requested attribute stays free text.

The flag gates visual resolution/rendering only. Transcript requests retain their
existing guided voiceover path with the flag off. There is no sport keyword
fallback or sport allowlist. New strategies and the frontend mirror contain none
of the retired per-feature fields.

Flow (flag on):

1. **Inventory the complete creator request.** `ClipIntentPlannerAgent`
   (`app/agents/clip_intent_planner.py`) independently extracts all requested
   clip operations, even when `MainCreatorAgent` omits every `clip_intents`
   entry. It reads creator-authored history plus the latest correction; unrelated
   follow-ups preserve prior requirements. Every operation must cite exact creator
   wording. Location, activity, dish, sport, and other attributes use the same
   open-vocabulary contract, with no category allowlist. Labels, grouping, order,
   inclusion, and chapter captions remain separate operations. More than six
   operations or ambiguous instructions require clarification, never a subset.
   The generic inventory owns labels when enabled; no label-request regex remains.
   Descriptive context is not an operation: mentioning sunset and night footage,
   walking and cycling, a route, or a capture sequence does not authorize a
   group/order intent unless the creator explicitly asks the edit to act on it.
   Generated Creative Brief labels and Main Creator candidate intents remain
   non-authoritative hints; the planner verifies them against creator-authored
   wording and returns `intents=[]`, `question=null` for context-only requests.
   `intent_id` is a handle the model invents, never part of the instruction: an
   over-long id is shortened (`fit_intent_id`), a missing one minted, a duplicate
   re-minted (KRI-422; a 41-45 character id used to drop a clear per-shot caption).
   When instructions are still dropped, the "I understood N of your clip
   instructions" reply carries closed-vocabulary `drop_classes` on the turn's
   `plan.diagnostics` (`/admin/creation-threads/<id>/turns`) and a
   `clip_intent_planner.salvaged` log line; the agent itself stores no raw output.
   `app/services/clip_intent_planning.py` forwards the complete inventory for
   grounding. Transcript-sourced requests stay in the complete inventory but
   bypass the visual resolver; they require a pinned guided narration before a
   strategy is accepted. Model-authored `resolved_clip_intents` remains untrusted.
   Both the classic creator route and Kria inventory instructions before proposing
   an executable plan. Kria accepts only server-resolved intents, persists fresh
   vision answers, and returns a question/recovery response for incomplete resolution.
   Dynamic partitioning (for example, separate chapters for every discovered city)
   asks for concrete groups instead of silently treating every clip as one group.
   Talking edits (`subtitled`, `talking_head`: `CLIP_INTENT_FREE_EDIT_FORMATS`)
   skip the inventory. Their renderers draw no clip intents and their captions are
   the creator's speech, so "Add captions" there is never a chapter `caption` op.
   `compile_strategy_to_plan` strips any footage intents the Main Creator proposed
   for them, with a notice. On other formats (montage) the planner prompt
   (2026-10-02.2) returns no intent and no question for a general "add captions" /
   "add subtitles" / "altyazı ekle" that names no clips and gives no caption words;
   a `caption` op is only for one chapter the creator named or described. Unlike
   the Talking skip this is a prompt rule, so the `montage_add_captions*` /
   `montage_altyazi_ekle` live-eval fixtures are its guard.
2. **Resolve, inside the chat turn** (`app/services/clip_intent_resolution.py`,
   DB-free, the session row lock is released around it): `ClipRequestResolverAgent`
   (text-only, media aliases, id set-membership) matches intents to the shared
   clip records. The resolver accepts the shared creator-request bound of 12,000
   characters, and replaces exact owned media IDs in that request with their
   per-call aliases before the opaque IDs reach the model; unknown or embedded IDs
   remain untouched. A valid alias explicitly selected for a chapter establishes
   membership even when its generic clip record does not match, while factual
   labels and authored captions still require evidence from the selected clip's
   record. Clips the record cannot answer go to `ClipQuestionAgent` (the vision
   model re-watches THAT clip): at most `clip_intents_max_vision_requeries`
   (4) per turn, under one `clip_intents_vision_deadline_s` (25 s) deadline, video
   only: a photo the record can't settle is an unknown the creator answers, never
   unavailable media (KRI-291). Completed answers survive another clip hitting
   the deadline; membership and caption authoring share that deadline. Membership checks are re-asked as
   closed yes/no questions; a confident "no" excludes the clip. New answers are
   cached on the asset's `analysis["answers"][normalized_question]` (pool assets only).
3. **Continue in the background (KRI-154).** On the foreground chat path,
   cacheable video questions left over
   after the cap, deadline, or a transient failure go to
   `app.tasks.clip_intent_requery.requery_clip_intent`, one question per task on
   `POOL_ASSET_ANALYSIS_QUEUE`. The chat returns an `assistant_question` event
   with `reason_code: clip_intent_pending` and asks the creator to send another
   message shortly. The next turn reloads the answers and re-resolves normally;
   no polling endpoint, new client event, or worker-authored chat event is needed.
   In-flight questions do not consume the next turn's synchronous vision budget.
   Pool assets carry tokened, 15-minute claims in `analysis["answer_queries"]`,
   deduplicating turns and deliveries. Broker failures release the claim; expired
   claims can be reclaimed on the next turn. Workers recheck ownership, ownership
   epoch, asset identity, and storage generation before querying and writing, and
   download the pinned generation. No DB lock spans vision I/O. Results merge into
   the latest analysis under a row lock. Two task retries bound transient failures;
   exhausted failures retain a typed technical error for the claim's lifetime,
   rather than becoming a visual "unknown". Budget/quota stops and unknown provider
   outcomes never trigger automatic retries. When KRI-151's
   `CREATOR_CLIP_PREPARATION_ENABLED` path owns a planning attempt, its background
   resolver and checkpoint callback finish the batch without enqueueing separate
   KRI-154 work. Both paths use the same generation-aware answer cache.
4. **Ask, never guess.** Settled visual uncertainty (ungrounded label, "unknown",
   or an empty group) becomes ONE `assistant_question` event
   (`reason_code: clip_intent_unresolved`). Provider, budget, media, and failed
   dispatch states retain KRI-151's technical error path. Pending queries and
   partial assignments never authorize a strategy or an on-screen label.
   The creator's answer arrives as a normal next message.
5. **Plan.** On confirm the intents travel `ProposalBrief.clip_intents` (chat
   `asset-{uuid}` ids translated to planner ids) into `EditProposalAgent` as
   alias-only constraints: group (creator's words as the chapter title), order
   first/last, include. `parse()` validates them; order and include are repaired,
   a split group falls to the clarification retry. `shot_labels` wins when both
   are present. Label values are grouping hints only, never beat copy.
6. **Render.** `EditProposalSnapshot.clip_intents` reaches the worker, which
   **re-grounds every label** (`generative_build._grounded_context_labels`) and
   feeds the existing context-label lane (same geometry, compaction, slot windows,
   replay pinning; iOS consumes the same server text elements). The
   timeline-revision rebuild in `guided_story.py` uses the same path. Multiple
   visual labels on one clip retain every intent’s independent grounding.

### Clip identity at render time (KRI-158)

The guided lane carries stable media IDs, so an exact ID match identifies the
intended clip even when multiple clips share a GCS path. The classic lane still
mints positional IDs per render and resolves path-only assignments only when
exactly one clip has that path. If two or more clips share it, the label is
omitted for every occurrence rather than assigned by insertion order; labels
for other, uniquely matched paths still render.

The classic mapping carries neither generation nor occurrence identity, so it
cannot distinguish even different generations of a shared path. Supporting
labels on these repeated sources requires threading stable media identity
through that lane. Guard: `tests/tasks/test_grounded_context_labels.py`.

### Visual label grounding

For clip-sourced intents, `ground_label()` owns the grounding rule. Transcript
intents are rejected at the visual resolver and worker boundary, even if they
carry forged resolved assignments. A visual label renders only when it is, in order:

- `creator_text` — the creator's own words: whole words, in order, contiguous in
  the confirmed request (not a letters-only substring);
- `vision_verified` — every word is in the vision model's answer for THAT clip,
  confidence >= 0.8;
- `record_span` — every word is in what the vision model WROTE about THAT clip
  (subject, summary, setting, activity, people note, moments; never the spoken
  transcript), resolver confidence >= 0.8.

Plus: <= 24 chars, <= 3 words, safe charset, every word counted. Stored
`value`/`grounding` are never trusted; the worker re-derives them. Guards:
`tests/schemas/test_clip_intents.py`, `tests/tasks/test_grounded_context_labels.py`
(sentinels: made-up value at 0.99, forged provenance, cross-clip evidence,
transcript-only evidence), and the offline acceptance replay on the real KRI-126
clips `tests/services/test_kri126_clip_intents_acceptance.py`.
`TestNoGeminiTextLeaks` is unchanged.

### Transcript labels and legacy strategies

Transcript intents stay on the confirmed `CreativeStrategy`; they are not sent
to the clip resolver or used as vision-derived per-clip answers. Confirmation
requires the existing `guided_voiceover_v1` contract and pinned narration.
`guided_narration_labels.py` derives internal materializer requirements after
both the transcript and final visual timeline are immutable. Score copy comes
from exact word spans (never the annotation model's text); participant labels
still require typed single-subject focus and retain asset-local identity. Topic
labels require a transcript span that is also label-shaped: inside the visual
label fence (at most `LABEL_MAX_WORDS` words / `LABEL_MAX_CHARS` characters),
or a proper name within `TOPIC_NAME_MAX_WORDS` / `TOPIC_NAME_MAX_CHARS`
(capitalized words joined only by short lowercase particles, such as "Parc de
la Ciutadella" or "Brighton and Hove Albion"). A grounded spoken sentence is
voiceover script, already captioned, and is rejected with
`TOPIC_SENTENCE_REJECTION` in the narration receipt. Visual and
transcript intents can coexist on a narrated edit, but neither source can
authorize the other's text.

`CreativeStrategy` decodes legacy JSON through `legacy_clip_intents()` before
strict validation, without writing to stored rows. Participant/score fields map
to transcript intents; sport maps to a visual intent, or a transcript topic for
guided voiceover strategies. Modern visual requests do not suppress legacy
participant/score requirements. Strategy equality normalizes both sides, while
execution receipts retain and verify their original approved payload hashes.

Already materialized context/narration label elements continue to replay. Old
narration receipt requirement shapes are compared semantically before reuse;
changed requirements regenerate instead of reusing stale labels. The retired
raw `context_label_intent` is accepted only for decoding old execution plans,
not as authority to generate fresh labels. An old unresolved visual request
must pass through chat resolution before a fresh label can render.

Guards: `test_label_intent_migration.py`, `test_creator_agent_clip_intents.py`,
`test_creator_execution_contract.py`, `test_guided_narration_labels.py`,
`test_narration_labels.py`, and the grounded-label/guided revision tests.

### Rollout / rollback

Server-only flag (no `NEXT_PUBLIC` twin; questions render through the existing
chat event). Enable: `fly secrets set CLIP_INTENTS_ENABLED=true --app nova-video`.
That rolls every machine, which matters: the runtime-v2 Kria planner reads the
flag on the `light` machine, not only api and worker. Rollback: set it `false`
the same way; visual intents are ignored (chat clears them, the build task and
worker gate on the flag). Transcript intents and already-confirmed label
elements keep their existing behavior. Flip while no task is running
(`celery -A app.worker inspect active` on any machine), since the roll restarts
the render worker. Optionally (paid) run the live evals before enabling:
`tests/evals/test_clip_request_resolver_evals.py`, `test_clip_question_evals.py`,
`test_main_creator_evals.py`, `test_edit_proposal_evals.py` (`--eval-mode=live`,
no judge).

**Production state: ON since 2026-10-01 (~17:07 UTC, Fly v1354).** The live
eval gate ran first with the flag on (78 calls, $1.21 settled, test-run IDs
`clip-intents-enable-20261001-*`): resolver 7/7, clip_question 3/3,
main_creator 27/30, edit_proposal 19/20. Each miss was re-run with the flag off:
- `kri190_route_facts_and_order` and `narrated_vo_cue_asr_errors` fail with the
  flag off too (pre-existing; the voiceover-is-never-chapter-text contract held
  in every run).
- `kri127_open_vocabulary_dish_labels` is wording variance (one run named the
  attribute "the food clips"); it passed on re-run. Watch early clip-label edits.
- `kri238_talking_add_captions` failed only flag-on: the model omitted
  `media_scope` on the one-clip Talking edit. The planner plans that answer in
  one attempt, pinned by
  `test_flag_on_answer_without_media_scope_still_captions_the_clip`, so the eval
  now rejects only the refused `"all"`.

The KRI-127 flag-on observation period requested by KRI-156 started with this
flip; retirement of the legacy label fields must not deploy until it has been
reviewed.

## Clip facts: when and where a clip was filmed (`CLIP_FACTS_ENABLED`, KRI-189)

`CLIP_FACTS_ENABLED` (default `false`) and `CLIP_FACTS_USER_IDS` (JSON list of
account UUIDs enabled while the global flag is off; `settings.clip_facts_for`)
gate SERVER CONSUMPTION only: the landmark agent, facts in the Main Creator and
edit-planner prompts, capture-time ordering and the `by_capture_time` /
`by_route` order intents. The attach API always accepts the new optional fields.
Flag off, everything below is byte-identical (pinned by tests, see the last
list). Rollback: `fly secrets set CLIP_FACTS_ENABLED=false CLIP_FACTS_USER_IDS=[]
--app nova-video` + restart api and worker. Production state: ON globally
(checked 2026-10-01).

**Flow.**

1. The phone reads `PHAsset.creationDate` / `location` (Photos read access is
   requested by the library-backed picker; without it the exported file's own
   QuickTime creation date and `com.apple.quicktime.location.ISO6709` tag fill in,
   `ClipCaptureReader.read(assetIdentifier:fileURL:)`) and reverse-geocodes
   (`CLGeocoder`, serialized, cached by rounded coordinate). It sends
   `capture_time` (ISO8601 UTC), `coarse_location {lat, lon}` (2 decimals) and
   `place {sub_locality, locality, country}` on `POST /creation-threads/{id}/media`.
   Any failure sends nothing; the settings toggle "Use when and where clips were
   filmed" (default ON) sends nothing when off, and turning it off also clears
   the filming context remembered for clips that have not attached yet.
2. `MediaInput` (`routes/creation_threads.py`) validates leniently (a bad value
   is dropped, never a failed attach) and re-rounds location server-side, so a
   precise fix can never be stored. It persists `capture` on the clip assignment
   only (existing JSON, no migration). `OriginalMediaDescriptor.capture`
   (`kria/media_sources.py`, still `extra=forbid`) exists as a read fallback but is
   NOT written: a capture copy inside the stored receipt would make older code
   (a rollback, or a worker still on the previous image) reject the whole receipt
   and fail phone-job admission. Capture never appears in the thread projection or
   events. **At rest:** the attach API stores capture (rounded location, place,
   time) for every iOS clip that sends it, whether or not `CLIP_FACTS` is on for
   the account; only consumption is gated. The privacy manifest and App Store
   labels declare Coarse Location accordingly.
3. At drafting time (`edit_proposal_build._run_draft_attempt`),
   `services/clip_facts.enrich_clip_facts` copies capture-derived facts onto
   `analysis["clip_facts"]` and asks `nova.video.landmark_guess` for one
   best-guess landmark per analyzed video (frames via the Gemini File API +
   place + coarse coordinates). Only clips with a place or coordinate are sent
   (no where, no guess: covers the toggle off, no GPS, no Photos access); the
   others are recorded as asked. Fail-open per clip, and the whole step is capped
   at `LANDMARK_BUDGET_S` (45s): clips that have not answered are skipped and
   retried next time. A guess, even "unknown", is recorded per storage generation
   (`generation`, else `storage_generation`) so a retry never pays twice.
4. `clip_record(...).facts` exposes them (`ClipFact {kind, value, provenance,
   confidence}`; kinds `capture_time|place|landmark|visible_text|creator`,
   provenance `exif|geocode|vision|creator|inferred`). Facts are stored beside,
   not inside, the `understanding` block so legacy analyses keep their legacy
   projection. `prompt_view(include_facts=True)` is opt-in per caller.

**Provenance rule (D4).** An `inferred` landmark is used directly, no
confidence gate. Every consumer must be able to surface it for correction; the
Main Creator prompt tells the model to say plainly when a name is a guess.

**Ordering.** `story_shapes.repair_day_vlog` sorts by capture time where known;
clips without one keep their attachment-order slot (`order_by_capture_time`).
`ClipIntent.order_by` (`capture_time` | `route`, op=order only, never with
`position`) is extracted by `clip_intent_planner` (teaches it only when the flag
is on), resolved deterministically over every clip in
`clip_intent_planning.resolve_order_by_intent` (no vision call) and applied by
`edit_proposal._reorder_beats_by_capture_time`. `by_route` equals capture-time
order for now, so receipts must say "ordered by when you filmed" for both. The
capture time is the Photos creation date, which for a clip saved from a messaging
app or re-exported is the save time, not the filming time; provenance stays
`exif` and this limitation is accepted. The basis is recorded on
`EditProposal.ordering` (`{ordering_basis, ordering_fallback_clip_ids}`) for
P2/P4 receipts; `ordering_basis` is `capture_time`, `attachment`, or
`not_applied` (with a `reason`: `fast_montage`, or `planner_path` for the
semantic/snapshot planners that never read facts). A receipt must read this
field, never assume a resolved `order_by` intent was honored.

**Not covered yet.** The semantic planner (`EDIT_PROPOSAL_SEMANTIC_ENABLED`),
the snapshot replan/direction-replacement planners and the editor-op tool
(KRI-191) do not read facts; fast_montage ignores `order_by`. Both record
`ordering_basis: not_applied`.

**Guards.** `tests/services/test_clip_facts.py`,
`tests/schemas/test_clip_understanding_facts.py` (parse threading),
`tests/routes/test_attach_clip_capture.py`,
`tests/agents/test_edit_proposal_clip_facts.py`,
`tests/services/test_clip_intent_order_by.py`,
`tests/tasks/test_edit_proposal_build_clip_facts.py`,
`tests/agents/test_landmark_guess.py`, `tests/evals/test_landmark_guess_evals.py`.
Current relevant prompt versions: `main_creator` 2026-10-02-v40,
`edit_proposal` 1.18.0, `clip_intent_planner` 2026-10-02.2, and
`landmark_guess` 2026-09-24.1. Optional
live evals (`--eval-mode=live`, no judge):
`test_landmark_guess_evals.py`, `test_clip_intent_planner_evals.py`,
`test_main_creator_evals.py`, `test_edit_proposal_evals.py`.

## Kria thread clips: one loop per item, and Visuals queue fairness

`attach_media` enqueues `tasks.analyze_kria_clips` (`app/tasks/kria_clip_understanding.py`)
on `POOL_ASSET_ANALYSIS_QUEUE` after each attach that includes video. A phone attaches
clips one at a time. On 2026-10-03, before #1354, both autoplace slots analysed the same
item's clips together and paid Gemini vision + Whisper twice.

- **Single flight per item (#1354).** A run takes the Redis lock
  `kria-clip-und:{item}` (`SET NX`, `LOCK_TTL_S`). Runs that find it held return
  `busy` at once. The running loop re-reads the item every chunk, and re-enqueues
  itself if a clip arrived after its last read. Without Redis the lock fails open.
- **Visuals queue.** Footage runs can hold a slot for up to `RUN_DEADLINE_S`, so photos
  queued FIFO behind them showed "Queued" in the app. Creator-facing
  `analyze_pool_asset` dispatches (upload/register, reanalyze, the stale-asset
  reaper, creator-preparation recovery) use `settings.pool_asset_visuals_queue`:
  `VISUALS_ANALYSIS_QUEUE` when set, otherwise `POOL_ASSET_ANALYSIS_QUEUE`.
  - Every fly.toml process that consumes `autoplace-jobs` (`autoplace`, and the
    unified `worker`) also consumes `visuals-analysis`.
  - Kombu's Redis transport rotates between a worker's queues after each pick, so a
    photo waits for the next free slot, not behind the footage backlog.
  - The stale-analysis refresh backfill stays on `POOL_ASSET_ANALYSIS_QUEUE`, so a
    backfill never delays a new upload.
  - Rollout: deploy first, then
    `fly secrets set VISUALS_ANALYSIS_QUEUE=visuals-analysis --app nova-video`.
    Rollback: `fly secrets unset VISUALS_ANALYSIS_QUEUE --app nova-video`.

Guards: `tests/test_pool_asset_fly_contract.py` and `tests/test_worker_prewarm_gate.py`
(the Visuals queue has exactly the consumers of `autoplace-jobs`, and the switch is not
in `[env]`); the queue assertions in `tests/routes/test_plan_item_assets.py` and
`tests/tasks/test_pool_asset_reconcile.py`.

## Chat turns wait for analysis in flight

A Kria turn whose clip instructions need clip understanding gates on it in
`plan_and_resolve_clip_intents` (`require_clip_understanding`). Before 2026-10-04 the
gate judged the clip snapshot taken at turn start, before the ~20 s Main Creator call,
and replied "I'm still checking some of your clips … Send it again" at once. On prod
thread b2a41da6, 7 phone clips were attached 11–35 s before the message. The reply
went out at 10:56:02Z, and the analysis run finished all 7 at 10:56:03Z.

- **Re-read, then wait.** The gate re-reads the item (`planner._reload_intent_clips`)
  and polls every 2 s until each clip is understood or the wait runs out. Only
  analysis for the same `media_id` + `gcs_path` + generation is taken, so the pinned
  manifest never changes.
- **Wait budget.** The wait ends at an absolute time, whichever comes first:
  `KRIA_CLIP_UNDERSTANDING_WAIT_S` (default 45) after clip-intent planning starts, or
  the resolver's vision deadline plus 20 s before `run_kria_turn`'s soft limit (150 s).
  A slow clip-intent planner call shortens the wait, never the resolver's time. With
  `0`, the gate re-reads once and does not wait.
- **Fallback.** If a clip is still unanalysed after the wait, the turn returns the same
  pending reply. `plan.diagnostics.understanding_wait_s` shows how long it waited.
- **Re-kick.** The turn re-enqueues `analyze_kria_clips` when its snapshot has
  unanalysed clips. The task is single flight, so a live run makes the call a no-op. A
  run killed mid-analysis is restarted once its lock expires, instead of every turn
  answering "still checking".

Guards: `tests/kria/test_clip_understanding_wait_postgres.py` (the real re-read and the
real background writer), `tests/services/test_clip_intent_understanding_gate.py`, and
the budget and deadline tests in `tests/kria/test_planner.py` and
`tests/kria/test_runtime_v2.py`.

## Chat turns that run out of vision budget

A Kria turn may spend `KRIA_CLIP_INTENTS_MAX_VISION_REQUERIES` (18) vision checks
within `KRIA_CLIP_INTENTS_VISION_DEADLINE_S` (40 s), in batches of 6. When unanswered
checks remain, it replies that it is still checking and asks for a short "go ahead".
It never asks the creator to resend the request: every user message joins the
combined creator request (12,000 characters), so a pasted resend duplicates each
instruction.

- **Answers survive the turn (KRI-433).** Pool-asset answers were already cached on
  `PlanItemAsset.analysis["answers"]`. Every iPhone clip is a raw `clip_assignments`
  row, and those answers were dropped, so the follow-up re-asked everything. On prod
  thread D1FDCA87 (2026-10-05), the follow-up finished only because its planner emitted
  fewer intents. `persist_clip_intent_vision_answers` now also writes
  `clip_assignments[i]["analysis"]["answers"]`. It locks the item first (the canonical
  lock order), keeps an answer only for the same media id and storage generation, goes
  through `mutate_plan_item_media`, and undoes the write if footage identity would
  change. A failed write loses only the cache and never fails the turn (KRI-291).
- **Why 18 is not enough.** A check is keyed on (clip, generation, question), and a
  membership question is built from the intent's `attribute`. On D1FDCA87 the planner
  emitted an include and a caption for each of 6 chapters, worded differently: 18 calls
  for 9 clip and chapter pairs. Two calls were still running at the deadline, and two
  clips stayed unchecked.
- **Diagnostics.** A pending turn's `plan.diagnostics` (`GET /admin/creation-threads/<id>/turns`)
  carries `vision_cap`, `vision_calls`, `vision_over_cap` (checks the cap left out),
  `vision_deadline_cut` (checks the deadline cut off), `vision_cached` (checks the
  cache answered) and `vision_multi_question_clips` (clips asked more than one distinct
  question, the doubling signal).

Guards: `tests/kria/test_clip_answer_cache_postgres.py` (real row, real writer, real
loader: the follow-up turn only checks what the budget left) and
`test_pending_diagnostics_show_differently_worded_chapter_intents_doubling_checks` in
`tests/services/test_clip_intent_resolution.py`.

## Known gaps

- Vision re-query remains video-only. Images need an inline media input path.
- KRI-154 overflow results are picked up on the next creator message. KRI-151's
  separate durable preparation flow can finish and publish the original turn.
- Two intents about the same chapter, worded differently, each pay for their own
  vision checks (see "Chat turns that run out of vision budget" below).
- A membership check sees only the intent's `attribute`. A bare chapter name
  ("Chapter 1") gives the vision model nothing to check against.

KRI-154 regression coverage: `tests/services/test_clip_intent_resolution.py`
replays the 30-clip / 8-vague-clip overflow and cache pickup;
`tests/tasks/test_clip_intent_requery.py` covers claims, retries, failed dispatch,
concurrent cache merges, and stale/deleted/unowned assets;
`tests/routes/test_creator_agent_clip_intents.py` pins the pending receipt and
clarification fallback.

### Multiple labels on one clip

Each requested label is grounded independently. The worker combines accepted
values into one overlay (for example, `Paris · Cycling`) and retains every
intent's provenance. Repeated text is displayed once. A multi-label request fails
clearly if any value cannot be re-verified, or if the combined text exceeds 120
characters; it never silently keeps the first label. Generic overlays use the
registered Inter font and a bounded width so combined values wrap safely. Single-label identity and
flag-off rendering remain unchanged. Narrated guided edits also retain these
generic visual labels alongside their narration labels.
