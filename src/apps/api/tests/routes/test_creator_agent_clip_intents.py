"""KRI-127 Lane D: chat-agent authoring, resolution, and brief forwarding for
open-vocabulary `clip_intents`.

Flag-off byte-identity for the model/prompt boundary is covered by
`tests/agents/test_main_creator_agent.py`
(`test_main_creator_prompt_flag_off_is_byte_identical_to_pre_kri127`). This
file covers the route-level turn: model-output hygiene, the sport-regex gate,
resolver invocation (without holding the session row lock across it),
needs-creator vs. resolved outcomes, vision-answer persistence, the legacy
mapping, and brief forwarding on confirm.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._schemas.creator_agent import (
    ContextLabelIntent,
    CreativeStrategy,
    ProposeStrategy,
    legacy_clip_intents,
)
from app.routes import creator_agent as creator_routes
from app.routes.creator_agent import _apply_explicit_render_intent, _seed_guided_specialist_brief
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services.clip_intent_resolution import ANSWERS_KEY, IntentClip, IntentResolution
from app.services.creator_capabilities import compile_strategy_to_plan, resolve_creator_manifest


def _manifest():
    return resolve_creator_manifest(
        item_id="item-dishes",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "clip-2", "kind": "video"},
        ],
        guided_capability_enabled=True,
    )


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=[],
        agent_call_count=0,
        agent_call_budget=2,
        question_count=0,
        question_budget=2,
        active_plan=None,
        last_error=None,
        manifest_hash=None,
    )


def _wire_common_turn_mocks(
    monkeypatch,
    *,
    item: SimpleNamespace,
    persona: SimpleNamespace,
    session: SimpleNamespace,
    manifest,
    action: ProposeStrategy,
    response: SimpleNamespace,
    append_event: AsyncMock,
) -> None:
    monkeypatch.setattr(
        creator_routes, "_owned_context", AsyncMock(return_value=(item, SimpleNamespace(), persona))
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes, "resolve_item_creator_context", AsyncMock(return_value=(manifest, []))
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes, "default_client", lambda: SimpleNamespace())
    monkeypatch.setattr(
        creator_routes.asyncio, "to_thread", AsyncMock(return_value=SimpleNamespace(action=action))
    )
    monkeypatch.setattr(creator_routes, "append_event", append_event)
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))


async def _run_turn(item: SimpleNamespace, session: SimpleNamespace, *, user_message: str):
    return await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=SimpleNamespace(id=uuid.uuid4()),
        session_id=session.id,
        expected_revision=1,
        user_message=user_message,
    )


# ---------------------------------------------------------------------------
# Flag off: byte-identical behavior, resolver never touched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_discards_model_authored_clip_intents_and_never_resolves(
    monkeypatch,
) -> None:
    monkeypatch.setattr(creator_routes.settings, "clip_intents_enabled", False)
    manifest = _manifest()
    item = SimpleNamespace(id=uuid.uuid4())
    persona = SimpleNamespace(user_id=uuid.uuid4())
    session = _session()
    response = SimpleNamespace(status="awaiting_confirmation")
    append_event = AsyncMock()
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="fast_montage",
            edit_format="montage",
            render_program="native",
            selected_media_ids=["clip-1", "clip-2"],
            target_duration_s=20,
            rationale="Name the dish on each clip.",
            # A model that ignores the (absent) instructions and emits the
            # generic shape anyway, or a stale resolved value from a prior
            # turn, must never survive when the flag is off.
            clip_intents=[
                ClipIntent(intent_id="dish", op="label", attribute="the dish shown in the clip")
            ],
            resolved_clip_intents=[
                ResolvedClipIntent(
                    intent_id="dish", op="label", attribute="the dish", status="resolved"
                )
            ],
        ),
        summary="A dish-labeled montage.",
    )
    _wire_common_turn_mocks(
        monkeypatch,
        item=item,
        persona=persona,
        session=session,
        manifest=manifest,
        action=action,
        response=response,
        append_event=append_event,
    )
    load_clips = AsyncMock()
    resolve = AsyncMock()
    monkeypatch.setattr(creator_routes, "load_intent_clips_for_item", load_clips)
    monkeypatch.setattr(creator_routes, "resolve_clip_intents_for_turn", resolve)

    result = await _run_turn(item, session, user_message="Name the dish on each clip")

    assert result is response
    load_clips.assert_not_called()
    resolve.assert_not_called()
    strategy = session.active_plan["edit_plan"]["strategy"]
    assert "clip_intents" not in strategy
    assert "resolved_clip_intents" not in strategy


# ---------------------------------------------------------------------------
# Flag on: resolved path reaches the strategy; sport regex yields to it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_on_resolved_intents_reach_strategy_and_block_sport_regex(monkeypatch) -> None:
    monkeypatch.setattr(creator_routes.settings, "clip_intents_enabled", True)
    manifest = _manifest()
    item = SimpleNamespace(id=uuid.uuid4())
    persona = SimpleNamespace(user_id=uuid.uuid4())
    session = _session()
    response = SimpleNamespace(status="awaiting_confirmation")
    append_event = AsyncMock()
    # Wording that WOULD trip the legacy sport regex, paired with a generic
    # intent the model authored instead -- the regex must not also force
    # `context_label`/`sport_labels` once the generic path owns this.
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="fast_montage",
            edit_format="montage",
            render_program="native",
            selected_media_ids=["clip-1", "clip-2"],
            target_duration_s=20,
            rationale="Label the sport per clip using the generic intent.",
            clip_intents=[
                ClipIntent(
                    intent_id="sport-label",
                    op="label",
                    attribute="the sport being played in the clip",
                )
            ],
            # Model-authored resolved_clip_intents must be discarded and
            # replaced by whatever the resolver actually decides.
            resolved_clip_intents=[
                ResolvedClipIntent(
                    intent_id="sport-label",
                    op="label",
                    attribute="stale model-authored value",
                    status="resolved",
                )
            ],
        ),
        summary="A sports montage with a per-clip label.",
    )
    _wire_common_turn_mocks(
        monkeypatch,
        item=item,
        persona=persona,
        session=session,
        manifest=manifest,
        action=action,
        response=response,
        append_event=append_event,
    )
    intent_clips = [
        IntentClip(media_id="clip-1", kind="video", analysis={}, gcs_path="clips/1.mp4"),
        IntentClip(media_id="clip-2", kind="video", analysis={}, gcs_path="clips/2.mp4"),
    ]
    resolved = [
        ResolvedClipIntent(
            intent_id="sport-label",
            op="label",
            attribute="the sport being played in the clip",
            status="resolved",
            assignments=[
                ClipAssignment(
                    media_id="clip-1",
                    value="Volleyball",
                    evidence="beach volleyball match",
                    confidence=0.9,
                    grounding="record_span",
                )
            ],
        )
    ]
    load_clips = AsyncMock(return_value=intent_clips)
    resolve = AsyncMock(return_value=IntentResolution(intents=resolved, vision_answers={}))
    monkeypatch.setattr(creator_routes, "load_intent_clips_for_item", load_clips)
    monkeypatch.setattr(creator_routes, "resolve_clip_intents_for_turn", resolve)

    result = await _run_turn(
        item,
        session,
        user_message="Label the sport being played in each clip, bottom right",
    )

    assert result is response
    load_clips.assert_awaited_once()
    resolve.assert_awaited_once()
    kwargs = resolve.await_args.kwargs
    assert kwargs["clips"] == intent_clips
    assert [intent.intent_id for intent in kwargs["intents"]] == ["sport-label"]

    strategy = session.active_plan["edit_plan"]["strategy"]
    assert strategy["resolved_clip_intents"][0]["assignments"][0]["value"] == "Volleyball"
    # The sport regex must not also force the legacy coded fields once the
    # generic intent owns this request.
    assert strategy.get("sport_labels") in (None, False)
    assert strategy.get("context_label") is None


@pytest.mark.asyncio
async def test_flag_on_needs_creator_asks_and_never_proposes_strategy(monkeypatch) -> None:
    monkeypatch.setattr(creator_routes.settings, "clip_intents_enabled", True)
    manifest = _manifest()
    item = SimpleNamespace(id=uuid.uuid4())
    persona = SimpleNamespace(user_id=uuid.uuid4())
    session = _session()
    response = SimpleNamespace(status="briefing")
    append_event = AsyncMock()
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="fast_montage",
            edit_format="montage",
            render_program="native",
            selected_media_ids=["clip-1", "clip-2"],
            target_duration_s=20,
            rationale="Group these clips by the requested attribute.",
            clip_intents=[
                ClipIntent(intent_id="grp", op="group", attribute="which city each clip is from")
            ],
        ),
        summary="Grouped by city.",
    )
    _wire_common_turn_mocks(
        monkeypatch,
        item=item,
        persona=persona,
        session=session,
        manifest=manifest,
        action=action,
        response=response,
        append_event=append_event,
    )
    monkeypatch.setattr(
        creator_routes,
        "load_intent_clips_for_item",
        AsyncMock(return_value=[IntentClip(media_id="clip-1", kind="video", analysis={})]),
    )
    resolve = AsyncMock(
        return_value=IntentResolution(question="Which city is each clip from?", vision_answers={})
    )
    monkeypatch.setattr(creator_routes, "resolve_clip_intents_for_turn", resolve)

    result = await _run_turn(item, session, user_message="Group these clips by city")

    assert result is response
    resolve.assert_awaited_once()
    assert session.active_plan is None
    assert session.status == "briefing"
    assert session.last_error is None
    append_event.assert_awaited_once()
    call = append_event.await_args
    assert call.kwargs["event_type"] == "assistant_question"
    assert call.kwargs["payload"]["message"] == "Which city is each clip from?"
    assert call.kwargs["payload"]["reason_code"] == "clip_intent_unresolved"


@pytest.mark.asyncio
async def test_resolver_exception_is_a_technical_failure_never_a_genuine_no_match(
    monkeypatch,
) -> None:
    monkeypatch.setattr(creator_routes.settings, "clip_intents_enabled", True)
    manifest = _manifest()
    item = SimpleNamespace(id=uuid.uuid4())
    persona = SimpleNamespace(user_id=uuid.uuid4())
    session = _session()
    response = SimpleNamespace(status="briefing")
    append_event = AsyncMock()
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="fast_montage",
            edit_format="montage",
            render_program="native",
            selected_media_ids=["clip-1", "clip-2"],
            target_duration_s=20,
            rationale="Only include clips with my dog in them.",
            clip_intents=[
                ClipIntent(intent_id="dog", op="include", attribute="clips with my dog in them")
            ],
        ),
        summary="Only the dog clips.",
    )
    _wire_common_turn_mocks(
        monkeypatch,
        item=item,
        persona=persona,
        session=session,
        manifest=manifest,
        action=action,
        response=response,
        append_event=append_event,
    )
    monkeypatch.setattr(
        creator_routes,
        "load_intent_clips_for_item",
        AsyncMock(return_value=[IntentClip(media_id="clip-1", kind="video", analysis={})]),
    )
    monkeypatch.setattr(
        creator_routes,
        "resolve_clip_intents_for_turn",
        AsyncMock(side_effect=RuntimeError("vision backend unavailable")),
    )

    result = await _run_turn(item, session, user_message="Only use the clips with my dog in them")

    assert result is response
    assert session.active_plan is None
    assert session.status == "briefing"
    assert session.last_error == {"code": "provider_unavailable"}
    call = append_event.await_args
    assert call.kwargs["event_type"] == "assistant_error"
    assert call.kwargs["payload"] == {
        "message": (
            "Clip analysis is unavailable right now. Your request is saved; try again later."
        ),
        "code": "provider_unavailable",
    }


# ---------------------------------------------------------------------------
# Sport-regex gate (unit-level, no route harness needed).
# ---------------------------------------------------------------------------


def test_sport_regex_still_forces_context_label_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(creator_routes.settings, "clip_intents_enabled", False)
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(render_program="native"),
        "Add a small text of the name of the sport being played on the bottom right",
    )
    assert strategy.sport_labels is True
    assert strategy.context_label is not None
    assert strategy.context_label.kind == "sport"


def test_sport_regex_yields_to_generic_clip_intents_when_flag_on(monkeypatch) -> None:
    monkeypatch.setattr(creator_routes.settings, "clip_intents_enabled", True)
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(
            render_program="native",
            clip_intents=[
                ClipIntent(
                    intent_id="sport",
                    op="label",
                    attribute="the sport being played in the clip",
                )
            ],
        ),
        "Add a small text of the name of the sport being played on the bottom right",
    )
    assert strategy.sport_labels is False
    assert strategy.context_label is None


def test_sport_regex_still_applies_when_flag_on_but_no_clip_intents(monkeypatch) -> None:
    # The flag alone does not suppress the regex -- only an actual
    # model-authored `clip_intents` list does, so an old/failed model
    # response that never adopts the new field keeps today's behavior.
    monkeypatch.setattr(creator_routes.settings, "clip_intents_enabled", True)
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(render_program="native"),
        "Add a small text of the name of the sport being played on the bottom right",
    )
    assert strategy.sport_labels is True
    assert strategy.context_label is not None


# ---------------------------------------------------------------------------
# Legacy -> generic mapping (schema-level).
# ---------------------------------------------------------------------------


def test_legacy_clip_intents_maps_sport_labels_flag() -> None:
    strategy = CreativeStrategy(render_program="native", sport_labels=True)
    mapped = legacy_clip_intents(strategy)
    assert len(mapped) == 1
    assert mapped[0].intent_id == "legacy-sport"
    assert mapped[0].op == "label"
    assert mapped[0].attribute == "the sport being played in the clip"


def test_legacy_clip_intents_maps_sport_context_label() -> None:
    strategy = CreativeStrategy(
        render_program="native",
        context_label=ContextLabelIntent(kind="sport"),
    )
    mapped = legacy_clip_intents(strategy)
    assert len(mapped) == 1
    assert mapped[0].op == "label"


def test_legacy_clip_intents_empty_when_generic_intents_already_present() -> None:
    strategy = CreativeStrategy(
        render_program="native",
        sport_labels=True,
        clip_intents=[ClipIntent(intent_id="x", op="label", attribute="the sport")],
    )
    assert legacy_clip_intents(strategy) == []


def test_legacy_clip_intents_empty_when_neither_field_set() -> None:
    strategy = CreativeStrategy(render_program="native")
    assert legacy_clip_intents(strategy) == []


# ---------------------------------------------------------------------------
# Vision-answer persistence (best-effort cache write).
# ---------------------------------------------------------------------------


class _Savepoint:
    """Stand-in for ``AsyncSession.begin_nested()``: an async context manager."""

    def __init__(self) -> None:
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.rolled_back = exc_type is not None
        return False


def _fake_db(**get_kwargs) -> AsyncMock:
    db = AsyncMock()
    db.get = AsyncMock(**get_kwargs)
    db.savepoint = _Savepoint()
    db.begin_nested = MagicMock(return_value=db.savepoint)
    return db


@pytest.mark.asyncio
async def test_vision_answers_persist_onto_pool_asset_analysis() -> None:
    item_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    asset = SimpleNamespace(plan_item_id=item_id, analysis={"summary": "a food clip"})
    item = SimpleNamespace(id=item_id)
    db = _fake_db(return_value=asset)

    await creator_routes._persist_clip_intent_vision_answers(
        db,
        item,
        {f"asset-{asset_id}": {"is the dish visible": {"answer": "yes", "confidence": 0.9}}},
    )

    assert asset.analysis["summary"] == "a food clip"
    assert asset.analysis[ANSWERS_KEY] == {
        "is the dish visible": {"answer": "yes", "confidence": 0.9}
    }


@pytest.mark.asyncio
async def test_vision_answers_merge_without_clobbering_existing_cache() -> None:
    item_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    asset = SimpleNamespace(
        plan_item_id=item_id, analysis={ANSWERS_KEY: {"old question": {"answer": "no"}}}
    )
    item = SimpleNamespace(id=item_id)
    db = _fake_db(return_value=asset)

    await creator_routes._persist_clip_intent_vision_answers(
        db, item, {f"asset-{asset_id}": {"new question": {"answer": "yes"}}}
    )

    assert asset.analysis[ANSWERS_KEY] == {
        "old question": {"answer": "no"},
        "new question": {"answer": "yes"},
    }


@pytest.mark.asyncio
async def test_vision_answers_skip_raw_clip_assignments_media() -> None:
    """Raw `clip_assignments` clips have no light-weight writer here (see the
    docstring on `_persist_clip_intent_vision_answers`); persistence for them
    is a documented no-op, never an error."""

    item = SimpleNamespace(id=uuid.uuid4())
    db = _fake_db(side_effect=AssertionError("must not be looked up"))

    await creator_routes._persist_clip_intent_vision_answers(
        db, item, {"clip-1": {"question": {"answer": "yes"}}}
    )  # must not raise


@pytest.mark.asyncio
async def test_vision_answers_persist_failure_never_raises() -> None:
    item = SimpleNamespace(id=uuid.uuid4())
    db = _fake_db(side_effect=RuntimeError("db is down"))

    await creator_routes._persist_clip_intent_vision_answers(
        db, item, {f"asset-{uuid.uuid4()}": {"q": {"answer": "yes"}}}
    )  # must not raise

    # The failure is confined to a SAVEPOINT: swallowing a flush error without one
    # leaves the turn's transaction "pending rollback" and the final commit 500s.
    db.begin_nested.assert_called_once()
    assert db.savepoint.rolled_back is True
    db.rollback.assert_not_called()


# ---------------------------------------------------------------------------
# Brief forwarding on confirm.
# ---------------------------------------------------------------------------


def test_seed_guided_specialist_brief_forwards_resolved_clip_intents() -> None:
    manifest = _manifest()
    resolved = [
        ResolvedClipIntent(
            intent_id="dish",
            op="label",
            attribute="the dish shown in the clip",
            status="resolved",
            assignments=[
                ClipAssignment(
                    media_id="clip-1",
                    value="Pasta",
                    evidence="a bowl of pasta",
                    confidence=0.85,
                    grounding="record_span",
                )
            ],
        )
    ]
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            render_program="guided",
            selected_media_ids=["clip-1"],
            resolved_clip_intents=resolved,
        ),
    )
    item = SimpleNamespace(edit_proposal=None)

    _seed_guided_specialist_brief(item, edit_plan, summary="A dish-labeled recap.")

    brief = item.edit_proposal["brief"]
    assert brief["clip_intents"][0]["intent_id"] == "dish"
    assert brief["clip_intents"][0]["assignments"][0]["value"] == "Pasta"


def test_seed_guided_specialist_brief_omits_clip_intents_when_unresolved() -> None:
    manifest = _manifest()
    edit_plan = compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            render_program="guided",
            selected_media_ids=["clip-1"],
        ),
    )
    item = SimpleNamespace(edit_proposal=None)

    _seed_guided_specialist_brief(item, edit_plan, summary="A plain recap.")

    assert "clip_intents" not in item.edit_proposal["brief"]


# ---------------------------------------------------------------------------
# Model-output hygiene sanity: resolved_clip_intents is always server-owned,
# even for an all-media-capacity choice or a TerminalError fallback (neither
# path routes through the model's `action` object, so both are already clean
# by construction -- pinned here so a future refactor cannot regress it).
# ---------------------------------------------------------------------------


def test_fallback_strategy_never_carries_clip_intents() -> None:
    from app.routes.creator_agent import _fallback_strategy

    strategy = _fallback_strategy(_manifest(), user_message="Make something great")
    assert strategy.clip_intents is None
    assert strategy.resolved_clip_intents is None


def test_specialist_brief_intents_use_planner_media_ids():
    """Chat names pool assets ``asset-{uuid}``; planner + render use the bare uuid."""
    from app.routes.creator_agent import _specialist_clip_intents
    from app.schemas.clip_intents import ResolvedClipIntent

    intent = ResolvedClipIntent(
        intent_id="i1",
        op="label",
        attribute="sport",
        assignments=[
            {"media_id": "asset-1b4e28ba-2fa1-11d2-883f-0016d3cca427", "value": "Soccer"},
            {"media_id": "analysis-proxy-ios-raw-clip.mp4", "value": "Volleyball"},
        ],
    )

    [translated] = _specialist_clip_intents([intent])

    assert translated.media_ids() == [
        "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "analysis-proxy-ios-raw-clip.mp4",
    ]
    assert intent.media_ids()[0].startswith("asset-")  # the stored strategy is not mutated
    assert _specialist_clip_intents(None) is None
    assert _specialist_clip_intents([]) is None
