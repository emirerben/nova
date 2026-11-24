# KRI-1563 ask ledger replay

`make kria-replay FIXTURE=kri1563_ask_ledger` runs `scripts/kria/score_kri1563_ask_ledger.py`;
`tests/kria/test_kri1563_ask_ledger.py` runs the same rows in CI. Both drive
`tests/kria/kri1563_ledger_cases.py`.

Each row in `fixture.json` is one chat edit on a ready draft: the requirements the brief
extractor wrote, the copilot's ops (or its no-op outcome) and what the reply must say. The real
editor compiler, diff, brief checkers, ask ledger and reply builder run; no model is called.

| Row | What it pins |
| --- | --- |
| `alanur-t2-tr` / `-en` | Turn 2 of thread 1b8974a0: the title Kria wrote is reported as added; the pictures and the corner layout appear in the reply. `prod_reply` is the reply production sent, which must fail the ledger check. |
| `alanur-t3-tr` / `-en` | Turn 3: captions moved down when the ask says middle are not done; the corner line says she is back at full screen. |
| `alanur-t3-tr-middle-control` | Same ask, captions moved to the middle: done (the check is not just strict). |
| `kri1476-*` | Captions "in the middle" as a chat edit: 0.86 is not done (wrong-content control), 0.62 is. |
| `kri1472-*` | "Only my voice" and "No music" both done by removing the music. |
| `kri1355-*` | "Kein Titel" / "No title": removing the title is done. |
| `alanur-t4-no-ops-tr`, `no-ops-question-en` | A turn with no ops still gives each part a state and a line. |
| `mixed-one-unsupported-en` / `-tr` | A mixed edit: "bigger captions" (an op does it) + "a deeper voice" (no op family can). The caption op applies and its change is named under the caption ask, which is done; only the voice part is declined, with the copilot's reason; no whole-message refusal or redo offer. Red controls in `test_kri1563_ask_ledger.py`: the row fails when the caption op is dropped or the whole ask is declined. |

Provenance: see `source` in `fixture.json`. The production ops are from the `draft_applied`
events of job fa10c4a4 (2026-11-19 21:15:58, 21:17:56, 21:19:15 UTC); the draft is the phone
Talking editor harness, not the creator's footage.
