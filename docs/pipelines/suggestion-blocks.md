# AI suggestion blocks

Evolves the KRI-756 chips ([`suggestion-chips`](suggestion-chips.md)) into "Improve" cards on the
iPhone app. iOS and the iOS-required backend only; `src/apps/web` is untouched. Everything here is
dark: every flag defaults to false and flag off is byte-identical to the chips.

**The agent picks, the server builds.** A model returns only `{kind, target_ids, why, copy?, rank}`
(`SuggestionAgentOutput`). The server turns a pick into an `Action` through the kind's
`build_action` and proves it with the kind's `witness`; a card that fails its witness never reaches
the phone. The `Action` stays server-side; a tap pins `{set_id, chip_id}`.

## Layout

| path | what |
| --- | --- |
| `services/suggestions/models.py` | `SuggestionKind` row, `Action` union, `Suggestion`, `SuggestionSet`, `SuggestionDigest`, `SuggestionAgentOutput` + `agent_response_schema` / `parse_agent_output` (strict) / `parse_agent_output_tolerant` (size repairs; what the agent uses), wire `SuggestionOut` |
| `services/suggestions/context.py` | `SuggestionContext` and the read-only snapshot readers every kind shares; `snapshot_digest` (the `EditorOpsAction.base_digest`) |
| `services/suggestions/registry.py` | `all_kinds()`, `legacy_gate()` (the engine's `CHIP_KIND_GATE`, original order), `enabled()`, `kinds_for()`, `problems()` |
| `services/suggestions/kinds/*.py` | ONE module per kind; the explicit list is `kinds/__init__.py` |
| `services/suggestions/pinned.py` | the ring reads a tap resolves against, the lane allow-lists, the registry rebuild, the restate base |
| `services/suggestions/brief_asks.py` | "did the creator already say something about this lane?" for the five ported editor kinds (KRI-129) |
| `services/suggestions/restate_judgment.py` | the one question the serve and a restating tap ask: would the commit approve this draft? (`restate_problem`, `plan_media`) |
| `services/suggestion_serve.py` | the GET's read path: freeze, ring write, and the strip of cards the tap would refuse |
| `services/clip_signals/` | per-clip measured signals, stored at `analysis["signals"]` next to `analysis["understanding"]` |
| `app/cli/backfill_clip_signals.py` | read-only skeleton; `--write` is refused until a store is wired |

## Adding or changing a kind

A kind is `kinds/<kind>.py` exporting `KIND` plus one line in `kinds/__init__.py`. Required: a
`witness` and a non-empty `surfaces` subset of `{phone, cloud}` (`registry.problems()`, guard test).
`flag` names the `Settings` attribute that gates the kind in v2 sets (the legacy chip engine ignores
it). `required_signals` also needs `KRIA_SUGGESTION_SIGNALS_ENABLED`. A kind with `legacy` set is
still driven by the KRI-756 engine unchanged; the v2 path uses `build_action` (None = not offered).

| module | kind | v2 action | state |
| --- | --- | --- | --- |
| `captions`, `sfx`, `transition`, `music_level` | KRI-756 kinds | `EditorOpsAction` from the row's own `op`, proved by `editor_ops_witness` | ported, live in the engine and as v2 cards |
| `default_edit`, `opener_clip`, `title_animation`, `pinned_text` | KRI-756 kinds | none | ported, legacy only |
| `hook` | KRI-756 kind | `CopyDecisionAction`: the agent's exact text, applied as one `set_title` op; the tap records the creative-copy decision (`source="suggestion"`) | v2 card behind `KRIA_SUGGESTION_HOOK_COPY_ENABLED`; the chip stays the only first-prompt hook |
| `mute_clip` | new | `BriefUpdateAction` (`clip_sound` off, `clip:<media_id>`) + the staged render approval | implemented but **dark by design** (`mute_clip.PINNED_TAP_APPLIES_CLIP_SOUND = False`): a pinned tap stages the typed requirement and the approval, but only a phone re-plan from the brief applies it. Run `verify-phone-render` or a device, plus the real-clip audio calibration, before the constant flips |
| `filler_words` | new | `ItemSettingAction` (`speech_cleanup_enabled`) + the staged render approval | v2 card behind `KRIA_SUGGESTION_FILLER_WORDS_ENABLED`; evidence is the speech-cleanup preflight row, no second detector |
| `duplicates` | new | `EditorOpsAction` (`remove_clip` on the weaker take) | v2 card behind `KRIA_SUGGESTION_DUPLICATES_ENABLED`; evidence is the `near_dup_hash` signal |
| `generated_overlay` | new | `AsyncGenerateAction` | stub, never served (last phase) |

Skill: `agents/skills/edit-improver/SKILL.md` (real body, under the 6000-character budget; proven by
`tests/evals/test_edit_improver_skill.py`).

## Wire (additive)

`SuggestionSetOut` gains `suggestions: [SuggestionOut]` and `digest` (`SuggestionDigest`). Both are
left out of the JSON while empty, so a legacy set serializes byte-for-byte as before. Old builds and
web ignore them. iOS contract: `python -m app.cli.kria_contracts --write`; `SuggestionSetOut` is a
mobile-only model, so the web contract is unchanged. The tap field stays `suggestion: {set_id,
chip_id}` (`SuggestionTapIn`); under `KRIA_SUGGESTION_PINNED_TAPS_ENABLED` a present pin joins
`request_digest`, off or pin-less it is unchanged.

## Pinned taps (`KRIA_SUGGESTION_PINNED_TAPS_ENABLED`)

A tap on a v2 card (`ss_` id) runs the action the thread was offered, never the card's sentence.
Code: `services/suggestions/pinned.py` (ring reads, lane allow-list, rebuild),
`kria/planner.plan_pinned_suggestion_turn`, and the row-locked commit in
`tasks/kria_runtime._complete_draft_turn`, which is the only apply path.

- **Submit** stamps `pinned_suggestion` on the creator's event when the flag is on, the creator is
  on the chips allow-list and the message is the card's own prompt. An `ss_` id the thread was never
  offered is stamped `unresolved`, so the worker answers "no longer fits" instead of planning it.
- **Plan** builds `[draft.apply_editor_ops]` from the stored action when the editor still matches
  its `base_digest`, otherwise from the registry's rebuild against the current facts. Any failure is
  the "no longer fits" reply plus a suggester refresh, never a fall-through to the free planner.
- **Commit** re-checks `projected_digest` inside the row lock (an index-addressed op must not land on
  whichever clip now sits there) and refuses a tap that compiles to no real change.
- **A card is one offer.** The commit marks the card on its ring entry (`consumed: {id: turn_id}`) in
  the draft's own transaction. A marked card is never rebuilt: a rebuild derives a new change from
  the edit as it is now, so a relative card (music level = 60% of the current mix) would apply a
  fresh step on every later tap. Only the exact stored action on its exact base can run again.
  `pinned.is_consumed` counts a mark on ANY ring entry, matched by kind and anchor fingerprint, so a
  tap on an applied card from an older set is refused `consumed` too, not applied a second time.
- **The GET stores every card it serves (served implies stored).** `serve_suggestion_set` builds
  the cards inside the live read session (`_prepare_cards`, carried on `Served.cards`) and
  `persist_served_set` only writes what it was handed: the route rolls the session back before
  writing, which expires the `Job` the editor witnesses close over. A set needs a ring entry when
  it offers chips OR cards (a set of v2-only kinds has `chips: []`), and a card the ring does not
  hold answers "no longer fits" however good it was. So a read whose write is skipped (the thread
  row stayed locked), or that does not end up holding every card it serves, puts the chips alone on
  the wire under the same set id, and the next read builds and stores the cards again.
  `attach_cards` merges by id: a card the entry lacks is added next to the ones it holds, never in
  place of them, because the phone may hold any card of an earlier read of the same set. That is
  also how a card the strip left off one read (a restating card, below) is already in the ring when
  a later read serves it again: the entry keeps every card the set was built with.
- **The master switch reaches a frozen set.** With `KRIA_SUGGESTION_PINNED_TAPS_ENABLED` off,
  `serve_suggestion_set` drops `suggestions` and `digest` from a set frozen while it was on (chips
  and set id untouched, the freeze not edited), so turning it back on restores the cards. The
  reverse is a rollout caveat: a set frozen while the flag was OFF has no cards, and flipping it on
  serves that chips-only set until the digest changes (an edit) or the 24 h freeze ends.
- **Restating cards are offered only while the head is the plan.** `mute_clip` and `filler_words`
  get no loader input while `pinned.restate_base` says `base_moved` (the head draft is newer editor
  work), so they are not shown over an editor head; they return after Undo or when the head is the
  plan again. With `KRIA_SUGGESTION_EDITOR_TRIGGERS_ENABLED` off the digest does not track the head,
  so the strip below is what keeps a frozen set honest.
- **Handled cards stay handled until the tap is taken back.** A refreshed set has a new `set_id`,
  but a dismissed or applied card is hidden on read by its kind and target fingerprint
  (`suggestion_serve.fingerprint`: kind, clip media id or ordinal, text bar, moment), from both
  `suggestions` and the legacy `chips` (the client falls back to chips when the cards run out). The
  last 32 fingerprints are kept after the ring rolls over. A kind with no target (captions, music
  level, hook) is therefore hidden for that creator's thread after one apply. A tap that is taken
  back (Undo, a deny, expiry, the sweep, a cancel, a claim that finds the video moved) makes the
  card an offer again, see "Undo takes the whole tap back".
- **No refusal is silent.** Every reply a tap can end in without applying (the planner's refusals,
  the consent, mark, readiness and choice gates, and the commit's error replies) carries the typed
  `suggestion_pin` record `{set_id, id, outcome: "refused", reason}` on the assistant event AND on
  the turn's plan diagnostics (a replayed submit reads the latter). A non-transient reason marks the
  card refused on its ring entry: the next GET hides it and mints a new set, which releases the
  phone's "hidden until the set changes" ledger. The transient reasons, `target_unavailable` (a
  render in flight, an editor state gone stale) and `unsaved_edits`, say nothing about the card:
  they are typed but never mark it, and the same tap applies once the cause is gone.
- **A restate runs only on the plan it restates.** Mute and filler re-draft the thread's plan as the
  variant's next revision, so they are only that plan's next revision when the variant's head draft
  IS the plan (`pinned.restate_base`); any other head is newer work and the tap refuses
  `base_moved`. The commit also fences the video: it refuses `base_moved` when the saved variant's
  generation differs from the one the tap was planned against, or from the one the agent session
  last read (`tasks/kria_runtime._restate_video_moved`). A native-editor Save moves that generation
  without writing a draft, and a restated plan holds none of the Save's edits. The serve asks the
  same question (`pinned.save_unread`) and offers no restating card while a Save is unread. An
  editor-op tap is built on the saved video, so it re-syncs the session's pointer instead (and
  Undo of that draft puts it back, below). A restate whose clips changed after planning is the same
  typed refusal, not a failed turn.
