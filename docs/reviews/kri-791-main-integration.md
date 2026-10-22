# KRI-791: combined Talking integration and native failure diagnosis

Original source: Yasin's #1724,
`daff439f9e871525e675971c95a9b7bae672b020`. The candidate retains its
spoken-word fallback, typed trigger/speaker facts, explicit speaker uncertainty,
partial finished receipt and unavailable-word behavior. It does not claim
speaker identification or complete fulfillment of the original T1 request.

## Ordered integration

Merge #1727, then #1733, then this PR. The reviewed caption/order candidate
`0995022c42eee53b7cfba4b56739804e819580cb` is an explicit dependency; both
authors' source commits remain in history. Current main also brings in merged
#1700's duration correction and synchronized native test spy before this
candidate's first hosted CI run.

Both prompt additions are retained: exact creator caption spellings and
spoken-text triggers/speaker. The combined Main Creator version is
`2026-10-22-v66`, extractor `2026-10-22-v14`. Only the two intentionally changed
brief-on hashes were recomputed after reviewing the combined instructions.
The two legacy brief-off hashes passed unchanged before re-pinning. Both
English/Turkish caption and speaker-limit messages remain in the honest set.

## Retained native failure and narrow helper repair

[Old native run](https://github.com/emirerben/nova/actions/runs/39624765111/attempts/1)
actually checked out `5b473155cef4925f6989a3ce2eab73a2c87b135c`. The Gameday
test failed at its second Add Clip after closing the text inspector. Its saved
debug attachment shows the button **and main window/ancestors** with infinite
origins and zero size: `{{inf, inf}, {0.0, 0.0}}`. Asking `isHittable` aborted
on the invalid activation point before the predicate could wait. This is not
proof that a production button is wrong or that the failure can be dismissed
as a harmless flake.

The separate test-helper change checks finite, positive element geometry
before querying hittability, within the original five-second wait. Hittability
must still become true, and the same tap follows. The fifth-clip, title,
preview, undo/redo, save, reopen and export assertions are unchanged. A
never-valid frame or genuinely unavailable button still fails. No coordinate
fallback, skip, broader retry, timeout increase or native app change is used.

The retained failed result is the before evidence. Swift parsing passes, but
the modified native E2E still needs its actual hosted result; web/backend
success does not establish that result.

## Timeline contract and executed checks

- The approved cut/cleanup windows and `compile_phone_subtitled_plan` continue
  to own output time and recipe duration. No new duration calculation is added.
- Generated word text uses the existing source-to-cut projection. Manual
  lanes, ripple behavior, continuous music, resize bounds and undo snapshots
  are unchanged.
- Existing native scrub geometry uses `timelineScrubDuration`; playback/frame
  requests retain their composition bound. This merge changes neither mapping.
- The real Talking-writer KRI-791/KRI-793 cases retain cue placement, timing,
  recipe and honest receipt expectations; the Gameday E2E retains final export.

Executed before the final native-helper check:

- Combined spoken-text/caption/order, real PostgreSQL receipts, prompt pins and
  bilingual regex controls: **171 passed**.
- After current main's duration fix, length planning and the combined Talking
  paths: **96 passed**.
- Free offline `tests/evals/` on the combined current-main prompts:
  **2,121 passed, 15 skipped, 7 xfailed**. No live model/judge call.
- Required `make verify-editor-timeline`: **209 frontend checks**, **28 desktop
  browser fixtures**, **42 mobile browser fixtures**, and **583 API checks**
  passed. This runs deterministic local fixtures, not a physical phone. It ran
  before the duration-main merge; unchanged frontend evidence was not repeated.
- `xcrun swiftc -frontend -parse` passes for the modified Gameday file. Native
  compilation and modified-helper E2E remain hosted-CI requirements.

These groups overlap and are not combined into one count. The full original
Talking request, model compliance with the new facts, speaker identity and
actual device video remain separate acceptance work in KRI-787 and siblings.
No physical-phone operation, paid call or manual deployment occurred.
