# AI suggestion blocks (scaffold)

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
| `services/suggestions/models.py` | `SuggestionKind` row, `Action` union, `Suggestion`, `SuggestionSet`, `SuggestionDigest`, `SuggestionAgentOutput` + `agent_response_schema` / `parse_agent_output`, wire `SuggestionOut` |
| `services/suggestions/context.py` | `SuggestionContext` and the read-only snapshot readers every kind shares; `snapshot_digest` (the `EditorOpsAction.base_digest`) |
| `services/suggestions/registry.py` | `all_kinds()`, `legacy_gate()` (the engine's `CHIP_KIND_GATE`, original order), `enabled()`, `kinds_for()`, `problems()` |
| `services/suggestions/kinds/*.py` | ONE module per kind; the explicit list is `kinds/__init__.py` |
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
| `captions`, `sfx`, `transition`, `music_level` | KRI-756 kinds | `EditorOpsAction` from the row's own `op`, proved by `editor_ops_witness` | ported, live in the engine |
| `default_edit`, `opener_clip`, `title_animation`, `pinned_text` | KRI-756 kinds | none | ported, legacy only |
| `hook` | KRI-756 kind | `CopyDecisionAction` (stub) | legacy ported; v2 stub |
| `mute_clip` | new | `BriefUpdateAction` (`clip_sound` off, `clip:<media_id>`) | stub |
| `filler_words` | new | `ItemSettingAction` (`speech_cleanup_enabled`) + re-render | stub |
| `duplicates` | new | `EditorOpsAction` (`remove_clip`) | stub |
| `generated_overlay` | new | `AsyncGenerateAction` | stub |

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
- **Handled cards never come back.** A refreshed set has a new `set_id`, but a dismissed or applied
  card is hidden on read by its kind and target fingerprint (`suggestion_serve.fingerprint`: kind,
  clip media id or ordinal, text bar, moment), from both `suggestions` and the legacy `chips`
  (the client falls back to chips when the cards run out). The last 32 fingerprints are kept after
  the ring rolls over. A kind with no target (captions, music level, hook) is therefore hidden for
  that creator's thread after one apply, even if they later undo that change.
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
  without writing a draft, and a restated plan holds none of the Save's edits. An editor-op tap is
  built on the saved video, so it re-syncs the session's pointer instead. A restate whose clips
  changed after planning is the same typed refusal, not a failed turn.
- **Reason codes.** The planner-side codes are the constants of `services/suggestions/pinned`; the
  codes of the commit's own stops (`consent_required`, `mark_needed`, `requirements_not_ready`,
  `choice_needed`, `request_too_long`) are the `_PIN_*` strings in `tasks/kria_runtime.py`, plain
  strings so a turn that is not a tap never imports the suggestion engine to build one.
- **A refused tap writes nothing.** A reply that ends a normal turn records what the creator stated
  (KRI-188); a refused tap stated nothing, and its typed requirement would read as the creator's own
  ask (KRI-129). So no brief version is written for it, its receipts are dropped (they judged a
  brief the refusal rolled back) and no "make it without these" question is offered (answering it
  would not apply the card).
- **Undo takes the whole tap back.** The pinned commit keeps its footprint on the draft's execution
  (`result["pinned_undo"]`: the requirement ids it wrote, the card's pin, and for the filler card
  the setting's pre-tap values). `undo_draft` withdraws those requirements as a server waiver in a
  new brief version, restores `speech_cleanup_enabled` / `user_edited` if the tap's flip is still in
  force, and clears the card's applied mark so it is an offer again (a tap on it applies once, not
  twice). What a later turn or the creator changed since is left alone. Limits: a fingerprint a GET
  already folded into the thread's kept list stays (`suggestion_serve.fold_suppressed`), so a new
  set may not offer the card again; and a hook's creative-copy decision stays in the event log
  (`fold_creative_copy(include_suggestions=True)`), so the hook card is still spent after Undo.

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
`KRIA_SUGGESTION_REFRESH_DEBOUNCE_S`. Skill: `agents/skills/edit-improver` (placeholder body).

## Ready-state editor target on phone threads (checked by reading the code)

A ready card needs `planner._load_editor_target` to return a target. For phone-rendered edits (and
every edit under `IOS_DEVICE_ONLY_MODE`, which only forces `phone_rendering_for`) it does, once
three things hold: the turn that started the render linked `thread.active_creator_agent_session_id`,
the session's `target_job_id` is the item's current job, and the phone's export completion set the
variant `render_status = "ready"` (`routes/device_render._complete_device_export`). `build_editor_snapshot`
is already device-aware (a phone Talking cut is projected from its pinned recipe). Not every ready
edit has a target: `allowed_op_families` can come back empty (`no_allowed_families`), and a device
Talking / Voiceover variant keeps `sfx` closed until a Save has written `sound_effects`. Those
threads show no ready cards (the safe failure). Not exercised on a device or against a database.

## Failure modes this scaffold was written against

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