- **Reason codes.** The planner-side codes are the constants of `services/suggestions/pinned`; the
  codes of the commit's own stops (`consent_required`, `mark_needed`, `requirements_not_ready`,
  `choice_needed`, `request_too_long`) are the `_PIN_*` strings in `tasks/kria_runtime.py`, plain
  strings so a turn that is not a tap never imports the suggestion engine to build one.
- **A refused tap writes nothing.** A reply that ends a normal turn records what the creator stated
  (KRI-188); a refused tap stated nothing, and its typed requirement would read as the creator's own
  ask (KRI-129). So no brief version is written for it, its receipts are dropped (they judged a
  brief the refusal rolled back) and no "make it without these" question is offered (answering it
  would not apply the card).
- **Undo takes the whole tap back.** Every pinned tap keeps its footprint on the draft's execution
  (`result["pinned_undo"]`: the card's pin and turn, the requirement ids it wrote, for the filler
  card the setting's pre-tap values, and the card's fingerprint with whether the creator's kept
  list already held it). It is written with or without the creative brief. `undo_draft` withdraws
  those requirements as a server waiver in a new brief version, restores
  `speech_cleanup_enabled` / `user_edited` if the tap's flip is still in force, clears the card's
  applied mark AND any refused mark a repeat of the tap left on that entry, and removes the
  fingerprint a GET folded into the kept list while the tap was pending (unless the creator had
  dismissed the card before: a dismissed card stays dismissed). The card is an offer again (a tap
  on it applies once, not twice). What a later turn or the creator changed since is left alone.
  A staged approval that ends without a render (deny, expiry, the sweep, a cancel, a claim that
  finds the video moved) takes back the same footprint, but only when the tap wrote a requirement
  or a setting: the draft of an editor tap that wrote neither is still the head, so its card stays
  spent until Undo. Limit: a hook's creative-copy decision stays in the event log
  (`fold_creative_copy(include_suggestions=True)`), so the hook card is still spent after Undo,
  with the creative brief or without it: the hook kind's own witness refuses it (`copy_decided`)
  and the serve asks that witness too (step 7 below). Without the brief nothing else moves the
  digest when the tap is undone, so the frozen set comes back as it was and only that re-proof
  keeps the card off the wire.
