# Kria agent runtime v2

Kria runtime v2 turns the existing creation thread, Main Creator Agent, editor,
and render pipeline into one durable creator workflow. It is a domain controller
for Nova editing, not a general assistant and not a second renderer.

## Golden path

```text
user message
  -> append semantic thread event + durable turn
  -> async planner receives a trusted bounded snapshot
  -> deterministic policy validates tools, risk, evidence, and revisions
  -> read/reversible strategy or editor draft OR exact approval
  -> typed receipt
  -> observed assistant response
  -> existing editor commit or Job dispatcher/render/review when approved
```

The model may propose intent. Server code owns authorization, revisions, risk,
idempotency, storage resolution, execution, and completion language. A plan is
never evidence that work happened. Only a settled receipt can support “changed,”
“rendered,” or other completed-action copy.

## State ownership

| Concern | Authority |
|---|---|
| Creator-visible transcript | append-only `CreationThreadEvent` |
| Runtime selection | immutable `CreationThread.runtime_version` |
| One user message lifecycle | `CreatorAgentTurn` |
| Reversible working edit | immutable, revisioned `CreatorEditDraft` |
| Consequential consent | `CreatorAgentApproval` |
| Tool result and side-effect identity | `CreatorAgentExecution` |
| Rendered media | existing `PlanItem` and `Job` |

Runtime-v1 threads keep their existing controller and projections. A thread never
changes runtime owner because a flag changes during its lifetime.

New threads also remain runtime v1 when the field is omitted. During internal
testing only, a client may request `runtime_version: 2` with no inline message
after `KRIA_RUNTIME_V2_ENABLED=true`; its first creator message then goes through
the durable `/turns` endpoint. This explicit opt-in prevents an old web build
from silently entering the new controller during a staggered deploy. Legacy
`/messages` and `/actions` mutations reject v2 threads, so they cannot bypass
the new turn ledger or render-consent boundary.

## Tool contract

`app.kria.registry.KRIA_TOOLS` is the only runtime-v2 tool registry. Each entry
owns its argument/result schema, version, capability, risk, execution mode,
retry policy, failure class, and executor binding. The model manifest and checked
contract snapshot are generated from it.

To add a read-only tool:

1. Define strict Pydantic argument and result models.
2. Add one deterministic executor and one registry entry.
3. Add a whole-turn fixture and expected receipt/semantic response.
4. Run `make verify-kria`.

Reversible draft tools must additionally validate the authoritative draft and
write a new revision atomically. Approval-required tools must never execute from
the planner output; deterministic policy creates a pinned approval first.

After a playable generation exists, the planner uses the existing versioned
Edit Copilot parser against a server-derived snapshot. The snapshot contains
only bounded text, caption, timeline, capability, and opaque target metadata;
storage paths and signed URLs are excluded. `draft.apply_editor_ops` compiles
the portable launch subset into Nova's full-section `EditorCommitRequest`:

- timeline reorder, remove, trim, split, duration, transition, and look;
- text/title content, timing, and parity-verified style fields;
- caption text, replacement, timing, emphasis, and presentation settings;
- music removal and mix changes;
- add unused sources, image duration, and image stacking;
- separately rendered, revision-fenced reviewed speech-cut candidates.

The compiler is non-mutating. Approval consumption calls the existing editor
commit or speech-cut validator while the exact Job is locked, records the new
generation in the accepted execution, commits, and only then publishes. A lost
broker acknowledgement becomes `outcome_unknown`; it is never blindly retried.

## Conversation contract

Every assistant turn has one value class: decision, question, action, progress,
review, or recovery. Repeating the brief is not a value class. Internal planning,
tool selection, retries, and worker callbacks are projected into quiet status or
one generation lifecycle artifact, not conversational bubbles.

Failures use `KriaProblem`: stable code, phase, useful message, retryability,
recovery action, trace ID, current revision, and safe target identities. The
creator sees the problem, what stayed safe, and one next action. Operators can
follow `thread -> turn -> approval/execution -> Job/variant/generation/task`.

Runtime-v2 HTTP reads are bounded. `GET /creation-threads/{id}` returns the
newest page in chronological display order; `after_sequence=N`
returns later events and `before_sequence=N` pages backward. The `/delta` path
remains a compatibility alias, while internal editor refreshes request
`projection=full`. Pending approval cards fetch
`GET /creation-threads/{id}/approvals/{approval_id}` to obtain the current
server fingerprint, revisions, expiry, and consequence copy before deciding.
Framework validation, authentication, rate-limit, and unexpected failures on
these routes use the same `KriaProblem` envelope as runtime failures.

## Creative Brief, router and receipts (KRI-188)

Behind `KRIA_CREATIVE_BRIEF_ENABLED` (or a `KRIA_CREATIVE_BRIEF_USER_IDS`
allowlist entry); off is byte-identical. Code: `app/kria/brief.py` (ledger,
router, request rendering), `app/kria/brief_checks.py` (receipts, reply).

