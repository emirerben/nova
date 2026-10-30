"""Failure modes: wrong source/window/question reuse, guessed timestamps, cache-only spend,
ambiguous positives, stale results, and paid work exceeding the turn cap.
"""

import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services import prompt_moment_inspection as subject
from app.services.named_moments import MomentSet, NamedMoment


def fixture():
    source = SimpleNamespace(
        id=uuid.uuid4(),
        asset_id=uuid.uuid4(),
        object_generation="7",
        uploaded_sha256="a" * 64,
        object_path="owned/video",
        byte_count=12,
    )
    moment = SimpleNamespace(id=uuid.uuid4(), start_s=3.0, end_s=5.0, fact_version="facts")
    named = MomentSet(
        "take",
        "7",
        20.0,
        {},
        (NamedMoment("wave", "hello wave", 0),),
        (),
        source_path="owned/video",
    )
    return source, moment, named


def positive():
    return {
        "status": "found",
        "matches": [{"start_s": 0.0, "end_s": 2.0, "confidence": 0.9, "evidence": "A hand waves"}],
        "sample_sha256": "b" * 64,
    }


def test_cache_identity_binds_question_context_source_and_interval():
    source, moment, named = fixture()
    first = subject.inspection_key("owner", source, moment, "hello wave", named.digest)
    assert first != subject.inspection_key("owner", source, moment, "goodbye wave", named.digest)
    assert first != subject.inspection_key("owner", source, moment, "hello wave", "different")
    source.object_generation = "8"
    assert first != subject.inspection_key("owner", source, moment, "hello wave", named.digest)
    source.object_generation = "7"
    moment.end_s = 6.0
    assert first != subject.inspection_key("owner", source, moment, "hello wave", named.digest)


@pytest.mark.parametrize(
    "patch",
    [
        {"confidence": 0.79},
        {"evidence": ""},
        {"end_s": 2.1},
        {"start_s": -1},
        {"confidence": float("nan")},
    ],
)
def test_invalid_or_negative_answers_do_not_ground(patch):
    _, moment, _ = fixture()
    result = positive()
    result["matches"][0].update(patch)
    assert subject.located_windows(result, moment, 3) is None


def test_boolean_answer_cannot_supply_timestamps():
    _, moment, _ = fixture()
    assert subject.located_windows({"answer": "yes", "sample_sha256": "b" * 64}, moment, 3) is None


@pytest.mark.asyncio
async def test_cache_only_miss_never_claims_or_calls_provider(monkeypatch):
    source, moment, named = fixture()

    async def target(*args, **kwargs):
        return source, [moment]

    monkeypatch.setattr(subject, "_target", target)
    monkeypatch.setattr(subject, "get_collection_analysis", lambda key: None)

    def forbidden(*args, **kwargs):
        pytest.fail("cache-only must not acquire paid work")

    monkeypatch.setattr(subject, "claim_collection_analysis", forbidden)
    monkeypatch.setattr(subject, "inspect_interval", forbidden)
    result = await subject.inspect_named_moments(
        None, creator_id=uuid.uuid4(), item_id=uuid.uuid4(), moment_set=named, events=[]
    )
    assert not result.groundings


@pytest.mark.asyncio
async def test_cached_answer_preserves_exact_bounds_and_uncertainty(monkeypatch):
    source, moment, named = fixture()

    async def target(*args, **kwargs):
        return source, [moment]

    monkeypatch.setattr(subject, "_target", target)
    monkeypatch.setattr(subject, "get_collection_analysis", lambda key: positive())
    result = await subject.inspect_named_moments(
        None, creator_id=uuid.uuid4(), item_id=uuid.uuid4(), moment_set=named, events=[]
    )
    found = result.groundings["wave"].found
    assert (found.start_s, found.end_s) == (3.0, 5.0)
    assert found.evidence["uncertain"] is True
    assert found.evidence["question"] == subject.inspection_question("hello wave")
    assert found.evidence["source_id"] == str(source.id)


@pytest.mark.asyncio
async def test_two_positive_windows_remain_ambiguous(monkeypatch):
    source, moment, named = fixture()
    second = SimpleNamespace(**{**vars(moment), "id": uuid.uuid4(), "start_s": 8.0, "end_s": 10.0})

    async def target(*args, **kwargs):
        return source, [moment, second]

    monkeypatch.setattr(subject, "_target", target)
    monkeypatch.setattr(subject, "get_collection_analysis", lambda key: positive())
    result = await subject.inspect_named_moments(
        None, creator_id=uuid.uuid4(), item_id=uuid.uuid4(), moment_set=named, events=[]
    )
    assert result.groundings["wave"].status == "ambiguous"
    assert result.groundings["wave"].found is None


@pytest.mark.asyncio
async def test_source_change_after_cache_read_discards_result(monkeypatch):
    source, moment, named = fixture()
    calls = 0

    async def target(*args, **kwargs):
        nonlocal calls
        calls += 1
        return (source, [moment]) if calls == 1 else None

    monkeypatch.setattr(subject, "_target", target)
    monkeypatch.setattr(subject, "get_collection_analysis", lambda key: positive())
    result = await subject.inspect_named_moments(
        None, creator_id=uuid.uuid4(), item_id=uuid.uuid4(), moment_set=named, events=[]
    )
    assert not result.groundings