- **Undo puts the session's pointer back.** An editor-op draft is built on the saved video, so its
  commit moves `session.target_generation_id` (the video the agent last read) onto it. The draft
  keeps the value it replaced (`result["pointer_sync"]`, written behind
  `KRIA_SUGGESTION_PINNED_TAPS_ENABLED`) and `undo_draft` restores it, with the session locked
  before the turns (Plan -> PlanItem -> Job -> Session -> Turn), only when nothing moved the pointer
  since. Without that, Save -> editor card -> Undo left the pointer on the saved video while the
  head was the plan again, which lifted the Save fence below over a plan that holds none of the
  Save's edits. The plan-review undo (`kria/plan_review`) syncs the pointer the same way and does
  not use this record yet.

### Serve and tap agree (`suggestion_serve._strip_dead_cards`)

A set is frozen for 24 h; the tap re-checks everything at the draft commit. A served card whose tap
is refused is a dead tap, so the GET asks the tap's own questions and leaves out the cards it would
refuse. The strip happens on the way out: the stored set and the ring keep the cards the set was
built with (a card that comes back needs no new run), ranks are renumbered, a set with no card left
drops its v2 digest, and the legacy chips are never touched. A check that cannot be made leaves its
cards out (what cannot be proven is not offered). The questions, in the tap's order:

