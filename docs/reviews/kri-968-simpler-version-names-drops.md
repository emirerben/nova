# KRI-968: the simpler version names what it leaves out

Parent incident: KRI-964 (Don Julio walk-in T1, prod thread F0F3D5FB, 2026-10-26, prod
`a2426885d`, Main Creator `2026-10-26-v98`, iPhone protocol 22). Same consent pattern as the
N1 voiceover run (KRI-960) and the Merope café T1 run (KRI-787 fixture: "Left out as agreed:
Show MEROPE ..."). Evidence level: **offline replay** (scratch Postgres, real runtime turns,
the planner's output replayed); no device, live model or production claim.

## Original request (kit T1 prompt, excerpt)

> ... Put an “ATAKAN” name tag when he first appears and “SUPER EXCITED” in big letters on
> his words. Title: “Before the meat”. No text on faces. About 7 seconds.

Brief row: `r4 {kind: text, scope: global, literal: "ATAKAN", facts: {}, description: "name
tag when he first appears"}`.

## What the creator saw (prod)

1. Held first draft (event 10, `simplification_requires_choice`): five receipt lines, three
   of them "Partly", one of them `- Partly: name tag when he first appears ("ATAKAN") (That
   exact text isn't in this draft)`, then "I held this first draft back. Should I try a
   different approach, or make this simpler version?". Nothing said which line the simpler
   version drops; the question payload recorded nothing either.
2. Tap "Make this simpler version" (event 11). The draft and approval card (events 12, 13)
   said `- Left out as agreed: name tag when he first appears ("ATAKAN")`, printed under
   "Have a look at these in the video:".
3. After-render review (event 20): title, captions, SUPER EXCITED, length; the tag is never
   mentioned.

## Acceptance (written before the change)

- The offer names exactly what the simpler version leaves out, before the question:
  `The simpler version leaves out: name tag when he first appears ("ATAKAN").` (EN + TR),
  and the question payload records those requirement ids.
- "Left out as agreed" is said only for requirements the answered offer named. If the unmet
  set changed between the offer and the tap, the creator is asked again (naming the new
  set); nothing is dropped.
- An offer stored before this change (no list) is still accepted, but the copy no longer
  claims an agreement the creator was never shown: `Left out in the simpler version: ...`.
- The after-render review lists every requirement the approved draft left out by that
  answer, so the creator is not told everything else is done while the drop goes unsaid.
- Still wrong after this change: the tag itself is not drawn (see remaining gaps).

## Failure modes (enumerated before coding)

- F1 The offer names something other than the unmet ids (every Partly line, or nothing).
- F2 Agreement is claimed for an id the answered offer did not name.
- F3 The unmet set changed between offer and tap (a new miss, or a different draft) and the
  tap still drops requirements instead of re-offering.
- F4 The after-render review omits a requirement left out by agreement.
- F5 The automatic omission path (`Left out: X (reason)`, no question) changes behaviour.
- F6 The strategy-refusal path (`dropped_fields`, KRI-759) changes behaviour or is mistaken
  for a named receipts offer.
- F7 A legacy offer (no list) stops being accepted, or keeps claiming "as agreed".
- F8 Turkish copy drifts from English (offer line, agreed line, legacy line, review line).
- F9 A second simpler offer in the same thread: the old question's tap or the old list is
  used for the new offer; an old omission is re-claimed for the new set.
- F10 The left-out line lands under "Have a look at these in the video:" and reads as one
  of the things to check (the prod card's layout).
- F11 The review lists an id that is live again in the approved brief (restored later).
- F12 A simpler version that left nothing live returns the bare "ready" sentence and drops
  the omission line.

- F13 (found by the KRI-901 readiness suite while implementing) The longer held-draft
  reply passes `KriaTurnPlan.response`'s 1200-character limit and the turn fails.

## Cause

All in `src/apps/api/app/tasks/kria_runtime.py` (base `18a46b862`):

- The receipts gate's offer (L2095-2097) asked `simpler_version_question()` with no
  requirement list and a reply that only said "Should I try a different approach, or make
  this simpler version?". Which rows the simpler version drops was decided on the server
  (`unmet_ids`: receipts that `needs_creator_choice`, or readiness rows that are not
  executable) and never shown; T1's other "Partly" rows are format limits, so only r4 was in
  the set. `_simpler_version_already_offered` (L918-991) did check the tap against that
  hidden set (L974), so the drop was consistent, just never disclosed.
- The simpler draft wrote `- Left out as agreed: {req.text()}` (L1999) for any accepted
  offer, and appended it after the receipts reply (L2019), i.e. under "Have a look at these
  in the video:".
- `_approved_generation_requirements_review` (L6396-6496) builds the review from the
  approved brief's live rows only; the draft's `brief_coverage.accepted_omission_ids`
  (L2284) was written but never read.

## Change

- The offer names exactly the unmet rows, in brief order, before the question:
  `The simpler version leaves out: A; B.` / `Daha basit sürümde çıkarılacak: A; B.`, and its
  `choice_question` records `omitted_requirement_ids` and `brief_version` (additive keys;
  the iPhone parser ignores unknown keys). The reply is fitted to the 1200-character limit
  by shortening the receipt lines above, never the naming sentence or the question; a
  naming sentence over 600 characters is not shown and the offer records no list.
- `_simpler_version_consent` (was `_simpler_version_already_offered`) returns `"named"`
  only when the tapped offer's list equals the current unmet set at the same brief
  revision; a changed set returns nothing, so the gate offers again (naming the new set).
  An offer stored before this change (no list) keeps the old receipts/readiness check and
  returns `"unnamed"`.
- The simpler draft says `- Left out as agreed: X` / `- Kararlaştırdığımız gibi çıkarıldı: X`
  only for a named consent, otherwise `- Left out in the simpler version: X` /
  `- Daha basit sürümde çıkarıldı: X`. Left-out lines (and the automatic path's
  `- Left out: X (reason)`) now sit above "Have a look at these in the video:". The draft's
  `brief_coverage` records `agreed_omission_ids` next to `accepted_omission_ids`.
- The after-render review reads the approved draft's coverage (via the render execution's
  `target_draft_id`) and adds the same left-out line for each accepted id the approved brief
  superseded, above "Have a look"; ids live again or unknown are skipped, and a simpler
  version with nothing live still says what was left out.
- Unchanged: the automatic-omission decision and its copy, the KRI-759 policy-refusal
  question (`dropped_fields`, planner side), and what the simpler version drops.

## Checks

Offline replay, scratch Postgres, tested on the commit that adds this note (parent
`18a46b862`).

`cd src/apps/api && pytest tests/kria/test_kri968_simpler_version_names_drops_postgres.py`
(fixture `tests/fixtures/kri968_don_julio_walkin_t1_simpler.json`: prod events 4-13 and 20,
brief v3/v4, the approved strategy, the take, the rendered variant; signed URLs and the
account id removed). Real `submit_turn` + `run_kria_turn`; only `planner._call_main_creator`
is replayed (as in KRI-787). The replayed held draft equals prod's event 10 receipt for
receipt and word for word, plus the naming sentence.

| Test | Failure mode | Before | After |
| --- | --- | --- | --- |
| `test_prod_offer_named_nothing_and_the_card_claimed_agreement` | fixture record | pass | pass |
| `test_the_held_draft_names_what_the_simpler_version_leaves_out` | F1, F6 | fail | pass |
| `test_the_tap_leaves_out_only_the_named_tag` | F2, F10 | fail | pass |
| `test_the_review_lists_the_agreed_omission` (prod review + the line) | F4 | fail | pass |
| `test_a_changed_unmet_set_is_offered_again_not_dropped` | F3 | fail | pass |
| `test_a_legacy_offer_is_accepted_without_claiming_agreement` (prod events 9-11) | F7 | fail | pass |
| `test_turkish_offer_draft_and_review_name_the_drop` | F8 | fail | pass |
| `test_a_second_offer_names_only_its_own_drop` | F9 | fail | pass |
| `test_review_left_out_lines_skip_live_ids_and_unknown_ids` | F11, F8 | fail | pass |
| `test_the_policy_refusal_question_carries_no_requirement_list` | F6 | pass | pass |

Neighbours run green: `test_runtime_simpler_version_loop`, `test_runtime_automatic_simplification`
(F5), `test_kri759_simpler_version_refusal` (F6), KRI-787 Merope replay + runtime,
KRI-775/794/795/901, approval readiness, the review suites (KRI-551/689/842, montage,
narrated alignment); `tests/kria` + `tests/tasks` in full (results below).

Red control: the same module on the base `kria_runtime.py` (the only app file changed):
8 failed, 2 passed (the fixture record and the policy-refusal check). Full `tests/kria` +
`tests/tasks` on the change: 10184 passed, 9 skipped, 1 xfailed.

Mutations (scratch, not committed), each against the module:

| Mutation | Red tests |
| --- | --- |
| Offer names every live row, not the unmet ones | 6 (held draft, tap, review, changed set, TR, second offer) |
| Consent ignores the recorded list (every offer treated as legacy) | 4 (tap, review, TR, second offer) |
| Named set comparison dropped (only the revision checked) | changed set |
| Review line removed | review, legacy, TR |
| Left-out line appended under "Have a look" | tap, review |
| Review lists ids that are live again | the review-lines unit test |
| "As agreed" for legacy offers too | legacy |

## Case matrix

- Covered: one unmet row (T1), two rows after a changed draft, readiness offers with five
  blocked rows (KRI-901 Merope, fitted to 1200 characters), legacy offer, Turkish, second
  offer in one thread and a stale first-offer tap, nothing live after the drop (unit), an
  omitted id live again (unit), automatic omissions and the policy refusal unchanged.
- Not covered: a typed "simpler version" in place of the tap on a named offer (same matcher
  as before, exercised by `test_runtime_simpler_version_loop`), web chat (no web change).

## Behaviour to know

- Every held draft that offers the simpler version now ends with one more sentence naming
  what it drops. Long readiness replies are cut short above that sentence (the cut lines
  are the same rows the sentence names).
- Left-out lines move above "Have a look at these in the video:" in drafts, cards and
  reviews (also the automatic `Left out: X (reason)` lines).
- Threads holding an offer made before deploy still accept the tap; their card says
  "Left out in the simpler version", not "as agreed".

## Remaining gaps

- The ATAKAN tag itself is not drawn. A name tag timed "when he first appears" has no
  placement path on a one-take Talking edit (the brief row has no trigger fact); placing it
  at the start of the cut needs a planner/prompt or a new placement path. Follow-up under
  KRI-964.
- An omission accepted on a turn that a later choice question blocked is recorded on that
  response, not on the next draft, so the next card and the review do not repeat it; an
  omission agreed in an earlier round is not repeated after a second simpler version.
- Not device-checked: re-send T1 from the kit prompt in a new chat on the iPhone after
  deploy and check the held draft, the card and the review.
