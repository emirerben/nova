# KRI-1394: chat makes part of a split screen full screen

## Request and acceptance

The iPhone editor's split panel has "Full screen from playhead" and "Full screen until playhead"
(`NativeEditorSession.makeSplitFullScreen`). KRI-1394 left it `agent_unsupported`: the Save
sections are chat's, but the cut is planned on the phone (`splitFullScreenResult`) and no server
op planned it. The ticket asked for fewer than 12 unsupported actions in the original 34-action
set; it ended #2311 at 12.

Accepted when:

- A server planner gives the same timeline, sections and moved lanes as the Swift planner on
  shared cases, asserted by both a pytest and an XCTest against one fixture file.
- The op parses and compiles on a device snapshot through the real builder, parser and compiler
  (`tests/kria/test_editor_capability_parity.py`) and writes the Save sections the phone writes.
- Every place the port does not cover what the phone does is refused in words, not drafted wrong.
- A live recording of a plain request applies the op.

## Can the server plan it?

Yes. `splitFullScreenResult` reads the timeline rows, the section, the playhead and the selected
clip's panel. It reads two facts from the files: each source's real length
(`resolvedSources[clip].asset.duration`, used only to cap how far a clip grows at its end) and
whether a source is a photo (a photo grows without limit). No frames, no faces. The server has
both facts: the measured length of each original (`immutable_video_source_duration`, the length
the phone reported for the original) and each source's kind in the ready source catalog.

The port is `src/apps/api/app/kria/split_full_screen.py`: `plan_split_full_screen` (with
`piece(of:)`, `NativeClipSourceClock`, `fitCollapseSectionDurations` and `splitScreenIsValid`)
and `remove_time` (`NativeSplitTimelineRebase.removeTime`, the lane step `makeSplitFullScreen`
runs instead of the serial-unit rebase).

## Proof the planners agree

`src/apps/api/tests/fixtures/split_full_screen.json` holds 24 cases. The server generates the
expected answer (`python -m tests.kria.test_split_full_screen_parity`);
`tests/kria/test_split_full_screen_parity.py` asserts the file equals the server output, and
`src/apps/ios/Tests/KriaTests/NativeSplitFullScreenParityTests.swift` runs the real editor action
on each case and compares the timeline section as the Save writes it, the sections and the moved
captions, sound effects and cards.

Cases: the selected panel, no selection, a selected panel with no footage there (the phone falls
back); a gap filled forward and backward; footage that runs out by its stored length, by the
file's measured length (shorter than stored), and with neither known; lanes after lost time and
inside it; until-side mirrors; a photo; 2x speed, a rate derived from `source_end_s`, a held
frame; a crossfade in the panel on both sides (the lead clip's transition into the section becomes
a cut); a collapsing three-panel section with each panel selected; and the refusals (within 0.5 s
of either edge, exactly 0.5 s in, nothing left in the split, the playhead outside the section).

