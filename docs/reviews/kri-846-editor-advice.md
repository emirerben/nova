# KRI-846: named line cuts no longer send the creator to a blocked editor action

## Request and failure

Café T1 (phone Talking): `Cut her slip "with bread, no," so it says "waffle cookie with
blackberry jam". Also cut my half question "how are you gonna".` After #1728 (KRI-792) both
asks were honestly "Partly", but the draft, approval card and finished-video receipt added
"a retake or a specific line isn't cut automatically yet, so trim that in the editor", and
every cleaned Talking/Narrated render ended "... a sentence you restarted stays in; trim it in
the editor." The iOS cleanup choice card said the same.

## Why no editor action can do it (checked on main 004f38182)

- iOS `NativeEditorSession.beginTrim` returns early for a talking-source slot (KRI-770).
- A fresh phone Talking variant has no `timeline` capability, so Range is locked; there is no
  Split action in the clip strip.
- `services/authored_editor.py` refuses any changed `native-phone-talking-source*` window with
  409 `TIMELINE_STALE` (pin from #1319). Captions, words and cards sit in cut-timeline seconds
  with no remap, which is what the pin protects.
- Whole-segment Delete works but removes the wanted words in that segment (the slip sits
  inside segment 24.25–27.12 s, "red no, with blackberry jam,").
- On Narrated the editor trims picture clips, not the recorded voice.

A word-range trim on talking slots was assessed and rejected for this ticket: it needs a new
server window rule plus a server-side caption/word/text/SFX remap (estimated 3–5 days) and
overlaps KRI-724 (Emir) / draft #1760, which already adds exact spoken-phrase removal.

## Change

Copy only: `_NAMED_CUTS_STAY` ("I can't cut out one exact line yet or confirm it's gone, so it
may still be in your video", TR "belirli bir cümleyi henüz tam olarak kesemiyorum ve
çıkarıldığını doğrulayamıyorum, o yüzden videoda kalmış olabilir"), the review-note footer
without the editor tail, and the iOS cleanup card without "trim it in the editor". Status stays
`partial`; the cleanup lead stays first.

After merging main (KRI-551 consented retake cleanup, #1753) the line says "may still be", not
"stays": a consented retake cut may have removed the line and a generic cleanup record cannot
prove which span it was. The footer and the iOS card still say a restarted sentence "stays in"
because they only say so when no retake was cut and none could have been.

## Evidence (offline replay)

- `tests/kria/test_kri846_no_blocked_editor_advice.py`, built on the recorded T1 brief
  (`tests/fixtures/kri792_t1_phrase_cuts.json`) and the kit's double-quoted wording: draft
  receipts (4 cleanup states), approval-card text, rendered receipts (applied / no_change /
  not_run), the finished reply, the review-note footer and its dedupe, all speech formats,
  EN and TR.
  - Red control: run against main's `brief_checks.py` and `SpeechCleanupOffer.swift`:
    54 failed, 5 passed (the 5 are the server window-pin cases, true on main by design).
  - With the fix: 59 passed.
- The same file pins the server refusal (4 changed-window saves → 409 `TIMELINE_STALE`, plus an
  unchanged-window control). It fails on purpose when KRI-724 makes those windows editable,
  so this copy is revisited then.
- Updated receipt tests (string only) and `test_kri787_merope_t1_runtime_postgres.py` on a
  scratch Postgres: all green.

## What a café T1 re-run shows

- Draft bubble / approval card: the long reply is compacted to ~70 characters per reason, so
  both phrase rows read "Partly: … (Choose Clean up speech when you approve and the long
  pauses are cut;…)" or, with cleanup chosen, "(Speech cleanup cuts the long pauses; I can't
  cut out one exact line yet…)". No editor action is suggested. The old line was cut at the same
  point, so this is not a regression.
- Cleanup choice card: "Choose whether to remove the detected pauses and filler sounds. A
  sentence you restarted stays in." (needs a new iOS build; older TestFlight builds still show
  the old text).
- Finished video reply: "Partly: Cut her slip … (Speech cleanup cut the long pauses; I can't cut
  out one exact line yet or confirm it's gone, so it may still be in your video)", the same for
  the half question, and no editor tail on the cleanup note.

## Gaps (not fixed here)

- The phrases are still not removed: KRI-724.
- On the draft card the compacted reason hides the "may still be in your video" clause.
- After a first segment Delete makes a Talking edit authored, Range (`setSourceWindow`) on a
  talking slot is not blocked on iOS and ends in 409; `moveClip`/`slideSourceWindow`/
  `setClipTiming` are unguarded. Whole-segment Delete on Talking has no test.
- No physical-iPhone export.
