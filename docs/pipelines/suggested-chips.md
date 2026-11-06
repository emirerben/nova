# Suggested chips: replay eval, telemetry and funnel (KRI-756)

How we know the chips are right, offline and for free. iOS app only (the web client ignores the
feature). The engine, the agent and the wire contract are in
[`suggestion-chips`](suggestion-chips.md); this page is the eval, the OLD-vs-NEW artifact, the
telemetry checks and the funnel. **Nothing here calls a model.**

## Run it

```bash
cd src/apps/api
pytest tests/evals/test_suggestion_chips_evals.py tests/evals/test_suggestion_agent_evals.py \
  tests/services/test_suggestion_chips_guards.py tests/scripts/test_suggestion_chip_funnel.py -q
        # CI (test_suggestion_chips_postgres.py also runs on a *_test database)
.venv/bin/python scripts/suggestion_chip_report.py          # writes <repo>/.chip-eval/report.md (gitignored)
.venv/bin/python scripts/suggestion_chip_report.py --stdout # print the whole report
.venv/bin/python scripts/suggestion_chip_report.py --write-contract   # refresh the iOS mock JSON
.venv/bin/python scripts/suggestion_chip_funnel.py --fixture          # offered/tapped/kept, offline
```

## What is checked

Two layers: the deterministic **engine** decides which chips may exist (G1 to G9, G11); the
**suggestion agent** `nova.chat.suggestion_agent` only picks and orders them (G10 + cassettes +
telemetry; the server builds and proves every card, see [`suggestion-chips`](suggestion-chips.md)).

| gate | proves | bar |
| --- | --- | --- |
| G1 grounding | every media id, ordinal, moment and text bar a chip names is in the input | 100% |
| G2 capability matrix | no chip whose manifest capability or editor op family is unavailable; `opener_clip` / `pinned_text` (dark) never | 0 violations |
| G3 not already in edit | no title / captions / music / SFX / transition repeat | 0 violations |
| G4 diversity | <= 4 chips, 4 chips => 3+ kinds, 1-based dense ranks, 1 per kind, <= 1 copy-bearing | 0 violations |
| G5 chip #1 | first prompt opens with `default_edit`, rank 1, exactly `Suggest an edit.` | 100% |
| G6 hygiene | label <= 36, message <= 120, language, no quotes, stoplist; an agent label passes the validator; a `why` is <= 48 chars and shares a word with the footage | 0 violations |
| G7 closed template set | every message is in the copy deck; footage text appears in neither message nor label (only in `why`) | 0 violations |
| G8 determinism | byte-identical output | 100% |
| G9 executability | a recorded planner answer per (kind, state) compiles to a non-empty change through `replay_v2_strategy` / `replay_v2_editor` | >= 95% |
| G10 agent fuzz | for ARBITRARY model output the served chips are live candidates with `label`, `message`, `kind`, `capability`, `anchor`, `requires_protocol` copied from them, and every served v2 card is an issued, enabled registry kind whose action passes its witness on the live snapshot, with closed-template title and prompt, no action on the wire; no flag-off / already-in-edit / dark kind, valid `why` / `copy`, limits held | 100% |
| G11 wire contract | status / state / `retry_after_s` / `wording` / closed kind enum / `default_edit` flag / ids | 100% |

Every gate has negative controls (a known-bad set it must flag). G10 also has **mutation controls**:
disabling the label validator, the `why` validator or the pool check must turn the fuzz red.

Fixtures: 60 engine cases (speech / music / silent B-roll, 1 vs 12 clips, SFX and overlays off,
empty catalog, phone vs cloud, en vs tr, title / captions / music already present, understanding
pending / empty / done, no media, photos only, photos plus one video, open question, failed
render, in-flight render, duplicate subjects, v1 without the agent, no witness) incl. the
`nermin-*` replay fixtures and `east-run-brief` (Turkish) projected into first-prompt and ready
cases. 31 suggestion-agent cassettes over those contexts.
`tests/fixtures/agent_evals/{suggestion_chips,suggestion_agent}/`.

Run `scripts/suggestion_chip_report.py` for the current numbers (`.chip-eval/report.md`):
engine gates, G9, the cassette pass table and the G10 fuzz (45 contexts x 30 seeded answers).

**Limits, stated plainly.** G9 is the COMPILE path: a hand-authored planner answer parses, adapts
and compiles; it does not prove today's Main Creator / copilot choose that answer. The first-prompt
`hook` is "reaches wording approval with a candidate". The cassettes are hand-authored, so they
measure what the safeguards do with answers I wrote, not what gpt-6.1-sol picks. Cost, latency and
the share of real picks that pass their witness are unmeasured (the paid live eval below). Witness tiers (`strategy` | `editor` | `policy_only`)
are reported per chip so "executable" is never claimed more strongly than it was proven;
`default_edit` and `title_animation` are `policy_only`.

## Labels

