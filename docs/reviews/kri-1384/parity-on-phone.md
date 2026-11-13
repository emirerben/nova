# KRI-1388: editor parity proven on a phone snapshot

## Request and acceptance

`app/kria/editor_capability_parity.py` answers "can the agent do this?" for every public action
of the iPhone editor. Its test checked only that op and field names exist. Seven `agent_ops`
groups passed that check and still could not run from iPhone chat, so the table told a creator,
and every later ticket, that chat could do things it could not.

The change is accepted when these hold:

- Every op of every `agent_ops` group survives the real copilot parser and the real compiler on a
  device snapshot (`render_destination: "device"`, the capability map a phone advertises when
  the lane's gate is open, production settings profile), keeps every field the group names, and
  changes a lane.
- A group the phone cannot run is `agent_unsupported`, with the reason and where it is refused.
- An `agent_unsupported` group that starts to compile on a phone fails the test, so the table
  cannot understate either.
- The copilot says honestly that it cannot do a refused action instead of emitting a doomed op or
  a false reason.

## What the test runs

`tests/kria/test_editor_capability_parity.py`:

- `test_every_agent_op_runs_from_iphone_chat[<group>.<op>]`: one case per op an `agent_ops`
  group names (65 claims over 31 groups). Each case builds the snapshot through
  `build_editor_snapshot`, parses a reference op with `EditCopilotAgent.parse`, checks the named
  fields survive, compiles it with `compile_editor_ops` on the same variant, and checks a lane
  changed (the compiled `before` record or the receipt diff). Probes are `SCENARIOS` (one plain request per group, also the
  per-action eval threads) plus `PHONE_OP_PROBES` (every other op), both in
  `tests/evals/request_following/author_action_fixtures.py`, through the same `build()` that
  authored the recorded threads.
- `test_an_unsupported_group_is_refused_on_a_phone[<thread>]`: each probe of a non-`agent_ops`
  group must be refused.

On the merge base (`786f0d7f4`) with the new test: 16 failed, 67 passed. The failures are the
seven groups below plus two claims the per-op probes found (`captions.patch_text_appearance`,
`clip_add_remove.remove_visual_media`). `clip_look_and_transition.set_transition` also failed
there, only because its probe now carries the renamed group. On this branch: 77 passed.

## Verdict per group

| Group (before) | Verdict | Evidence |
| --- | --- | --- |
| `history` (undo/redo) | Unsupported | `undo_last_edit` needs the last chat turn's pre-edit lanes. Only a flagged plan-review update stores them (`plan_review_before`, `kria_runtime.py`, KRI-442, behind `LIVE_PLAN_REVIEW_ENABLED`). The server never builds `history_state`, so the parser drops the op. `repeat_last_edit` replays the web client's local op log. |
| `visual_blocks_and_motion` | Unsupported | `validate_phone_pilot_recipe` raises "Motion scenes await native parity and device qualification"; `phone_authored_timeline` refuses `motion_scenes`; `motion` is not a portable family. |
| `carousel` | Unsupported | `NativeEditorRenderCompiler` throws `unsupportedLane("carousel")`; `phone_authored_timeline` and `phone_voiceover_montage_plan` refuse `carousel_moment`; no portable handler. |
| custom effect (`apply_custom_effect`) | Unsupported, now in `clip_look` | `customEffects` is vocabulary only (docs/reviews/kri-29/capability-matrix.md, local-later); `phone_authored_timeline` refuses `custom_effects`. |
| `camera_effects` | Unsupported | `validate_phone_pilot_recipe` raises "Camera effects await native parity and device qualification"; `_PHONE_UNSUPPORTED_TOP_LEVEL` closes `camera_effects` on a device variant. Cloud edits keep the ops. |
| music swap (`swap_music`) | Unsupported, now `music_swap` | The authored phone compile only re-plays the pinned library asset; a guided phone plan refuses a `music` lane (`musicBed`); the Talking and Voiceover Save sections have no `music`. |
| look preset (`set_look_preset`) | Unsupported, now `clip_look` | `_PHONE_UNSUPPORTED_CLIP_OPERATIONS` closes `clips.looks` on device (golden_hour compiles only on exact-canvas, unrotated sources); `NativeEditorSession.canSetClipLook` refuses when `rendersOnDevice`. |
| `captions` claimed `patch_text_appearance` | Claim removed | The chat snapshot's appearance inventory models text bars only (`_text_appearance_inventory`), never caption targets. Caption looks go through `set_caption_meta`. |
| `clip_add_remove` claimed `remove_visual_media` | **Works now** | See below. |

`PHONE_RENDER_VERIFIED_FEATURES` in production names `cameraEffects`, `customEffects`,
`goldenHourLook`, `motionScenes` and `musicBed` (`tests/_prod_profile.py`). That does not open
these lanes: the refusals above are unconditional, or (looks) closed by the device capability
clamp regardless of the token.

## Made to work: removing a picture from a phone edit

`_removable_visual_media` required `base_video_path`, the clean base a cloud edit re-burns over.
A phone edit never has one, so chat could never remove a picture block the creator added on the
phone, though the editor's own Save can (KRI-1142 `phone_media`). The gate now admits a device
variant; the clamped `visual_blocks` capability, which opens only where a device Save compiles
picture blocks, stays the gate.

Failure modes, written before the change, and what covers each:

| Id | Failure mode | Covered by |
| --- | --- | --- |
| V1 | A device edit whose Save cannot compile picture blocks is offered removal. | The capability check runs first and is unchanged (`test_visual_removal_rejects_uneditable_or_text_linked_media[disabled]`). |
| V2 | A cloud edit without a clean base is offered removal. | Unchanged for cloud variants (`...[no_base]`). |
| V3 | A lyric variant or a block linked to a label bar is removed. | Unchanged filters (`...[lyrics]`, `...[linked_text]`). |
| V4 | An AI-placed or motion block is removed. | `kind == "media"` and `origin == "user"` filters unchanged (`...[ai_origin]`). |
| V5 | The removal compiles into a field the iPhone does not read. | The payload is `EditorCommitRequest.visual_blocks`, the same section the phone editor saves (`NativeEditorDocument.swift` `visualBlocks = "visual_blocks"`, decoded at line 1000). The authored phone compile rebuilds its media track from `visual_blocks` (`phone_authored_timeline.compile_editor_media_track`). |
| V6 | The live model does not use the new family. | `action_clip_add_remove__visual_media`, recorded live: the model emitted `remove_visual_media` with `target_ids: ["photo-1"]`. |

## Copilot honesty

The recorded refusals for camera effects, carousel, looks, custom effects and motion were already
plain ("Camera effects aren't available for this edit."). Two were not:

- Undo said "There is nothing to undo right now." The prompt told it to. Prompt `v125`: with no
  HISTORY STATE it says it cannot undo from chat, never that there is nothing to undo, and offers
  to change the specific thing back. The same fix for "repeat that".
- Swap said "there are no alternative tracks available". A device edit with a matched song now
  carries an AUDIO LIMITS note: "This iPhone edit keeps the song it was made with; you cannot
  change it to a different song from chat."

The `v125` wording is not measured live: `action_history` and `action_music_swap` keep their
baseline recordings (v122).

## Replay

Offline replay of the per-action set, scores regenerated in
`request-following-baseline-scores.md`:

| | Before | After |
| --- | ---: | ---: |
| Per-action threads | 38 | 39 |
| Met | 30 | 31 |
| Unmet | 8 | 8 |

No group flips from unmet to met: the seven stay unmet, now as declared refusals. The new met
thread is `action_clip_add_remove__visual_media`. Renamed threads keep their recordings
(`action_clip_transition`, `action_clip_look`, `action_clip_look__custom_effect`,
`action_music_swap`). The report's turn time moved from p50 10.9 s / p95 37.7 s to 10.2 s / 28.2 s.
That is a nearest-rank artifact of adding one 6.8 s call to 79 (the 76th of 80 values is the
old 75th), not a speedup.

## Live cost

One call, `gemini-3.1-pro-preview`, prompt `2026-11-13-v125`, development ledger, test run
`rf-kri1388-20261113-a`, `--max-cost-usd 2 --approve-reservation`. Priced first at $0.16
(reservation $0.47). Settled **$0.0644** (29,043 tokens in, 141 out, 389 thought).

## Open

- **Chat undo.** Built in [chat-undo.md](chat-undo.md): every chat turn stores a sealed restore
  record, the phone build at protocol 50 drops a staged lane a newer head puts back, and a second
  undo is refused rather than redone.
- **Receipts for background edits.** `EditorDiff` has no `speaker_background` lane, so every
  background op compiles to an empty diff. A pinned suggestion tap that only changes the
  background would be refused as "no change".