@pytest.mark.asyncio
async def test_provider_replay_uses_one_shared_deadline_and_maximum_three_calls(monkeypatch):
    source, moment, named = fixture()
    named = replace(named, moments=tuple(NamedMoment(str(i), f"action {i}", i) for i in range(5)))

    async def target(*args, **kwargs):
        return source, [moment]

    monkeypatch.setattr(subject, "_target", target)
    monkeypatch.setattr(subject, "get_collection_analysis", lambda key: None)
    monkeypatch.setattr(
        subject, "claim_collection_analysis", lambda *a, **kw: SimpleNamespace(status="claimed")
    )
    monkeypatch.setattr(subject, "complete_media_asset_fact", lambda *a, **kw: True)
    contexts = []

    def transport(source, moment, question, *, ctx, agent_factory):
        assert agent_factory is subject.SourceMomentLocationAgent
        contexts.append(ctx)
        return positive()

    monkeypatch.setattr(subject, "inspect_interval", transport)
    await subject.inspect_named_moments(
        None,
        creator_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        moment_set=named,
        events=[],
        allow_provider=True,
    )
    assert len(contexts) == 3
    assert len({ctx.deadline_monotonic for ctx in contexts}) == 1
    assert all(ctx.estimated_max_cost_usd == 0.05 for ctx in contexts)


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [False, True])
async def test_grounded_snapshot_needs_current_index_before_reuse(monkeypatch, current):
    source, moment, named = fixture()
    moment.text = "hello wave"
    named = replace(
        named,
        analysis={"best_moments": [{"start_s": 3.0, "end_s": 5.0, "description": "hello wave"}]},
    )
    looked_up = []

    async def target(*args, **kwargs):
        looked_up.append(True)
        return (source, [moment]) if current else None

    monkeypatch.setattr(subject, "_target", target)

    def forbidden(*args, **kwargs):
        pytest.fail("Validated existing grounding needs no model inspection")

    monkeypatch.setattr(subject, "get_collection_analysis", forbidden)
    monkeypatch.setattr(subject, "inspect_interval", forbidden)
    result = await subject.inspect_named_moments(
        None, creator_id=uuid.uuid4(), item_id=uuid.uuid4(), moment_set=named, events=[]
    )
    assert looked_up
    if current:
        assert not result.groundings
    else:
        assert result.groundings["wave"].status == "unlocated"


def test_uncertain_sample_is_not_conclusive_absence():
    _, moment, _ = fixture()
    uncertain = {"status": "uncertain", "matches": [], "sample_sha256": "b" * 64}
    assert subject.located_windows(uncertain, moment, 3) is None
    absent = {**uncertain, "status": "absent"}
    assert subject.located_windows(absent, moment, 3) == []


@pytest.mark.asyncio
async def test_inspection_evidence_never_replaces_turkish_creator_labels(monkeypatch):
    source, moment, named = fixture()
    named = replace(named, moments=(NamedMoment("wave", "selam verme", 0),))

    async def target(*args, **kwargs):
        return source, [moment]

    monkeypatch.setattr(subject, "_target", target)
    monkeypatch.setattr(subject, "get_collection_analysis", lambda key: positive())
    result = await subject.inspect_named_moments(
        None, creator_id=uuid.uuid4(), item_id=uuid.uuid4(), moment_set=named, events=[]
    )
    found = result.groundings["wave"].found
    assert found.description == "selam verme"
    assert found.evidence["answer_evidence"] == "A hand waves"


def test_turkish_request_words_reach_the_model_unescaped():
    from app.agents.clip_question import ClipQuestionInput
    from app.agents.source_moment_location import SourceMomentLocationAgent

    agent = SourceMomentLocationAgent.__new__(SourceMomentLocationAgent)
    prompt = agent.render_prompt(
        ClipQuestionInput(
            file_uri="gs://bucket/sample", question=subject.inspection_question("Atakan'ın ısırığı")
        )
    )
    assert "ısırığı" in prompt
    assert "\\u0131" not in prompt


@pytest.mark.asyncio
async def test_spent_turn_budget_starts_no_inspection(monkeypatch):
    _source, _moment, named = fixture()

    async def target(*args, **kwargs):
        pytest.fail("a turn with no time left must not read the index")

    monkeypatch.setattr(subject, "_target", target)
    result = await subject.inspect_named_moments(
        None,
        creator_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        moment_set=named,
        events=[],
        allow_provider=True,
        budget_s=0.0,
    )
    assert not result.groundings


@pytest.mark.asyncio
async def test_inspection_never_outlives_the_turn_budget(monkeypatch):
    import asyncio
    import time

    _source, _moment, named = fixture()

    async def slow_target(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(subject, "_target", slow_target)
    started = time.monotonic()
    result = await subject.inspect_named_moments(
        None,
        creator_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        moment_set=named,
        events=[],
        budget_s=0.2,
    )
    assert time.monotonic() - started < 5
    assert not result.groundings


@pytest.mark.asyncio
async def test_cache_only_conflict_on_one_moment_keeps_other_cached_answers(monkeypatch):
    from fastapi import HTTPException

    source, moment, named = fixture()
    creator = uuid.uuid4()
    named = replace(
        named,
        moments=(NamedMoment("a", "uncached action", 0), NamedMoment("b", "cached action", 1)),
    )
    cached_key = subject.inspection_key(creator, source, moment, "cached action", named.digest)

    async def target(*args, **kwargs):
        return source, [moment]

    def cached(key):
        return positive() if key == cached_key else None

    def conflict(*args, **kwargs):
        raise HTTPException(409, {"code": "collection_analysis_unavailable"})

    monkeypatch.setattr(subject, "_target", target)
    monkeypatch.setattr(subject, "get_collection_analysis", cached)
    monkeypatch.setattr(subject, "claim_collection_analysis", conflict)
    result = await subject.inspect_named_moments(
        None,
        creator_id=creator,
        item_id=uuid.uuid4(),
        moment_set=named,
        events=[],
        allow_provider=True,
    )
    assert result.groundings["b"].status == "grounded"
    assert "a" not in result.groundings or result.groundings["a"].status != "grounded"