- **Ledger.** `creative_brief_versions` (migration 0111) is append-only, one
  version per turn (`UNIQUE(thread_id, version)`, idempotent per `source_turn_id`).
  Requirements have a server-assigned id (`r<n>`), `kind`, `scope`, `literal`
  (creator-written text only) or `description`, `facts`, and `status`. A later
  requirement with the same `(kind, scope)` supersedes the earlier one, except
  dictated shot texts (KRI-422): a `per_clip` text with both a `literal` and a
  `description` is keyed by its shot too, so six shots dictated in one message
  all stay live and restating a shot replaces only that shot. A label-every-clip
  rule (no such pair) still replaces the whole per-clip lane, shot texts
  included. The Main Creator (prompt v43) only proposes `brief_updates`, at most
  16 per turn; unparseable entries are dropped, never fatal. Versions are written by the turn-completion
  transaction under the thread lock, after the revision fence, so a requeued
  turn never persists one.
- **Context is not a command.** The brief records requested output changes, not
  facts that merely describe the footage. Times, activities, locations, routes,
  or named subsets can shape the proposed story without becoming durable
  `order`, `select`, or per-clip text requirements. `order/global` is emitted
  only when the creator explicitly asks the edit to arrange clips (for example,
  "put them in the order I filmed them"). A broad request for creative ideas
  does not promote surrounding context into clip constraints.
- **Router.** `route_requirements` is deterministic. `replan` when a new
  requirement is `order`/`select`, a per-clip text requirement arrives and the
  plan has no per-clip text lane, the kinds are mixed, there is no editable
  render, or the message asks to redo it ("do it again based on my prompt").
  Otherwise `editor_ops`. With a render present the Main Creator runs first (it
  extracts the requirements) and the copilot only when the router says
  `editor_ops`. `_validate_draft_plan` rejects an editor-ops-only plan when the
  route is `replan`.
- **Request.** With the flag on, the Main Creator and the clip-intent planner
  read `render_brief_request(brief)`, and the approval dispatch passes it as
  `creator_request` instead of the draft summary.
- **Receipts.** Each draft turn attaches `requirement_receipts`
  (`met | partial | not_possible`, reason, `inferred`) to its `draft_applied`
  event payload. Checks: per-clip text coverage, order basis
  (`ordering_basis` / `ordering_fallback_clip_ids` when present), duration
  within +/-10%, literal text (whole-word, Turkish-aware match). `clip:<id>`
  is checked against that clip only; editor payloads carry no per-clip
  structure, so per-clip text on an editor edit is judged only by its exact
  words (missing from the edit is `partial`, never `not_possible`), unless the
  compiled edit carries a `text_diff` (persisted as `document.editor_text_diff`):
  then `plan_facts_from_editor_payload` fills the per-clip facts and the check is
  a real before/after one (`met`/`partial` per label; "just say X" / "only" must
  match exactly). A style ask is `met` when the payload has edited text elements,
  and a total-length ask when the timeline slots sum to the target. A
  requirement nothing could judge gets no receipt and no reply line: no
  checker (e.g. "add captions" or "make it warm"), or a neutral reason when the
  facts were missing (beats without a manifest, a strategy draft's clip order,
  which drafts never record, an editor edit's length or per-clip text with no
  exact words). It stays `open` in the brief, so an unchecked ask never reads
  "Partly". The model's summary is dropped only when
  a requirement a checker actually judged is not met. Unjudged receipts stored
  before this rule are skipped wherever stored receipts are read (`is_judged`:
  the reply, the unified montage review, and `GET /brief`). `open` therefore
  means "nothing has judged this", not "a check is pending": a requirement with
  no checker stays `open` for good, so clients must not show it as in progress.
  `KriaObservedTurnResponse.requirement_receipts` is reserved for the
  observed-turn projection; today receipts ride on the `draft_applied` event
  payload and `GET /creation-threads/{id}/brief` returns the current
  brief with the newest receipt per requirement (empty when the flag is off).
