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

Which turns seal, measured by compiling each phone probe of the parity table and sealing its record
over a clean editor: text (add, edit, remove, timing, style), captions (text, timing, emphasis,
meta), the timeline (trim, duration, in point, reorder, split, remove, duplicate, transitions, tilt,
crop, speed, source gain, replace), sound effects, picture cards, mix and music, the speaker
background and camera effects all seal. `set_title`, `remove_visual_media` and the
caption-animation `set_caption_meta` do not (the record has no exact pre-turn value for them), so
chat refuses to undo those turns.

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
| F10 | A lane the record cannot restore exactly | `seal` requires an exact replay (every lane the turn changed) |
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
  both and the parser kept it. Per-action set: 33/40 met (was 31/39).
- The prompt is unchanged. With `can_undo_last_turn=True` the v125 rules already emit the op; with
  no history state they already refuse plainly.

## Open

- Native: the iOS reconcile is unit-tested in the simulator test target; a device run is not
  part of this change.
- KRI-1392 decides when "revert" is served by this undo versus a rebuild.