1. **Kind switch** (`pinned.kind_enabled`): a kind whose own flag was pulled after the serve
   (`KRIA_SUGGESTION_{HOOK_COPY,DUPLICATES,FILLER_WORDS,MUTE_CLIP}_ENABLED`) is gone on the next GET,
   like the master switch.
2. **Consent gate** (`creative_copy_gate.creative_copy_problem`, KRI-506): the same function over
   the same events and media. Which cards it holds depends on what the tap writes. The **hook**
   card is the one tapped card that writes the on-screen title (one `set_title` op), so ANY
   decision about a title refuses it: open (authorship, write_my_own, generate, revise), stale (the
   footage changed after the answer), approved, creator-supplied, cancelled, on the opening or the
   closing title. Every other editor-ops card runs only ops on `TITLE_NEUTRAL_OPS` (`add_sfx`,
   `set_transition`, `set_mix`, `set_caption_meta`, `remove_clip`), carries no copy and is refused
   only under an OPEN wording discussion; a settled decision, stale included, is none of its
   business (stale is treated as settled for a title-neutral tap: the footage moved since the
   answer but the tap carries no copy). A restating card is committed with the head plan, so only a
   plan that disagrees with the decision refuses it (a different title, or a cancelled title the
   plan still carries); one whose plan agrees stays. Open states also hold the whole set back in
   the loader, legacy chips included, as before.