- **One receipt, each fact once (KRI-739).** An editor turn that bound receipts also
  puts `reply_lead` (`receipt_lead`: "Done." / "Done — have a look." / "Partly done." /
  "I did 1 of your 2 asks." / "I couldn't do that.", then the server's notices) and
  `also_changed` (changes no ask claimed; `describe_diff`) on the `draft_applied` payload.
  The apps show the lead plus one static row per receipt (outcome icon + `reason`). `content`
  keeps the full prose for the web chat and apps without the keys; the two must not drift,
  so `reply_from_editor_receipts` is the only writer of that prose. The stored `reason` of an
  ask that names no dimension is the bare change list ("I changed:" is added to the prose only).
- **Length answers (KRI-737/738).** A longer clip keeps its old window and starts earlier
  in its source when the footage after its start runs out (`editor_ops_v2/timeline.py:
  _fit_window`; an explicit `in_s` is never moved). The copilot's "how short/long?" question
  records its options as `length_options` (`[{label, seconds}]`); `agents/length_options.py`
  keeps one only while the model's own reply is the one shown and states that length, and the
  planner emits them as `options` on the `assistant_response` event. A tap sends the label as
  plain text (the copilot's ANSWER rule resolves it); no protocol bump, the prose still lists them.
- **Ask facts (KRI-667).** Which checker judges an ask is read from `facts` first,
  in English keys and values whatever the creator's language: `triggers` (spoken
  words, never translated), `keep_whole_take`, `closing`, `speech_cleanup`
  (true/false), `captions` (`on`/`off`/`word_by_word`), `caption_language` (a
  two-letter code). The Main Creator and the brief extractor are asked for them
  (Main Creator v58, extractor v8). The word recognisers in `brief_checks.py` run
  only when the fact is absent or malformed, and no new ones may be added:
  `tests/kria/test_kri667_no_new_intent_regexes.py` ratchets the `re.*` count of
  every creator-wording module down (exempt a non-wording regex with
  `# kri667: <why>`). Pins: `tests/kria/test_kri667_structured_ask_facts.py`.
- **Clip speed (KRI-689).** The phone montage plays every clip at natural speed
  (`phone_guided_plan` refuses a `playback_rate` other than 1). A speed ask ("speed the
  river shots up 2x", slow motion, time-lapse, "2 kat hızlandır") used to be dropped
  silently and only listed under "Have a look at these in the video" after the render.
  `brief_checks.clip_speed_notice` now adds one plain sentence to the draft reply and the
  approval card for a draft the unified montage will render, and the render-ready review
  reports the ask as "Couldn't" with the same reason (a format limit: it never asks
  "simpler version?" and never blocks the approved render). `kria/speed_ask.py` reads
  `facts.playback_rate` (or `speed`) first; the Main Creator is not asked for it yet, so
  today the narrow EN/TR wording fallback decides. Next step: ask for the fact in the
  Main Creator / extractor prompt, then delete the wording fallback. Pins:
  `tests/kria/test_kri689_clip_speed_ask.py`, `tests/kria/test_kri689_speed_notice_postgres.py`,
  `tests/tasks/test_kri689_speed_review.py`, the KRI-689 cases in
  `tests/tasks/test_unified_montage_dispatch.py`.
- **Freeze frame (KRI-777).** No edit can hold a frame (no strategy field; neither the
  phone recipe nor the cloud renderer freezes a creator-chosen frame). Café M2 filed
  "freeze the frame for one second on Atakan's bite" as `timing/global {"duration_s": 1,
  "freeze_frame": true}`: the draft and approval card promised the freeze, and the phone
  receipt read "This draft is about 4.7s; you asked for 1s." Now a freeze ask is an honest
  `not_possible` format limit on every draft and finished render (KRI-700's rule, not the
  KRI-689 notice: the receipt replaces the model's summary, so neither the reply, the change
  list nor the card can promise it). The phone montage draft checks it although it defers
  every other `timing` ask to the render. `kria/freeze_ask.py` reads `facts.freeze_frame`
  (or `freeze`) first, else a narrow EN/TR wording fallback (no `re.*`). Its
  `times_one_moment` (a freeze, a clip-speed ask, "N seconds on <a moment>") keeps those
  seconds out of everything that treats a timing ask as the video's length: the global
  timing receipt, `length_policy.creator_duration_s`, the duration-conflict questions, the
  render contract and the phone planner's target (`unified_montage.brief_view`). Pins:
  `tests/kria/test_kri777_freeze_frame_ask.py`, `tests/kria/test_kri777_freeze_frame_postgres.py`,
  `tests/tasks/test_kri777_freeze_render.py`.
- **Cost.** With a render present the flag adds one Main Creator call to
  editor-op turns (the planner extracts requirements before the router runs).
  If that call fails, a plain edit falls back to the legacy copilot-first path
  with no brief update, so a Main Creator outage never blocks a simple edit.
- **Known gaps (deferred).** No retraction ("drop the title") yet; a render
  dispatched later reads the latest brief version, not the version the approved
  draft was checked against; requirements past the 40-live cap are dropped
  silently; `read_creative_brief` scans only the newest 30 assistant events.

## One montage plan (KRI-190)

Always on (KRI-220 removed `MONTAGE_UNIFIED_PLAN_ENABLED` and its user allowlist; it
had been ON in prod): every non-voiceover phone montage goes through this planner.
A montage WITH a recorded voiceover keeps its own writer,
`_run_phone_voiceover_montage_job` (see agents/DECISIONS.md "Two montage writers by
design"). Code: `app/pipeline/unified_montage.py` (pure planner),
`_run_phone_unified_montage_job` in `app/tasks/generative_build.py` (worker),
`_unified_montage_review` in `app/tasks/kria_runtime.py` (reply).

**Why.** A runtime-v2 phone approval has no approved guided proposal, so the worker
fell to the plain phone-montage lane. That lane shows one intro hook, orders by
matcher score, ignores the brief, and rejects every portrait-canvas job at the
item's default `landscape_fit="fit"` (`phone_plan_unsupported: letterboxed landscape
fit ...`, job 94c4c865). It also 422'd every chat text edit (`unsupported_phone_edit`)
because it never wrote a `guided_story_execution_plan`.

**What happens.** In `_run_generative_job_impl`, a phone job in the
montage family (`GUIDED_EDIT_FORMATS`), no recorded voiceover, no `guided_edit`,
plans a guided fast montage and then runs the existing `_run_phone_guided_job`:

1. Inputs: the job's clip order (`all_candidates.clip_paths`), the phone bindings
   (durations, dimensions), the item's clip assignments (capture time, place,
   stored landmark facts), the thread's latest Creative Brief, the strategy's
   confirmed copy. `CLIP_FACTS_*` gates the facts; landmark guesses use the
   existing `enrich_clip_facts` (45s cap, fail-open).
2. `plan_unified_montage` returns an `EditProposalSnapshot` (`fast_montage`, exact
   `fast_cuts`, `video_reuse_policy="once"`, new `clip_labels`) persisted as the
   job's immutable `guided_edit`, plus a small `unified_montage` record.
3. The guided compiler, its validators, the guided phone compiler and the guided
   phone editor apply unchanged; text-only chat saves recompile normally.

**Decisions the planner owns** (nothing else):

- *Order*: capture time only when the brief has an `order` requirement with a
  capture key and two clips carry a time; otherwise the creator's pinned order
  (`all_candidates.creator_clip_order`, a revision keeps a reordered timeline,
  basis `creator_order`), else attachment order. The basis and
  the clips that fell back are recorded (`ordering_basis`,
  `ordering_fallback_clip_ids`). A selection order is never silently overridden.
- *Per-clip text*: only when the brief (or confirmed shot labels / verified clip
  intents) asks for it. Grounding, in priority: creator words (`clip:<id>` literal,
  positional `shot_labels`, creator-text intent) > verified intent > clip fact
  (`creator` > `landmark` > `place`, most specific part) > brief `start`/`end` for
  the first/last clip. No grounding means no label; the receipt says partial.
  Landmarks are `inferred` and listed for correction.
- *Reading time*: `min_display_s = clamp(0.8 + 0.06 * chars, 1.2, 3.0)`; a labelled
  cut lasts at least that long, capped by the clip itself (then the receipt says
  the label is too short to read). Unlabelled cuts are 1.2s. A requested length
  (`brief timing.duration_s`, else `strategy.target_duration_s`) grows cuts (up to the
  clip's length; without a stated length, at most 3s each) or shrinks unlabelled
  cuts (not below 0.8s); readable text wins. A clip shorter than the snapshot's
  0.4s video-cut floor is shown whole (`MIN_VIDEO_CUT_S`, KRI-217): whole frames
  stopped a fraction of a frame short of a 0.298s iPhone clip, which the strict
  snapshot refused.
- *Closing spoken line (KRI-517)*: when a resolved `last` order intent seats the
  closing clip, that clip's analysed `understanding.speech` has segments, and the
  camera audio is kept (`montage_audio.preserve_source_audio`, or
  `audio_strategy="original_audio"`), the closing cut holds the whole line, from
  `SPEECH_LEAD_S` (0.15s) before the first segment to `SPEECH_TAIL_S` (0.3s) after
  the last. That cut never shrinks or grows, and the other cuts share the rest of the
  length. A line the creator's length can't also fit, a song montage, or a montage
  that merely ends on a talking clip keeps the ordinary cut. The record carries
  `closing_speech` ({media_id, source window}) only when a line was held. Incident:
  stress kit M3, prod thread 0b1f9556. "End on Elif's sentence in her own voice" was
  cut from 2.70-4.97s of a 0.00-5.44s line. Guard:
  `tests/pipeline/test_unified_montage_closing_speech.py`.
- *Quoted closing line (KRI-597)*: "end on the moment I say '...'" names a spoken
  line, so `app/services/spoken_quotes.py` decides it from transcripts, not the
  resolver's 200-character view: `regound_quoted_intents` (after resolution in
  `clip_intent_planning`) moves a single-clip intent whose quote its clip never says
  to the one clip whose full transcript does (word order and fillers may differ;
  ambiguous or unspoken quotes are left alone). Before the length plan freezes,
  `ground_closing_quote` finds when the line is said: the timed segments first, then
  a word-timed whisper pass on that proxy (language hint + the quote as prompt).
  `build_length_plan` pins the closing take's last window to the line and records
  `length_plan.closing_quote` (`found` / `not_found` / `too_long` / `not_held`); any
  status but `found` adds a plain notice to the pre-approval reply, and the order
  receipt is `met` only when the finished closing cut holds the quoted line.
  whisper's "thanks for watching" outro segments no longer stretch a held line.
- *Several moments per take (KRI-596)*: "the best six moments, each very short"
  (`style_asks.moment_ask`: a count of moments/shots/cuts, EN + TR, and "each short"
  = at most 3s) lets `best_parts.select_moment_windows` take several non-overlapping
  windows from one long take: the count shares the length equally, takes share it by
  footage length (one each at least), a take's strongest analysed moments win, and
  its moments play together in source order (a quoted closing line last). Only a
  footage-owned montage of videos uses it; the plan then carries
  `video_reuse_policy="distinct_windows"`, and `read_length_plan` accepts repeated
  takes but refuses overlapping windows. Moments that don't fit fall back to one
  window per take, with a notice. Incident: cabin kit M2, prod thread 6605cf61.
  Guard: `tests/tasks/test_long_take_moments_montage.py`.
  KRI-686: the count may sit apart from the noun ("eight of them", "8 in total", "sekiz
  tane") and a per-moment length ("about 2 seconds each", "her biri 2 saniye") is
  `MomentAsk.each_s` (<= 3s also means `short`; it sizes the moments when no count is
  given). Read by tokens, not patterns (KRI-667 ratchet); a recorded
  `facts.moment_count` / `facts.moment_length_s` decides first. "no dark stretches"
  sets `excludes`, so that half of the ask stays an honest "can't check". When a take's
  analysis has no `best_moments` (the model may return `[]` for uniform footage) the
  moment path spreads windows over the take (`moment_fill`); the plain one-window
  selector falls back to the take's first seconds (`analysis_fallback`). Incident:
  boat kit M2, thread 1390E686. Guard: `tests/tasks/test_moment_ask_wording.py`.
  KRI-776: the creator's own numbers reach the brief deterministically. The planner runs
  `style_asks.carry_moment_ask` on every turn's brief updates (Main Creator batch,
  pre-extracted batch, `extract_deferred_brief`): it reads the creator's message (quoted
  phrases and the brief's exact on-screen text left out) and, when the brief would not
  already carry that count / length, writes `moment_count` / `moment_length_s` facts onto
  this turn's `select` moment update, the live `select` moment requirement (a `change`), or
  a new `select` requirement worded as the creator's sentence. One word may sit between
  the count and the noun ("six separate moments", "altı ayrı an"), never a count changer
  or a unit ("2 more moments", "5 minute shots"). With no creator total the moments are
  `each_s` long (`build_length_plan(moment_each_s=...)`), and the draft check judges the
  moment ask against the approved length plan's windows (`brief_checks.with_planned_cuts`),
  so a plan short of the count is held back before approval instead of promised. Incident:
  café kit M2, thread EE7B4DCA. Guard: `tests/kria/test_kri776_moment_ask_in_brief.py`.
- *Clips ruled out by length (KRI-623)*: "from the long takes", "skip the short
  clips", "uzun çekimlerden", "kısa klipleri kullanma" leave clips out whatever the
  Main Creator picked. `app/kria/clip_scope.py` reads the live brief's `select` asks
  and the current message on every plan (`_plan_creator_action`, after the KRI-593
  voiceover rule), splits the own videos at one clear duration gap (2x, no second gap
  like it), and rewrites an "all"/empty pick into an explicit `selected` list, so
  dispatch, the length plan and the phone montage all see it. A length word counts
  only in a selecting phrase about a determined subset ("the long takes"), never a
  description or a cut-length style; a later "use the short clips too" adds them back.
  Photos and Visuals are never dropped; "skip the dark ones" stays the model's call
  (no lighting signal yet). Incident: cabin kit M2 re-send, prod thread 35ccc8ef.
  KRI-758: ONE clip told apart by what it shows ("Caro'nun … bıraktığı kısa klibi
  kullanma", "skip the short clip where …") is never a length group — a singular
  phrase is the clip-intent `exclude` path; the single "en uzun klibi" / "the longest
  clip" still scopes, and media the strategy itself places (montage audio source,
  reaction beats, closing media) is never dropped by a length word. Guard:
  `tests/kria/test_kri758_singular_clip_is_not_a_length_group.py`. Incident: café kit M1,
  thread 91256BF1.
  Guard: `tests/kria/test_kri623_ruled_out_clips.py`.
- *What the clips say orders a camera-sound montage (KRI-585)*: a phone montage with no
  song, voiceover or named voice source plays every clip's own sound
  (`narration_notes.keeps_camera_sound`), so the narration is heard cut by cut.
  `app/services/spoken_roles.py` reads each clip's spoken role from its analysed speech
  by folded tokens (no regex, KRI-667 ratchet): `opening` ("hoş geldiniz", "welcome
  to"), `closing` ("görüşmek üzere", "see you next time"; whisper's invented outro is
  ignored) and `introduction` ("Bu da Deniz", "this is Deniz", with someone on screen).
  A welcome or sign-off said on more than one clip (a room-by-room tour) moves nothing.
  Only in an order nobody chose (no filming- or upload-order ask, no grouping answer, no
  contract-pinned order, no song; a `narrative` / first-last key is not a basis):
  welcomes lead and sign-offs close, under the creator's own first/last rows.
  `spoken_seats` is the one rule for the order and two windows: the length plan seats the
  welcome's whole line (`spoken_moments`) and, for a creator-pinned closing clip whose
  words only introduce someone, a stretch with no words (`quiet_moments`, capped there);
  it records `length_plan.spoken_order` (order only, plain windows, if the seats would
  make the length impossible). The phone unified writer reorders
  (`plan_unified_montage(speech_order=True)`) only when that record is on the approved
  plan and the strategy still keeps every clip's sound (`speech_order_approved`), so it
  never moves a clip the reply did not name. The pre-approval reply then says the
  narration plays and where those lines go (`narration_note_parts`, EN + TR); it adds
  nothing when the words changed nothing, and claims the quiet ending only when the
  approved windows stay off the words. Kill switch `SPOKEN_ORDER_ENABLED` (default on;
  off = no seats, no record, no reorder, no note): `fly secrets set
  SPOKEN_ORDER_ENABLED=false -a nova-video`, then restart api and worker. Incident: cabin
  kit M1, prod thread 7ccbde0a. Guards: `tests/tasks/test_kri585_spoken_order_montage.py`
  (incl. the worker's negative cases), `tests/pipeline/test_unified_montage_spoken_order.py`,
  `tests/services/test_spoken_roles.py`.
- *Clip exclusions (KRI-667)*: "Skip the quad bike clip", "don't use the blurry ones",
  "çamaşır klibini kullanma" are one `exclude` clip intent each, stated by the
  clip-intent planner in any language (`ClipIntentOp` includes `exclude`; before, the
  model wrote `include`, which forced the clip IN). The parser's KRI-511 regex is only
  a fallback that repairs an `include` whose own quote skips exactly those clips
  (`exclusion_repaired`, log-only); an include beside a skip ("Skip the laundry, but
  the gym has to be in") is never inverted, and a text or sound removal ("remove the
  sport labels") is never an exclude. The resolver is asked the membership question
  (`include`); a clip is kept for the exclude only at confidence >= 0.8, with no vision
  check and no creator question: nothing matched (or the resolver down) means nothing
  is left out. `app/kria/clip_scope.py` `apply_clip_exclusions` then narrows the pick
  like KRI-623 (after resolution in `_plan_creator_action`, re-checked by
  `check_strategy_for_runtime_v2`; in the v1 route before `compile_active_plan`). It
  never removes a clip another resolved intent names or the strategy places (reaction
  beat, closing media, montage audio/cadence source), never the last own clip, never
  under a guided voiceover contract, and clears an excluded hero. The semantic planner
  and the narrated aligner skip `exclude` rows, so nothing forces the clip back in.
  Kill switch `CLIP_INTENT_EXCLUDE_ENABLED` (default on; off = the pre-KRI-667 prompt
  byte for byte, the skip dropped silently, nothing removed): `fly secrets set
  CLIP_INTENT_EXCLUDE_ENABLED=false -a nova-video`, then restart api and worker.
  Guards: `tests/kria/test_kri667_clip_exclusions.py`, the KRI-667 cases in
  `tests/agents/test_clip_intent_planner.py`.
  *Look-alike and clashing excludes (KRI-761, Merope café M1)*: `app/kria/clip_exclusions.py`.
  An exclude the resolver matched to a clip another ask pins (a first/last placement, a
  creator-written label, media the strategy places) or to 2+ clips only just over the bar
  (< 0.9) is not guessed: the planner asks with the clip picker (category op `exclude`, the
  creator taps the clips to leave out, pinned clips are never offered, the matches come
  pre-ticked). Taps and typed file names are the creator's word and are never asked again;
  the answer rides `fold_clip_selections`, so it holds on later turns. A file name beside
  an exclude ("M1_05 klibini") now settles without the resolver (`picker_eligible` is true
  for `exclude`). An exclude the resolver matched in the head draft that matches nothing
  now keeps that match (`carry_forward_exclusions`, reads `CreatorEditDraft`, fails open).
  Picker off (`KRIA_CLIP_SELECTION_QUESTIONS_ENABLED=false`) = the silent pin-wins of
  KRI-667. Guard: `tests/kria/test_kri761_look_alike_exclusions.py`.
  *Unmatched excludes (KRI-807, Don Julio M1)*: an exclude that leaves no clip out
  ("skip the blurry shot of the empty table", resolver `n=0`) asks with the same picker
  instead of the free-text "Tell me which ones" notice (`exclusions_to_ask`). Every
  unpinned clip is offered; the clips the resolver put forward below the bar or for a
  vision check (`IntentResolution.exclusion_suspects`, in memory only) come pre-ticked
  and are never left out without a tap. When the resolver itself asks a clip picker,
  the exclude rows ride in that same card (`with_exclusion_categories`). The notice is
  kept for a planner-dropped exclude, the picker flag off, a one-clip project, or
  nothing tappable. Blur is not a clip-record fact: a measured sharpness probe on 170
  kit clips could not tell M1_04 from its sharp look-alike. Guard:
  `tests/kria/test_kri807_unmatched_exclusion_picker.py`.
- *Title*: confirmed strategy title > brief title literal > brief global literal
  (+ route) > facts ("20K Run · Arnavutköy → Eminönü", `title_from_facts`). With
  none of those sources, the visible opening title is omitted; `Montage` remains
  only the internal snapshot fallback. Never a model hook, never place text nobody
  asked for. Creator-written labels keep up to 120 characters; fact/model labels
  are cut at 60.
  Text stays NFC; nothing is folded to ASCII.
- *Typography*: Fraunces has no "→" glyph and the phone lays out from exact glyph
  ids (a missing glyph fails the whole recipe), so `skia_font_covers` picks the
  first bundled font (creator font, Fraunces, DM Sans) that covers every string;
  only uncovered characters are dropped when none does.
- *Visuals (KRI-217)*: the item's ready Visuals-pool photos and videos
  (`_load_unified_montage_visuals`: not a dedupe receipt, registered before the
  job was minted, narrowed to an explicit `selected` strategy scope) are spread
  evenly between the clips (`_scatter`; the montage opens on a clip) in upload
  order, as `lane="asset"` fast cuts. A photo holds 1.2s (never more than 3s,
  even with a stated length); the creator's `image_layout` ("don't crop my
  photos") is honoured. `_run_phone_guided_job` binds them like any approved
  guided story's Visuals (`bind_phone_visuals`, `stillImages`/`visualVideos`). A
  kind the phone cannot draw fails the job (`UnsupportedPhonePlan`), never drops.
- *A Visual between the moments of one take (KRI-796)*: "six moments from it, in
  this order: A, B, ... put the photo right after B". When every order intent is a
  `then` row naming either the one take or only Visuals, and the moments they name
  (one row listing them comma by comma, or one row per moment) are exactly the take's
  approved length-plan windows, `clip_order_sequence.moment_seats` reads the i-th
  moment as the i-th window. A Visual's row plays it right after the moment its own
  words anchor ("after" / "before" + the moment's folded words, either way contained),
  or, with one row per moment, where it is listed; `_seat_among_moments` moves only
  that cut. `_moment_outcomes` judges those rows off the finished cuts (`moment` on
  the outcome row). An anchor naming no moment or two, a count that is not the window
  count, a Visual before the first moment, more than one take, or no approved
  windows: no seat, the KRI-217 scatter and `visual` outcome stand (an honest partial).

**Dispatch (KRI-217).** Every runtime-v2 approval dispatches with
`bypass_guided_edit_gate=True`, and the bypass used to refuse any item with a pool
row (`guided_edit_bypass_unsafe`), so every iOS montage with a photo answered "I
couldn't start the render" with a retry that could never pass (thread 6BF1213E).
For a v2 approval `_v2_visuals_refusal` now counts only the creator's Visuals
(manifest-visible states; an abandoned upload reservation or a failed photo never
blocks). The unified phone lane renders them, refusing only while one is still
uploaded/queued/analyzing (`visuals_processing`) or when the phone cannot draw its
kind. A cloud-rendered v2 montage is clip-only and still refuses them. `_finish_approval_dispatch` answers both with
`_VISUALS_DISPATCH_REFUSALS` copy and `recovery: ask_user`. Non-v2 bypass callers
are byte-identical.

**Not covered / known gaps.** The worker plans from the thread's *latest* brief, not
the approved version (a redelivery before the plan is pinned can pick up a newer
one; receipts are dropped from the reply when `brief_version` differs). A
unified phone montage renders source audio only (no matched music bed, beat-snap
or hero intro). A single clip under 3s fails as "too short to make a montage".

**Snapshot lane.** `EditProposalSnapshot.clip_labels` (omitted when `None`, so
stored snapshots and approval hashes are byte-identical) and the
`clip_labels is not None` branch of `guided_story._text_elements` draw the title at
the top and one label per cut at the bottom. `TimelineClip.text` exists in the
recipe schema but the guided compiler, like every guided plan, emits positioned
`text_layers`, which the iPhone engine already renders.

**Receipts.** The worker computes receipts from what it put in the plan
(`brief_checks.plan_facts_from_unified_montage`: clip ids, labels, inferred labels,
title, total length, ordering basis, too-short labels) and stores the judged ones
(`is_judged`) on `unified_montage.requirement_receipts`. When the render is ready the observer's
`assistant_review` event is composed from them (`reply_from_receipts`) and carries
`requirement_receipts`; with the brief off or no record it is the unchanged default
review.

**Planner prompt.** `main_creator` v39 always records route/distance/activity
`facts` and an `order` requirement when the creator names a sequence.

**Two writers by design (KRI-220).** The old plain writer is not deleted: it is
narrowed and renamed `compile_phone_voiceover_montage_plan`, and handles ONLY
montages with a recorded voiceover (voice + optional low music bed, trim-to-footage,
intro hook), which the unified planner cannot express yet. Cloud (non-phone) montage
is untouched. A cloud-rendered v2 montage still cannot place Visuals (v2 has no
guided execution there; KRI-217 follow-up).

Guards: `tests/pipeline/test_unified_montage.py`,
`tests/tasks/test_unified_montage_dispatch.py` (the East Run repro, no-flag routing,
voiceover-lane exclusion, chat edit, photos in the device recipe),
`tests/kria/test_unified_montage_receipts.py`,
`tests/evals/test_main_creator_evals.py::kri190_route_facts_and_order`,
`tests/routes/test_plan_item_sync_dispatch.py` (KRI-217 real-Postgres dispatch and
Visuals loader), `tests/tasks/test_content_plan_build.py` (`test_v2_phone_montage_*`
Visuals cases), `tests/kria/test_runtime_phone_v2.py`
(`test_a_visuals_refusal_says_what_to_do_instead_of_retry`).
There is no flag to roll back; revert the PR. In-flight jobs keep their pinned plan.

## Reply language (KRI-520)

A chat replies in the creator's language: Turkish or English. `app/kria/reply_language.py`
owns the rule; kill switch `KRIA_REPLY_LANGUAGE_ENABLED` (default true; false = every
reply English and prompts byte-identical).

- **Deciding.** `submit_turn` resolves the language from the creator's own message
  (`detect_chat_language`: Turkish letters and common Turkish/English chat words, ASCII-typed
  Turkish included). Stock sentences the apps send for a tap ("Suggest an edit.", "Use this
  order: clips …") never count. The result sticks to the thread in
  `CreationThread.state["reply_language"]`; leaving it takes a message with three clear
  words of the other language. With no clear message and no stored language, the request's
  `Accept-Language` decides (iOS sends the device languages; only the top preference counts).
  Other languages resolve to none: server copy stays English and the models mirror the
  creator as before.
- **Binding.** Every entry point that writes chat events binds the language for its copy
  (`reply_language_for` / `bind_reply_language`): `submit_turn`, `run_kria_turn` (from the
  trusted snapshot), approval dispatch, render observation and expiry. Server copy is written
  `say(en=..., tr=...)`; unbound means English.
- **Render workers (KRI-765).** A Celery worker has no chat. Dispatch stamps
  `assembly_plan["creator_reply_language"]` beside `creator_brief_binding`, and the phone
  montage/narrated writers bind it (`generative_build._worker_reply_language`) around the
  requirement receipts and the block recovery they store. A reason that names clips can't be
  re-worded by the chat afterwards, so it must be written in one language. No stamp (English
  chat, older job) = English, as before.
- **Models.** Main Creator, the edit copilot and the clip-intent agents get
  `reply_language`; for Turkish their prompt ends with `prompt_language_line("tr")`, which
  asks for every creator-facing sentence in Turkish while on-video text stays exactly as the
  creator wrote it. English/unknown adds nothing, so those prompts are unchanged.
- **Understanding.** Typed answers, status/help questions, redo/move/remove asks, the
  copilot honesty guard and the follow-up parsers accept Turkish, folded with
  `brief_route.fold_text`/`loose_text` (İ/I/ı and ASCII spellings match). The honesty
  guard reads the copilot's `claimed_ops` (the ops its reply calls done, restated by the
  parser for server-written replies) first; the EN/TR wording regexes are only the fallback
  when that field is missing (KRI-667).
- **Never localized.** HTTP/`KriaProblem` messages and codes, payload machine values, the
  "Render this draft:" approval prefix and the two command sentences iOS sends
  ("Retry preparing my clips.", "Keep the same plan with my current footage").
- **Guard (KRI-667).** `tests/kria/test_turkish_chat_guard.py` fails on English written to a
  Turkish chat (a net over every v2 event writer) and on fixed English handed to a writer,
  turn plan or strategy refusal without `say()`. Worker refusal text is re-worded at the chat
  side by exact text (`kria_runtime._WORKER_TEXT_TR`); one with no Turkish twin is left out,
  never shown in English. Allowlist: `tests/kria/turkish_chat_guard.py`.

## Render consent

Every initial render and rerender requires a separate, expiring approval pinned
to the current draft, manifest, ownership epoch, Job/variant/generation, and
expected revisions. “Make this” may prepare the approval but cannot consume it.
Approval consumption and exact editor/Job commit happen atomically; broker
publication follows the commit through the existing deterministic Job dispatcher
or editor rerender task. Accepted editor executions persist enough dispatch
metadata for the reconciler to publish the same generation after process death.

## Local proof

From a prepared worktree:

```bash
make kria-replay FIXTURE=nermin-matcha-update
make kria-replay FIXTURE=render-approval-required
make kria-replay FIXTURE=ambiguous-render-dispatch
make kria-replay FIXTURE=stale-project-replan
make verify-kria
```

The replay uses no model, storage, broker, or renderer. It proves snapshot,
planner contract, policy, typed tool execution, receipt, and observed semantic
response across happy, approval, ambiguous-dispatch, and stale-state paths. Real
model/media/render validation remains a separate rollout gate. `make verify-kria`
also checks the generated registry/API types, the 240-case structural all-format
corpus, editor reducer, runtime state machine, HTTP envelopes, admin trace, and
migrations. It writes duration/flakiness metadata to
`test-results/kria-verify.json`; feature flags stay off until the live gates pass.
