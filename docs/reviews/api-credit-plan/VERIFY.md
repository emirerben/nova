# Verify the offline credit increment

Run these commands from the repository root with the existing API environment active. No provider credentials are required.

1. Run the contract checks and the existing comparison guards.

```sh
python -m pytest -c src/apps/api/pyproject.toml src/apps/api/tests/agents/test_credit_plan_contracts.py src/apps/api/tests/agents/test_comparison_workflow.py src/apps/api/tests/agents/test_text_provider_contracts.py src/apps/api/tests/agents/test_sealed_comparison.py -q
```

2. Create a private directory outside Git. Keep packets there because future inputs may contain private creator material.

```sh
credit_workspace=$(mktemp -d /tmp/kria-credit-plan.XXXXXX)
```

3. Freeze the existing synthetic fixture. The local settings below satisfy configuration validation. These offline operations do not open a database connection.

```sh
STORAGE_BUCKET=nova-test DATABASE_URL=postgresql://postgres:postgres@localhost:5432/nova_test PYTHONPATH=src/apps/api python -m app.cli.compare_creator_models freeze --case src/apps/api/tests/fixtures/agent_evals/main_creator/montage_strategy.json --arm O:gpt-6.1-sol:medium --arm S:claude-sonnet-5-5:high --output "$credit_workspace/packet.json"
```

4. Produce the provider allocation report.

```sh
STORAGE_BUCKET=nova-test DATABASE_URL=postgresql://postgres:postgres@localhost:5432/nova_test PYTHONPATH=src/apps/api python -m app.cli.compare_creator_models credit-preflight --packet "$credit_workspace/packet.json"
```

The report must say `live_authorized: false`, `quality_verdict: unassessed`, and `actual_export: unassessed`. Adding a Gemini arm must report Google as unfunded by the OpenAI and Claude credit plan. The existing `live` and `sealed-run` approval gates remain separate. This command does not approve, charge, or execute a packet.

The retained `offline-preflight.json` contains aggregate costs only. Its packet digest refers to the fixture frozen in this checkout. Freeze a new packet after prompt, schema, price, or runtime changes.