3. **Would the commit approve the draft** (`restate_judgment.restate_problem`; restating cards).
   A restating tap re-drafts the head plan with one more requirement and the commit refuses it
   (`requirements_not_ready`, `choice_needed`) when the draft is not ready, writing nothing. The
   serve asks the same questions on the same inputs, in one module the planner also reads:
   - an account OFF approval readiness: the draft check's receipts (`plan_facts_from_strategy` ->
     `build_receipts` -> `needs_creator_choice`) for the requirements the card did not write. It
     reads only the plan and the brief; facts that need the phone montage preflight or the
     research step stay unknown, which is an unchecked receipt, never a verdict;
   - an account ON approval readiness (`KRIA_APPROVAL_READINESS_*`): `assess_readiness` over the
     brief plus the card's own typed requirement, the strategy and renderer
     `tasks/kria_runtime._with_approval_readiness` resolves (the helper the tap runs, called, not
     copied), and the media the tap judges. That media is `restate_judgment.plan_media`: the
     item's media plus the length plan the head draft PINNED in its brief binding, borrowed only
     when the pinned sources are the item's current sources (`brief_binding.media_identity`).
     The planner (`plan_pinned_suggestion_turn`) hands the commit that same media; the bare media
     of the item has no length plan, which made every stated length "evidence missing" and every
     tap refused. A head that pinned no plan, or one pinned on other footage, leaves a stated
     length unproven, so the card is not served (what cannot be proven is not offered). The other
     inputs the commit hands `assess_readiness` (order execution and its source context, the
     phone-plan blockers, the protected marks) are read through the same helpers;
   - both: the choice gate (`tasks/kria_runtime._unresolved_choice_plan`) over the same strategy,
     brief, media and thread events.

   The commit's own composition stays inline in `_complete_draft_turn`; what keeps the two from
   parting is `tests/e2e_suggestions/test_r6_restate_drift.py`, which runs both on 11 requirement
   shapes (the fixtures' own: a stated length exact / ceiling / too long, a title the plan has or
   lacks, a Turkish volume ask, captions off, per-clip text, an order ask, the recorded east-run
   brief, none), on and off readiness (a head that pinned its plan and one that did not), for both
   restating cards: a card is served IFF its tap executes, the truth being the tap with the strip
   switched off. Limits, in the safe direction or not exercised: an unmet requirement the commit
   would leave out on its own (`_automatic_omission_ids`) is held here although its tap would run;
   the commit's route-disclosure rewrite of `target_duration_s` (phone montage, no stated length)
   is not repeated; `_marks_route` is derived from the GET's protocol where the commit reads the
   flag stamped at submit.
4. **The kind's own evidence** (`filler_words.unoffered_reason`, `_filler_unoffered`): the card's
   evidence as it is NOW, not as the frozen set saw it: the engine and the capability levers it
   rides on (`SILENCE_CUT_ENABLED`, `capability_for_item`), the creator's decision on the analysis
   (kept the original, already clean), the item's setting, the source-policy fingerprint, the
   analysis status, retakes. It is not part of the set digest on purpose: the evidence is read
   only while the kind's switch is on, so a digest that followed it would make pulling the switch a
   new set (a model run and a pending screen) instead of the instant strip of the same frozen set.
   Caveat: a filler card the analysis newly earns after the freeze appears only when something else
   changes the digest or the 24 h freeze lapses.
5. **The protected-region question** (`_mark_needed`, KRI-901): when the app shows the mark card,
   the draft renders on the phone Talking writer and a live `keep_text_off` object has no current
   mark, the commit writes nothing and asks "Mark the plate" (`mark_needed`). The serve asks
   `protected_targets.missing_object_marks` over the same brief, the creator's marks, the clips and
   the plan. It is a second composition of the gate (the gate itself needs a turn and a sync
   session): seven fixtures compare its verdict with the real tap, there is no structural drift
   guard yet.
6. **A plan to restate** (`pinned.restate_base`): no strategy draft, or a head that is newer
   editor work, leaves the restating cards out.
