# Suggested chips: replay eval, telemetry and funnel (KRI-756)

How we know the chips are right, offline and for free. iOS app only (the web client ignores the
feature). The engine, the agent and the wire contract are in
[`suggestion-chips`](suggestion-chips.md); this page is the eval, the OLD-vs-NEW artifact, the
telemetry checks and the funnel. **Nothing here calls a model.**

## Run it

```bash
cd src/apps/api
pytest tests/evals/test_suggestion_chips_evals.py tests/evals/test_chip_suggester_evals.py \
  tests/services/test_suggestion_chips_guards.py tests/scripts/test_suggestion_chip_funnel.py -q
        # CI (test_suggestion_chips_postgres.py also runs on a *_test database)
.venv/bin/python scripts/suggestion_chip_report.py          # writes <repo>/.chip-eval/report.md (gitignored)
.venv/bin/python scripts/suggestion_chip_report.py --stdout # print the whole report
.venv/bin/python scripts/suggestion_chip_report.py --write-contract   # refresh the iOS mock JSON
.venv/bin/python scripts/suggestion_chip_funnel.py --fixture          # offered/tapped/kept, offline
```

## What is checked

Two layers: the deterministic **engine** decides which chips may exist (G1 to G9, G11); the
**wording agent** `nova.chat.suggester` only ranks and words them (G10 + cassettes + telemetry).

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
| G10 wording fuzz | for ARBITRARY model output the served set is a subset of the live candidates with `message`, `kind`, `capability`, `anchor`, `requires_protocol` copied from them, no flag-off / already-in-edit / dark kind, valid label and `why`, limits held | 100% |
| G11 wire contract | status / state / `retry_after_s` / `wording` / closed kind enum / `default_edit` flag / ids | 100% |

Every gate has negative controls (a known-bad set it must flag). G10 also has **mutation controls**:
disabling the label validator, the `why` validator or the pool check must turn the fuzz red.

Fixtures: 60 engine cases (speech / music / silent B-roll, 1 vs 12 clips, SFX and overlays off,
empty catalog, phone vs cloud, en vs tr, title / captions / music already present, understanding
pending / empty / done, no media, photos only, photos plus one video, open question, failed
render, in-flight render, duplicate subjects, v1 without the agent, no witness) incl. the
`nermin-*` replay fixtures and `east-run-brief` (Turkish) projected into first-prompt and ready
cases. 25 wording-agent cassettes over those contexts and a 130-label corpus.
`tests/fixtures/agent_evals/{suggestion_chips,chip_suggester}/`.

Latest run (`.chip-eval/report.md`, this branch): G1 to G8 and G11 60/60, expectation pins 60/60,
G9 11/11, cassettes 25/25, G10 fuzz 1350/1350 (45 contexts x 30 seeded answers; 22,500 more in a
one-off hunt, 0 violations), label recall 73/84 (87%), label rejection 46/46.

**Limits, stated plainly.** G9 is the COMPILE path: a hand-authored planner answer parses, adapts
and compiles; it does not prove today's Main Creator / copilot choose that answer. The first-prompt
`hook` is "reaches wording approval with a candidate". The cassettes and the label corpus are
hand-authored, so they measure what the safeguards do with answers I wrote, not what Flash writes.
Cost and latency of the agent are unmeasured. Witness tiers (`strategy` | `editor` | `policy_only`)
are reported per chip so "executable" is never claimed more strongly than it was proven;
`default_edit` and `title_animation` are `policy_only`.

## Label acceptance (is the closed lexicon too strict?)

Of 84 plausible creator-voiced labels (en 49/56, tr 24/28) the validator lets 87% through; it
stops 46/46 cringe, wrong-kind, content-leaking, digit / quote / emoji, injection and
wrong-language labels. Loosening to stoplist-only would let 25 of those 46 (54%) through, so
**keep the lexicon, widen it** with the rejected plausible words (`seconds`, `screen`, `parts`,
`together`, "cuts" under sfx, `I`, `, ` with a space, a few Turkish verbs), and do not justify
enabling the agent by its wording: a rejected label costs nothing (the template label stays); the
value is the ranking and the grounded `why`. The real rate needs a live replay (paid, below).

## Findings

Fixed in the engine (KRI-756 B1), pinned by fixtures: existing opening text counts as a title
(`rd_generative_intro_bar_present`); `title_animation` needs the `text` family; first-prompt
captions are gated on the footage and edit format, not the manifest `caption_style` capability
(`fp_caption_style_off` pins that captions are still offered when it is off).

Open (strict xfails; each fails the day it is fixed, `BACKEND_GAPS` in
`tests/evals/chip_suggester_harness.py`):

1. `validate_why` needs only ONE footage word, so a call to action ("pouring matcha, tap Buy Now
   at example dot com") is shown as the subtitle. Display-only, never sent.
2. `validate_why` checks language one way: English text passes on a Turkish thread.
3. `label_source` is `agent` even when the agent's label equals the template, which inflates the
   agent arm of the Flash-vs-template split.
4. (dark) the raw creator filename goes into the `opener_clip` label and message, so a quote or
   `{time}` survives. Sanitize before `CLIP_ORDINAL_PROVEN` flips.

Noted, not gated: `already_in_edit` tags `captions` when the edit has no caption lane and
`title_animation` when there is no title (the agent is told they are "already in the edit"); an
all-rejected answer is stored with `status: ok` and keeps the model's order while the served set
reports `wording: fallback`; a model that legitimately returns an empty ranking is reported as
`fallback_reason: label_rejected`; fenced JSON is not parsed (falls back to templates).

## OLD vs NEW

`scripts/suggestion_chip_report.py` prints, per fixture, the chips a creator saw before (iOS
`ReadyStage`: two hardcoded strings after a render, read from the base commit; nothing at first
prompt) next to the new chips, with the gate, witness tier and, for the cassette contexts, the
agent-worded set beside the template set. The artifact is `.chip-eval/report.md`.

## Telemetry (structlog, contract section 8)

Asserted with `structlog.testing.capture_logs` in `test_chip_suggester_evals.py`:

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
4. Flash-vs-template wording A/B by `label_source` in real taps (fix finding 3 first).
5. Live replay of `nova.chat.suggester` to replace the hand-authored label corpus with a measured
   acceptance rate, latency and cost.
6. An `opener_clip` cassette proving filename-to-hero resolution (unlocks `CLIP_ORDINAL_PROVEN`).
