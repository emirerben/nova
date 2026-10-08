"""KRI-237: an editor-eligible ask on a rendered item must never silently become a render.

Every silent `None` exit of `_load_editor_target` is logged with a reason, and the
copilot-first fast path answers with a recovery message instead of re-planning.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from app.agents.main_creator import MAIN_CREATOR_CONVERSATION_MAX, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.models import CreationThread, CreatorAgentSession, Job
from tests.kria.test_creative_brief import (
    _ASK,
    _MANIFEST,
    _ExpiringItem,
    _planner_db,
    _upd,
)

pytestmark = pytest.mark.asyncio


def _db(thread, session, job, execute_rows=None):  # noqa: ANN001, ANN202
    async def get(model, _identifier):  # noqa: ANN001, ANN202
        return {CreationThread: thread, CreatorAgentSession: session, Job: job}[model]

    return SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: None,
                all=lambda: [],
                scalars=lambda: SimpleNamespace(all=lambda: execute_rows or []),
            )
        ),
        rollback=AsyncMock(),
    )


async def _load(db, item):  # noqa: ANN001, ANN202
    with capture_logs() as logs:
        target = await planner._load_editor_target(db, thread_id=uuid.uuid4(), item=item)
    return target, [e for e in logs if e["event"] == "kria_editor_target_unavailable"]


async def test_no_current_job_logs_reason() -> None:
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=None)
    target, logs = await _load(_db(None, None, None), item)
    assert target is None
    assert [e["reason"] for e in logs] == ["no_current_job"]
    assert planner._editor_target_miss.get() == "no_current_job"


async def test_no_active_session_logs_reason() -> None:
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=uuid.uuid4())
    thread = SimpleNamespace(active_creator_agent_session_id=None)
    target, logs = await _load(_db(thread, None, None), item)
    assert target is None
    assert [e["reason"] for e in logs] == ["no_active_session"]


async def test_session_job_mismatch_logs_both_ids() -> None:
    job_id, other = uuid.uuid4(), uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=job_id)
    thread = SimpleNamespace(active_creator_agent_session_id=uuid.uuid4())
    session = SimpleNamespace(id=uuid.uuid4(), target_job_id=other)
    job = SimpleNamespace(id=job_id, assembly_plan={})
    target, logs = await _load(_db(thread, session, job), item)
    assert target is None
    assert [e["reason"] for e in logs] == ["session_job_mismatch"]
    assert logs[0]["job_id"] == str(job_id)
    assert logs[0]["session_target_job_id"] == str(other)


async def test_no_ready_variant_logs_variants_seen() -> None:
    job_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=job_id)
    thread = SimpleNamespace(active_creator_agent_session_id=uuid.uuid4())
    session = SimpleNamespace(id=uuid.uuid4(), target_job_id=job_id, target_variant_id="v1")
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={"variants": [{"variant_id": "v1", "render_status": "failed"}]},
    )
    target, logs = await _load(_db(thread, session, job), item)
    assert target is None
    assert [e["reason"] for e in logs] == ["no_ready_variant"]
    assert logs[0]["variants_seen"] == [{"variant_id": "v1", "render_status": "failed"}]
    assert logs[0]["target_variant_id"] == "v1"


async def test_no_allowed_families_logs_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=job_id)
    thread = SimpleNamespace(active_creator_agent_session_id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        plan_item_id=item.id,
        target_job_id=job_id,
        target_variant_id="v1",
        target_generation_id=None,
    )
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={"variants": [{"variant_id": "v1", "render_status": "ready"}]},
    )
    monkeypatch.setattr(planner, "_copilot_clip_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner, "build_editor_snapshot", lambda *_a, **_k: {"allowed_op_families": []}
    )
    target, logs = await _load(_db(thread, session, job), item)
    assert target is None
    assert [e["reason"] for e in logs] == ["no_allowed_families"]


def _wire(monkeypatch, *, miss, plan_after=None):  # noqa: ANN001, ANN202
    item = _ExpiringItem(
        id=uuid.uuid4(),
        content_plan_id=uuid.uuid4(),
        current_job_id=uuid.uuid4(),
        edit_format="montage",
    )
    db, creator_id = _planner_db(item)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(settings, "kria_copilot_first_enabled", True)
    manifest = _MANIFEST.model_copy(update={"item_id": str(item.id)})
    monkeypatch.setattr(
        planner, "resolve_item_creator_context", AsyncMock(return_value=(manifest, []))
    )
    monkeypatch.setattr(planner, "creator_context", lambda *_a: ("creator", "item"))
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=None))
    # Planner now folds the full copy lifecycle before routing. These editor-target
    # tests intentionally have no thread events; keep their fake DB out of that path.
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))

    async def revision(_db, *, thread_id, item, user_message):  # noqa: ANN001, ANN202
        planner._editor_target_miss.set(miss)
        return None

    monkeypatch.setattr(planner, "_plan_editor_revision", AsyncMock(side_effect=revision))
    monkeypatch.setattr(planner, "_load_editor_target", AsyncMock(return_value=None))
    creator_runs = []

    class FakeAgent:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, agent_input, **_kwargs):  # noqa: ANN001, ANN003, ANN201
            creator_runs.append(agent_input)
            from app.agents._schemas.creator_agent import AskUser

            return SimpleNamespace(
                action=AskUser(**_ASK),
                brief_updates=[_upd("select", "global")],
                creative_decision=None,
            )

    monkeypatch.setattr(planner, "MainCreatorAgent", FakeAgent)

    class FakeBriefExtractor:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return SimpleNamespace(brief_updates=[_upd("select", "global")])

    monkeypatch.setattr(planner, "BriefExtractorAgent", FakeBriefExtractor)
    monkeypatch.setattr(planner, "default_client", lambda: object())
    return db, item, creator_id, creator_runs


async def test_guard_returns_recovery_instead_of_replan(monkeypatch: pytest.MonkeyPatch) -> None:
    db, item, creator_id, runs = _wire(monkeypatch, miss="no_ready_variant")
    result = await planner.plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item._fields["id"],
        creator_id=creator_id,
        user_message="make the title bigger",
    )
    assert result.plan.mode == "respond"
    assert result.plan.turn_value == "recovery"
    assert "new version" in result.plan.response
    assert not result.plan.intents
    assert runs == []  # never reached the strategy / render path


async def test_replan_cue_still_replans(monkeypatch: pytest.MonkeyPatch) -> None:
    db, item, creator_id, runs = _wire(monkeypatch, miss="no_ready_variant")
    result = await planner.plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item._fields["id"],
        creator_id=creator_id,
        user_message="give me a completely different vibe",
    )
    assert result.plan.turn_value == "recovery"
    assert result.plan.turn_value == "recovery"


# --- KRI-237: variant render_status drives the miss reason -----------------------------


async def test_render_in_flight_is_a_distinct_miss() -> None:
    job_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=job_id)
    thread = SimpleNamespace(active_creator_agent_session_id=uuid.uuid4())
    session = SimpleNamespace(id=uuid.uuid4(), target_job_id=job_id, target_variant_id="v1")
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={"variants": [{"variant_id": "v1", "render_status": "rendering"}]},
    )
    target, logs = await _load(_db(thread, session, job), item)
    assert target is None
    assert [e["reason"] for e in logs] == ["render_in_flight"]
    assert logs[0]["render_status"] == "rendering"


# Pre-routing requirement extractions made by the last `_wire_real` run.
EXTRACTOR_RUNS: list[int] = []


def _wire_real(  # noqa: ANN202
    monkeypatch,  # noqa: ANN001
    *,
    render_status,  # noqa: ANN001
    copilot=None,  # noqa: ANN001
    job_status=None,  # noqa: ANN001
    variant_extra=None,  # noqa: ANN001
    binding=False,  # noqa: ANN001
    extracted_updates=None,  # noqa: ANN001
):
    """Real `_load_editor_target` / `_plan_editor_revision`; only I/O edges are stubbed.

    `render_status=None` = the job carries no variant at all (a first render that
    failed before producing one); `job_status` is the `Job.status` it ended in.
    """
    item = _ExpiringItem(
        id=uuid.uuid4(),
        content_plan_id=uuid.uuid4(),
        current_job_id=uuid.uuid4(),
        edit_format="montage",
    )
    db, creator_id = _planner_db(item)
    job_id = item._fields["current_job_id"]
    thread = SimpleNamespace(active_creator_agent_session_id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        plan_item_id=item._fields["id"],
        target_job_id=job_id,
        target_variant_id="v1",
        target_generation_id=None,
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=creator_id,
        status=job_status,
        assembly_plan={
            "variants": (
                []
                if render_status is None
                else [{"variant_id": "v1", "render_status": render_status, **(variant_extra or {})}]
            )
        },
    )
    inner_get = db.get

    async def get(model, identifier, **kw):  # noqa: ANN001, ANN202
        if model in (CreationThread, CreatorAgentSession, Job):
            return {CreationThread: thread, CreatorAgentSession: session, Job: job}[model]
        return await inner_get(model, identifier, **kw)

    db.get = AsyncMock(side_effect=get)
    inner_execute = db.execute

    async def execute(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        res = await inner_execute(*a, **kw)
        if not hasattr(res, "scalar_one_or_none"):
            res.scalar_one_or_none = lambda: None
        if not hasattr(res, "all"):
            res.all = lambda: []
        return res

    db.execute = AsyncMock(side_effect=execute)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(settings, "kria_copilot_first_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", binding)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    manifest = _MANIFEST.model_copy(update={"item_id": str(item._fields["id"])})
    monkeypatch.setattr(
        planner, "resolve_item_creator_context", AsyncMock(return_value=(manifest, []))
    )
    monkeypatch.setattr(planner, "creator_context", lambda *_a: ("creator", "item"))
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=None))
    monkeypatch.setattr(planner, "_copilot_clip_context", AsyncMock(return_value=None))
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        planner,
        "build_editor_snapshot",
        lambda *_a, **_k: {"allowed_op_families": ["text"], "text_bars": []},
    )
    copilot = copilot or AsyncMock()
    monkeypatch.setattr(planner, "run_copilot_turn", copilot)
    creator_runs = []

    class FakeAgent:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, agent_input, **_kwargs):  # noqa: ANN001, ANN003, ANN201
            creator_runs.append(agent_input)
            from app.agents._schemas.creator_agent import AskUser

            return SimpleNamespace(
                action=AskUser(**_ASK),
                brief_updates=[_upd("select", "global")],
                creative_decision=None,
            )

    monkeypatch.setattr(planner, "MainCreatorAgent", FakeAgent)

    EXTRACTOR_RUNS.clear()

    class FakeBriefExtractor:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            EXTRACTOR_RUNS.append(1)
            return SimpleNamespace(
                brief_updates=extracted_updates
                if extracted_updates is not None
                else [_upd("select", "global")]
            )

    monkeypatch.setattr(planner, "BriefExtractorAgent", FakeBriefExtractor)
    monkeypatch.setattr(planner, "default_client", lambda: object())
    return db, item, creator_id, creator_runs, copilot


async def _ask(db, item, creator_id, message):  # noqa: ANN001, ANN202
    return await planner.plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item._fields["id"],
        creator_id=creator_id,
        user_message=message,
    )


async def test_ask_while_variant_rendering_gets_in_flight_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, item, creator_id, runs, copilot = _wire_real(monkeypatch, render_status="rendering")
    result = await _ask(db, item, creator_id, "Add animation to the title")
    assert result.plan.mode == "respond"
    assert result.plan.turn_value == "recovery"
    assert "still rendering" in result.plan.response
    assert not result.plan.intents
    copilot.assert_not_called()
    assert runs == []


async def test_same_ask_once_ready_runs_copilot_and_stages_editor_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ops = [
        {
            "op": "patch_text",
            "bar_id": "guided-title",
            "patch": {"animation_phases": {"in": "fade_up"}},
        }
    ]
    copilot = AsyncMock(
        return_value=SimpleNamespace(
            ops=ops, outcome="proposed", reply="Added.", intent="edit", rejection_reasons=[]
        )
    )
    db, item, creator_id, runs, copilot = _wire_real(
        monkeypatch, render_status="ready", copilot=copilot, extracted_updates=[]
    )
    result = await _ask(db, item, creator_id, "Add animation to the title")
    copilot.assert_awaited()
    assert [i.tool_name for i in result.plan.intents] == ["draft.apply_editor_ops"]
    staged = result.plan.intents[0].arguments["operations"]
    assert staged[0]["op"] == "patch_text"
    assert "animation_phases" in str(staged[0])
    assert "guided-title" in str(staged[0])
    assert result.plan.turn_value != "recovery"


async def test_no_active_session_does_not_trigger_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, item, creator_id, runs, copilot = _wire_real(monkeypatch, render_status="ready")
    inner = db.get.side_effect

    async def get(model, identifier, **kw):  # noqa: ANN001, ANN202
        if model is CreationThread:
            return SimpleNamespace(active_creator_agent_session_id=None)
        return await inner(model, identifier, **kw)

    db.get = AsyncMock(side_effect=get)
    result = await _ask(db, item, creator_id, "Add animation to the title")
    assert result.plan.turn_value == "recovery"
    copilot.assert_not_called()


async def test_replan_cue_still_replans_while_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, item, creator_id, runs, copilot = _wire_real(monkeypatch, render_status="rendering")
    result = await _ask(db, item, creator_id, "give me a completely different vibe")
    assert result.plan.turn_value == "recovery"


async def test_router_path_recovers_when_target_missing_without_copilot_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, item, creator_id, runs, copilot = _wire_real(monkeypatch, render_status="rendering")
    result = await planner.plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item._fields["id"],
        creator_id=creator_id,
        user_message="Add animation to the title",
        allow_fast_path=False,
    )
    assert result.plan.turn_value == "recovery"
    assert "still rendering" in result.plan.response
    copilot.assert_not_called()


# --- a FAILED first render is not a render: follow-ups re-plan, never dead-end ----------


@pytest.mark.parametrize("binding", [True, False])
@pytest.mark.parametrize("failed_status", ["processing_failed", "variants_failed"])
async def test_follow_up_after_a_failed_first_render_replans(
    monkeypatch: pytest.MonkeyPatch, binding: bool, failed_status: str
) -> None:
    """Incident shape: the item's only job failed with no variant; the creator then
    types the missing words. That must reach the planner, not the 'couldn't open your
    current edit' dead end, and the brief must be extracted exactly once."""
    db, item, creator_id, runs, copilot = _wire_real(
        monkeypatch,
        render_status=None,
        job_status=failed_status,
        binding=binding,
    )
    result = await _ask(db, item, creator_id, "The hook should say 'Weekend away'")
    assert result.plan.response != planner._EDITOR_TARGET_RECOVERY_REPLY
    assert "couldn't open your current edit" not in (result.plan.response or "")
    assert len(runs) == 1  # the Main Creator planned it
    assert EXTRACTOR_RUNS == []  # no second, pre-routing extraction of the same message
    assert len(result.brief_updates) == 1  # the brief update is applied once
    copilot.assert_not_called()