7. **The card's own proof** (`suggestion_serve._unproven`; every card that is not a restating one:
   the editor-ops cards and the hook). The tap's last word on such a card is its kind's witness on
   the facts as they are, else the registry's rebuild (`pinned.rebuild_action`: still switched on,
   still available, not already in the edit, built again, proven). The serve runs that rebuild on
   the context this GET already loaded, on the card the tap would run: the ring's stored card, or
   on the first read of a set the card the ring write is about to store (`_offered_cards`). A card
   its kind would no longer build is left out: a hook whose own tap already wrote the title
   decision (Undo does not take it back), a duplicates card over a length the creator then stated,
   any card the creator's word now rules out (KRI-129). It is what keeps a frozen set honest with
   `KRIA_SUGGESTION_EDITOR_TRIGGERS_ENABLED` off (nothing then moves the digest when the creator
   speaks after the serve) and after an Undo without the brief (the digest returns to its first
   value). One card the rebuild cannot make costs that card only, never its neighbours. A card
   with no stored or rebuilt copy, or a rebuild that raises, is left out. Pinned by
   `tests/e2e_suggestions/test_r7_serve_proof.py` (GAP-17 closed by it).
8. **The request budget** (`suggestion_serve._request_budget_dead`; every card). With brief binding
   on, every pinned draft pins the creator's whole conversation plus the brief into one request of
   at most `brief_binding.MAX_REQUEST_CHARS` (12,000), and the commit refuses the tap
   `request_too_long` past it, editor-op taps included. `request_budget.request_over_budget` is the
   one predicate: the thread's text now (the commit's own join, `creator_request_text_sync`), the
   tap's own message, and `brief_binding.bound_request` over the brief that holds the typed
   requirement the tap writes (`pinned.tap_updates`). A thread past it serves no v2 card (chips
   unchanged); at the edge each card goes on its own length (a hook, which writes the title,
   before a captions card). Off with brief binding off: nothing is built, nothing is withheld.
   Pinned by `tests/e2e_suggestions/test_r8_request_budget.py` (withheld exactly when the tap is
   refused, across the edge and with a thick brief).

The ring entry holds the cards that rebuild (`_built_cards`), in their built order: one card the
rebuild cannot make (the hook once its decision is in the log, with the set's entry rolled out of
the ring) costs that card, and the ring write asks only that the entry hold the cards on the wire
(`_served_ids`). Pinned by `tests/e2e_suggestions/test_r8_partial_ring.py`.

`refresh_for_item` / `refresh_for_thread` (the editor-triggers refresh) do nothing without the chip
capability: no thread lookup, no Redis debounce key, no run, for everyone or for this creator.

Kill-switch window (accepted). The pin is stamped at submit and read again at claim. If
`KRIA_SUGGESTION_PINNED_TAPS_ENABLED` is switched off, or the creator leaves the chips allow-list,
between those two points, the stamp is ignored and the card's words go to the legacy planner, the
same as a KRI-756 chip. Turning the flag off therefore means "legacy behavior from now on", not
"finish what was pinned". `request_digest` follows the flag too, so a client retry of the same
`client_event_id` across a flip is a digest conflict, not a replay.

## Flags (all default off; subordinate to `KRIA_SUGGESTION_CHIPS_ENABLED`)

`KRIA_SUGGESTION_PINNED_TAPS_ENABLED`, `KRIA_SUGGESTION_SIGNALS_ENABLED`,
`KRIA_SUGGESTION_EDITOR_TRIGGERS_ENABLED`, `KRIA_SUGGESTION_{HOOK_COPY,MUTE_CLIP,FILLER_WORDS,
DUPLICATES,GENERATED_OVERLAY}_ENABLED`. Settings: `KRIA_SUGGESTER_MODEL` (default `gpt-6.1-sol`),
`KRIA_SUGGESTER_MAX_USD`, `AI_OPENAI_SUGGESTIONS_BUDGET_USD` (0 = no spend),
`KRIA_SUGGESTION_REFRESH_DEBOUNCE_S`. Skill: `agents/skills/edit-improver` (see above).

