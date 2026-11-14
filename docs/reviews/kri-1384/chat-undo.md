# KRI-1384: chat "undo that" on the iPhone

## Request and acceptance

A creator who types "undo that", "revert that" or "go back to before" in iPhone chat right after
a chat edit gets that edit taken back, and the reply says what was put back. KRI-1388 left this
`agent_unsupported`: no ordinary chat turn stored what it replaced, the server never built
`history_state`, and `undo_last_edit` had no compile handler. KRI-1392 needs the undo served as an
in-place edit.

Accepted when:

- After a chat edit, "undo that" from a phone build at protocol 50 restores every lane and row the
  turn changed to what the creator had before it, as a new draft revision, and the receipt names
  what it put back.
- An undo never overwrites the creator's own work: a hand edit made after the chat turn to
  something the turn changed takes the offer away, and a hand edit anywhere else survives.
- The live iPhone editor shows the undo. A chat edit that returns a lane to the saved video must
  reach an editor that already staged the edit.
- Anything chat cannot undo exactly is refused with an honest reply, and nothing is written.

## What blocked a server-only fix

The iPhone editor stages a chat head's lanes by comparing each against the saved video
(`DraftSnapshot.stagedChatEdit`). A lane equal to the saved video stages nothing, and
`runPromptRevisionSync` then keeps the editor's earlier staged copy (`onlyChatStaged, staged ==
nil` returns). So an undo back to the saved value never reached a live editor, and every shipped
build would have said "undone" while still showing the edit. The editor is warmed while the chat
is open and sends its staged lanes with every turn, so this is the common case, not an edge.

Fix on the phone: `restoredChatSections` reports the lanes a newer head carries at the saved
value, and `dropRestoredChatSections` returns each one the chat staged and the creator has not
touched since to the clean document (one Undo brings the edit back). Protocol 50 declares it; the
server offers chat undo only from `chat_undo_min_client_protocol` (50).

## The rule

Undo is offered only when all of these hold (`kria_editor_undo.undo_point`, re-derived under the
head lock at commit):

1. The sending build is at protocol 50 or later.
2. The head draft is the last chat turn's own draft, on the same job, built on the variant's
   current render. A Save, a render, another chat turn or a re-plan moves the head or the
   generation, so nothing is offered.
3. That turn stored a sealed record: replaying the undo over the turn's own draft (restore,
   validate, dump, merge) gives back exactly the lanes the creator had before, and the editor
   looks the same to the copilot (`seal`). A lane the record cannot restore exactly makes the turn
   not undoable instead of half undone.
4. When the phone sends its editor state, it staged that very draft (`draft_revision`) and every
   lane and row the undo would restore still holds what the chat wrote. Other rows and lanes may
   differ; the undo keeps them.

Which turns seal is pinned by `src/apps/api/tests/kria/test_chat_undo_seal_table.py`. It compiles
every phone probe of the parity guard (88 probes; the 7 in groups a phone cannot run are skipped)
and seals each record over a clean editor the way `kria_runtime` does.

These seal: text (add, edit, remove, timing, style), captions (text, timing, emphasis, meta,
the animation default, one line's or all lines' size and position), the timeline (trim,
duration, in point, reorder, split, remove, duplicate, transitions, tilt, crop, speed, source
gain, replace), split screens, sound effects, picture cards and picture blocks (including their
layer order), mix and music, the speaker background, its frame and focus, removing the
full-screen opening (`remove_full_screen_opening`) and the video's shape (`set_video_shape`:
orientation and landscape fit). Two more seal on a saved video but not on their probe's data:
`set_title` on a video that already has a title, and `remove_visual_media` on picture rows as a
Save stores them. The seal table pins both
(`test_a_refusal_that_depends_on_the_probe_data_seals_on_a_saved_video`).

These do not, so chat refuses to undo the turn:

| Turn | Why the record cannot put it back exactly |
| --- | --- |
| `set_title` on a video with no title yet | The compiler records the old title only from `intro_text`, so the record has no pre-turn title, and a commit cannot clear one (`title: null` means leave it). |
| `remove_visual_media` on a picture row with no `asset_id` / `src_gcs_path` | The commit schema rejects the pre-turn row, so the record cannot be replayed. Every row a Save stores has both fields. Only the probe's thin fixture row lacks them, and a recorded thread pins that fixture. |
| `set_user_song` | The record holds the derived song state (`song_state`: volume, start and end as the video plays them), not the creator's own `user_song` section. The replay writes the song's end back as if the creator had set it (`creator_end_s` appears), so the editor differs from the pre-turn one. Undoing it needs the record to keep the creator's section, and the iPhone to report a restored song (#2320 stages the song outside `stagedLaneKeys`, so `restoredChatSections` never names it). |

### How the gaps from #2311 were closed

KRI-1394 (#2311) added the ops that measured as not sealing. Each was a gap in the restore record or
in the seal comparison, not in the undo rule.

- **A restore that deletes a key** (`patch_caption_line`, the caption animation default). A
  commit's None means "leave it", and `merge_editor_draft` merges `caption_meta` and `mix` key by
  key, so the record's "this key had no value before" never reached the draft.
  `kria_editor_undo._replay` now builds the draft the undo writes. That is the merge, minus every
  keyed-lane key the record holds as None. The commit stores that draft (`CompiledUndo.draft`),
  and `seal` checks that same draft. A key with a pre-turn value is set back, not deleted, so a
  creator's own override on another line before the turn stays. A hand edit after the turn to a
  key the undo restores takes the offer away, because `_untouched` compares every restored key,
  deleted ones included. A hand edit to another key survives.
- **Schema defaults** (`set_layer_order` and `remove_visual_media` on picture blocks). Rows read
  off the variant lack defaults the replayed rows carry. `seal` now compares each lane on both
  sides through the commit schema (`_canon`), which is how the Save persists a lane. A Save stores
  validated blocks, and the schema ignores unknown keys. A value the schema rejects compares raw.
  The copilot-visible editor check stays raw.
- **Unowned lanes** (`set_video_shape`). `orientation` and `landscape_fit` are stripped from every
  scoped commit (`repair_payload`) because no plan-review section owns them. That stays. The
  restore record now carries them under `look` when the data has them, and only an unscoped
  chat turn's data does.
- **A turn that changed nothing.** If no lane the turn wrote differs from what the creator had,
  `seal` returns None, so the receipt never claims an undo that put nothing back.

The iPhone needs no change for these lanes. `caption_meta`, `visual_blocks`, `orientation` and
`title` are in `DraftSnapshot.stagedLaneKeys`. A newer head at the saved value reaches the editor
through `restoredChatSections`. A head that still differs from saved in its representation (a
draft `caption_meta` with `font_set: false`) re-stages the lane wholesale through
`mergeChatDraft`, so the editor shows the pre-turn content. `landscape_fit` is not a staged lane, so
the phone never showed the chat's fit change either. An editor state without the chat's value
refuses the undo (`_untouched`). That staging gap belongs to KRI-1394, not to undo.

An undo turn stores no record, so a second "undo" is refused rather than chained or toggled.
`repeat_last_edit` stays unsupported (the `redo` parity group): it replays the web client's op log.

## Failure modes

Written before the code. Tests are in `src/apps/api/tests/kria/test_chat_undo_postgres.py` (real
Postgres, real planner, parser, compiler and drafts; only the model is scripted) and
`src/apps/ios/Tests/KriaTests/ChatDraftStagingTests.swift`.

| Id | Failure mode | Covered by |
| --- | --- | --- |
| F1 | A hand edit after the chat turn to a changed row is overwritten | `test_a_hand_edit_on_what_the_chat_changed_is_never_undone` |
| F2 | A hand edit elsewhere is lost | `test_a_hand_edit_elsewhere_survives_the_undo` |
| F3 | Undo after a Save | `test_undo_is_not_offered_when_the_phone_or_the_video_moved[rendered_since]` |
| F4 | Double undo chains or toggles | `test_undo_puts_back_the_chat_turn_and_says_what_it_undid` (second undo) |
| F5 | Undo with no prior chat turn | same test, first turn |
| F6 | Render in flight or finished since | `[rendered_since]` |
| F7 | Re-plan onto a new job | `[replanned]` |
| F8 | Repeat offered without a handler | `history_state` carries no `last_turn_summary` (asserted in the first test) |
| F9 | Something lands between planning and commit | forced-op half of the F1 test: refused under the lock, nothing written |
| F10 | A lane the record cannot restore exactly | `seal` requires an exact replay (every lane the turn changed); `test_chat_undo_seal_table.py` pins which turns seal |
| F11 | Old app build | `[old_build]`, `[web]` |
| F12 | The phone drops a lane the creator touched | `testANewerChatHeadThatPutsALaneBackLeavesALaneTheCreatorEditedSinceAlone` |
| F13 | The phone re-encodes an untouched staged lane | `testAnUntouchedStagedChatLaneIsSentBackAsTheHeadWroteIt` |
| F14 | The receipt overclaims | the receipt is built from the restored lanes' diff |

The gaps closed after #2311 add these, in `src/apps/api/tests/kria/test_chat_undo_seal_gaps_postgres.py`
(the parity probe's own variant as a real job, the real runtime, a phone at protocol 50):

| Id | Failure mode | Covered by |
| --- | --- | --- |
| U1 | A restore that must take a key away keeps the chat's value | `test_undo_puts_back_exactly_what_the_creator_had[caption line size]`, `[all caption lines]`, `[caption animation default]` |
| U2 | The delete takes a key the creator wrote after the turn | `test_a_hand_edit_after_the_chat_turn_takes_the_undo_away` (same key, refused); `test_a_hand_edit_to_another_caption_key_survives_the_delete` |
| U3 | The delete takes what the creator had before the turn | `test_the_undo_keeps_a_caption_override_the_creator_had_before_the_turn` |
| U4 | Rows read off the variant compare unequal only for schema defaults | `[picture layer order]`; the seal table's saved-rows case for `remove_visual_media` |
| U5 | Lanes no plan-review section owns are left out of the record | `[orientation]`, `[landscape fit]` |
| U6 | The seal checks a replay the commit does not write | `seal` and the commit both read `_replay` (`CompiledUndo.draft`) |
| U7 | A hand edit to the lane after the turn is overwritten | `test_a_hand_edit_after_the_chat_turn_takes_the_undo_away`, all six cases |
| U8 | A turn that changed nothing is offered and the receipt claims an undo | `seal` refuses a turn with no changed lane (a no-op `set_layer_order` does not even compile) |

## Evidence

- End to end on Postgres: a chat turn turns the hook red; "Undo that" from a phone state that
  staged it writes revision 2 with the hook back to `#FFFFFF`, on the same generation (chat
  edits stay unsaved on the phone until Save). The receipt reads "I undid my last change (Colour →
  white (#FFFFFF) on the title). Save when you're happy with it."
- Live model: `action_history` ("Undo that.") and `action_history__revert` ("Revert that, I
  liked it before.") on `gemini-3.1-pro-preview`, prompt `2026-11-13-v125`, development ledger,
  test run `rf-kri1384-undo-20261113-a`, `--max-cost-usd 2 --approve-reservation`. Priced at
  $0.32 expected ($0.47 reservation each); settled $0.1261. The model emitted `undo_last_edit` on
  both and the parser kept it. Per-action set, rebased on #2311: 38/45 met (main has 36/44).
- This change does not edit the prompt. With `can_undo_last_turn=True` the v125 rules already emit
  the op; with no history state they already refuse plainly. Main's v126 (#2311) adds the five new
  op sections and leaves the history rules as they were.

## Open

- Native: the iOS reconcile is unit-tested in the simulator test target; a device run is not
  part of this change.
- KRI-1392 decides when "revert" is served by this undo versus a rebuild.
