# End-to-end proof: every served suggestion card, tapped

Free and offline (no model, no Redis). Needs a migrated scratch `*_test` Postgres:

```bash
bash scripts/dev/test-db.sh                      # prints the DATABASE_URL export line
cd src/apps/api && pytest tests/e2e_suggestions  # writes <repo>/.chip-eval/e2e-report.{md,json}
python scripts/suggestion_chip_report.py         # the replay report; it reads the JSON twin
bash scripts/dev/test-db.sh --drop               # when done
```

Without a test database the directory skips; under `CI` it fails instead.

## What runs

For each replay fixture (`nermin-matcha-update`, `nermin-travel-diary`, `east-run-brief` with and
without its recorded brief) `threads.py` seeds a real thread: a ready render with an editor target,
clips with finished understanding, the strategy draft the render came from, the recorded brief and
the measured signals (`audio_quality` from committed recordings, `near_dup_hash` from the duplicates
lane's recorded hashes). Three footage profiles per fixture: `editor`, `signals`, `talking`.

`driver.Phone` is the iPhone: the real `GET /creation-threads/{id}/suggestions`, the real
`kria_suggester` body with the real `SuggestionAgent` (a cassette in `cassettes/` answers), then
`submit_turn` -> `run_kria_turn` with the card's own prompt and `{set_id, chip_id}`.
`flow.tap_every_card` taps each card and records ten checks per cell (served, in ring, accepted,
real change, new revision, receipts, chat = prompt, Undo, consumed, second tap refused, refreshed
set). `test_negative_cases.py` covers stale base, a kind no longer offered, flags off, KRI-129, copy
laundering, mute dark and first prompt.

## Gaps are named and strict

A check that fails because of code outside this directory carries a `Gap` (`gaps.py`): the report
prints it, the run stays green, and the day the check passes the run fails with "gap closed, remove
the marker". `test_probe_gaps.py` holds the minimal repros of GAP-1 and GAP-2.

The cassette is a preference list, not a recorded model answer: the call's own strict
`response_schema` decides which of its kinds and clip handles are possible answers, so one file
serves a thread before and after cards are applied. It pins what the server does with an answer;
the paid live eval measures what a model would choose.
