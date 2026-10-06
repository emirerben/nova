# Request-following real-v2 replay

This directory evaluates a finished-plan projection, not a model response alone.
The deterministic `v2_food_title_cassette` fixture replays a recorded model response through
the real editor agent, runtime-v2 adapter, tool registry, and editor compiler. Its expected
outcome is independent from the produced result; `recorded.plan_after` is never used as the
actual v2 cassette result.

Strategy cassettes similarly run Main Creator through `adapt_creator_action` and
`compile_strategy_to_plan`. The compiler produces an inert `CreatorEditPlan`, so the eval must
not invent a clip timeline or claim a render.

## Evidence states

`compile_only` proves an in-memory adapter/compiler execution. It does not prove staging, a DB
write, task dispatch, device-side application, save, render, or export. Each cassette result
lists those unchecked stages in its notes. `replay_only` is useful deterministic regression
evidence but cannot pass the rollout gate. Only independently supplied live ledger evidence can
qualify a live result for rollout; a fixture budget is never evidence of spend.

## Authored journey register

The deterministic cassettes cover six captions followed by one correction and unsupported
Talking/slides asks against unsaved editor state. The aggregate
[`validation_manifest.md`](validation_manifest.md) links the other scenarios to their focused
tests and calls out gaps. Missing/ambiguous Lisbon location, cooking alignment, replacement,
retry, and later-message state still need a reviewed replay cassette or production capture.
Do not turn a hand-authored reference edit into execution or render proof.

Run the free suite from `src/apps/api`:

```bash
GEMINI_API_KEY=test DATABASE_URL=postgresql://postgres:postgres@localhost:5432/nova_kri459_test \\
  .venv/bin/python -m pytest tests/evals/request_following -q
```
