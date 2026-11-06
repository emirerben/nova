# End-to-end proof: every served suggestion card, tapped

Free and offline (no model, no Redis). Needs a migrated scratch `*_test` Postgres:

```bash
bash scripts/dev/test-db.sh                      # prints the DATABASE_URL export line
cd src/apps/api && pytest tests/e2e_suggestions  # writes <repo>/.chip-eval/e2e-report.{md,json}
python scripts/suggestion_chip_report.py         # the replay report; it reads the JSON twin
bash scripts/dev/test-db.sh --drop               # when done
```

Run it serially for the report of record (about 15 minutes). `-n 4` works (the report merges the
workers' partials) but the workers share one database, and the expiry sweep of one worker can touch
another's pending approvals: an occasional failure under `-n` that does not reproduce alone is that,
not a defect. A second session that wants to iterate should use its own scratch database and set
`CHIP_EVAL_DIR` so it neither writes into nor cleans the first one's report.

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
| `test_undo_retap.py` | U1: Undo gives the card back exactly once (a hook stays spent, brief on or off: documented) |
| `test_r4_serve_*.py` | the serve lane's fixtures: consent gate, kill switches, restate readiness, Turkish fold, look-alike letters, flag subordination |
| `test_r5_save.py` | V1-V7: a native Save through the REAL editor-commit route, cards served before and after it, the fence in front of the filler card |
| `test_r5_lifecycle.py` | L1-L5: a staged filler/mute render that is denied, lapses (lazily and by the sweep), is cancelled, outlived by a newer video or undone; then the card is tapped again |
| `test_r5_kill.py` | K1-K8: a per-kind, signals, master or capability switch pulled after the freeze, put back, and the chip capability behind the real Save and turn |
| `test_r5_copy_states.py` | A1-A4: the on-screen title's decision in every state, decided before and after the set was served |
| `test_r5_turkish_asks.py` | T1-T5: the softened-k forms of "muzik", the other lanes' words, over-withholding, volume asks, English |
| `test_r5_matrix.py` | M1-M5: the mute matrix (constant patched, marked not shipped), a held set tapped without a refresh, a card queued over a pending approval, coverage in both languages |
| `test_r5_serve_*.py`, `test_r6_brief_asks.py`, `test_r6_duplicates_length.py`, `test_r6_restate_drift.py` | the serve lanes' fixtures: identity, evidence, strip, brief asks (KRI-667), a stated length, serve-and-tap never part |
| `test_r6_orders.py` | O1-O6: a native Save, an editor card and Undo in six orders (also with the brief off); a readiness account after a Save. Every tap sits on the saved video, an Undo never takes a Save, the held filler card is refused `base_moved` |
| `test_r6_scenarios.py` | M (the mark gate, four states, both languages), R (approval readiness with a 6 s / 8 s / 20 s length ask, pinned plan or not, the east-run brief), B (a busy ring write: chips alone, then every card; held cards survive), D (duplicates and a stated length, before and after the serve), U (a taken-back tap is an offer again, brief on and off) |
| `test_r7_serve_proof.py` | A: the hook after its own Undo (brief on/off x triggers on/off, both languages) is not served, and everything served executes. B: every other kind undone without the brief is an offer again and every served card executes. C: with the editor triggers off, a card the creator's word has since ruled out (a stated length, a title requirement, a captions / music / sound-effects / transition ask) is not served |
| `test_r7_flag_off_brief_asks.py` | F1/F2: with pinned taps off, `brief_asks` and the real GET read the brief exactly as origin/main does (a verbatim copy of main's rule is the oracle) |
| `scenes.py` | the state helpers: `route_save`, `decide`, `lapse_approval`, `sweep_expired_approvals`, `agent_reads_video` |

## States, not just footage

Every cell of the report carries the state its thread was in when the card was served: `baseline`
(a ready render, nothing unusual) or what a round-5 scenario did first (`after-save`,
`saved-and-read`, `after-deny`, `after-sweep`, `switch-pulled-and-restored`, `title-<state>`,
`patched-constant`, `order-o1` ... `order-o6`, `mark-gate-<case>`, `readiness-<length>-<plan>`,
`ring-write-busy`, `length-<kind>-stated`, `undo-reoffer-brief-on|off`, `kind-switched-on-after-
freeze`, `lever-pulled-and-restored`, `volume-ask-in-force`). The coverage matrix counts baseline cells only; the state table under it shows
the rest, and `kinds_proven.tap_executes` is true only when EVERY shipped served cell of the kind,
in every state, was applied.

The mute kind is dark as shipped. Every mute cell runs with `PINNED_TAP_APPLIES_CLIP_SOUND` patched
on, is marked `[not shipped]` and never counts toward `tap_executes`.

## Gaps are named and strict

A check that fails because of code outside this directory carries a `Gap` (`gaps.py`): the report
prints it, the run stays green, and the day the check passes the run fails with "gap closed, remove
the marker". Gaps closed upstream have no marker, so they are ordinary failing checks now.
`test_probe_gaps.py` keeps the minimal repros of the closed GAP-1 and GAP-2 as plain regression tests.
Open now: none (GAP-17, a hook card served again after its own Undo on an account without the
creative brief and refused at the tap, was closed in round 7). A gap whose card was served and
refused is a `serves_dead_card` gap: `kinds_proven` reports `tap_executes: false` for its kind
whatever the other cells did.

The cassette is a preference list, not a recorded model answer: the call's own strict
`response_schema` decides which of its kinds and clip handles are possible answers, so one file
serves a thread before and after cards are applied. It pins what the server does with an answer;
the paid live eval measures what a model would choose.

## What stands in (so nobody reads more into a green run than is in it)

The model (cassette), Redis (in memory), the editor capability map (fixed open), the creator
manifest for ready threads (a stub; first-prompt threads use the real resolver), the render dispatch
(a recorder), and the render itself, the phone export and any device (not run).

The round-5 states add: the render kick, the durable-sources prefix and the storage existence check
of a timeline Save (no bucket), the render worker's completion write after a Save, the session
pointer following a render (written by hand), an approval's clock (`expires_at` moved into the
past), and the legacy planner as the answer to a typed message.

The round-6 states add: a Kria render landing is a hand-written variant update (a new
`render_generation_id`) followed by the session pointer following it; a busy ring write is a second
transaction holding the thread row `FOR UPDATE`; an account on approval readiness is the readiness
flag plus the product's own manifest resolver (`readiness_on`), with the head plan naming its clips
(`plan_names_its_clips`) and, in the pinned columns, pinning the length plan the live planner makes
(`pin_the_plan`); the phone's client protocol is the header the GET sends and the build the tap's
turn is bound to.

Receipts: the card's OWN requirement must be met. A requirement the creator wrote earlier that
nothing can verify yet (a keep-the-text-off-the-mug ask) is honestly partial and unchecked, and is
reported as such, not counted against the tap. The mute card's own receipt may be partial and
unchecked too: whether a phone then mutes the clip is a device question (the kind is dark).