Run on the simulator (iPhone 17 Pro, iOS 26.5) with `NativeSplitGapTests`: 24 tests, 0 failures.
The numbers also match the hand-written assertions `NativeSplitGapTests` already makes for the
same cases. A red control planted three wrong answers in the fixture (a slot's `in_s`, a moved
sound's `at_s`, a refused case marked planned); the Swift test failed on all three.

## The op

`make_split_full_screen {section_id, at_s, side: "from" | "until", panel?}`. `at_s` is the
playhead in video seconds; `panel` is the selected clip's panel. The snapshot's
`split_full_screen` section lists each section that can be cut with its `at_s` range, only where
split screen is available on a device edit, so every other snapshot and prompt is byte-identical.
Prompt `2026-11-14-v128` teaches it in `prompts/edit_copilot_ops/split_full_screen.txt`, appended
only with that section.

The compile writes `timeline_slots` and `split_screen_sections` and, when time is lost, every lane
`removeTime` moves that changed: `caption_cues` (and their words), `sound_effects`,
`media_overlays`, `drawings`, `camera_effects`, `speaker_background` windows and `visual_blocks`.
An unchanged lane is not sent. The serial-unit canvas rebase every other split edit gets is
skipped, as the phone skips it (`rebasesSplitLanes: false`).

## Failure modes

Written before the op. Planner cases are in the parity file; op cases in
`tests/kria/test_kri1394_split_full_screen.py`.

| Id | Failure mode | Behaviour |
| --- | --- | --- |
| F1 | Offered where the phone cannot plan it or this port does not move what the phone moves: no split screen, a narrated edit (clips refit to the voiceover), a Talking source edit (lanes follow the speech), a section under 1 s. | No `split_full_screen` section; the reply says part of a split can't go full screen from chat on this edit. |
| F2 | A section the snapshot does not list. | `stale_target`. |
| F3 | A moment within 0.5 s of an edge or outside the section. | `invalid_value` naming the range. |
| F4 | A panel the section does not have. | `invalid_value`. |
| F5 | A named panel with no footage on that side. | Refused. The phone's tap falls back to the top-most panel with footage; for a named request that would be a silent override (KRI-129). Without a panel the fallback is the phone's. |
| F6 | Another clip or timeline op in the same bundle. | Refused, before or after: its lanes would follow a different rebase. |
| F7 | Text the phone re-times (`GuidedLabelRebase`: labels, titles, pins, any free text on a clip-timeline edit) over the section, or after it when time is lost. | Refused. Text before the section, caption bars and lyric bars block nothing. Whole-video text blocks nothing while the length is unchanged. |
| F8 | Lost time: lanes after it stay behind, a lane inside it survives, an untouched lane is sent. | Lanes after it move earlier by its length, a lane inside it goes, only changed lanes are sent. |
| F9 | Nothing lost and a lane sent anyway. | Only the timeline sections are sent. |
| F10 | The Save model refuses the sections. | Every compiled payload validates as `EditorCommitRequest`. |

## Live evidence

`gemini-3.1-pro-preview`, prompt `2026-11-14-v128`, development ledger, test run
`rf-kri1394-split-20261114-a`, `--usage-purpose live_eval --max-cost-usd 2 --approve-reservation`.
Priced first: 1 call, $0.16 expected, $0.47 reservation. Settled **$0.0715** (30,097 tokens in,
173 out, 772 thought).

| Thread | Request | Op the model emitted |
| --- | --- | --- |
| `action_split_screen_full_screen` | After two seconds, let the bottom video take over the whole screen. | `make_split_full_screen {section_id: duo, at_s: 2.0, side: from, panel: 1}` |

The prompt's example uses another section, side and moment, so the request copies none of it.
Offline replay of the per-action set: 46 threads, 39 met, the same 7 unmet
(`request-following-baseline-scores.md`).

## What is not proven

- `speaker_background` windows, `drawings`, `visual_blocks` and `camera_effects` are moved by the
  same `remove_time` code the fixture proves for captions, sounds and cards, but the Swift
  fixture does not decode them, so their parity is by reading, not by test.
- Text is never re-timed by this op; where the phone would re-time it, the op refuses (F7). A port
  of `GuidedLabelRebase` over split sections would remove that refusal.
- The section's start comes from the server layout (`layout_split_timeline`), the clock
  `split_screen_timeline.json` pins against the phone's projection. A timeline whose transition
  overlaps are not whole milliseconds can put the cut up to 1 ms apart (the phone rounds overlaps
  to the millisecond). Not covered by a case.
- A source whose measured length the server cannot resolve grows only to its stored
  `source_duration_s`; the phone would also cap it at the file's length.

## Unsupported counts

| Count | Before (`origin/main`) | After |
| --- | ---: | ---: |
| The original 34-action set | 12 | **11** |
| Every `agent_unsupported` action in the table | 36 | **35** |

The 11 left are the 9 drawing actions, motion speed and the lip-sync trim.
