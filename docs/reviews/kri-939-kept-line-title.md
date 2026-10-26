# KRI-939: title and clip labels on a kept-spoken-line montage (Don Julio walk-in M1)

Offline verification of a bounded local fix. It does not establish fulfillment of the
whole M1 request or a physical-iPhone export. Parent KRI-938 keeps those gaps (wrong
butcher clip KRI-940, listed order KRI-941, kept-audible clip dropped on correction
KRI-942, song reference KRI-943).

## Request and failure

Kit `M1_walk_in_story_montage` (Don Julio walk-in, 2026-10-25, TestFlight 1709, prod
2d6ddbfde): a montage that ends on "Super excited!" kept whole with its sound, a title
"Parrilla Don Julio 🇦🇷", and the labels "The butcher's counter 🔪" / "MICHELIN 2024 ·
2025" on their shots. The first approval (job 1cb33402) failed in 11 s:

```
phone_guided_job_failed error='This edit is missing confirmed on-screen text.'
failure_reason=phone_plan_unsupported field_path=opening_title decline_reason=evidence_missing
```

A named voice clip routes to `run_phone_speech_montage_job`. Its excerpt compiler
`compile_phone_speech_montage_plan` built the recipe with `text_layers=[]`; the only
text added afterwards was the creator's corner pins. `verify_phone_recipe` found none of
the contract's exact texts and refused on the first one (the title). The
voice-behind-footage branch of the same job already took `opening_title=`, so the flag
emoji was never the cause.

## Fix

- `compile_phone_speech_montage_plan` takes `opening_title` / `opening_title_hold_s` /
  `clip_labels` (+ pins for title placement). The title is drawn from 0 s for its
  confirmed hold, stepped below a top corner pin exactly as voice-behind-footage does.
  Each label is drawn inside **every** cut of its clip (`verify_phone_recipe` checks
  each cut; a label spanning a cut, or only on the first cut, is "in the wrong place").
  Two labels on one clip stack instead of overlapping.
- The title/pin code moves out of `compile_phone_voice_behind_footage_plan` into a shared
  `_with_confirmed_text`; that route's output is unchanged (its tests pass unedited).
- `run_phone_speech_montage_job` reads the title (`opening`, else first `any`) and the
  `clip` labels from the pinned contract. `closing` texts and a second free-standing
  `any` text are not drawn here, so the verifier still refuses them by name.
- The variant row carries the drawn title/label rows (read-only), so the editor preview
  shows what the device renders. Uncontracted (legacy) jobs get no new text.

## Evidence (offline replay)

Expectation source: the kit prompt's exact words and placements, encoded in
`src/apps/api/tests/services/test_phone_speech_montage_job.py`
(`test_kept_line_montage_draws_the_confirmed_title_and_clip_labels`). The real job
entry, excerpt compiler, text layout, pilot validation, `verify_phone_recipe` and
`verify_device_record_contract` run. The speech-excerpt planner agent's output and the
transcript words are fixed test inputs (no model call).

| | main 6dceb6729 | this branch |
| --- | --- | --- |
| Kept-line montage + opening/any title + two clip labels | refused: `missing confirmed on-screen text` (`CreatorRenderContractError`) | pinned `awaiting_device` |
| Title layer | none | one layer, 0–2.2 s, words intact (🇦🇷 kept, wrapped over two lines) |
| "The butcher's counter 🔪" (clip met twice) | none | one layer inside each of its two cuts |
| "MICHELIN 2024 · 2025" | none | inside its cut |
| Variant `text_elements` | pins only | title + label rows, read-only |

Red control: the new test was written and run before the fix and failed with the same
error as the phone (`app/services/creator_render_contract.py:2310`).

Wrong-content control: `test_a_label_for_a_clip_the_edit_never_shows_still_refuses`: a
label for the speaker clip when the speech plays over cutaways (the clip is never on
screen) still raises `CreatorRenderContractError`, and nothing is committed. The fix
never claims words it could not place.

Neighbouring suites: every test file that touches the speech-excerpt, voice-behind-footage,
narrated-title or `verify_phone_recipe` paths (81 files): 2768 passed. Local-only
failures: 86 errors + 6 failures need Postgres (`role "postgres" does not exist`), and
2 `test_kri824_photo_hold_e2e` failures appear only when run after its `tests/kria`
neighbours (same on unchanged main). Preship mapped gates `kri-455-voiceover-titles`
and `kri-519-521-550-reaction-beats`: passed.

## Not verified

- iPhone export of the title/labels on this route (native output).
- The full M1 request: it also needs KRI-942 so the correction keeps sound on the
  kept line only; KRI-940/941/943 are separate.
- `closing` text on this route (still refused by name).
