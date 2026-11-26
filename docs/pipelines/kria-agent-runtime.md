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

## Captions and added text

Captions and subtitles name the same speech lane (including translated speech;
Turkish `altyazı`). Titles, labels, callouts and decorative words are authored text.
A request for English subtitles, a supplied title and "no text except the title"
must preserve both the subtitles and that title, without adding other text or
asking the creator to resolve a false conflict. The same applies to a follow-up
removing extra text. Only an explicit caption/subtitle removal, or an explicit
request to remove all writing including subtitles, turns captions off.
An explicitly requested smaller heading under a title remains authored text.

Acceptance for the cabin N1 wording: keep `captions=on`, `caption_language=en`
and the supplied title; no new labels, pinned text or duplicated speech text.
Check both EN/TR and subsequent edits. Negative controls must detect missing
subtitles and an unexpected decorative label. Recipe checks establish layer
separation, not live-model interpretation or a verified TestFlight export.

## Only the text she asked for (`TEXT_COMPOSER_ASKED_ONLY_ENABLED`)

With the switch on, the creation text pass keeps a composer `add_text` only when it spells one of the creator's literal asks exactly, alone or with the lines right after it on screen (`_exact_covers`): same words in the same order, nothing skipped, added or repeated, and the literal finished ("this is good" is not "this is not good", "go go go go" is not "go go", and "The prompt I run" alone is an unfinished line). Each literal is on screen at most as many times as she asked for it, counting a planned bar that still shows it (a per-clip ask is not capped); a piece of her line beyond that is a repeat. Words compare with the shared Turkish-aware fold, so case, accents, punctuation and emoji do not matter. Her emoji and punctuation (`_ask_marks`: one mark per grapheme cluster, so a skin-toned or ZWJ emoji is one mark and a piece of it is another; "❤" and "❤️" are one heart) share one budget: each mark is on screen at most as often as her literals have it in total (a per-clip ask's marks are not capped). A planned bar that shows her line and every kept line of her words use up the marks they show; emoji inside a line of her words never drop that line, they only use her marks. A line of only marks then stays, first on screen first, when its marks sit together in her order inside one of her literals ("👀🔥" is not "🔥👀") and each is still left ("🔥🔥" and "🔥" together are three fires). A literal of only marks ("🔥") is checked like any other: it allows its marks, never a word. An invented line or a second copy of her line is dropped, not raised (raising would fail her whole video), and logged as `creation_text_unasked_dropped`. The drop is verified: if the program would no longer replay without the dropped line, the drop is skipped (`creation_text_unasked_kept`) and the composer's ops stand, exactly as with the switch off.

Exempt, unchanged: no brief bound, described asks without a literal, story/editorial builds (they keep the old unrequested-text check), research ops (always kept: her own research ask), pinned programs replayed from a stored snapshot, and chat/editor edits.

With the switch on, a title-only or style-only brief's extra line is also dropped instead of failing the render, so `test_approved_hook_journey.py::test_bound_styling_composition_adds_text_only_when_live_brief_requests_it[False]` (the old raise) holds only with the switch off. Known limit: a copy the composer adds next to her planned bar is dropped even when the planned bar is shorter (a 2.2 s title); the composer must retime or move that bar, and a whole-video line belongs in the planner's pin. Known limit: an emoji the composer adds inside a line of her words is kept with that line ("best day ever 😍" for "Best day ever"); dropping it would drop her words too, and the pass drops whole lines, it never rewrites one.

Default off and byte-identical off (guard: `tests/services/test_creation_text_asked_only.py::test_switch_off_keeps_the_composer_line_exactly_as_written`). Apply: fly secret `TEXT_COMPOSER_ASKED_ONLY_ENABLED=true` on api + worker, then restart. Related: KRI-767 (copy guard).

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

### One latest editable document