## Ready-state editor target on phone threads (checked by reading the code)

A ready card needs `planner._load_editor_target` to return a target. For phone-rendered edits (and
every edit under `IOS_DEVICE_ONLY_MODE`, which only forces `phone_rendering_for`) it does, once
three things hold: the turn that started the render linked `thread.active_creator_agent_session_id`,
the session's `target_job_id` is the item's current job, and the phone's export completion set the
variant `render_status = "ready"` (`routes/device_render._complete_device_export`). `build_editor_snapshot`
is already device-aware (a phone Talking cut is projected from its pinned recipe). Not every ready
edit has a target: `allowed_op_families` can come back empty (`no_allowed_families`), and a device
Talking / Voiceover variant keeps `sfx` closed until a Save has written `sound_effects`. Those
threads show no ready cards (the safe failure). Not exercised on a device.

**The creator's request wins (KRI-129).** `captions`, `music_level`, `sfx`, `transition` and
`title_animation` read the brief in their own `available` through `services/suggestions/brief_asks.py`
(`spoke(ctx, DIMENSION)`): a live requirement that says anything about the dimension withholds the
card, whether it contradicts or restates the ask. It holds on every path (chip set, deterministic
cards, an agent's pick). The brief is `ctx.kind_inputs["brief"]` (`suggestion_serve.BRIEF_KEY`): no
key = the loader never ran (flags off) = unchanged; a value that is not a brief offers nothing, and
so does a failed read: with a v2 flag on, `suggestion_serve` stores `brief_asks.UNREADABLE` under the
key when the read (or the stage around it) raises, never leaving it absent. The helper is not in
`kinds/` because the registry guard requires every module there to be a kind.

What counts as "said something" is typed data (KRI-667: the server does not guess a meaning from the
creator's words, because every word list needs a twin per language and misses a paraphrase):

- **kind**: the brief writer files every volume, music and sound ask as an `audio` requirement, in
  any language, so an `audio` requirement decides the music and the sound-effects cards. The one
  exception is the footage's own sound lane: a requirement whose typed `clip_sound` facts
  `clip_sound_toggle_ask` reads (a clip's sound turned on or off, what a mute-clip tap writes and
  what "clip sound off except that last line" is) is about what a clip plays and withholds neither
  card, but only while `KRIA_SUGGESTION_PINNED_TAPS_ENABLED` is on: with it off every `audio`
  requirement speaks, exactly as on main (`test_r7_flag_off_brief_asks.py` compares both). A
  `clip_sound` fact that reader cannot parse is not that lane, so the requirement still speaks. An `audio` requirement with no such fact withholds in any wording (GAP-15 stays closed);
  a mis-kinded ask the writer files as `audio` withholds too, which is the safe direction.
- **facts**: `caption*` (captions), `animation` / `text_animation` (title animation),
  `audio_intent: no_music` and `music*` (music), `sfx` / `sound_effect*` / `licensed_sfx*` (sound
  effects), `transition*`. A requirement the writer records for captions or a title animation
  WITHOUT its typed fact no longer withholds the card by its wording (the same silent gap
  DECISIONS.md already accepts for animation).
- **description, read once over the Turkish-aware fold** (`app.text_fold`), never the on-screen
  `literal`: only the two splits with no typed signal. Music against sound effects (both are
  `audio`: a requirement naming sound effects is a sound-effects ask and not a music ask) and
  transitions (no fact and no kind). Both readers are on the raw chat text allow-list
  (`tests/test_raw_chat_text_readers.py`). `tests/e2e_suggestions/test_r5_serve_brief_asks.py` is the
  table; `test_r6_brief_asks.py` pins the clip-sound lane.

The duplicates card reads the brief through `needed_media` (a loader input, `suggestion_serve`):
every clip a requirement names, pins, labels or places is needed and never removed, and so is every
clip when a requirement orders, selects or labels clips in a way that cannot be resolved, or when a
live `timing` requirement carries a `duration_s` fact (the creator stated the video's length: taking
any clip out moves it). Typed fields only; a freeze or a moment's seconds carries the same fact and
withholds as well, because telling them apart takes the creator's words. The card's witness
re-checks the same set at the tap, so a length stated after the card was served refuses its tap
(`test_r6_duplicates_length.py`).

**`PROMPT_VERSION` and ids.** With every flag off, `set_id` and `input_digest` differ from
origin/main by design (the digest hashes `PROMPT_VERSION`); the wire is otherwise identical. Noted at
the constant in `services/suggestion_chips.py`.

**Hook copy gate.** `suggestion_chip_copy.has_lookalike_letter` refuses a letter that only looks
like another (`ᴏ`, `ɡ`, `Ｏ`, `ſ`, `𝗼`, `ﬁ`) and passes an ASCII letter plus accents. It reads the
shared fold (`app.text_fold.strip_marks`), never a private NFD (KRI-786 guard
`tests/kria/test_single_text_fold.py`); `tests/e2e_suggestions/test_r4_serve_lookalike.py` compares
every Unicode letter with a frozen copy of the original check.

## Failure modes the first scaffold was written against

1. A kind with no witness or surface ships unchecked (`registry.problems`, guard test + negative controls).
2. An unregistered kind module, or a stub that starts offering (explicit list, disk-vs-list guard).
3. The engine's kind order moving, which re-ranks ties (order pinned; golden dump of 136 fixture
   contexts byte-identical before and after the move).
4. An import cycle engine <-> registry <-> kinds (context/models import nothing from the engine;
   each entry point is imported first in a subprocess).
5. A mistyped or default-on kind flag (every `kind.flag` must be a real `Settings` field, default off).
6. The model returning an op, an unissued kind or an unissued target (closed schema; `extra="forbid"`).
7. A stored signal outliving its thresholds (per-signal `v`; `signals_version` feeds the set digest).
8. Flag-off wire or `request_digest` bytes drifting (omit-when-empty serializer; digests pinned to
   values captured before the change).

## Postgres-backed suggestion suites and CI

`test_suggestion_freshness_postgres.py`, `test_near_dup_store_postgres.py`,
`test_suggestion_serve_wiring_postgres.py`, `test_kria_pinned_suggestion.py`,
`test_kria_pinned_kinds.py` and `tests/e2e_suggestions` need a migrated `*_test` database. Without
one they skip, unless `NOVA_REQUIRE_POSTGRES_TESTS=1` (`tests/postgres_required.py`), which only the
api test shards in `ci.yml` set: there a missing database fails collection. It is not keyed on
`CI`, because the "Structural evals" job runs `pytest tests/evals/` with `CI=true` and no Postgres
service. Pinned by `tests/evals/test_suggestion_gates_ci_guard.py`. `tests/tasks/test_reaper_jsonpath.py`
follows the same rule at both of its skip sites (no `*_test` database, database unreachable): a
laptop run skips, a run that sets the variable fails. The harness imports
`app.tasks.kria_suggester` before any fixture patches `enqueue_suggester` (`driver.py`,
`conftest.py`): the task binds the name on first import, so importing it under the recorder kept the
recorder for the rest of the process and failed two freshness tests whenever the harness ran first.
`tests/tasks/test_kria_pinned_r5_ci_wiring.py` runs both in a child pytest, and checks that the
screens-leg step of `ci.yml` runs and verifies every `KriaUITests/` test the `kri-756-suggestion-chips`
journey names.

The end-to-end proof (`tests/e2e_suggestions`, `tests/e2e_suggestions/README.md`) drives the real
path for every kind in English and Turkish: GET -> the real worker body with a cassette -> GET ->
`submit_turn` -> `run_kria_turn`. A check that fails because of a defect in code the proof does not
own carries a named `Gap`; the day it passes the run fails with "gap closed, remove the marker".
GAP-4 was closed by the typed duplicate-send signal; GAP-5, GAP-10 and GAP-11 were closed by the serve strip and the Turkish word list above; GAP-17 (a hook served again after its own Undo, refused at the tap) by the card's own proof (step 7).
