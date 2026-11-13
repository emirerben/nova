# KRI-1394: chat ops for the editor actions it could not do

## Request and acceptance

KRI-1384 asked that chat do what the iPhone editor does. After KRI-1388 the parity table
(`app/kria/editor_capability_parity.py`) still listed 34 actions in groups chat had no op for:
caption style scope (5), motion speed (1), split to full screen (1), the creator's own song (12),
layer order (4), drawing (9), the speaker's full-screen opening (1) and video shape (1).

The change is accepted when these hold:

- Each new op parses and compiles on a device snapshot (`render_destination: "device"`, the
  phone's capability map, production settings) through the real builder, parser and compiler,
  proven by `tests/kria/test_editor_capability_parity.py`.
- Each op writes a Save section the iPhone reads (named below with its Swift reader).
- An action that stays unsupported says what refuses it.
- A live recording of a plain request applies the op.

## Verdict per group

| Group | Actions | Verdict | Op |
| --- | ---: | --- | --- |
| `creator_song` | 6 | agent_ops | `set_user_song {volume, window_start_s, window_end_s, remove}` |
| `creator_song_audition` | 5 | not_an_edit | Drag brackets (begin/end of the start and end drags) and the lip-sync trim preview. They open an undo step and play the song; the edit is the move or apply they bracket. |
| `creator_song_lipsync_trim` | 1 | agent_unsupported | `applyLipsyncSongTrim` cuts the video at song-matched points the phone plans (`NativeLipsyncSongTrim.plan`). The Save locks a lip-sync window (`user_song_lipsync_locked`), so `set_user_song` refuses it, and no server op plans those cuts. |
| `caption_style_scope` | 5 | agent_ops | `patch_caption_line {cue_id or all, size_px, position_x_frac, position_y_frac}` |
| `layer_order` | 4 | agent_ops | `set_layer_order {layer, z or direction}` |
| `speaker_full_opening` | 1 | agent_ops | `remove_full_screen_opening {}` |
| `video_shape` | 1 | agent_ops | `set_video_shape {orientation, landscape_fit}` |
| `motion_speed` | 1 | agent_unsupported | It sets `motion_scenes[].motion.speed`. A phone edit has no motion scenes: `validate_phone_pilot_recipe` and the authored phone compile refuse them. |
| `split_screen_full_screen` | 1 | agent_unsupported | The Save sections are chat's (`timeline_slots`, `split_screen_sections`), but the cut is planned on the phone (`NativeEditorSession.splitFullScreenResult`): every panel clip splits at the playhead and one panel's footage becomes full-screen clips grown to fill the lost time. No server op plans that yet. |
| `drawing` | 9 | agent_unsupported | New strokes are finger paths a chat request cannot describe. Recolouring, timing, entrance and exit and moving a drawing have no op yet; the snapshot shows drawings read-only. |

### Unsupported counts

| Count | Before (`origin/main`, after #2300) | After |
| --- | ---: | ---: |
| The original 34-action set | 34 | **12** (17 agent ops, 5 not an edit) |
| Every `agent_unsupported` action in the table | 59 | **37** |

The ticket asked for fewer than 12. The original set ends at 12, one over. Five of the 22 that
left the unsupported column are the song-drag gesture steps, reclassified as `not_an_edit`
because they change nothing on their own. The 12 that remain are the 9 drawing actions, motion
speed, split to full screen and the lip-sync trim. The last two are the next candidates: each
needs a planner ported from Swift.

The whole-table number also counts the groups #2300 moved to unsupported because the phone cannot
render them (undo, motion blocks, carousel, camera effects, looks, custom effects, music swap).

## Where the iPhone reads each op

| Op | Save section it writes | Server path | iPhone |
| --- | --- | --- | --- |
| `set_user_song` | `user_song` (`EditorCommitUserSong`) | `_merge_user_song_edit` folds it into the plan the device recipe pins (`_phone_editor_saved_plan_v1.user_song` or the guided revision) | the same section `setUserSongVolume` / `setUserSongStart` / `setUserSongEnd` / `removeUserSong` send (`user_song.volume`, `.window`, `.remove`) |
| `patch_caption_line` | `caption_meta.appearance.line_overrides[<cue id>]`, or `caption_meta.size_px` and `appearance.position_*` for every line | merged key by key into `caption_editor_style` | `NativeEditorRenderCompiler` reads `line_overrides` by cue id; `writeCaptionStyle` writes the same keys |
| `set_layer_order` | `media_overlays[].z` (cards on top) or `visual_blocks[].z` (picture blocks), dense from 0 | `phone_subtitled_plan._compile_overlay_track` and `phone_editor_visuals.compile_editor_media_track` draw by `(z, start_s, id)` | the preview sorts cards and media blocks by `z`; `setLayerZ` writes it |
| `remove_full_screen_opening` | `speaker_background.full_screen_windows` without the window that starts at 0 | `_validate_speaker_full_screen_windows`, gated by `speaker_full_opening_unavailable_reason` | `removeFullScreenOpening` drops the same window |
| `set_video_shape` | `orientation`, `landscape_fit` | `validate_orientation_section`, `_validate_landscape_fit_section` | `setVideoShape` sends the same two sections |

Each op is offered only where the editor's capability map opens it on the phone: `user_song`
(a creator song and the editor's song controls), `caption_style_scope` plus a cue list,
two or more pictures in a stack, the full-opening gate (flag, `speakerFrameWindows` and the
app build) and an open shape axis. Every other snapshot and prompt is byte-identical.

## Failure modes

Written before each handler. Each is a case in `tests/kria/test_kri1394_chat_ops.py`, which runs
the real snapshot builder, parser, compiler and Save model.

| Id | Failure mode | Behaviour |
| --- | --- | --- |
| U1 | A lip-sync song's start or end moves. | Refused in the Save's words: "Lip-sync keeps the song where you filmed it." Volume and removal still apply. |
| U2 | A window under `MIN_PLAYABLE_SONG_S` (1 s). | Refused with the start, end and minimum named. |
| U3 | A start later than the song's last playable second. | Refused. |
| U4 | No song, or no song controls. | The family is not offered; the reply says chat cannot change the song on this edit. |
| U5 | A volume outside 0..1. | Clamped and rounded to 0.01, as the slider does. |
| U6 | A removal with other fields. | The Save gets the removal only. |
| U7, U8 | A second turn loses the first; `removed: false` un-removes. | Turns merge field by field; a removal stays; a removed song refuses further changes. |
| C1 | A cue id the snapshot does not list. | `stale_target`. |
| C2 | Values outside the phone's clamps. | Clamped to size 36-160, x 0.05-0.95, y 0.10-0.90. |
| C3 | Both or neither of `cue_id` and `all`. | `invalid_value`. |
| C4 | No `caption_style_scope` (an older build). | Not offered; refused in plain words. |
| C5 | "Every line" keeps a line's own value. | The key leaves every override, and a size leaves each cue's own size (cue ids kept). |
| C6, C7 | A line loses its other keys; the next turn does not see the change. | Keys merge; the draft projects the appearance onto `caption_editor_style`. |
| L1 | An unknown layer. | `stale_target`. |
| L2, L3 | Equal z values; a z past the top. | Order follows the renderer `(z, start_s, id)`; z is clamped and comes out dense. |
| L4 | A move that changes nothing. | The compiler refuses ("already there"), so no reply claims it. |
| L5 | A full-screen card. | Refused: it covers the video and has no layer. |
| L6 | A picture move touches the cards. | Only the picture stack is written. |
| O1, O2, O3 | No opening; the gate is closed; the ending or fill goes too. | Refused, refused, and only the opening window leaves. |
| S1, S2 | A closed axis; the same value. | Refused; the compiler refuses "already has that shape". |

## Live evidence

`gemini-3.1-pro-preview`, prompt `2026-11-13-v126`, development ledger,
`--usage-purpose live_eval --max-cost-usd 2 --approve-reservation`. Priced first: 5 calls, $0.80
expected, $0.47 largest reservation.

| Thread | Request | Op the model emitted |
| --- | --- | --- |
| `action_creator_song` | Make my song quieter. | `set_user_song {volume: 0.6}` |
| `action_caption_style_scope` | Make the second caption bigger. | `patch_caption_line {cue_id: cue-2, size_px: 100}` |
| `action_layer_order` | Put the second photo in front of the first one. | `set_layer_order {layer: card-2, direction: front}` |
| `action_speaker_full_opening` | Start with the background behind me, no full-screen intro. | `remove_full_screen_opening` |
| `action_video_shape` | Don't crop my landscape clips, I want to see the whole frame. | `set_video_shape {landscape_fit: fit}` |

Two recordings were redone, and both earlier answers are kept here because they found defects.

- The first layer-order request named "the beach photo". The model asked which photo, correctly:
  OVERLAY CARDS does not print card labels. The request now names the cards by order.
- The first caption recording chose 72 px for "bigger". The snapshot showed the shared size as
  unset, while the phone draws 78 px. The snapshot now carries the drawn look
  (`caption_look_from_variant`), and the re-recorded answer is 100 px.

The prompt examples were also reworded so that no eval request copies one.

| Run (`rf-kri1394-20261113-`) | Calls | Settled |
| --- | ---: | ---: |
| `a` (first pass, earlier fragments) | 5 | $0.3795 |
| `b` (layer order, request reworded) | 1 | $0.0688 |
| `c` (all five, final prompt) | 5 | $0.3430 |
| `d` (caption, drawn size in the snapshot) | 1 | $0.0700 |
| Total | 12 | **$0.8613** |

## Replay

Offline replay of the per-action set, scores regenerated in
`request-following-baseline-scores.md`:

| | Before | After |
| --- | ---: | ---: |
| Per-action threads | 39 | 44 |
| Met | 31 | 36 |
| Unmet | 8 | 8 |

The five new threads are met. The eight unmet are unchanged: the groups the phone cannot render
(declared refusals) and the `move_split_screen` miss.

## Open

- **A chat song change seen from the editor.** On approval the runtime runs the real Save, which
  merges `user_song`. But the phone's draft loader (`NativeEditorDocument`) decodes no
  `user_song` section. If the creator opens the editor before approving and saves from there, the
  Sounds tab shows the saved song and the chat's song change is dropped. The other four ops'
  sections (`caption_meta`, `media_overlays` / `visual_blocks`, `speaker_background`,
  `orientation` / `landscape_fit`) are decoded. Fixing it is an iOS change.
- **Card labels.** OVERLAY CARDS lists a card's id, kind, time and place, not its label, so
  "the beach photo" cannot be resolved.
- **Background receipts.** `EditorDiff` has no `speaker_background` lane (open since #2300), so
  `remove_full_screen_opening` compiles to an empty diff like every background op.
- **Captions kept as text rows** (guided edits) are not offered `patch_caption_line`: lines are
  addressed by cue id.
- **Lip-sync trim and split to full screen** need their Swift planners ported.
