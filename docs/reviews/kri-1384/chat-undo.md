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

These seal: text (add, edit, remove, timing, style), captions (text, timing, emphasis, meta), the
timeline (trim, duration, in point, reorder, split, remove, duplicate, transitions, tilt, crop,
speed, source gain, replace), split screens, sound effects, picture cards (including their layer
order), mix and music, the speaker background, its frame and focus, and removing the full-screen
opening (`remove_full_screen_opening`).

These do not, so chat refuses to undo the turn:

| Turn | Why the record cannot put it back exactly |
| --- | --- |
| `set_title` | The title lane has no exact pre-turn value in the record. |
| `remove_visual_media` | The removed picture block does not come back as it was. |
| `set_caption_meta` (caption animation) | The animation phases are not restored exactly. |
| `patch_caption_line` | The record restores `caption_meta` by value and cannot delete a key, so the line's `appearance.line_overrides` entry (or the all-lines size and position) stays after the replay. |
| `set_user_song` | The replayed song lane gains `removed: false`, and the editor it projects differs from the pre-turn one. |
| `set_layer_order` on a picture block | `visual_blocks` rows come back schema-normalized (version, timing mode, transitions), not equal to the pre-turn rows. On a picture card (`media_overlays`) it seals. |
| `set_video_shape` | No plan-review section covers `orientation` or `landscape_fit`, so the record is empty for them. |

The last four arrived with KRI-1394 (#2311). Each is a gap in the restore record, not in the undo
rule. Undoing them needs the record to cover the lane exactly.

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
