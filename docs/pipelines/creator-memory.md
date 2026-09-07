# Creator memory pipeline

Creator memory is the account-wide, creator-authored direction that Kria carries
between projects. The ledger is the source of truth; a resolver compiles it into
an immutable, generation-scoped snapshot. `Persona.style` and feedback summaries
remain compatibility inputs and must not be read directly by new planning or
render code.

## Boundaries

1. A committed creator message may create one extraction-outbox row in the same
   database transaction as its `CreationThreadEvent`.
2. Extraction is best effort. It may activate only explicit durable language,
   create an inert suggestion for softer evidence, or produce a no-op. A worker
   failure never blocks the chat or a render.
3. `CreatorDirectionService` owns writes. `CreatorDirectionResolver` owns the
   pure read projection. Routes and tasks must not assign `Persona.style` or
   rebuild direction blocks directly.
4. A snapshot is minted at a generation boundary and is immutable once queued.
   Retry and fast reburn reuse the original snapshot; an explicit refresh creates
   a new generation and snapshot.
5. Logs, agent traces, admin projections, public job responses, and assembly
   plans contain ids, revisions, statuses, and safe error codes only. Readable
   instruction text is owner-visible through the memory surface and account
   export.

## Snapshot contract

`CreatorDirectionSnapshotV1` carries the resolver version, memory revision,
applied item ids, resolved typed values, bounded prompt sections,
capability/enforcement results, conflict ids, project overrides, compatibility
input version, and creation time. It does not carry raw source messages.

Every integration point must use the shared mint/reuse helper:

| Path | Snapshot boundary | Retry behavior |
| --- | --- | --- |
| Creator Agent/chat | session or turn that creates output | Reuse unless the creator explicitly refreshes |
| Content plan | generation/regeneration dispatch | Retry reuses; regenerate may mint current memory |
| Generative job | dispatch before enqueue | Worker and normal retry reuse |
| Classic template | job creation before enqueue | Orchestration and retry reuse |
| Music/auto-music | job creation before enqueue | Orchestration and retry reuse |
| Editor reburn/retime/text edit | render-generation boundary | Pure reburn reuses |
| Legacy job | first safe read before mutation/enqueue | Stamp once; never silently use live memory |

Missing or incompatible snapshots must produce a safe, structured conformance
event. The fallback may continue with compatibility direction, but the receipt
must not claim that the missing rule was enforced.

## Adding a structured key

Adding a key such as `shadow_enabled` requires one change set covering:

- `app/services/creator_direction_capabilities.py`, the single typed-key and
  execution-capability registry, plus normalization rules;
- resolver precedence and capability status;
- the profile editor label and value validation;
- planner prompt mapping and project-override behavior;
- preview, Pillow, Skia, classic, music, generative, and reburn paths;
- renderer-parity tests and `make verify-overlays` when overlays are affected.

Unknown safe text remains advisory prompt context. It must not become an
executable setting by inference.

## Verification

The provider-free smoke test is the first implementation check:

```bash
cd src/apps/api
.venv/bin/pytest tests/services/test_creator_direction_postgres.py::test_activate_resolve_replay_undo_and_soft_suggestion -q
```

The test must prove activation, snapshot resolution, duplicate delivery, exact
Undo, and absence of raw instruction text from safe projections. Focused route,
outbox, and generation-mode tests then cover the failure matrix before the full
backend and preship gates.
