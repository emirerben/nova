# KRI-750: editor Save contract, failure modes

Written BEFORE the matrix code (see `src/apps/api/tests/routes/test_editor_save_contract_matrix.py`).
Each line is a way an editor Save (`POST /plan-items/{id}/variants/{v}/editor-commit`) can go
wrong, the archetype it hits, and the outcome the contract requires. A mode here is a ROW in the
matrix, never a new test file. A new Save failure mode is added to this list first.

Why: KRI-736 needed five fix PRs in a day. Every one was a Save that 422'd in a way a creator
could not recover from, found by hand on a real job. They were five faces of one contract
("whatever the creator's own manual edits produce, Save accepts it, unless it would lose the
words or the media"). The matrix states that contract once, for every archetype.

Legend: `ok` = Save accepted and the pinned recipe is consistent; `422 <code>` = refused with a
stable code; `unit` = a pure helper the Save path leans on, pinned at its own seam.

## 1. Clip delete, reorder and split (guided story converts to an authored timeline)

| Row id | Shape | Outcome |
| --- | --- | --- |
| `delete-m0-order-required` | delete the first clip, contract fixed the clip order | ok, `order_ids` lose m0, 3 clips on device |
| `delete-m1-order-required` | delete a middle clip | ok, `order_ids` lose m1 |
| `delete-m3-order-required` | delete the last clip | ok, `order_ids` lose m3 |
| `reorder-reversed-cut` | delete m1, then reverse the cut | ok, order approved against ITS order (m3, m2, m0) |
| `unidentified-slot-order-required` | a slot with no `media_id` against an order-required contract | 422, reason names "source-identified" |
| `delete-order-not-required` | delete a clip, contract has no required order | ok, `order_required` stays false |
| `split-keeps-required-order` | split one clip into two adjacent pieces | ok, 5 pieces, `order_ids` still the 4 sources |

## 2. Text vs the new timeline length

| Row id | Shape | Outcome |
| --- | --- | --- |
| `retimed-lane-total-7.5` / `7.4333333` / `7.4999999` / `7.4916667` | phone re-timed text lane; cut total not millisecond-exact | ok, no text layer ends past the recipe duration |
| `old-clock-lane-fitted` | text still on the pre-delete clock (old app build, stale draft) | ok, fitted onto surviving footage, pin clamped never moved |
| `fit-text-of-deleted-footage-goes-with-it` | label whose only footage was deleted | unit: dropped, other text kept |
| `fit-already-fitting-text-untouched` | text inside the new timeline | unit: same object back |
| `fit-small-overshoot-clamped` | text ends 50 ms past the new total | unit: clamped, not re-projected |
| `fit-own-clock-caption` / `lyric-id` / `lyric-role` / `narration-score` | caption cue, lyric, narration text | unit: keep their own clock, untouched |

## 3. Text schema (the iOS editor encodes `role: null`)

| Row id | Shape | Outcome |
| --- | --- | --- |
| `new-text-null-role` | new text sent with `"role": null` | ok, role defaults to `generative_intro` |
| `null-role-equals-absent-role[absent\|none]` | `role` null vs missing | unit: identical element |
| `unknown-role-rejected` | `role: "bogus"` | unit: rejected, not coerced |
| `null-defaulted-field[role\|position\|wrap_lines\|removed\|behind_subject]` | null on each of the 5 defaulted `TextElement` fields | unit: equals the default |

## 4. Source-window bounds (client allows 0.05 s past the end, server must agree)

| Row id | Shape | Outcome |
| --- | --- | --- |
| `overshoot-0.0003s` | window ends 0.3 ms past source (frame rounding) | ok, persisted exactly |
| `overshoot-0.02s` | 20 ms past | ok, shortened to the source end |
| `overshoot-0.05s` | exactly the 50 ms tolerance | ok, shortened to the source end |
| `overshoot-rate-2x` | shortened duration respects playback rate 2 | unit: `in_s + duration * rate <= source` |
| `overshoot-0.06s-refused` | 60 ms past | 422 `TIMELINE_OUT_OF_BOUNDS` with diagnostics (clip, in_s, source, overshoot, rate, slot) |
| `in-point-past-end-refused` | `in_s` beyond the source | 422 `TIMELINE_OUT_OF_BOUNDS` |
| `below-minimum-length-refused` | only 40 ms left, shortened slot under 0.1 s | 422 `TIMELINE_OUT_OF_BOUNDS` |

## 5. The creator's manual edits win over approved text-role rules

| Row id | Shape | Outcome |
| --- | --- | --- |
| `opening-unmoved` | opening title left at frame 0 | ok, keeps its `opening` rule |
| `opening-moved-later` | opening title moved to 3 s | ok, degrades to `any`, words still on screen |
| `opening-nudged-off-zero` | nudged to 1 s | ok, degrades to `any` |
| `opening-shortened` | held 0.6 s | ok, still `opening` (start unchanged), words on screen |
| `opening-held-shorter-than-approved` | approved 2 s hold, creator holds 1 s | ok, degrades to `any` |
| `opening-reworded-and-moved` | new words and moved | ok, required as the NEW words only |
| `closing-moved-off-last-frame` | closing title moved earlier | ok, degrades to `any` |
| `text-deleted-by-creator` | creator deletes a required label | ok, requirement dropped |
| `words-missing-from-screen-refused` | a required text is not on screen at all | 422 `unsupported_phone_edit` with field path and reason |
| `strict-verifier-for-automatic-callers` | misplaced opening through the shared verifier | refused, `opening_title` (relaxation is manual-Save only) |
| `manual-trim-moves-length-target` | clips shortened, contract had 30 s | ok, contract `duration_s` == new total |
| `guided-opening-moved` | same as above on the non-authored guided editor | ok, degrades to `any` |
| `guided-shot-label-moved-out-of-shot` | `clip` role label moved off its shot | ok, degrades to `any`, words required |
| `mute-all-clears-required-original-audio` | every clip muted, contract requires camera sound | ok, requirement cleared and reported in `requirements_relaxed` |
| `unmute-clears-forbidden-original-audio` | clips unmuted, contract forbids camera sound | ok, requirement cleared |

## 6. Other archetypes (text-only Save)

| Row id | Shape | Outcome |
| --- | --- | --- |
| `talking-caption-text-save` | phone Talking (`subtitled`): reword a caption | ok, revision 2 pinned, caption text in recipe |
| `narrated-caption-text-save` | phone `narrated`: reword a caption | ok, only the caption lane moved |
| `voiceover-montage-text-save` | phone montage voiceover: caption edit | refused 422 "Captions can be selected and edited in this editor." (a montage has no caption base; plain-string detail, no code); nothing recompiled |
| `speech-montage-save-refused` | spoken-excerpt montage (device), text Save | 422 `unsupported_phone_edit` naming the spoken-excerpt montage, nothing recompiled |
| `slides-save-refused` | slide post, text Save (capability map already closes every lane) | 422 `slide_post_edit_unsupported`, variant untouched. **Found red by this matrix**: Save accepted it and queued a montage re-render over the slide post; fixed in `_prepare_editor_commit` |

## 7. Cross-platform seams (the shared iOS fixture, `editor_save_matrix.json`)

The phone decides these BEFORE a Save is sent, so the same rows run on both sides.

| Fixture group | iOS seam | Server truth |
| --- | --- | --- |
| `bounds` | `KriaSourceWindowGeometry.isWithinBounds` | sections 4 rows: accept/refuse must agree |
| `text_encode` | `NativeEditorDocument.encodeText` emits `"role": null` | section 3: server accepts null role |

Regenerate after a deliberate contract change:
`cd src/apps/api && UPDATE_EDITOR_SAVE_FIXTURE=1 .venv/bin/python -m pytest tests/routes/test_editor_save_contract_matrix.py -k ios_fixture_is_the_server_truth`.
A server test fails when the checked-in JSON drifts from the real server verdicts.

A fixture row that iOS accepts but the server refuses (or the reverse) is the bug class behind
KRI-736 #4 (`TIMELINE_OUT_OF_BOUNDS` at 50 ms).

## Not covered here on purpose

* Cloud-rendered (non-device) Save: separate contract, separate tests (`test_editor_commit.py`).
* Song/lyric lanes, drawings, SFX lanes: own suites; only their interaction with deleting a clip
  is a matrix row (`mute-all`, `unmute`).
