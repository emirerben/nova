"""Preparation may inspect code and inputs but must never invoke a model."""

import uuid

import pytest

from app.services import slide_post_ai_prepared as prepared


@pytest.mark.asyncio
async def test_preparation_captures_without_invoking_paid_operation():
    calls = []

    async def paid():
        calls.append(True)

    gate = prepared.PrepareAIExecution(uuid.uuid4())
    with pytest.raises(prepared.PreparedInput) as result:
        await gate.run({"agent_input": {"original": True}}, paid)
    assert result.value.snapshot == {"agent_input": {"original": True}}
    assert calls == []


def test_context_snapshot_excludes_callback_and_preserves_attribution():
    from app.agents._runtime import RunContext

    ctx = RunContext(
        creator_id="owner",
        request_id="intent",
        request_id_authoritative=True,
        usage_purpose="test",
        test_run_id="fixture",
        thought_summary_callback=lambda _: None,
    )
    value = prepared.prepared_context(ctx)
    assert value["request_id"] == "intent"
    assert value["test_run_id"] == "fixture"
    assert "thought_summary_callback" not in value