A draft head is current only while its stamps match the variant's generation (or the generation a
phone publish was made from; `app/kria/editor_head.py`, read-only). **The server never moves a
head when the generation moves**: approval execution, phone publish, speech cut and every other
writer leave the head rows as they are, and a head on an old generation is not current and every
reader ignores it. The server never merges draft content. `GET /draft` adds `head_current` and
`base_generation`; the phone converges on the latest and replays its own staged AI edit.
On a variant that has only `render_finished_at`, a head stamped with that value (written by this
branch, or by `main`'s plan-review undo) is current; `main`'s draft PUT refused such rows and
this branch accepts them.

- **The one server write** is at Save: a v2 variant whose pinned `editor_draft` is not current
  (409 `editor_draft_unavailable` forever before) has the PINNED row replaced by the committed
  document (`editor_empty_drafts.replace_stranded_saved_draft`; the pin moves from that row to
  its replacement and nowhere else, and no head is touched). The timeline GET serves the same
  document without writing. An empty-state variant keeps its 409.
- Editor-commit 409/412 carry `problem {code, recovery, current_base_generation}` beside the
  unchanged `detail`; `reload` only for `baseline_conflict`, `GUIDED_REVISION_STALE`,
  `deletion_target_stale`.
- Heads already left behind: `python -m app.cli.repair_editor_heads` is a dry run by default;
  `--apply` needs `--variant` and `--operation {retire,keep-ai-lanes}`, refuses a current head,
  and needs the owner's approval before a production run.

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

### Partial apply and repair (KRI-1386, KRI-1387)

A rejected op rejects only itself. The confidence gate reads the model's own
confidence, so one bad value no longer turns a whole edit into a question. The
planner binds `editor_compile_check` (`validate_ops_per_op` on a detached copy of
the job and projected variant) to the copilot, so an op the compiler refuses is
found inside the turn instead of failing the draft later. A bulk media op and
`patch_text_appearance` stay atomic: when one fails, its siblings are held back.

Every rejected op the model could fix goes back to it in one `## REPAIR` block
(`repair_feedback`), with the open op families or the valid index range where
those fix it. A limit inside an open family (a text that follows its narration
timing) is named, not re-asked, because asking again only repeats it. The model
answers with corrected ops tagged `"repairs": "r<n>"` or an unmet entry. Kept
ops are never re-asked. `run_copilot_turn` runs at most
two repair rounds and only while one more copilot call fits before the turn
deadline (one round for a stateless request). Each round is its own `agent_run`
with `repair` in `input_json`. A rejected op no round fixed is named in
`unmet_requests`, so the reply and the phone receipts say it was not done.

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

### Two questions in one reply (KRI-802)

Every answer re-plans the whole request (Main Creator included, 1-2 min), so
questions asked one per turn cost the creator one wait each. For an app at
`X-Kria-Client-Protocol` >= `kria_question_batch_min_client_protocol` (14), the
submit route stamps `answers_batched_questions` on the user message and the turn
plans with `batch_questions`: a title-wording question (`_plan_creator_action`,
KRI-506) no longer ends the turn before the clip checks. When those end on a plain
clip picker, `_with_wording_question` sends ONE reply with both the
`choice_question` and the `clip_question`; the app holds the first answer and sends
both on one turn (`choice_selection` + `clip_selection`, already accepted together).
Any other ending (a draft, a text question, another choice card, a refusal, a
"still checking" reply, a picker about the title's own words, a reply over 1200
characters) keeps the wording question alone, as before, so nothing is drafted with
unapproved words. Not batched, by design: the label-text question (a typed answer
cannot ride a tap), the held-draft "simpler version?" choice (it reads the compiled
draft) and order/length choice cards (they need resolved clips; one choice slot per
reply). Older builds and the web keep one question per turn. Kill switch:
`fly secrets set KRIA_QUESTION_BATCH_MIN_CLIENT_PROTOCOL=999` (api + worker) +
restart. Tests: `tests/kria/test_kri802_question_batch.py` (+ `_postgres`).

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
  16 per turn (32 with `BRIEF_LONG_REQUEST_ENABLED` when the batch's JSON is within
  9,000 chars); a larger batch is refused whole, never truncated. Versions are written by the turn-completion
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
  render, or the turn carries a rebuild verdict. Otherwise `editor_ops`. No
  wording decides a rebuild (KRI-1392): the brief extractor's `request_scope`
  does where it runs, and the copilot answers every other rendered turn with
  ops, a question, or a refusal that offers a fresh version. Replying with the
  word that offer quotes ("redo", "yeniden yap", "nochmal") re-plans; an
  editor target that cannot be opened never re-plans, and its reply carries the
  same offer. Evidence: `docs/reviews/kri-1384/routing-cues.md`. With a render present the Main Creator runs first (it
  extracts the requirements) and the copilot only when the router says
  `editor_ops`. `_validate_draft_plan` rejects an editor-ops-only plan when the
  route is `replan`.
- **Request.** With the flag on, the Main Creator and the clip-intent planner
  read `render_brief_request(brief)`, and the approval dispatch passes it as
  `creator_request` instead of the draft summary.
- **Approved hook wording (KRI-945).** Approving a generated wording ("Use this wording")
  writes it into the one live `text`/`title` requirement's `literal`, marked
  `facts.approved_copy = {target, text}` (`creative_copy_decisions.bind_approved_copy`, added
  by the planner to the turn's updates and persisted in its brief version). Only a title ask
  stated no later than the approving tap is filled, so "Start fresh" or a newer hook ask never
  inherits old words. The marked literal is withdrawn when the fold stops approving it (revise,
  skip, new footage) and replaced by a newer approval; words anyone else wrote are never
  touched. Readiness then proves the hook with the literal check.
- **Whole-video text the render draws (KRI-945).** A `global` literal missing from a draft's
  texts is `met` when the draft renders on a phone route whose writer runs
  `phone_plan_repair.repair_plan_for_contract` (unified, user-song and lip-sync montages;
  `contract_text_drawn`) and the contract pins it with a role that repair always places. Not
  for the spoken-excerpt, voice-behind-footage or voiceover writers, cloud renders, or an ask
  with facts of its own (a position, a clip, a spoken-word cue): those keep "That exact text
  isn't in this draft." `assess_readiness` takes `creator_id` to resolve the route; a caller
  without it gets no such evidence (fail closed). Pins: `tests/kria/test_kri945_*`.
- **Automatic simpler drafts.** The runtime planner removes only typed, unavailable
  optional text/SFX fields and recompiles the remaining strategy. On a phone Talking edit
  with its title lane open, one whole-video top-corner text is the opening title instead
  of an omission (KRI-1009, `strategy_policy._talking_top_text_as_title`). At draft
  completion, unquoted native/Talking clip labels and typed brightness requests can be
  superseded automatically when they are the only blockers. Text the creator quoted (a
  brief `literal`) never is: the draft is held back with the "simpler version?" question
  naming it and the compiler's reason, and only the tap drops it (KRI-1009).
  Receipt/readiness failure alone is insufficient: a missing supported label or
  spoken trigger still needs repair/clarification. The same transaction
  appends an omission revision, rebuilds approval readiness, and pins the effective
  brief. The reply and approval name what was left out and why; they do not claim the
  creator already agreed. Unknown evidence, source-audio restrictions, selection,
  ordering, timing conflicts, and unavailable footage remain blocking. Existing
  explicit simpler-version choices still work. Regression coverage:
  `test_automatic_strategy_fallback.py`, `test_runtime_automatic_simplification.py`,
  and the EN/TR phone/cloud cases in `test_approval_readiness_postgres.py`.
  Evidence and repeatable native verification: [automatic supported-version recovery](../reviews/automatic-render-fallback.md).
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
- **Ask ledger (KRI-1563, `KRIA_ASK_LEDGER_ENABLED`, default true).** An editor turn's receipts
  and reply come from `app/kria/ask_ledger.py`: every requirement of the turn ends `done`
  (with the change that proves it), `declined` (with a reason) or `needs_choice` (the turn's
  question), and every change in the diff is named on the line of the ask that caused it or in
  "Also changed" (no "first two" cut; the part of a change beyond what an ask named is listed
  too). `AskLedger.gaps` checks both against the reply actually written and logs
  `kria_ask_ledger_gap` (lane names and ids only). Proof is per field: a caption ask that names
  a field ("in the middle", a font, a colour) is proved by that field only, and a place is
  checked against where the captions end up (0.82 is not "middle"). A title the turn added
  proves "add a title" whatever wrote its words (whether model words may be applied without
  approval is KRI-1560's rule); a removed title proves "no title" (KRI-1355); "only my voice"
  is the music off (KRI-1472). The diff reads the background behind a Talking speaker (pictures,
  fill, corner, full-screen windows), colour moments and a Talking song. A copilot turn with no
  ops answers each part (`no_op_ledger`; receipts ride the `assistant_response` event), and on a
  failed-render correction a part the copilot could not do no longer throws away the in-place
  ops for the others. Replay: `make kria-replay FIXTURE=kri1563_ask_ledger`.
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
- **Start / end lines are a trim (KRI-789).** "Start at "…" and end right after "…"" inside a
  take is one `timing` requirement with `facts` {`start_line`, `end_line`} (the creator's
  exact quoted words; either alone), never an `order` requirement (Main Creator v65,
  extractor v13). Not `start` / `end`: those are route keys, and `unified_montage.brief_view`
  merges every requirement's facts into the route title. `app.kria.take_trim` re-files a
  v64-shaped reply (key-less `order` with quoted `start` / `end`) on a one-video edit, and
  `choice_questions.one_clip_edit` keeps both the order_basis question and the render
  contract quiet about any free-form order rule on one clip.
- **Clip speed (KRI-689).** A chat speed ask at creation ("speed the river shots up 2x",
  "slow motion on the pour", "klipleri 2 kat hızlandır", time-lapse) is planned on a phone
  Montage. `kria/speed_ask.py` reads it into a `SpeedSpec` (`facts.playback_rate` first --
  Main Creator v95 / extractor v35 record it -- else the words incl. the number; "slow motion"
  = 0.5, "speed up" = 2; held to the native 0.25-4x). `unified_montage.brief_view` carries the
  specs and `plan_unified_montage(clip_speed=...)` grounds each to its cuts
  (`_ground_speeds`: a `clip:<id>` scope, the creator-pointed moment, one clip's analysed
  moment, "the last clip", else every video cut) and writes `FastMontageCut.playback_rate`;
  `compile_execution_plan` carries it onto the moment and `phone_guided_plan` emits `rate` +
  `variableSpeed`. A planned cut keeps its OUTPUT length (no drift): its source window becomes
  `length x rate` on the same start, lowered to what the footage covers when it runs out; a
  creator-approved window keeps its edges and its slot follows the rate. The plan record's
  `clip_speeds` rows drive the receipt (`brief_checks._check_clip_speed`: "Clips 1, 2 and 3
  play at 2x" / "Her video klibi 2x hızda oynuyor"; partial for a held or lowered rate; honest
  "Couldn't" for the locked cases: no verified `variableSpeed` (`phone_rollout.clip_speed_planned_for`),
  a song montage, a frozen frame, a photo, a speed ramp; "which clip?" is the creator's call).
  The pre-approval plan (`phone_montage_preflight`) returns speed receipts whatever their
  status so the draft says it before the card; `clip_speed_notice` keeps the one-line heads-up
  only where no plan can carry the speed. A record without `clip_speeds` (written before this)
  keeps the original "Couldn't". The finished-video review re-judges the ask against the
  variant's `story_timeline` (`playback_rate` on the moment rows). Still open: narrated /
  Talking formats, the web editor, a device acceptance pass. Pins:
  `tests/kria/test_kri689_chat_speed_spec.py`, `tests/kria/test_kri689_clip_speed_ask.py`,
  `tests/kria/test_kri689_speed_notice_postgres.py`, `tests/tasks/test_kri689_speed_review.py`,
  the KRI-689 groups in `tests/tasks/test_unified_montage_dispatch.py` and
  `tests/pipeline/test_phone_guided_speed.py`; failure modes:
  `docs/reviews/kri-689-chat-speed-failure-modes.md`; contract: `plans/playback-speed.md`.
  *A clip named by its number (Leverkusen M2 run 3)*: "put Taha's scream at the end of clip 2
  in slow motion" lost "clip 2" (an ask word plus a number) and found no "scream" in any record,
  so the speed was `not_grounded` ("which clip?"). `speed_ask._clip_number` now reads the app's
  own numbering ("clip 2", "video two", "2. klip", "klip 2"; never "2 clips" / "2 klip", "2x",
  seconds; two numbers name none) into `SpeedSpec.clip_number`, and `plan_unified_montage`
  maps it through `clip_numbers` (`app_clip_numbers`: the item's clip rows, Visuals never
  counted). The words left ("taha scream") must then be shown by that clip's cut
  (`named_moments.locate_in_clip` over the clip's stored moments, one content word, place words
  like "end" / "moments" ignored; `shows`): otherwise the row is `moment_not_found` /
  `moment_not_shown` / `clip_not_in_edit` and the receipt says so naming the clip ("I couldn't
  find that moment in clip 2, ... I can slow down all of clip 2 instead"), never a "Done" on
  footage that isn't the scream. With approval readiness on (that account), the runtime skipped
  the KRI-775 plan, so readiness said "I'll point it out after"; `readiness_plan_blockers` now
  runs the same plan for a speed ask and `assess_readiness(plan_blockers=...)` takes its checked,
  creator-fixable miss (never a renderer limit, never a pass, so an approval re-check that runs
  no plan always agrees with a ready draft). Guard:
  `tests/kria/test_leverkusen_m2_run3_moment_window.py`.
- **Curved text (KRI-1029).** "Curved title X on the palms" / "kavisli başlık" becomes the
  brief fact `text_shape` (`curved` / `straight`), stamped by `brief.apply_updates` from the
  requirement's own description (`kria/text_shape_ask.py`: folded tokens, the shape word must
  touch a text noun, so "label on the curved bridge" is not one; never the literal) and kept
  across rewrites (`STICKY_FACTS`), so "make the title just X" no longer drops it. A phone
  Montage bends the matching title / label (`CURVED_TEXT_BEND`, `ClipLabel.bend` /
  `EditProposalSnapshot.title_bend` -> `TextElement.bend` -> `PortableTextLayer.bend`) only when
  `phone_rollout.text_bend_planned_for` holds (kill switch, `textBend` verified, the drafting /
  approving build >= `text_bend_min_client_protocol`); otherwise it stays straight and the
  record's `text_shapes` row says why. The draft (pre-approval check) and the review say "Done
  (curved)" or "it stays straight" (`brief_checks.check_text_shape`), never the model's promise;
  the creation-text composer's "nothing to compose" gate judges the words only
  (`text_shape_ask.words_only`: it can't curve text, so a straight title never calls it).
  No prompt change: a model-written `text_shape` is honoured first. Guard:
  `tests/kria/test_kri1029_1030_plaza_title_replay.py`.
- **One title per opening clip (KRI-1030).** A confirmed `opening_title` with the same words as
  the first cut's own creator label (a re-draft's title beside the brief's "title on the palm
  trees") is not drawn again: `plan_unified_montage` keeps the label, at the creator's spot, and
  records `title_shown_as_label` (receipts read that label as the title). The label covers
  the opening from 0 s, so the contract's `opening` role and `phone_plan_repair._shows` accept
  it. Not folded when the title has a typed hold or an entrance animation. Done where the
  snapshot is built: `guided_story` recompiles stored plans for equality
  (`validate_execution_plan`), so old approvals stay byte-identical. Likewise a global brief
  literal that is one of the creator's own labels ('label "Casa Rosada" on the pink building')
  is never also the opening title (`_title`'s `creator_label_texts`).
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
  nothing tappable. Blur is not a measured clip-record fact: a sharpness probe on 170
  kit clips could not tell M1_04 from its sharp look-alike. KRI-845 adds the analyzer's
  own judgment instead (`visual_quality`, with `props` / `actions`; see
  `docs/pipelines/clip-understanding.md`). Guard:
  `tests/kria/test_kri807_unmatched_exclusion_picker.py`.
  *Filming order and taps (KRI-1026, Plaza de Mayo M1)*: a whole-set order (`order_by`
  capture_time / route) assigns every clip and names none, so it never protects a clip
  from an exclude (`clip_scope.excluded_clips`; before, "in the order we filmed it" kept
  every clip, and a title-only re-draft with an empty pick played both clips the creator
  had tapped out). A tapped (or file-named) exclusion also beats a resolver guess for
  another ask on the same clip; only another tap, or media the strategy places, keeps it.
  Any resolved exclusion whose clips the drafted pick still plays is said in the draft
  ("I couldn't leave out clip 2, so it is still in the video": `still_played_exclusions`).
  Guard: `tests/kria/test_kri1026_plaza_exclusions.py`.
  *Too short to use (KRI-1031)*: a length-only skip ("anything too short to use", "the
  mis-taps", "nothing under a second") is an `exclude` with `by_length: "too_short"` (and
  the creator's own seconds in `under_s`), clip-intent planner v2026-10-28. The server
  settles it from clip lengths, after the creator's taps and before the resolver
  (`app/services/too_short_clips.py`): with `under_s`, shorter than that; otherwise under
  1.5 s and under a quarter of the median own-video length (so a set of quick ~1 s clips
  stays whole). Photos and Visuals are never judged; an unknown length sends the ask to the
  resolver as before. Evidence `clip length`; never a picker. The draft names the clips
  ("I left out clips 2 and 4 because they are too short to use.") or says none is too
  short. Guard: `tests/kria/test_kri1031_too_short_clips.py`.
  *Voiceover ties (KRI-962, Don Julio walk-in N1)*: `app/kria/clip_ties.py`. In a
  Voiceover edit (`_is_voiceover_strategy`) each named shot sits on one moment of the
  voice, so a resolved `include` whose top-confidence footage matches are 2+ videos no
  other ask needs is asked with the picker before the draft (N1: "the DON JULIO sign"
  matched the window sign and Atakan at the plaque, both 0.9; both really show a Don
  Julio sign). A video is needed elsewhere when it is another ask's only footage match
  (N1's "the grill" matched three clips, two of them the only flowers and butcher clips:
  no question). Photos, clips an exclude leaves out, an include matching every footage
  clip (the whole set), order/group/caption asks and shots the creator already answered
  never count. The tied clips come pre-ticked, every clip not left out is offered, and the
  rows ride in the card the turn already asks (`with_tie_categories`). The tap settles the
  shot through `fold_clip_selections` / `apply_creator_selections`; keeping both ticked
  leaves the narrated alignment to place them, as before. Of the 6 prod Voiceover jobs with an
  alignment record (2026-10-08..27), only N1 would have been asked. Kill switch
  `KRIA_VOICEOVER_TIE_QUESTIONS_ENABLED` (default on; needs the picker flag). Guard:
  `tests/kria/test_kri962_voiceover_tie_question.py`.
  *A named shot is never "missing" before Kria looks (Leverkusen M2)*:
  `app/kria/missing_media.py`. The Main Creator reads two moments per clip, so "the TOR on
  the big screen" (1.5 s inside an 18 s goal clip) got either a strategy (and a vision
  check) or `AskUser(missing_media)` "Build without it / I'll upload it" on the same input.
  Now an `AskUser` whose machine reason code says footage is missing (`missing_media`,
  `shot_not_found`; never a voiceover/song code, never read from the ask's prose or
  options, KRI-667), on an item with clips (not Talking/Subtitled) runs
  the same planner, resolver and vision checks first (`_look_inside_clips_first`, sharing
  `_plan_turn_clip_intents` with the strategy path; answers cached). Unsure or not found:
  the clip picker. Budget stop: still checking. Every named shot found: a card saying where
  ("found every shot you named ... Should I build the edit with them?"). No shot read: a
  question naming the clips. The Main Creator's ask (upload included) stands only once
  every clip was checked: the creator tapped "None" for it, or already answered that card.
  On with `CLIP_INTENTS_ENABLED`. Guard: `tests/kria/test_leverkusen_m2_missing_shot.py`.
- *A moment named inside one of several clips (Leverkusen M2 run 3)*: "then the TOR on the
  big screen" (the creator tapped clip 2, an 18 s goal clip, for it). KRI-723's pins cover one
  take only, so the window was the clip's opening (its record had `best_moments: []`:
  `analysis_fallback`) and the row was "met" because the clip was in its place.
  `named_moments.clip_moments` locates each stated-order row's words inside each video it names
  from the stored record alone (`locate_in_clip`); a located moment seats that clip's window
  through the length plan's described moments (`_enrich_best_parts_snapshot`), and the
  finished plan judges it (`_clip_moment_miss`): met only when a cut shows it or plays the whole
  clip; `not_shown_in_clip` when located but not shown; `not_found_in_clip` when no record
  places it and the creator's tap is the only evidence (a row the resolver matched from the
  record keeps its clip-level verdict). Both are KRI-723-style limits (a note naming the moment
  and clip, `_MOMENT_NOT_IN_CLIP`, never a hold-back that would drop the whole order), read on
  the filming-order path too. The pre-approval reply says it first (`_clip_moment_notice`: "I
  couldn't find the "TOR" on the big screen in clip 2 on my own, so clip 2 plays from 0.0 s to
  5.6 s and may not show it."). Still open: no model localizes a moment a record omits.
  Guard: `tests/kria/test_leverkusen_m2_run3_moment_window.py`.
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

## Voiceover script before a recording (KRI-815)

With footage attached and no voiceover, a request for words to say over the clips gets a
script, never a narrated draft that cannot render yet:

- Main Creator may answer `draft_voiceover_script` (only offered when
  `MainCreatorInput.voiceover_script_available`: flag on, a video clip attached, no
  `item.voiceover_gcs_path`). Its `request_evidence` must be the creator's own words, or the
  turn asks instead. The model never writes the script.
- A narrated `audio_strategy: "voiceover"` proposal with no voiceover attached takes the same
  path before the strategy check (`_plan_creator_action`), so no draft, approval or Job is
  minted; claim-time `voiceover_required` stays as the backstop.
- `app/kria/voiceover_script.py` builds one prompt line per attached video clip from the
  stored clip understanding (no new vision call), runs `nova.voiceover.script_writer`, and
  persists `voiceover_script` on the `assistant_response` payload: `script_id`, per-thread
  `revision`, `source_media_ids`, `item_id`, `language`, `needs_check` (the writer's list plus a
  backstop for numbers and names absent from the inputs) and the `record_or_import`
  continuation. The event `content` also carries the script, so clients without the card read it.
- A revision keeps `script_id` and `source_media_ids`, bumps `revision`, and writes no brief
  version. A writer failure is `status: "failed"` with no text.
- The app shows the card when `voiceover_script_cards` is advertised; Record / Import open the
  voiceover step with the script to read. The upload uses the normal, ownership-checked voiceover
  attach; the next turn plans the narrated edit as usual.
- Kill switch `KRIA_VOICEOVER_SCRIPT_ENABLED=false` (api + worker restart): no script, but a
  narrated plan without a voiceover still asks to record or import instead of reaching approval.
  Independent of `TRANSCRIPT_HELPER_ENABLED`. Journey: `tests/kria/test_kri815_voiceover_script_journey.py`.

## Research before approval (KRI-881)

A creator who asks for sources ("Include sources for the historical facts", T06) sees the
exact sourced wording and its sources **before** the approval card, and the render puts
exactly that wording on exactly those clips.

- **Typed ask.** Main Creator / the brief extractor record one `text` requirement, scope
  `global`, with `facts.research = {topic, request_quote}` (prompt v81 / extractor v23).
  `ResearchRequest` rejects a URL or private-path topic; `request_quote` must be the
  creator's own words (`ResearchRequest.authorize` against the full creator request).
  Never matched from words on the server; "Start fresh" withdraws it like any requirement.
- **When.** `research_preview.with_research` runs after planning, outside every lock, for a
  strategy draft with a render request that renders through the unified phone montage. It
  builds the worker's own plan (`phone_montage_preflight.preflight_montage_plan`, shared with
  KRI-775), the composer's model view (`creation_text_composition.creation_model_snapshot`)
  and runs `researched_text.run_researched_revision` unchanged (search, copy, review).
- **Budget.** Research runs under a thread heartbeat that renews the 15 s turn lease
  (`_lease_heartbeat_thread`; planning's own heartbeat has stopped). Inline when at least
  `INLINE_MIN_S` (100 s) of the turn is left; otherwise `tasks.prepare_kria_research`
  (soft 170 s / hard 200 s) runs it and the turn continues (KRI-709, bounded by
  `kria_turn_auto_continue_max_runs`). The deferring run anchors its `draft.apply_strategy`
  arguments to the turn in Redis; the continued run drafts that anchored strategy (a
  re-plan never writes the same wording) when its basis still matches, and waits for the
  in-flight task instead of researching the same draft again (one task per basis, via an
  in-flight marker).
- **Cache.** Redis, keyed by `basis_digest`: research ask + full creator request + live
  requirements + reply language + strategy + media identity + length plan. Only finished
  results are cached and the first one wins (`SET NX`); a timeout is never cached, so the
  next run (or the creator's resend) researches again.
- **Ready.** The reply lists each explanation (`0:11–0:20 · "Troy: …" · Source: whc.unesco.org`),
  the `draft_applied` payload carries `research_preview` (explanations + `{title, url}`
  sources), and `BriefBinding.prepared_research` pins the result under the binding digest
  (absent when unused, so stored bindings keep their digest and wire shape).
- **Failed / unmet.** A checked `not_possible` receipt with the precise reason joins the
  KRI-775 hold-back: "a different approach, or this simpler version?" No approval card.
- **Render.** `_run_phone_unified_montage_job` reads the pinned result from the approved
  binding and `compose_creation_text(prepared_research=…)` replays it through
  `researched_text.pinned_research_operations`: same consecutive clips, inside their render
  windows, same placement rules, approved text protected from the composer. No search, no
  rewrite. Song-lyric caption rows are hidden from placement exactly as they were for the
  preview, so they are never removed or treated as text to avoid. A plan it no longer fits
  fails the job naming the explanation.
- **Never speech.** Sourced text is `add_text` rows with `research` provenance, never caption
  cues, narration or voiceover inputs.
- **Scope.** Unified phone montage only, on both draft paths. Since KRI-945 a KRI-721
  readiness draft is researched too, and readiness judges the research requirement on the
  result (`brief_checks._check_research`): `met` only when the pinned explanations were
  researched for exactly that requirement, `unsupported` with the precise reason for a
  research limit, `evidence_missing` when nothing was researched. The approval re-check reads
  the same result from `BriefBinding.prepared_research`. Guided proposals already research
  before their offer; follow-up chat research already shows a draft first.
- **Kill switch.** `KRIA_RESEARCH_PREVIEW_ENABLED=false` + restart api + worker: nothing runs,
  no field is pinned, render-time research is unchanged.
- **Proof.** `tests/evals/request_following/test_kri881_research_before_approval.py` (real
  runtime turn over scratch Postgres + real phone worker, recorded model I/O) writes
  `.dev/kri-881/report.json`.

### Keeping some of the facts (CA-5, KRI-1644)

The creator ticks the sourced facts to keep before approving. `app.kria.research_selection`
saves the narrowed draft and asks for a new approval inside the request: no planner, search or
model call.

- **Preview.** With `KRIA_RESEARCH_SELECTION_ENABLED` on and a drafting build at
  `research_selection_min_client_protocol` (58), `research_preview` carries `selection_id` (the
  pinned `basis_digest`) and a `fact_id` per explanation. A heading and its detail are one fact.
  The id is a hash of the fact's own rows, so a kept fact keeps its id in the narrowed draft.
  Text-layer research (word sequences, translations) carries no ids. A layer answers an ask of
  its own, and a translation names its source by index.
- **Request.** `POST /creation-threads/{id}/turns` with `research_selection: {selection_id,
  kept_fact_ids}`. `message` is a placeholder that is never planned or stored. A build below the
  minimum gets 426 `client_update_required`. With the flag off the key is ignored and the
  message runs as a normal turn.
- **Stale.** 409 `research_selection_stale` when the pending approval's head draft does not pin
  that `selection_id`, or when `research_preview.selection_basis_sync` no longer reproduces it.
  That function is `for_draft` over what the draft and its researching turn persist, plus the
  item's ready Visuals now. The draft commit offers ids only when the same function reproduces
  the pinned basis, so an offered card never reads as stale on an unchanged draft. A Visual or
  slot window that moved since research is stale. So is an approval already decided or expired,
  a media stash or staged tap on it, and a follow-up already queued (that last one is 409
  `queued_successor_exists`).
- **Write.** One transaction, locks in `decide_approval`'s order (Plan, PlanItem, Job key
  share, Session, Turn, Draft, Approval, Execution, Thread). It cancels the old approval (an
  `approval_cancelled` event names it), marks its execution `stale` and its turn `superseded`,
  then saves revision N+1. That revision is the same document with the binding rebuilt around
  the kept research, under a recomputed digest. A saved readiness is re-assessed on the kept
  research, and the brief, media and capability digests must not move. The selection's own turn
  awaits the new approval, so the fingerprint changes. Its user event is `inert` with no
  content. The `draft_applied` reply lists the kept facts and carries `research_selection:
  {selection_id, kept_fact_ids, waived}`.
- **Idempotent.** Selections only narrow. An id the current draft does not show is dropped. When
  nothing narrows, nothing is written and the reply is the awaiting turn with `replayed: true`.
  The same holds for repeating an empty list on a waived draft. Only a literal empty list waives:
  ids that match no shown fact (an older card) are stale. Undo on the selection's reply restores
  the full research and closes its card, like any draft's Undo.
- **Waived.** Keeping none pins `PreparedResearch(status="ready", texts=[], waived=true)`. A
  pin must stay: without one, the render's composer researches again.
  `pinned_research_operations` replays nothing for it. `brief_checks._check_research` records
  a checked `met` receipt whose reason names the creator's choice, so readiness still approves.
  `waived` is omitted when false, so every existing binding keeps its digest.
- **Capability.** `research_selections` on `GET /creation-threads/capabilities`.
- **Proof.** `tests/kria/test_research_selection.py` covers the real draft turn, selection,
  approval (readiness on and off) and phone worker pin on scratch Postgres. Its failure-mode
  list is the module docstring.

## Render consent

Every initial render and rerender requires a separate, expiring approval pinned
to the current draft, manifest, ownership epoch, Job/variant/generation, and
expected revisions. “Make this” may prepare the approval but cannot consume it.
Approval consumption and exact editor/Job commit happen atomically; broker
publication follows the commit through the existing deterministic Job dispatcher
or editor rerender task. Accepted editor executions persist enough dispatch
metadata for the reconciler to publish the same generation after process death.

## On-screen text shown in sequence (KRI-1038)

A quoted sentence the creator wants shown a word at a time is a `text_sequence` brief requirement
(`kria/text_sequence.py`), not `captions: word_by_word`. The Main Creator prompt teaches it;
`refile_text_sequence` only re-files a captions reply when the creator did not name captions, no
voiceover is recorded, and the message does not talk about words said in the footage
(`names_footage_speech`: "when I say", "dediğim yerde").

Deploy order: roll api and worker together. A thread stored with `text_sequence` fails
`CreativeBrief` validation on an older api or worker machine (autostopped machines, or a revert),
and this kind has no kill switch of its own.

## Compose with the editor ops (KRI-1039, `KRIA_COMPOSE_UNMET_WITH_OPS_ENABLED`)

The copilot's ops are the base layer: an ask a person could do in the editor is composed with them,
not refused, asked about or re-planned.

- A `text_sequence` ask is one `replace_text_sequence`: windows are weighted by character count
  (`weight: "length"`, the default; `"equal"` keeps equal windows), and `mode: "cumulative"` builds
  the sentence up (step k shows the first k words, children carry `source_params.sequence_mode`,
  the full sentence holds the last quarter). Both fields are written into the op when it is
  authored, so replaying a saved draft never depends on the flag. Evidence
  (`sequence_text_evidence.complete_sequences`) reads a cumulative chain as its last step.
- Creation: Main Creator plans the footage; `compose_creation_text` is the one op pass on the
  new draft, in the same render job, and it reads `text_sequence` requirements. `check_requirement`
  judges one (`PlanFacts.sequence_texts`): met when the words are on screen in pieces, otherwise a
  neutral receipt, so an uncomposed sequence is never a creator question.
- Follow-ups: an edit-shaped turn on a draft that has a render goes to the copilot with no
  `nova.creator.main` call; `replace_text_sequence` is on the fast path. A job that FAILED before
  producing any variant has no editable lanes (prod job debug: no `variants` key), so its follow-up
  is still a re-plan; the fix for that case is that the render no longer fails (title fit, lane A).
  This includes text-shaped asks ("skip the selfie label", KRI-1103): a text ask is a failed-render
  correction (staged on the editor head, or refused honestly) only when the failed job has a variant
  or an editor draft built on it; the pre-render strategy draft alone re-plans.
  The re-plan cues are unchanged.
- Readability floor: an op authored with `weight` gives every piece at least 0.25 s
  (`_MIN_PIECE_S`). When the source window is too short the compiler lengthens it, up to the end of
  the video or the next text in the same role and place; if even that cannot hold the pieces, or the
  video length cannot be read, the op fails naming the seconds needed and left (never an unbounded
  stretch).
- Capability parity: `kria/editor_capability_parity.py` maps every public action of
  `NativeEditorSession.swift` and the files extending it (`NativeEditorRetiming.swift`,
  `NativeTextEditing.swift`) to agent ops, `agent_unsupported` (with a reason) or `not_an_edit`;
  `tests/kria/test_editor_capability_parity.py::test_ios_editor_actions_have_agent_ops` fails when
  an action has none. `test_every_agent_op_runs_from_iphone_chat` (KRI-1388) runs every op of every
  `agent_ops` group through the real parser and compiler on a device snapshot, and
  `test_an_unsupported_group_is_refused_on_a_phone` fails when an unsupported group starts to run.
  Verdicts and evidence: `docs/reviews/kri-1384/parity-on-phone.md`.

Default `false` (off until enabled). Flag off restores the earlier routing, cues, fast-path set and op shape
(`test_compose_unmet_flag_off_is_byte_identical`). Apply: fly secret + restart api and worker.

## The approval gate on a new 29 Ekim thread (prod 64bb9853)

With every lane above live, the same first message in a new thread was still a question
("Bunu henüz yapamıyorum ... Daha basit sürümde çıkarılacak"). The brief was right (prod stores it
redacted; it was rebuilt and proven exact against `approval_readiness.brief_digest`): r1
`select {all}`, r2 `order {key: random, seed}`, r3 `text_sequence {unit: word, mode: agent_choice}`.
The approval-readiness gate refused it for reasons the offline scorecard never reached, because it
replayed hand-made strategies, not the live clip-intent planner or this gate:

- r2 `needs_choice`: the live clip-intent planner wrote `{"op": "order", "attribute": "all
  content", "order_by": "random"}`. `order_by` only knows `capture_time | route`, so it was nulled
  into a positionless order group over all 28 clips with `ordered_members` unset, and
  `_check_order` reports such a group's inner order as "wasn't recorded" before it looks at the
  random key. Now an `order` whose `order_by` is a random / any order is dropped by the parser
  (`order_mode_dropped`; the brief owns that ask), and under a brief random / any-order ask a
  positionless group with no stated inner order is `ordered_members: false`
  (`order_mode.confirm_unordered_groups`, in `planner.brief_order_fields`), so a model that writes
  null instead lands the same way.
- r2 next in line, `evidence_missing`: a strategy draft carried `ordering_choice: "random"` but the
  gate read the order from `strategy.ordering_basis` only. A draft the phone unified montage writer
  renders now commits to `ordering_basis: "random"` (`approval_readiness._planned_order_basis`);
  that writer draws the stored-seed permutation and its own receipt checks it after render.
- r3 `evidence_missing`: a `text_sequence` is composed by `compose_creation_text`, which runs after
  approval in the render job, and is judged on the finished text lane. A strategy draft has no
  text lane, so the gate asked before the step that does it had run. It is now `unverified` on a
  strategy draft (approvable, named for after the render); an editor draft still proves it from
  its ops.
- Latent: the same planner answer also minted a `caption` on "each word" carrying the whole
  sentence, which the resolver put on all 28 clips (a static copy under the word-by-word sequence).
  A caption whose exact words are a live `text_sequence` literal is dropped
  (`without_whole_video_captions`, `sequence_texts`). The clip-intent planner prompt now says both
  asks are no clip operation.

Proof: scorecard check 6 (`make kria-replay FIXTURE=29-ekim`, also
`tests/kria/test_29ekim_live_approval_gate.py`) replays the live Main Creator, clip-intent planner
and resolver answers to that exact message (`29-ekim/live_new_thread_t1.json`) through the real
parsers, clip-intent planning, the planner's order fields, `assess_readiness` and the choice gate,
plus a variant of the planner answer without `order_by`. On main it reproduces prod's dispositions
exactly (r1 unverified, r2 needs_choice, r3 evidence_missing).

### Filming order with a first / last group (Leverkusen M1 run 2)

"In the order I filmed it, starting with the train ride" hit both gates above with a filming-order
key instead of a random one (prod thread 1a2189cc, 2026-10-30):

- `needs_choice`: "the train ride" was a `first` group of three with `ordered_members` unset, so
  `_check_order` asked how to order it. Under a filming-order ask (`CAPTURE_ORDER_KEYS`) the planner
  seats a group's members in filming order and the contract pins it, so
  `confirm_unordered_groups` now marks first / last / positionless groups `ordered_members: false`
  there too (random / any order still marks positionless groups only).
- `evidence_missing` ("I can't confirm the order this draft uses"): every filming-order draft on
  the phone hit it once the gate was on. `_planned_order_basis` now also returns `capture_time`
  when the render contract pins a capture-time order (every clip and Visual shown is dated) and
  the phone unified montage writes it; the phone verifier refuses any other order after render.
- The leftover ask (a group with no basis order, e.g. a pin-only "start with X") names the group
  in plain words; "saved group" no longer reaches a creator.

Proof: `tests/kria/test_lev_m1_filming_order_group.py` replays the run's brief and approved media
snapshot (both proven exact against the turns' `brief_digest` / `media_digest`) through the
planner's order fields, `assess_readiness` and the worker's own phone montage plan.

## After approval: the words need a bar to split

With the gate fixed, the approved job failed every render as `creation_text_failed` ("The text
couldn't be added"). `unified_montage.brief_view` took the opening title only from a `text`
requirement, so a `text_sequence` seeded no title bar (`title_source_exists` was False) and the
phone worker handed the creation text composer `text_bars == []`. The composer then answered
"the text must already be established before it is split" (pro) or "I couldn't find a title"
(flash), and `_check_added_text` refuses an `add_text` when no live non-title text was asked for.

- `brief_view` seeds `title_literal` from a live `text_sequence` literal in `title` / `global`
  scope (a `title` text literal keeps the bar). Everything that reads the title from it follows:
  `_title`, `title_source_exists`, the contract's exact text, `snapshot_copy_constraints`.
- The composer splits that bar itself (`set_texts_timing`, then `replace_text_sequence` with one
  word per segment). There is no server-side split and no repair of its answer. Prompt
  `v16-text-sequence-title-bar` says the sequence is the title bar, mode `agent_choice` means
  replace unless the words should stay, and every piece gets at least 0.25 s. The composer input
  carries `text_sequence` requirements of any scope. The "already met, skip the model" shortcut
  judges them with `brief_checks.text_sequence_shown` whatever the compose flag says.
- A clip-scoped `text_sequence` has no seeded bar. It allows `add_text`, like a non-title `text`.
- The `weight` stretch in `op_replace_text_sequence` rounds its wanted end up to the millisecond
  and compares at microsecond tolerance. A live answer with an 8.1 s window was declined because
  the re-rounded edges measured 0.249999 s ("needs about 12.9 s and the video only leaves 12.9 s").

Proof: scorecard check 7 (no model) and check 8 (every recorded live composer answer in
`29-ekim/live_post_approval.json` through the worker, phone text style and device pin), also
`tests/kria/test_29ekim_post_approval.py`. To capture new answers (paid, development ledger, $1 cap,
needs a throwaway migrated database), run `scripts/kria/capture_29_ekim_post_approval.py`.

## Intent review of the finished video (KRI-1171)

After a render is published, one paid look at a few frames of it (flag-gated, off by default) can add chat events to the thread: an `assistant_response` receipt (rows are ordinary `requirement_receipts` with `turn_value: "intent_review"`, skipped by `GET brief`), at most one `assistant_question`, and a repair staged through `_complete_draft_turn` under a system source event the planner never reads. Pipeline: [`intent-review`](intent-review.md). The runtime contract it relies on: a background event bumps the thread revision, so it is written only into a quiet thread (no creator message since the render, no turn in flight), under the thread lock, once per status.

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

## Provider deadlines and the Main Creator ladder (KRI-1037)

A Main Creator call that ends at our own provider deadline (`ProviderDeadlineExceededError`, a
subclass of `ProviderOutcomeUnknownError`, so the paid-call fence is unchanged) is no longer repeated:

1. rung 1 keeps the spec thinking level (`high`) and gets the shared turn budget minus 75 s;
2. a deadline steps thinking to `medium` for up to 50 s (new paid call, new reservation);
3. a second deadline runs the fallback `gemini-3.6-flash` at `low` with what remains.

The turn budget is the 200 s Celery soft limit minus the planner's 20 s reserve (180 s); the rungs
never exceed it. "Try again" after a deadline starts at rung 2: the failed turn stores
`error.provider_deadline=true` (only when our own deadline fired, not for a transport outcome-unknown),
`_claim` stamps `snapshot["start_degraded"]` when the thread's last finished turn hit a deadline
within 10 minutes AND the new message retries that request (same words after casefold and punctuation
strip, or a bare "try again"; `_is_retry_of`). An unrelated new request inside the window runs at full
strength. The planner reads the stamp through `start_degraded_retry`. Kill switch: `MAIN_CREATOR_DEADLINE_LADDER_ENABLED=false`
plus restart of api and worker (read at import). Truncation now fails as `runtime_output_truncated`
(`TerminalOutputTruncatedError`) with its own copy instead of the generic `runtime_turn_failed`.

The rung sizes (105 s first rung, 50 s medium, 75 s reserve) are not measured on a lighter rung:
the only evidence is the recorded successful `high` calls on this 27-clip set (23.9, 41.3, 52.3 s
single attempt; 77.6 s over two attempts), all under the first rung, while the two failed turns sat
at the deadline for 180 s with 2k output tokens (read as a stalled call rather than a slow success; not proven). Medium and
flash latencies in the `lane_c` replay are assumed. Worst case is three paid Main Creator calls in
one turn (each fenced under its own reservation; the planner is a pure call with no side effects).
Check `agent_deadline_degraded` / `agent_deadline_fallback` log lines and `deadline_degraded` on
`agent_run` events after rollout before tuning `degraded_timeout_s` / `deadline_ladder_reserve_s`.

## A non-Gemini Main Creator (KRI-1385)

Model facts (provider, reasoning efforts, degrade ladder, token price, media and tool support) live
in one table, `app/agents/model_registry.py`. Dispatch, preflight, the ledger price, the comparison
harness and the ladder above read it; `tests/agents/test_model_registry_sentinel.py` fails if any
other module spells a non-Gemini model name. The ladder is the model's `effort_ladder`: a deadline
steps one rung down once per model (Gemini 3 `high` -> `medium`, Sol `medium` -> `low`), and a
truncation or a post-deadline fallback runs on the lightest rung.

With `MAIN_CREATOR_MODEL=gpt-6.1-sol` the spec's fallback is `gemini-3.1-pro-preview`. When the next
model is at another provider, any provider or ledger failure moves the turn to it: an outage (a
5xx or a connection that never opened is a `TransientError`, released and retried within
`max_attempts` first), an unknown outcome (held as `unknown` in the ledger, never repeated at that
provider), a 4xx, a spent `planning` budget, or an attribution refusal. A schema or refusal failure
still ends the turn. The fallback starts only with at least 10 s of turn budget left, and runs at a
level its own vocabulary takes (Sol `xhigh` becomes Gemini `high`). Sol's calls spend the `planning`
capability (`AI_OPENAI_PLANNING_BUDGET_USD`), metered even while `AI_COST_CONTROL_ENABLED` is off.
The thinking row streams Sol's `concise` reasoning summary (and Anthropic's `summarized` thinking)
into the same `ThoughtSummaryPublisher`. Evidence and rollout: `docs/reviews/kri-1384/sol-main-creator.md`.

A compact per-clip evidence tier for large clip sets was tried and removed: it saved 9 to 13 percent of
input tokens on a synthetic 27-clip prompt, input size is not the latency lever (thinking and answer
tokens are), and no plan-quality eval covers thinner evidence. Prove the ladder with the `lane_c`
block of `make kria-replay FIXTURE=29-ekim`.

`EditCopilotAgent` has `max_output_tokens=16_384` with a 130 s timeout: a runaway call needs 124.3 s to
reach the cap, so it must stay under the timeout (`test_edit_copilot_budget_runs_out_before_its_provider_timeout`).

## Re-recording a take (`MEDIA_REPLACE_ENABLED`)

`POST /creation-threads/{id}/media/replace` swaps ONE attached speaker clip for a new upload in
one transaction. A client-side remove-then-attach can commit with zero speakers, and on a
multi-clip Talking item an attach adds a second one; the swap keeps the clip count and its slot, so
neither state is ever committed. It is additive: old builds never call it, and they drop the
`media_replaced` event and ignore `state.source_replacement`.

Flag: `MEDIA_REPLACE_ENABLED`, default `false`. Off: the route and `upload-urls`
`replaces_media_id` answer 404 `media_replace_unavailable`, and `media_capabilities.clips.replace`
is absent (not `false`). Apply: `fly secrets set MEDIA_REPLACE_ENABLED=true --app nova-video`, restart
api + worker, then ship the app that calls it. New app against an old server gets that 404, so the app
must read `media_capabilities.clips.replace` first. Rollback: unset the secret and restart.

Request body (`StrictBody`, unknown fields are 422): `replaced_media_id`, `media` (a `MediaInput`,
kind `video`, the proxy reservation id on a device-only thread), `reason` (only `"retake"`),
`client_event_id`, `expected_revision`. Response 200 is the same `CreationThreadOut` as attach. Errors are
`{"problem": {"code", "message", "retryable", "recovery", "current_revision"}}`:

| Status | `problem.code` |
| --- | --- |
| 404 | `media_replace_unavailable`, or a thread code from `_load` (`thread_not_found`, `thread_deleted`, `thread_id_invalid`) |
| 409 | `thread_archived`, `idempotency_key_reused`, `revision_mismatch` (carries `current_revision`), `render_in_progress`, `draft_missing`, `retake_target_missing`, `retake_target_not_speaker`, `retake_speaker_unknown`, `retake_same_source`, `source_already_attached`, `retake_source_unresolved`, `upload_not_finished` (retryable), `upload_target_unavailable` (upload a new source), `registration_changed` (retryable) |
| 422 | `retake_unsupported_format`, `retake_target_not_clip`, `retake_kind_mismatch`, `retake_library_unsupported`, `retake_has_no_audio`, `invalid_media_id`, `path_mismatch`, `device_render_unsupported`, `video_could_not_be_read`, `proxy_geometry_mismatch`, `proxy_verification_failed`, `proxy_kind_mismatch`, `kind_mismatch`, plus the other verifier codes it shares with attach |
| 503 | `identity_unavailable`, `upload_verification_error`, `media_verification_error` (retryable) |

Data changes: the clip row at the same index is rebuilt by the attach row builder (only `shot_id`
carries over; trims, speech coverage and sound toggles describe the old take and are dropped), written
only through `mutate_plan_item_media`, so the speech-cleanup analysis for the old take is superseded,
consent cleared and the edit proposal marked stale. `thread.state.media` swaps the entry in place and gains
`source_replacement = {replaced_media_id, media_id, superseded_job_id, revision, speaker_media_ids}`
(`speaker_media_ids` is who speaks after the swap: until a new Job renders, the old Job's receipt still names the original take, so a second re-record reads this instead). Voiceover, song,
script, and the speaker background are untouched. The old Job is never written: its variants keep
the take they were cut from, and the old take stays in storage (retention and thread deletion own it,
because the old Job and its editor still name it).

Fresh plan: while `item.current_job_id` is the superseded Job, `planner._load_editor_target` misses
with `source_replaced` (deliberately not a guarded miss), so a follow-up routes to `replan` and the next
strategy claim mints a new Job from the new take. The marker goes inert on its own once that Job is current.
The server never starts the planning turn; the client sends the follow-up message. Pending approvals
prepared against the old take are closed with code `source_replaced` after the commit
(`retire_pending_approvals_for_source_change`, canonical lock order, idempotent, re-run by every replay).

`upload-urls` accepts `replaces_media_id`: a video that replaces an attached clip does not count
against the clip cap (one exemption per replaced clip). Guard: `tests/kria/test_replace_media_e2e.py`
(real route, real Postgres, real ffprobe; `.replace-media-e2e/report.json`).

Not closed server-side (known residuals):
- A crash-resume of a replacement upload that calls attach instead of replace: a safe 409
  `clip_cap_exceeded` for one-clip Talking, but with `TALKING_MULTI_CLIP_ENABLED` on it adds a second
  speaker. Closing it needs a column on `creation_thread_upload_reservations` marking a replacement.
- A new take with an audio track that is silent or inaudible passes registration; the on-device
  audio check and the existing render failure path are the guards.
- Brief requirements scoped to the old take (`clip:<old id>`, protected text naming the old speaker,
  timed background windows) are not retargeted. The next plan reads the brief as it stands and the planner
  reports what it cannot place as unresolved.
- A phone export record stuck in `awaiting_device` or `syncing` keeps answering 409
  `render_in_progress` until the stale-record reaper (24 h) settles it.

## Unstated clip order never blocks (`KRIA_UNSTATED_ORDER_DEFAULT_ENABLED`)

Prod threads 666b996f and de471354 (Toroko, 19 clips): "the map video first, the train entry parts
second, the videos of friends inside the train next, then the scenery" is an order over groups of
clips and says nothing about the order INSIDE a group. The draft was held (`approval_requirements_not_ready`,
conflict `simplification_requires_choice`) on "you didn't say what order the clips in ... should play in",
with a "simpler version" card that dropped the whole ordering. The rule: what the creator says is kept
exactly; what they did not say (the order inside a described group, clips nobody mentioned) is the order
they ADDED the clips, said on the card, and never a hold. Default `false` (byte-identical when off).
Apply: fly secret + restart api and worker (the planner, the render contract and the worker's group
outcome all read it).

One flag, read in four places that must agree (KRI-503: planner, contract pin and receipt seat the same order):

- `order_mode.confirm_unordered_groups` stamps `ordered_members=False` ("no internal sequence requested")
  on every resolved multi-clip order group left unset (positions none, `first`, `last`), exactly as the
  random / filming branches do. Groups the creator ordered, basis intents (`order_by`) and one-clip groups
  are untouched.
- `clip_order_sequence.unconfirmed_order_groups` is empty while the flag is on. It is the one read that the
  brief receipt, readiness, the saved-draft approval re-check (`runtime`), the reopened-receipt rewrite and
  the worker's `member_order_unknown` outcome share, so a draft stored with the marker unset reads as
  defaulted without being rewritten (`ordered_members` is in the strategy digest; never re-stamp a store).
- `creator_render_contract._unstated_order_defaults_to_attachment` pins the full order
  (`order_basis = "attachment_order"`, `order_ids` = the clips in the order they were added, with the
  described groups seated on it by the KRI-503 block) wherever a described sequence names no basis, so
  "start with the map video" with the rest unmentioned is pinned too (it is held today on "I can't confirm
  this draft does it yet"). Only for the unified phone montage shape: a montage-family format, phone proxy
  clip rows, no voiceover or song, no Visuals in the edit (a Visual is spread, not seated by upload order),
  not the `native` render program, no random / "any order" / "group first" and no confirmed hand reorder.
  The pin turns the spoken-welcome reorder off at draft time (`speech_decides_order`) and in the worker
  (`speech_order_approved`), so a welcome clip stays where the order puts it.
- `approval_readiness._planned_order_basis` credits that pin (and `brief_checks._check_order` returns `met`
  with "where you didn't say, the clips play in the order you added them") only on the phone unified montage
  route. A creator who asked for the upload order has the answer path, which this does not change.
  `with_defaulted_order_receipts` copies that reason onto the draft turn's own receipt for the ask (a strategy
  draft records no ordering basis, so it read "I can't confirm the order this draft uses"), only for a ready
  report.

Not covered by the pin: an edit that shows Visuals photos, a non-phone writer or the native program (the
groups are no longer asked about there). KRI-1592: on those writers an order ask nothing commits to is
declined instead of held on "I can't confirm this draft does it yet": `approval_readiness._order_not_promised`
rates it `declined` ("I can't promise the clip order on this kind of video yet, so it may not match what you
asked", EN/TR/DE, a format limit in `brief_checks._FORMAT_LIMIT_EN`, so never a "simpler version?" card) and
`with_declined_order_receipts` puts the same line on the draft turn's own receipt. Only with the flag on, only
for a strategy draft (an editor draft's ops are its whole change), never for an order replacement, and only
when no writer vouches for an order and the contract pins none: a pinned order is one the phone verifier
refuses a render for, so it keeps its hold (approval would only move the refusal after the render). After
approval the unified montage worker renders a plan whose own order receipt misses and only says so in a note
(KRI-129), so with the flag on `phone_montage_preflight.readiness_plan_blockers` also returns order asks: a
Visuals montage whose plan misses the order is held before approval instead of rendering in the wrong order.
The flag is on in production (KRI-1587). Guard: `tests/kria/test_kri1592_order_on_other_writers.py` (a
Visuals montage, a voiceover montage on the native program and a Talking "start with X", each held before and
offered after; `test_flag_on_replay_per_writer` runs production's flags, including #2483's clip-order ledger,
across the three writers: an order nothing pins proceeds, an explicit filming or added order and a pinned order
keep the verdict they have without this change; the checked cases stay held). The disclosure rides the requirement receipt, not the reply prose (the reply keeps the model's summary
when every judged ask is met, like the "group first" clause). After render the receipt says `met` with no
reason. A later change plans story order with the model, keeps a ledger and offers drag cards; this one is
the deterministic default only. Guard: `tests/kria/test_unstated_order_default.py` (the failure-mode list
is its docstring; it runs the real planner hook, readiness and `preflight_montage_plan`).

Open decision (owner): with the flag on, an unset marker is "no internal sequence requested", and the marker is
the only thing that tells a creator's listed order ("in this order: the door, the butcher, ...", KRI-941) from
a group (wording is never parsed, KRI-667). A legacy row stored with the marker unset plays in upload order and
its post-render receipt reads `met`. New turns are protected only as far as the model writes
`ordered_members: true` for a listed order. Flag-off pins: `test_a_saved_list_without_ordering_certainty_cannot_claim_checked_order`,
`test_reconfirming_a_saved_list_recovers_the_order_receipt`.

## The clip order is one live, AI-aware state (`KRIA_CLIP_ORDER_LEDGER_ENABLED`)

Owner rules (final): the creator's clip order is ONE state the AI always sees up to date and can read
and change when asked; anything the creator specified is kept exactly; what they did not specify is the
order the clips were added; a drag or a message is the NEWEST statement, never an immutable lock; the AI
never reorders on its own; no creator-visible string shows a media id, UUID or file name. Default `false`
(prompts, ops, strategy dumps and receipts byte-identical when off). Apply: fly secret + restart api and
worker. This is the server core; the card payload and the `clip_order_ops` turn field are the next
section; the iPhone cards build on them.

**The ledger** (`app/services/clip_order_ledger.py`, pure). One `LedgerEntry {actor, before_ids,
after_ids, turn_id, rows}` per change, whatever produced it: `server_default`, `creator_text`,
`creator_drag` (reserved for the card), `creator_editor` (a manual timeline edit) or `ai_story`. Entries
ride the turn's assistant event payload (`clip_order_entries`, written by `_clip_order_payload` in
`tasks/kria_runtime.py` on the draft and reply events) and are folded like `song_order`
(`fold_clip_order`); a creator event can never write one, and a malformed row is skipped, never raised.
There is NO lock flag: a later entry replaces an earlier one. Per-card `source` is the actor of the last
entry that moved the card (`moved_ids`: the cards outside the longest run that kept its sequence).
`current_clip_order()` is the one reader: after a render the first-appearance media order of the editor
timeline (which already holds every drag, manual edit and chat edit), before one the ledger replayed over
the order the clips were added. Every change is one full sequence of media ids applied by one function,
`apply_order`: with no `known` set a PARTIAL order, so the named clips lead in their sequence and the rest
keep theirs ("put the map first", or a full order that forgot a clip); with a `known` set a bundle written
against an earlier timeline, so the named clips take the sequence in the slots the known clips hold and a
clip added since keeps its slot (never a 409/422 dead end for a chat reorder). `previous` is one undo step:
an entry that restores the order before the last change pops it.

**Where it is read and written** (`kria/planner.py`): `plan_live_turn` builds the `TurnClipOrder`
(`_turn_clip_order`; a hand edit it finds, `creator_editor` when the app sent unsaved lanes or the variant
has a saved `user_timeline`, else `ai_story`), sets it in a ContextVar, stamps the approval-bound media
snapshot with `clip_order_ids` and writes this turn's entries (`_with_clip_order_entries`, plus the editor
draft's media-order change in `kria_runtime._with_editor_order_entry`). The snapshot key is the ONE
carrier every reader seats on: the render contract's `_edit_rows`, the phone preflight and dispatch
(`content_plan_build._ledger_ordered_paths`), so the worker, readiness and the contract agree (KRI-503);
`media_identity` never reads it, so it never invalidates an approval.

**Order rows apply once.** Order intents carry no requirement link, so "this turn's rows" is read from
the brief: a live `order` requirement with no `source_turn_id` at planning time is this turn's own
statement, and then every row seats again on the CURRENT order (newest statement wins). Otherwise
`brief_order_fields` calls `mark_seated_rows`: a row an earlier turn seated (its fingerprint is in a
ledger entry's `rows`) that the order has since moved away from gets `already_seated`. A marked row seats
nothing (`sequence_rows` skips it; `sequence_rows(seated=True)` still reads it for "was it placed"), the
planner records it as a met outcome with `code: replaced`, and the receipt reads `met` ("the order you set
is the one that plays"). A row the order still satisfies is left alone, so an unrelated edit behaves as
before. After a creator changed the order the disclosure for what they did not say reads "in the order you
have them now" (`PlanFacts.order_confirmed`, from the snapshot key).

**AI awareness.** The Main Creator's `media_context` and the model-facing `capability_manifest.media` are
sorted by the current order (the hashed manifest the approval fence reads is NOT reordered) and
`item_context` gains one line (`order_line`: position, name, who set the spot), only once the ledger has
an entry. `prompts/main_creator.txt` already defines clip numbers as play order, so no template changed.
The copilot snapshot gains `previous_order` (the exact `order` that restores the order before the last
change) and `order_listing` (the current order by name) through `clip_context`. Whether a live copilot uses
them is unmeasured.

**Chat reorder.** `route_requirements` serves an `order` requirement on a rendered edit as an editor op
unless its key names a basis the editor has no data for (`attachment`, `route`, `narrative`); no wording
decides. `reorder_clips_by custom` completes an order that omits clips (`apply_order`) and a stale bundle
rebases by media id (`_rebased_permutation`). `planner._without_unasked_reorders` strips a reorder op
from a turn whose brief was read and states no `order`/`select`, keeping every sibling op, and says "I
left your clip order as it was." The draft's change list says the reorder by name (`kria_runtime._with_order_receipt`, from the entry's
before/after ids: "Moved “swing video” before “map”."). `_scrubbed_reply` replaces any media id, UUID or file name in a reply
with the clip's name (`clip_label`: the creator's group words, the upload note, a short record subject,
else "Clip N" by play position). The v1 creator route's re-plan base
(`_previous_creator_clip_order`) comes from the ledger instead of the "keep the order" phrase check.

**Guards and limits worth knowing.** The snapshot carrier is stamped only when a CREATOR actor changed
the order (or the story sequencer chose one: its own section below); an `ai_story` order the plan
was only seen to have laid out is the plan's, never pinned. The contract pins the
creator's order (`order_basis` stays `attachment_order`) for the unified phone montage shape only, and not
under an `editorial_plan` (its beat order is the story's own: the old behaviour, no raise). A strip never
touches a tapped suggestion card or a degraded turn (`brief_coverage.degraded_from`). The router keeps its
own routing for `capture_time`, `random`, `any_order`, `attachment` and route keys; the ledger adds
`custom`/`move`/`swap`/`undo`/`reverse`. A manual edit is read from the client state's timeline lane or a
saved `user_timeline`, never from an unsaved text lane. Duplicate names are told apart by position.

**Before a render, a move nothing can serve is declined.** `planner._declined_pre_render_move`: when the
turn's only new requirements are order requirements keyed `custom`/`move`/`swap`/`undo`/`reverse` and no
rendered timeline exists, the turn answers with one plain line (EN/TR/DE) and records no requirement, so a
later render is not blocked by an unservable ask. Key-less "first/last/then" asks stay clip-intent rows and
keep their draft; a message that asks for anything else keeps its draft. A turn with a render whose editor
target could not load also reads as "not rendered".

**Taking an order back (KRI-1594).** Every reader folds the same events (`ledger_events_clause`:
the assistant events plus the system `turn_cancelled` events), and `fold_clip_order` drops what was
taken back. A draft undo (`draft_undone`, `restored_from_draft_id`) puts the ledger back to what it was
the moment the restored draft was last the newest one, so the undone draft's entries leave with it and
undoing the undo brings them back; a restored draft with no event of its own (the baseline under a first
editor update) goes back to just before the undone draft. A removed or stopped message
(`runtime.cancel_turn`) writes a `retract` row naming its turn: that turn's entries never count, even
when its worker finishes after the cancel. A turn that restarts the request before a render (the
extraction's typed `starts_fresh`, or the message after a "Change my request" tap, noted through
`clip_order_ledger.note_restarted`) plans on the order the clips were added (`planning_state`) and
writes a reset row ahead of its own entries. After a render the timeline is the order and no reset is
written. Retraction rows use the actor `retract`, which `LedgerEntry.from_payload` rejects, so a reader
that only replays entries skips them. The `creator_agent` route folds each chat on its own.

**One reader per rule (KRI-1594).** Which order rows place clips now is `clip_order_sequence.seats_now`
(the planner, the contract, the phone preflight, dispatch's `order_paths_by_resolved_intents` and the
narrated worker's first/last/group pins all ask it; an `already_seated` row places nothing and its
narrated receipt row claims no seat). The order-basis question gate's "placed" check
(`choice_questions._placed_sequence`) reads `sequence_rows(seated=True)`, the rows the contract's own
check reads, so a basis `order_by` row no longer counts as a placed sequence there. Clip names read the
creator's group words through `clip_order_card.known_group_words` (the newest card's words, then the
newest strategy's) in the card, the Main Creator's order line, the order listing and the reorder
receipt; a note or group name that is exactly any clip's media id, file name or file stem is never a
name (`labels_for`).

**Not covered here.** Relative moves, swaps and undo BEFORE a render (the copilot needs a rendered
variant; `_load_editor_target` returns nothing without one) are declined in one plain line when they are
the turn's only ask (above). The first draft turn's `creator_text` entry seats only rows the planner resolved; an order that
is the model's own story order (`ai_story`) is recorded only as the difference the next turn finds. The
native render program (a recorded voiceover) still plays the model's `selected_media_ids` order (KRI-1594
left it open: see the issue). Guards: `tests/kria/test_clip_order_ledger.py` and
`tests/kria/test_clip_order_history_gaps.py` (the failure-mode lists are their docstrings).

## The clip-order card (`KRIA_CLIP_ORDER_LEDGER_ENABLED` + client protocol 54, KRI-1590)

A projection of the ledger, not state: `app/services/clip_order_card.py` (pure) builds the card, the
draft / reply completions attach it (`tasks/kria_runtime._clip_order_card_block`), and
`runtime._submit_clip_order_turn` takes the drag. Same flag as the ledger (no new flag), plus
`CLIP_ORDER_CARD_MIN_CLIENT_PROTOCOL` (default 54, `CLIP_ORDER_CARD_PROTOCOL`; `999` = kill switch: text
only for every build). Capability `clip_order_cards` on `GET /creation-threads/capabilities` (ledger flag on
and the switch not at 999; the build still has to declare protocol >= 54).

**Rollout.** The ledger flag is on in prod and no shipped build declares protocol 54, so on deploy every
current build is a `text` build: an unstated-order draft gets the plain listing appended to its message, and
`CLIP_ORDER_CARD_MIN_CLIENT_PROTOCOL=999` does not turn that off (only the ledger flag does).

**Who gets what** (`clip_order_card.card_gate(protocol)`; the protocol is the sending build, stamped on the
user event, so the worker needs no request): `card` (>= 54) gets the `clip_order_card` key; `text` (a
declared older build, or any build under the kill switch) gets NO key and NO 426: the draft / reply
`content` ends with the order as plain text ("Your clips play in this order:" and one `N. name` line per
clip, EN/TR/DE) when the strip would have been shown; `none` (flag off, or no declared build such as the web)
gets exactly today's event. Chat reorder works for every gate.

**Wire: the card** (`payload.clip_order_card`, on every `draft_applied` event of a draft that requests a
render when the strip is `shown`, on a reply event that changed the order, on the drag's own
`clip_order_card` event, and on the `assistant_error` of a closed approval; an editor draft that requests no
render carries none; the newest card in the thread is what the creator last saw):

```json
{"version": 1, "proposal_id": "9f2c41d0a7b3e655", "shown": true, "confirmed": false, "editable": true,
 "total": 3, "rebased": true,
 "cards": [{"media_id": "<opaque>", "position": 1, "label": "the map video", "kind": "video",
            "group": "the map video", "source": "creator_text", "moved": false}]}
```

- `cards`: every clip the edit plays, in play order (a draft that selects a subset lists only those:
  `selected_media_ids`), no urls (about 150 bytes each). `media_id` is the clip's id in the
  thread's own media (the phone maps it to its local asset and thumbnail; it is never shown). `label` is
  already safe to show: the creator's group words, else the note they typed on the upload, else a short
  description, else `Clip N` (N = play position); two clips that read the same get ` (position)` appended;
  never an id, UUID or file name. `group` is the creator's words for the group or null.
- `source`: `server_default` (upload order, "Yükleme sırası"), `creator_text` (stated in chat) or
  `creator_drag` / `creator_editor` (moved by hand), all three "Senin sıran"; `ai_story` is the AI's own
  story order: after a render the order the plan laid out, before one the story sequencer's
  (`KRIA_STORY_SEQUENCER_ENABLED`; the card then also has `story: true`). `spoken_roles` is not
  produced (no such ledger actor).
- `moved`: moved since the previous card in the thread (the minimal set: moving one clip past three marks
  that clip). The first card has none.
- `shown`: the strip is in the chat. True only with 2+ clips and at least one position whose source is not a
  creator actor; a draft also needs its order to be the upload order (`card_applies`: a guided edit format,
  original or licensed audio, not the native program, no random / group-first / filming-time order, no
  `order_by`). A clip the creator stated alone ("end with the beach clip", a listed order) counts as theirs;
  a clip inside a described group does not (the Toroko incident shows the strip). After a drag or a confirm
  the strip stays (sticky), so the chat does not lose it. Draft and reply events carry the card only when
  `shown`; the drag's own event always carries it (`shown` may be false).
- `confirmed`: the creator tapped "use this order" for exactly this order; a changed order clears it.
- `editable`: false once a video exists, is being made, or its render was approved (the timeline is the
  order): show the strip, hide the buttons.
- `proposal_id`: a digest of (brief version, clips shown, order). Send back the id of the card the creator
  was looking at. `rebased: true` only on a drag's card, when the server did not apply the list as sent.

**Wire: the drag** (`POST .../turns`, optional field on `SubmitTurnBody`; only builds >= 54 send it):

```json
{"message": "Order updated", "client_event_id": "...", "expected_thread_revision": 7,
 "clip_order_ops": {"proposal_id": "9f2c41d0a7b3e655", "ordered_media_ids": ["m3","m1","m2"], "confirm": true}}
```

Send the WHOLE order the creator sees, once, on "use this order" (`confirm: true`) or on each drop
(`confirm: false`). `message` is a placeholder the server never plans, shows or stores: the user event is stored with
`content: null` and the drag in its payload (`clip_order_ops`), so no transcript reader takes it for the
creator's words; hide it in the chat. The response is `TurnAccepted` with `status: "completed"` (no worker); the
result is the new `clip_order_card` event (`event_type: "clip_order_card"`, role assistant, `content`
null): no chat message. It is never a 409/422 for state: a stale `proposal_id` or `expected_thread_revision`,
a missing, extra, repeated or unknown id, or an empty list is rebased by media id (`clip_order_card.rebase_drag`
= `apply_order` with the named clips as the known set: they take the sequence in the slots they hold, a clip
added since keeps its place, a removed or unknown one is dropped, nothing named changes nothing). Only the
same `client_event_id` with a different body is `409 idempotency_key_reused`, like every turn. A build or
server that cannot take it (flag off, kill switch, older build) gets a `status_update` line and changes
nothing.

**What a drag does.** Before a video exists: one `creator_drag` ledger entry on the event (the next plan, the
render contract, readiness and the worker all read it); a second drag right after the first (nothing else
in the thread between) is `coalesce: true`, so the gesture is one undo step and the last full order wins.
After a video exists, while it is being made, or once its render was approved (a set `current_job_id`, an
approved approval, or a loadable editor timeline): nothing is recorded and the card answers with the order
that plays (`editable: false`, `rebased: true`): a recorded drag would be overruled by the timeline on the
next turn. A drag that lands while a turn is planning makes that turn's commit requeue (the thread-revision
fence), so it plans again on the dragged order. It never
queues behind a running turn or a pending approval, and `expected_thread_revision` is not checked.

**An approval prepared before a drag** binds the order its draft was planned on, so the drag is carried
INTO the draft when the creator approves (`runtime._rebind_dragged_order`, inside `decide_approval`'s own
transaction, after the locks it already holds in canonical order: PlanItem, Session, Turn, Draft, Approval,
Execution, Thread; a drag takes the item, job and session `FOR KEY SHARE` before the thread lock, as a Steer
does, so the two never deadlock). "Drag, use this order, make the video" renders the dragged order; there is
no 409 and no cancelled plan on that path. The rewrite: the approval-bound media snapshot stamps the ledger
order, an `order` row the drag moved away from is marked `already_seated` (`clip_order_card.rebound`, the
rule a fresh plan applies), the binding digest and draft hash are recomputed, and the saved readiness is
replaced by a fresh assessment of the same evidence. It triggers only for a creator drag after the draft
event (`entries_after_draft`); an AI or server entry never rewrites an approval. Why in place and not a new
draft revision: the approval pins the draft id and revision and the client echoes them, so a new revision
would need a second approval, a superseding event and a second set of locks; the order is in no pinned
fingerprint (`media_identity` and the approval fingerprint never read it), so rewriting the unrendered
head draft changes nothing the creator signed. Only the strategy digest of the readiness may move: the
rewrite is refused unless the fresh report is ready and the brief, media, evidence and capability digests
equal the saved ones. The refusal (the old fallback) is the only 409 `clip_order_changed`: the approval is
closed with the refreshed card in an `assistant_error` payload, nothing rendered. It is reached only when a
draft has no brief binding (no channel for the order), is not a strategy draft, or an entry other than a
creator drag followed it, or the evidence moved. A message sent while the approval is pending still waits
behind it (queued) and is planned after the approval, on the order that rendered. A drag after the render
was approved is not recorded (above). Applies to threads whose first drag set `state.clip_order_dragged`;
others pay no query at approval.

**Reading the thread.** The card event has its own type, so it closes no open question; its user event is
`inert` with no content. `request_digest`
excludes `clip_order_ops` when absent (every existing digest holds), duplicate-send and the simpler-version
answer keys list it. Guards: `tests/kria/test_clip_order_card.py` (the failure-mode list is its docstring),
`tests/kria/test_clip_order_card_turns_postgres.py` (real submit, worker and approval; needs a scratch DB,
`bash scripts/dev/test-db.sh`).

## The AI picks the order nobody stated (`KRIA_STORY_SEQUENCER_ENABLED`, KRI-1593)

Owner rule (KRI-1586): "the AI decides what fits the story; if it can't, upload order". Where the
creator said nothing about a clip's place, a small separate model ranks those clips into the order
that tells the story; the entry's actor is `ai_story`, the card and the receipts say the order is
the AI's, and a clip the creator placed never moves. Default `false`; needs the ledger flag. Apply:
fly secret + restart api and worker. Off = no model call and no new story entry; a story order an
earlier turn wrote is still the thread's order (the creator saw it on the card) and is still taken
back when a draft can no longer carry it, so the flag is a stop for new orders, not an undo.

**Two halves.** `app/agents/story_sequencer.py` (`nova.kria.story_sequencer`, prompt
`prompts/story_sequencer.txt`, Gemini Flash, one bounded call) only RANKS: it returns the open
clips in play order, or `decided: false`. It is a separate agent on purpose, so the Main Creator's
prompt and fingerprint do not change. `app/services/story_order.py` (pure) decides everything
else:

- **May this draft carry a story order** (`carries_story`, asked on EVERY pre-render strategy
  draft, not only the one that calls the model): the draft's order is the upload order the creator
  can re-arrange (`card_applies`), it has no `editorial_plan` (its beats are already the model's
  order), the creator stated nothing a re-order would overrule (`states_order`: an answer to the
  order-basis question such as "use the order I added the clips", a live order ask that names a
  basis, or text given by position: `shot_labels`, the creator's own sentences laid over the clips
  in turn, a route's start / end labels), and `one_order` holds: the render contract pins exactly
  that order AND the phone montage planner the worker runs (`preflight_montage_plan`, this
  creator's route) plays exactly that order. The phone verifier refuses any other order than the
  pinned ids, so a story order exists only where the card, the pin and the render are one order
  (KRI-503). That is the unified phone montage with no Visuals, voiceover, song or native program.
- **What is open** (`open_spans`). A clip is fixed when a creator actor moved it in the ledger (a
  message, a drag, a hand edit), an order row of the draft names it alone ("the map video first", a
  listed order: `clip_order_card.stated_ids`), or it is on the newest card and the creator tapped
  "use this order" (`TurnClipOrder.confirmed_ids`). The rest are grouped into spans: two clips
  share a span only when the creator's rows treat them alike (the same described group, or no
  group at all), by the rows that seat now and by the rows an earlier turn seated. A span needs
  two clips. A row the server could not resolve means nothing is open (guessing which clip the
  creator meant is how a placed clip would move).
- **How a ranking is applied** (`apply_ranking`): span by span, into the slots that span holds.
  A fixed clip keeps its exact position and a clip never leaves its group, by construction. Clips
  an earlier run already looked at (`StoryPlan.placed`) keep their order among themselves: a later
  run only seats the clips nobody looked at between them, so an order the creator has seen (or
  confirmed) is never reshuffled when a clip is added.
- **What a draft does** (`review_story`): asks (a `StoryPlan`) when the draft can carry a story
  order, at most 40 clips are open and at least one was never looked at (`story_decided_ids`);
  takes the order back (`withdraw_story`) when the thread has a story order and the draft can no
  longer carry it; otherwise nothing.
- **Whether the answer is used** (`settle_story`). The result must keep every fixed clip's
  position, be unchanged by seating the creator's rows on it again (`apply_sequence`, the way
  every reader of the snapshot reads it) and pass `one_order`. Otherwise the order stays as it was.

**Taking it back** (`withdraw_story`). When the creator picks the upload order, changes to a
format that cannot carry a story order, or the contract and the worker no longer agree, the AI's
clips go back to the order they were added, in the slots they hold; a clip the creator placed keeps
its exact position. The entry's actor is `server_default`, which also clears the story mark from
every clip (`_replay`), so the snapshot carries only the creator's own order, or none. This path
needs no model and runs with the story flag off too.

**Fallback is upload order, never a hold.** A provider failure, a slow call, an answer that is not
a full ranking (the agent refuses a missing, unknown or repeated id, and ignores a stray key or a
long note), `decided: false`, or an order no reader would hold all leave the clips as they were,
and the turn goes on. `decided: false` and an unchanged order are still written as an `ai_story`
entry whose order did not change, so the next turn does not ask again; a failed call writes
nothing, so the next draft does.

**Where it runs** (`planner._with_story_order`, in `plan_live_turn` AFTER the choice gate, on the
draft approval will bind: the gate's answers are on the strategy and its enriched snapshot holds
the length plan the worker check needs; a turn that ends in a question writes nothing). The brief
is previewed as the gate previews it. The call goes through `checkpointed` (a continued run of the
same turn reads the first answer) and its paid-call key carries the turn id, so the same clips and
words on a later turn are a new call. It is skipped when under 35 s of the turn's deadline is left.

**What it writes.** One ledger entry on the turn's event, after the turn's `creator_text` entry:
`{actor: ai_story, before_ids, after_ids, story_ids}`. `story_ids` are the open clips it was asked
about and only its own entries carry them (`LedgerEntry.from_payload` drops the key for any other
actor), which is what tells its order from an `ai_story` order the plan was only SEEN to have laid
out after a render (`observed_entry`: still never pinned). In `replay` every `story_ids` clip of an
entry that changed the order reads as `ai_story`; `ClipOrderState.story_ids` are the ones nobody
has moved since. The snapshot carries the full order in `clip_order_ids` (as for a creator's own
order) plus `clip_order_story: true` (`with_order`, `snapshot_story`); `media_identity` reads
neither. On later turns `with_clip_order_ids` keeps stamping it and the hook re-checks
`carries_story`, so a later draft plays the same order with no second call, or takes it back.

**What the creator is told.** Card: the open clips read `source: ai_story` (the app's "AI order"
badge, already shipped with the card) and the card has `story: true`. A build with no card gets the
list under "Where you didn't set the order, I picked the one that tells the story best. Your clips
play in this order:" (EN/TR/DE). An order ask's receipt reads "where you didn't say, I put the clips
in the order that tells the story best" (`brief_checks._UNSTATED_ORDER_STORY`, from
`PlanFacts.order_story`) instead of "the order you have them now". The Main Creator's order line
already says "(my story order)". The model's own `reason` is trace-only and reaches no creator
string. The model sees clips by alias (`c1`…) with the creator's group words and notes, never a
media id, a file name, or a note that is exactly some clip's file name.

**The creator's later word wins.** A drag after a story order is a `creator_drag` entry as before:
the dragged clip reads as theirs, leaves `story_ids` and is fixed for every later run. "Start with
X" on a later turn seats on the story order. Choosing the upload order takes the story order back
(above). A removed or undone turn takes its story entry with it (the ledger's own retraction rules).

**Not covered.** An explicit "put them in the order that tells the story" ask has no typed key in
the brief, so it is not a trigger (no wording is parsed); after a render the sequencer never runs
(the timeline is the order; a chat reorder is an editor op); the tappable "suggest a better order"
card is not built; drafts with an `editorial_plan`, Visuals, a voiceover, a creator song or the
native program are untouched. The Main Creator writes its own summary before the order is chosen.
A restated-strategy suggestion tap (`plan_pinned_suggestion_turn`) re-plans without the order keys
(`restate_judgment.plan_media` copies only the length-plan keys; the same gap a creator's own order
has), so after a story-ordered render it would re-render from upload order: close it before the
flag is turned on.

**Measured (2026-11-24, one live run, 15 Gemini Flash calls, $0.025).** On the held-out set
(`tests/evals/story_sequencer/heldout.json`: 14 hand-written cases, clips listed in the order they
were added, with the precedence pairs a story order must satisfy) the played order satisfied every
pair: mean 1.00 against 0.47 for upload order, no creator-placed clip moved, no answer refused. The
run is recorded in `tests/evals/baselines/story_sequencer/heldout_run.json` and
`test_recorded_run_beats_upload_order` re-scores it on every CI run (it fails if the prompt version
moves without a new run). What this does not show: the cases are short, clean, hand-written clip
records with an obvious chronology, so it says the model follows a clear story (also in German and
Turkish, and inside a creator's groups), not how it does on real, thin or noisy clip records. On
the one "nothing to decide" case it did not hold back: it moved the clip described as "a little
closer" to the end. No device render of a story-ordered draft has been inspected. Guards:
`tests/kria/test_story_order.py` (the
failure-mode list is its docstring; it runs the real contract, readiness and
`preflight_montage_plan`, with hand-written rankings).
