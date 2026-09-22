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
   `app/services/clip_intent_planning.py` forwards the complete inventory for
   grounding. Transcript-sourced requests stay in the complete inventory but
   bypass the visual resolver; they require a pinned guided narration before a
   strategy is accepted. Model-authored `resolved_clip_intents` remains untrusted.
   Both the classic creator route and Kria inventory instructions before proposing
   an executable plan. Kria accepts only server-resolved intents, persists fresh
   vision answers, and returns a question/recovery response for incomplete resolution.
   Dynamic partitioning (for example, separate chapters for every discovered city)
   asks for concrete groups instead of silently treating every clip as one group.
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
   only. Completed answers survive another clip hitting the deadline; membership
   and caption authoring share that deadline. Membership checks are re-asked as
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
labels require a transcript span. Visual and transcript intents can coexist on
a narrated edit, but neither source can authorize the other's text.

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
chat event). Enable: `fly secrets set CLIP_INTENTS_ENABLED=true --app nova-video`
+ restart api + worker. Rollback: set it `false`; visual intents are ignored
(chat clears them, the build task and worker gate on the flag). Transcript
intents and already-confirmed label elements keep their existing behavior.
Retirement must not be deployed until the KRI-127 flag-on observation period
requested by KRI-156 has been reviewed; this code change does not enable the flag
or establish production observation evidence. Before enabling, run
the live evals: `tests/evals/test_clip_request_resolver_evals.py`,
`test_clip_question_evals.py`, `test_main_creator_evals.py`,
`test_edit_proposal_evals.py` (`--eval-mode=live`, no judge).

## Known gaps

- Vision re-query remains video-only. Images need an inline media input path.
- KRI-154 overflow results are picked up on the next creator message. KRI-151's
  separate durable preparation flow can finish and publish the original turn.
- Vision answers are cached for pool assets only, not raw `clip_assignments`.

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
