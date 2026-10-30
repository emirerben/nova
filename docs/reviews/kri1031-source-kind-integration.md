# Length exclusions preserve source constraints

This local integration combines #1928 with main `e24e18eec304423d577360e3a2a47f4df72dfd12`.
The starting PR head is `002c81f695f2d02da59d59e0ff8db0a467db0825`.

An image-scoped length exclusion previously resolved a short video from mixed attachments.
The measured shortcut now requires `intent.accepts_source_kind("video")`.
Unspecified and explicit video requests retain measured behavior. Image requests reach the existing resolver.
Creator choices run first. A conflicting source-kind choice still stops before the resolver.

The merge retains the measured pass and the new source-kind conflict guard.
It also registers `by_length` and `under_s` inside rejected selections under the existing server-owned evidence rule.
Those fields are source hints. They do not promise rendered output.

## Verification

The new mixed-media matrix failed twice before the guard and passed all nine cases after it.
The failures were the image-only direct resolver and full-planner cases.
Creator-choice precedence, explicit video and legacy requests passed throughout.

The first combined contract matrix found four guard failures for the two unregistered nested fields.
After registration, the focused matrix passed 230 tests. One Postgres reader fixture skipped because `nova_test` was unreachable.
The strict schema guard remains enabled. No ignored-field or unsupported-field exception was added.

The matrix covers length exclusions, compound bus-exclusion receipts, source-kind resolution, selection provenance, reader compatibility, None binding, story reconciliation and render contracts.
It uses frozen provider outputs at the transport boundary and real planner and resolver code.
Credit and generated-media gates stay off. Budgets are zero and `NOVA_PAID_AI_WORKFLOW=0`.

Run from `src/apps/api` with the API virtual environment and dummy CI configuration.

```bash
python -m pytest tests/kria/test_kri1031_too_short_clips.py tests/kria/test_kri1026_plaza_exclusions.py tests/services/test_clip_intent_source_kind.py tests/services/test_clip_selection_provenance.py tests/kria/test_selection_reader_compatibility.py tests/kria/test_selection_reader_postgres.py tests/services/test_story_intent_reconciliation.py tests/kria/test_kri1025_none_answer_pin.py tests/services/test_clip_selection.py tests/services/test_creator_render_contract.py -q -p no:cacheprovider -rs
```

The local logs are retained under the task evidence directory.
They are `verify1928-source-kind-red-tests.log`, `verify1928-source-kind-green-tests.log`,
`verify1928-source-kind-matrix.log` and `verify1928-source-kind-matrix-green.log`.

## Tested source

| File | SHA-256 |
| --- | --- |
| `src/apps/api/app/services/clip_intent_planning.py` | `d5d94aee778dae2f76aabe8ea6716eda3ff914899ca29f2875568fe359feed19` |
| `src/apps/api/app/services/too_short_clips.py` | `3bc0a4a6b00d189bab3ffc4e80aa46a3158a60613e235a87e5d54f3a595a4f4d` |
| `src/apps/api/app/services/creator_render_contract.py` | `a26483efdd5d45d2f2d77b7b5e4bdc24fc2f07bb63289cd85cb9c17930dbe317` |
| `src/apps/api/tests/kria/test_kri1031_too_short_clips.py` | `53bcc608d3b8aa9431db884dfdf552a9db37ff42da300cd0760b71cd095d24a0` |

Independent review and fresh exact-head hosted checks remain required.
This record does not claim phone output, original Plaza incident fulfillment or production exposure.
