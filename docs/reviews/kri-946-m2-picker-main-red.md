# KRI-946: main red after the M2 picker fixes (KRI-949 + KRI-947/951)

Both Don Julio walk-in M2 picker fixes merged on 2026-10-26: KRI-949 (#1849, `548f5af33`)
and KRI-947/951 (#1852, `ca9cf789a`). Each replay test passed on its own branch and pinned
the other fix's pre-fix behaviour, so on main (`135c86d7b`) two tests failed:

- `test_kri947_kri951_m2_picker_rows.py::test_prod_rounds_1_to_3_ask_the_butcher_once_and_never_the_queue_again`
  expected round 1's queue row to offer only the video and the round-3 copy to say "in your video".
- `test_kri949_named_photo.py::test_round_four_offers_the_photo_for_both_rows` expected two
  rows (include + caption) for "the ribeye photo".

## Which behaviour is right

- Round 1 / 3: prod's resolver returned nothing for the queue (round 1,
  `clip_moment_queue_include` n=0) and for the butcher (round 3). KRI-949's rule is that
  silence rules no photo out, so on main both rows also offer the thread's Visuals photo and
  ask "which of your clips". The KRI-947 test's [video] expectation predates KRI-949.
- Round 4: include + caption about the photo offer the same clips, so KRI-951 joins them into
  one row (the caption, with "Ojo de bife 🥩", in `also_answers`). That join is the KRI-951
  fix for the photo being listed twice; the KRI-949 test's two-row expectation predates it.

No code change: the two tests now pin the merged behaviour. The KRI-949 round-4 test also
pins the real M2 path after both fixes: one tap on the photo settles include, caption and
round 6's re-plan under `order`.

## Failure modes checked

| Failure mode | Pinned by |
| --- | --- |
| The queue stub is wrong (prod was not silent) | prod round-1 `intent_stats` (n=0) |
| The photo offer stops the queue tap carrying to round 3's `order` row | KRI-947 chain test |
| Photo candidates break the KRI-951 join / the row-count check drops the picker | KRI-947 chain test, KRI-949 round 4 |
| The joined row loses the caption's creator text or the photo thumbnail | KRI-949 round 4 |
| The joined question says "in your video" or names the photo twice | KRI-949 round 4 (exact text) |
| One tap on the photo misses the hidden caption or the later `order` row | KRI-949 round 4 |

Scratch mutations (not committed), each run against both files: no KRI-951 join (9 red),
no KRI-949 photo offer (5 red), `order` not sharing answers (6 red), hidden row dropping
`creator_text` (3 red). The KRI-949 round-4 test is red in all four.

Not covered: two rows about one moment that differ only by photo narrowing (two or more
photos, one flagged by the resolver) stay two rows. Rare; no incident.

## Checks

- `cd src/apps/api && pytest tests/services/test_kri947_kri951_m2_picker_rows.py tests/services/test_kri949_named_photo.py`: 23 passed.
- Neighbouring picker/clip-selection suites (clip_intent_*, clip_selection, KRI-126/520/693/
  696/779, kria picker/question suites incl. postgres): 332 passed.

Evidence level: offline replay. M2 iPhone re-send stays owed under KRI-946.