async def test_failed_first_render_miss_is_not_guarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, item, creator_id, runs, copilot = _wire_real(
        monkeypatch, render_status=None, job_status="processing_failed"
    )
    with capture_logs() as logs:
        target = await planner._load_editor_target(db, thread_id=uuid.uuid4(), item=item)
    assert target is None
    assert planner._editor_target_miss.get() == "no_render"
    assert not planner._editor_target_miss_guarded()
    assert [e["reason"] for e in logs if e["event"] == "kria_editor_target_unavailable"] == [
        "no_render"
    ]


@pytest.mark.parametrize("binding", [True, False])
async def test_rendering_variant_still_gets_the_in_flight_reply(
    monkeypatch: pytest.MonkeyPatch, binding: bool
) -> None:
    db, item, creator_id, runs, copilot = _wire_real(
        monkeypatch, render_status="rendering", job_status="processing", binding=binding
    )
    result = await _ask(db, item, creator_id, "The hook should say 'Weekend away'")
    assert result.plan.turn_value == "recovery"
    assert result.plan.response == planner._EDITOR_TARGET_IN_FLIGHT_REPLY
    assert runs == []


async def test_failed_job_with_a_ready_variant_is_not_a_first_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-render that failed after a ready variant exists keeps the guarded path."""
    db, item, creator_id, runs, copilot = _wire_real(
        monkeypatch, render_status="failed", job_status="processing_failed"
    )
    job = (await db.get(Job, item._fields["current_job_id"])).assembly_plan
    job["variants"].append({"variant_id": "v0", "render_status": "ready"})
    # The session still points at the failed variant: nothing editable is ready for it.
    await planner._load_editor_target(db, thread_id=uuid.uuid4(), item=item)
    assert planner._editor_target_miss.get() == "no_ready_variant"
    assert planner._editor_target_miss_guarded()


async def test_ready_variant_with_stale_editor_state_still_gets_the_stale_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.kria.test_editor_state_turns import _state

    db, item, creator_id, runs, copilot = _wire_real(
        monkeypatch,
        render_status="ready",
        job_status="variants_ready",
        variant_extra={"render_generation_id": "G2"},
        binding=True,
    )
    result = await planner.plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item._fields["id"],
        creator_id=creator_id,
        user_message="The hook should say 'Weekend away'",
        editor_state=_state({}, base="G1"),
    )
    assert result.plan.turn_value == "recovery"
    assert result.plan.response == planner.EDITOR_STATE_STALE_REPLY
    assert runs == []


# --- conversation cap: MainCreatorInput.conversation is max 20 --------------------------


def _event_rows(n):  # noqa: ANN001, ANN202
    # Newest first, as the `ORDER BY sequence DESC LIMIT` query returns them.
    return [
        SimpleNamespace(role="user" if i % 2 == 0 else "assistant", content=f"m{i}", sequence=i)
        for i in reversed(range(n))
    ]


def _inputs_db(rows):  # noqa: ANN001, ANN202
    captured = {}

    async def execute(stmt):  # noqa: ANN001, ANN202
        if stmt._limit is not None:
            captured["limit"] = stmt._limit
        event_rows = [
            (
                row.role,
                getattr(row, "payload", None),
                getattr(row, "event_type", None),
                getattr(row, "content", None),
            )
            for row in rows
        ]
        return SimpleNamespace(
            all=lambda: event_rows,
            scalars=lambda: SimpleNamespace(all=lambda: rows[: stmt._limit]),
        )

    return (
        SimpleNamespace(execute=AsyncMock(side_effect=execute), rollback=AsyncMock()),
        captured,
    )


async def test_long_thread_conversation_is_most_recent_cap_rows_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(planner, "creator_context", lambda *_a: ("creator", "item"))
    db, captured = _inputs_db(_event_rows(60))
    item = SimpleNamespace(id=uuid.uuid4(), edit_format="montage")
    inputs = await planner._load_creator_inputs(
        db,
        thread_id=uuid.uuid4(),
        item=item,
        persona=SimpleNamespace(),
        creator_id=uuid.uuid4(),
        user_message="make them shorter",
        manifest=_MANIFEST,
        media_context=[],
        prior_brief=None,
        brief_on=False,
    )
    convo = inputs.agent_input.conversation
    assert captured["limit"] == MAIN_CREATOR_CONVERSATION_MAX
    assert [t["content"] for t in convo] == [
        f"m{i}" for i in range(60 - MAIN_CREATOR_CONVERSATION_MAX, 60)
    ]


async def test_every_conversation_builder_respects_the_input_cap() -> None:
    import inspect

    from app.routes import creator_agent

    assert MAIN_CREATOR_CONVERSATION_MAX <= (
        MainCreatorInput.model_fields["conversation"].metadata[0].max_length
    )
    assert planner.MAIN_CREATOR_CONVERSATION_MAX is MAIN_CREATOR_CONVERSATION_MAX
    assert "MAIN_CREATOR_CONVERSATION_MAX" in inspect.getsource(planner._load_creator_inputs)
    assert "_ROUTE_CONVERSATION_WINDOW" in inspect.getsource(creator_agent._conversation)
    assert creator_agent._ROUTE_CONVERSATION_WINDOW <= MAIN_CREATOR_CONVERSATION_MAX
    # The route builder caps even with a carried-brief header prepended.
    events = [
        SimpleNamespace(role="user", sequence=i, payload={"message": f"m{i}"}) for i in range(80)
    ]
    assert len(creator_agent._conversation(events)) <= MAIN_CREATOR_CONVERSATION_MAX


async def test_extraction_validation_error_does_not_stage_partial_copilot_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    db, item, creator_id, _runs = _wire(monkeypatch, miss="no_ready_variant")
    monkeypatch.setattr(settings, "kria_copilot_first_enabled", False)
    try:
        MainCreatorInput(user_message="", capability_manifest=_MANIFEST)
    except ValidationError as exc:
        err = exc
    monkeypatch.setattr(planner, "_load_creator_inputs", AsyncMock(side_effect=err))
    fallback = SimpleNamespace(mode="respond", turn_value="recovery", response="ok", intents=())
    monkeypatch.setattr(planner, "_plan_editor_revision", AsyncMock(return_value=fallback))
    result = await planner.plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item._fields["id"],
        creator_id=creator_id,
        user_message="make the title bigger",
    )
    assert result.plan.mode == "respond" and not result.plan.intents
    assert result.brief_coverage["reason"] == "request_extraction_failed"
    planner._plan_editor_revision.assert_not_awaited()


# KRI-282: a clip-picker answer ("Dodgeball: clip 21" + structured clip_selection) used to
# be read by the editor copilot as a text edit ("put 'dodgeball' on bar 18"), so the label
# landed on the wrong clip and the selection was never folded into the clip intents.
@pytest.mark.parametrize("brief_on", [True, False])
async def test_clip_selection_answer_never_reaches_the_copilot(
    monkeypatch: pytest.MonkeyPatch, brief_on: bool
) -> None:
    ops = [{"op": "edit_text", "text": "dodgeball", "bar_index": 18}]
    copilot = AsyncMock(
        return_value=SimpleNamespace(
            ops=ops, outcome="proposed", reply="Done.", intent="edit", rejection_reasons=[]
        )
    )
    db, item, creator_id, runs, copilot = _wire_real(
        monkeypatch, render_status="ready", copilot=copilot
    )
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", brief_on)
    result = await planner.plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item._fields["id"],
        creator_id=creator_id,
        user_message="Dodgeball: clip 21",
        answers_clip_question=True,
    )
    copilot.assert_not_called()
    assert len(runs) == 1  # the Main Creator re-planned instead
    assert not any(
        getattr(i, "tool_name", "") == "draft.apply_editor_ops" for i in result.plan.intents
    )


async def test_same_short_message_without_selection_still_takes_the_copilot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copilot = AsyncMock(
        return_value=SimpleNamespace(
            ops=[{"op": "edit_text", "text": "dodgeball", "bar_index": 3}],
            outcome="proposed",
            reply="Done.",
            intent="edit",
            rejection_reasons=[],
        )
    )
    db, item, creator_id, _runs, copilot = _wire_real(
        monkeypatch, render_status="ready", copilot=copilot, extracted_updates=[]
    )
    await _ask(db, item, creator_id, "Dodgeball: clip 21")
    copilot.assert_awaited()