The agent no longer writes labels, so there is no label-acceptance measurement: every label is a
template, and `test_every_template_label_passes_the_label_validator` keeps the template deck inside
its own closed vocabulary (the old 130-label corpus was deleted with the label agent).

## Findings

Fixed in the engine (KRI-756 B1), pinned by fixtures: existing opening text counts as a title
(`rd_generative_intro_bar_present`); `title_animation` needs the `text` family; first-prompt
captions are gated on the footage and edit format, not the manifest `caption_style` capability
(`fp_caption_style_off` pins that captions are still offered when it is off).

Open:

1. (dark) the raw creator filename goes into the `opener_clip` label and message, so a quote or
   `{time}` survives. Sanitize before `CLIP_ORDINAL_PROVEN` flips.

The agent-era gaps the strict xfails used to pin are closed by construction: a `why` or `copy`
that repeats a run of transcript words is dropped (`shares_run`), and there is no `label_source:
agent` any more.

Noted, not gated: `already_in_edit` tags `captions` when the edit has no caption lane and
`title_animation` when there is no title (the agent is told they are "already in the edit"); an
empty answer is stored as `fallback_reason: empty_ranking` and an answer none of whose picks
serves anything as `no_usable_picks`; fenced JSON is not parsed (strict mode never returns it;
the run falls back).

## OLD vs NEW

`scripts/suggestion_chip_report.py` prints, per fixture, the chips a creator saw before (iOS
`ReadyStage`: two hardcoded strings after a render, read from the base commit; nothing at first
prompt) next to the new chips, with the gate, witness tier and, for the cassette contexts, the
agent-picked set beside the deterministic set. The artifact is `.chip-eval/report.md`.

## Telemetry (structlog, contract section 8)

Asserted with `structlog.testing.capture_logs` in `test_suggestion_agent_evals.py`:

* `suggestion_chips.shown {thread_id, set_id, state, wording, kinds, n}`: once per FINAL set_id
  (the pending set does not emit; a pending set is replaced in place under the same set_id).
  "Shown" means served, not seen: there is no client beacon yet.
* `suggestion_chips.agent_run {agent_run_id, model, prompt_version, latency_ms, n_in, n_out,
  n_dropped, drop_reasons, cost_usd, fallback_reason?}`: one per run, with `fallback_reason` in
  `schema_error | label_rejected | timeout | budget | stale_digest | flag_off | run_cap`, each
  triggered from its real cause (a cassette, a raising client, or the Celery task entry).
* `suggestion_chips.tap` = the 8-key stamp (`set_id, chip_id, kind, rank, state, label_source,
  prompt_version, default_edit`) written to `user_message.payload.suggestion`.
* Flag off: no event, no payload key, no enqueue.

## Funnel

`scripts/suggestion_chip_funnel.py` (read-only; database mode reads `creation_thread_events` and
the `thread.state["suggestion_sets"]` ring):

* offered = chips of every served set (the ring keeps 5 per thread; `--shown-log` merges the
  unbounded `suggestion_chips.shown` lines, which carry kinds and wording but not `label_source`);
* tapped = `user_message` with `payload.suggestion`;
* kept (derived, nothing emits it) = tapped AND turn completed AND no `plan_section_undo` /
  `plan_undo_all` / `draft_undone` before the next `user_message`; questions asked back and
  failures are counted apart;
* every table is also cut by `label_source` and `wording`.

**Chip #1 bias.** A media-only send transmits exactly `Suggest an edit.`, and the stamp has no
explicit-vs-text flag (the wire contract is fixed), so a media-only send counts as a tap of chip
#1 and a typed lookalike counts too. Read chip #1 as an upper bound; the report prints chip-#1
taps apart from lever taps. Adding a `via: id | text` key to the stamp is the clean fix (separate
ticket). The old iOS PromptChips only filled the composer, so there is **no tap baseline**:
compare completed first renders and chat edits per render between a flag-off and a flag-on cohort
(`KRIA_SUGGESTION_CHIPS_USER_IDS` gives a clean arm), not a tap rate.

## Paid / approval-needed TODOs (not run)

1. Live tap-to-op executability against today's `main_creator` and `edit_copilot` (bar 95%,
   development ledger cap $2, no judge).
2. Empathy / cringe judge (`audit-user-empathy`) on the template deck and on recorded agent labels.
3. Blind old-vs-new pairwise on about 20 sessions (`llm_judge.py` has no pairwise mode).
4. Live replay of `nova.chat.suggestion_agent` (gpt-6.1-sol, about 10 real threads, development
   ledger cap $2, `AI_OPENAI_SUGGESTIONS_BUDGET_USD` set for the run): record cost per run, latency
   and the share of picks that pass their witness, then replay the rubric judge.
5. Agent-picked vs deterministic order A/B in real taps (`wording` in `shown` / `tap`).
6. An `opener_clip` cassette proving filename-to-hero resolution (unlocks `CLIP_ORDINAL_PROVEN`).
