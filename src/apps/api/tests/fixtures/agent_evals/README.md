# Agent eval fixtures

Per-agent JSON cassettes used by `tests/evals/`.

## Layout

```
agent_evals/
├── template_recipe/
│   ├── prod_snapshots/   ← exported from VideoTemplate.recipe_cached
│   └── golden/           ← hand-crafted, human-rated
├── clip_metadata/
│   ├── prod_snapshots/   ← (currently empty — best_moments not persisted in DB)
│   └── golden/
└── creative_direction/
    ├── prod_snapshots/
    └── golden/
```

## Format

Every fixture is a single JSON file with this shape:

```json
{
  "agent": "<structlog name, e.g. nova.compose.template_recipe>",
  "prompt_version": "<the prompt_version that produced this output>",
  "input": { /* validated Agent.Input dict */ },
  "raw_text": "/* the recorded raw model response (the cassette payload) */",
  "output": { /* the parsed Agent.Output dict — sanity + judge sees this */ },
  "meta": { /* free-form: template_id, source, exported_at, notes */ }
}
```

`raw_text` is what the runtime would have received from Gemini. For JSON-mode agents (template_recipe, clip_metadata) it is a JSON string. For freeform agents (creative_direction) it is a plain text paragraph.

## Adding new fixtures

Run `scripts/export_eval_fixtures.py` to populate `prod_snapshots/` from the local DB. For `clip_metadata` (where `best_moments` aren't persisted in DB) and for `golden/` cases, hand-author the JSON.

## Drift policy

Cassettes are **frozen at write time**. If you bump an agent's `prompt_version` and the new prompt produces structurally different output (e.g., new fields), regenerate fixtures explicitly — don't silently let evals drift.

## `suggestion_chips/` and `chip_suggester/` (KRI-756)

`suggestion_chips/` holds deterministic chip-engine cases, not model cassettes, so they do NOT have
the `agent` / `raw_text` shape above. `contract_example_*.json` are the mocks for the iOS lane
(regenerate with `scripts/suggestion_chip_report.py --write-contract`).

`chip_suggester/` holds hand-authored raw answers for `nova.chat.suggester` (NOT recorded model
output) plus `label_corpus.json`. A cassette is `{id, description, context, raw_text |
model_output, expect, tags}`: `context` names an engine case, so the candidate ids come from the
live plan via `{{cid:<kind>}}` / `{{guess:<kind>}}` placeholders instead of being frozen. See
`tests/evals/README.md` ("Suggestion-chip replay eval").
