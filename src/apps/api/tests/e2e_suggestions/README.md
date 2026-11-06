# End-to-end proof: every served suggestion card, tapped

Free and offline (no model, no Redis). Needs a migrated scratch `*_test` Postgres:

```bash
bash scripts/dev/test-db.sh                      # prints the DATABASE_URL export line
cd src/apps/api && pytest tests/e2e_suggestions  # writes <repo>/.chip-eval/e2e-report.{md,json}
python scripts/suggestion_chip_report.py         # the replay report; it reads the JSON twin
bash scripts/dev/test-db.sh --drop               # when done
```

Without a test database the directory skips; under `NOVA_REQUIRE_POSTGRES_TESTS` (the api shards)
it fails instead.

## What runs

For each replay fixture (`nermin-matcha-update`, `nermin-travel-diary`, `east-run-brief` with and
without its recorded brief) `threads.py` seeds a real thread: a ready render with an editor target,
clips with finished understanding, the strategy draft the render came from, the recorded brief and
the measured signals (`audio_quality` from committed recordings, `near_dup_hash` from the duplicates
lane's recorded hashes). Three footage profiles per fixture: `editor`, `signals`, `talking` (a
Talking take gets a Talking plan: approving a plan of another format would re-map the item and
switch its speech cleanup off).

Two scenarios are the same footage told in the other language (`nermin-matcha-update/tr`,
`east-run-brief/footage-only/en`): only the words change (clip subjects, what the speaker says, the
request, the plan's rationale). That is how every kind is tapped in English AND Turkish.

`driver.Phone` is the iPhone:

* `get` is `GET /creation-threads/{id}/suggestions` through the real ASGI app (only the creator's
  identity is overridden). `last_wire` is the JSON the phone would parse.
* the worker run it enqueues is the real `kria_suggester` task body with the real
  `SuggestionAgent`, answered by a cassette in `cassettes/`.
* `tap` is `submit_turn` -> `run_kria_turn` with the card's own prompt and `{set_id, chip_id}`.
* `approve_render` is `decide_approval` then the real `execute_kria_approval` task; only the render
  dispatch is a stand-in that records its call and writes the queued Job.

`flow.tap_every_card` taps each card and records the checks of one cell: served (on the wire, no
Action), stored in the ring by the route itself (nothing is seeded), accepted, real change in the
promised direction, ONE change (no other lane moved), new revision, receipts met, render kick
(restating cards, on a twin thread), chat line = prompt, consumed, second tap refused, refreshed
set, and after Undo: the draft, the approval, the brief and the item settings are what they were.

## Files

| file | what it proves |
| --- | --- |
| `test_tap_every_card.py` | the per-kind matrix, E1-E12 |
| `test_ring_*.py`, `test_probe_gaps.py` | the ring: the GET stores every card, a frozen set obeys the master switch, a card is one offer through every set, a restating card follows the head draft |
| `test_negative_cases.py` | N1-N6: stale base, a kind pulled after the serve, flags off, KRI-129, copy laundering, mute dark |
| `test_first_prompt.py` | N7: before any render only chips; each chip tap stays on the KRI-756 path |
| `test_kill_and_stale.py` | S1/S2/K1/K3: the change already in the edit, a Save that moved the video, per-kind and master switches, for every kind |
| `test_creators_word.py` | W1/W2: the creator's request withholds the card (EN and TR), and hook copy gates |
| `test_undo_retap.py` | U1: Undo gives the card back exactly once (a hook stays spent: documented) |

## Gaps are named and strict

A check that fails because of code outside this directory carries a `Gap` (`gaps.py`): the report
prints it, the run stays green, and the day the check passes the run fails with "gap closed, remove
the marker". Gaps closed upstream have no marker, so they are ordinary failing checks now.
`test_probe_gaps.py` keeps the minimal repros of the closed GAP-1 and GAP-2 as plain regression tests.

The cassette is a preference list, not a recorded model answer: the call's own strict
`response_schema` decides which of its kinds and clip handles are possible answers, so one file
serves a thread before and after cards are applied. It pins what the server does with an answer;
the paid live eval measures what a model would choose.

## What stands in (so nobody reads more into a green run than is in it)

The model (cassette), Redis (in memory), the editor capability map (fixed open), the creator
manifest for ready threads (a stub; first-prompt threads use the real resolver), the render dispatch
(a recorder), and the render itself, the phone export and any device (not run).
