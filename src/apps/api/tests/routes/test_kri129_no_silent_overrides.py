"""KRI-129: the creator's written prompt wins; nothing may silently replace it.

Incident: prod job 1186da65 (16 clips, guided_story, 60s). The creator wrote,
across turns, "Continue with 16 clips ... Keep 60 seconds and include
everything with faster pacing ... Do it again using all the videos ...".
``_explicit_media_scope`` correctly resolved "all". One of the 16 clips was
1.27s, under ``GUIDED_STORY_MIN_MOMENT_S`` (1.4s), so
``assess_all_media_capacity`` reported the full set infeasible and
``_all_media_capacity_question`` wanted to ask a clarifying question. The
session's question budget was already spent, so the route silently applied
the "strongest clips" mapping -- discarding both the explicit "all" and the
creator's own earlier answer to this exact question ("keep everything with
faster pacing"). Nothing told the creator anything was left out.

A short clip must NEVER be left out of an edit for being short: it plays at
its own length. ``assess_all_media_capacity`` (edit_direction_planner.py)
now counts a short clip at its own length via ``guided_source_floor_s``, so
the prod scenario above is simply feasible as "all" -- no question, no
resolution, no exclusion. This file no longer has a duration-based exclusion
path; a clip is dropped from a resolved plan only by the creator's own
explicit words, never by the system's editorial judgement about clip length.

These tests cover:
  1. a short clip is never excluded from the strength-ranked recommendation
     for being short (only unknown-kind media, e.g. "audio", is excluded);
     the exact prod-incident shape resolves to "all" with every clip
     (including the short one) and no question, since it's genuinely
     feasible now;
  2. when the question budget is spent and the creator already answered this
     exact question earlier in the session, that answer's KIND (never its
     stale values) is reapplied against the mapping computed fresh for the
     current turn; a newer, explicit narrowing instruction overrides it;
  3. when the budget is spent with no prior answer, the mapping that keeps
     every clip (faster pacing) is preferred over one that drops clips;
  4. an explicit ``media_scope="selected"`` with real ids survives
     ``normalize_creator_strategy_media`` for a guided program and reaches
     the seeded specialist brief; unknown ids are dropped only under repair;
  5. ``MainCreatorAgent.parse()`` keeps a model-authored scope when the
     regex finds nothing, and the regex still wins when it fires;
  6. a creator who never mentions scope at all gets byte-identical legacy
     behavior.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.agents._schemas.creator_policy import normalize_creator_strategy_media
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.routes import creator_agent as creator_routes
from app.routes.creator_agent import (
    _all_media_capacity_question,
    _apply_explicit_render_intent,
    _historical_all_media_capacity_choice_kind,
    _seed_guided_specialist_brief,
    _strongest_guided_subset_ids,
)
from app.services.creator_capabilities import compile_strategy_to_plan, resolve_creator_manifest

# Reuse the KRI-126 manifest/fixture builders rather than duplicating them.
from tests.routes.test_kri126_creator_media_scope import _fixture_manifest


@pytest.fixture(autouse=True)
def _stub_creator_clip_metadata_dispatch(monkeypatch) -> None:
    from app.tasks.creator_clip_metadata import analyze_creator_clip_metadata

    monkeypatch.setattr(analyze_creator_clip_metadata, "apply_async", MagicMock())


def _sixteen_clip_manifest(*, one_short: bool = True):
    """16 clips mirroring prod job 1186da65: one clip below the guided floor."""

    durations = [5.0] * 16
    if one_short:
        durations[7] = 1.27
    return resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index:02d}", "kind": "video", "duration_s": durations[index]}
            for index in range(16)
        ],
        guided_capability_enabled=True,
    )


def _event(sequence: int, *, role: str, event_type: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(sequence=sequence, role=role, event_type=event_type, payload=payload)


async def _drive_planning_turn(
    monkeypatch,
    *,
    manifest,
    session,
    model_strategy: CreativeStrategy,
    model_summary: str,
    user_message: str,
    previous_active_plan: dict | None = None,
):
    """Mirror tests/routes/test_creator_agent.py's `_run_planning_turn` harness."""

    item = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4())
    response = SimpleNamespace(status="awaiting_confirmation")
    to_thread = AsyncMock(
        return_value=SimpleNamespace(
            action=ProposeStrategy(
                kind="propose_strategy", strategy=model_strategy, summary=model_summary
            )
        )
    )
    monkeypatch.setattr(
        creator_routes,
        "_owned_context",
        AsyncMock(return_value=(item, SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(creator_routes, "_load_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        creator_routes,
        "resolve_item_creator_context",
        AsyncMock(return_value=(manifest, [])),
    )
    monkeypatch.setattr(creator_routes, "creator_context", lambda *_args: ("creator", "item"))
    monkeypatch.setattr(creator_routes.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(creator_routes, "append_event", AsyncMock())
    monkeypatch.setattr(creator_routes, "_response", AsyncMock(return_value=response))

    result = await creator_routes._run_planning_turn(
        AsyncMock(),
        item_id=str(item.id),
        user=user,
        session_id=session.id,
        expected_revision=session.revision,
        user_message=user_message,
        previous_active_plan=previous_active_plan,
    )
    assert result is response
    return session


def _base_session(
    *, manifest, events, question_count: int, question_budget: int
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        revision=1,
        status="planning",
        events=events,
        agent_call_count=0,
        agent_call_budget=2,
        question_count=question_count,
        question_budget=question_budget,
        active_plan=None,
        last_error=None,
        manifest_hash=manifest.manifest_hash,
    )


# --- (1) a short clip is never excluded from an edit for being short -------


def test_strongest_guided_subset_ids_never_excludes_a_short_clip() -> None:
    """Ranking by strength is fine; excluding for shortness is not.

    Reproduces the ranking math the prod incident's "strongest clips"
    fallback used to run: a sub-floor clip must remain eligible for the
    recommendation, never be filtered out of it for its duration alone.
    """

    manifest = _sixteen_clip_manifest()
    short_id = "clip-07"

    subset_ids = _strongest_guided_subset_ids(manifest, 60, min_moment_s=1.4)

    # With target=60 and min_moment_s=1.4, the limit comfortably covers all
    # 16 clips (GUIDED_STORY_MAX_MEDIA is the only cap) -- the short clip
    # must be among them.
    assert short_id in subset_ids
    assert len(subset_ids) == 16


def test_strongest_guided_subset_ids_excludes_only_disallowed_kinds() -> None:
    """Allowlists {"video", "image"} the way `_attached_media_count` does --
    other kinds (e.g. "audio") never enter this ranking, but that is a kind
    exclusion, never a duration one."""

    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        narration={
            "gcs_path": "voiceover-uploads/user/item/voice.webm",
            "generation": "voice-generation-1",
            "duration_s": 12.0,
        },
        media=[
            {"media_id": "clip-video", "kind": "video", "duration_s": 0.2},
            {"media_id": "clip-image", "kind": "image"},
        ],
        guided_capability_enabled=True,
    )

    subset_ids = _strongest_guided_subset_ids(manifest, 60, min_moment_s=1.4)

    assert "clip-video" in subset_ids  # short, but usable at its own length
    assert "clip-image" in subset_ids


@pytest.mark.asyncio
async def test_prod_incident_full_turn_keeps_the_short_clip_with_no_question(
    monkeypatch,
) -> None:
    """End-to-end: the exact prod shape resolves to "all", short clip
    included, with no question and no "left out" explanation -- it is
    genuinely feasible now that capacity math counts a short clip at its own
    length (`guided_source_floor_s`), never the story-minimum floor."""

    manifest = _sixteen_clip_manifest()
    session = _base_session(
        manifest=manifest,
        events=[
            _event(
                1,
                role="user",
                event_type="user_message",
                payload={"message": "Continue with 16 clips"},
            )
        ],
        question_count=2,
        question_budget=2,
    )
    model_strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        media_scope="all",
        selected_media_ids=[ref.media_id for ref in manifest.media],
        target_duration_s=60,
        audio_strategy="licensed_music",
        rationale="Tell the whole day in order.",
    )

    session = await _drive_planning_turn(
        monkeypatch,
        manifest=manifest,
        session=session,
        model_strategy=model_strategy,
        model_summary="Building your edit.",
        user_message="Please redo this using all the videos",
    )

    strategy = session.active_plan["edit_plan"]["strategy"]
    assert strategy["media_scope"] == "all"
    assert len(strategy["selected_media_ids"]) == 16
    assert "clip-07" in strategy["selected_media_ids"]
    summary = session.active_plan["summary"]
    assert "left out" not in summary.lower()
    assert "shorter than" not in summary.lower()
    # A question must never have been asked for this turn.
    assert session.status != "briefing"


# --- (2) + (3) budget exhausted with a genuine (non-sub-floor) blocker -----


def _five_clip_manifest():
    """5 clips, all well above the guided floor, so "all" is infeasible here
    purely on duration math (guided needs >= 7s; fast fits between 4s-6s)."""

    return resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": f"clip-{index}", "kind": "video", "duration_s": 20.0} for index in range(5)
        ],
        guided_capability_enabled=True,
    )


def _five_clip_all_scope_strategy(manifest, *, target_duration_s: float = 5.0) -> CreativeStrategy:
    return CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        media_scope="all",
        selected_media_ids=[ref.media_id for ref in manifest.media],
        target_duration_s=target_duration_s,
        audio_strategy="licensed_music",
        rationale="A tight highlight of the day.",
    )


def test_five_clip_scenario_is_infeasible_for_a_genuine_duration_reason() -> None:
    """Pins the fixture shape this section's tests depend on."""

    manifest = _five_clip_manifest()
    strategy = _five_clip_all_scope_strategy(manifest)

    question = _all_media_capacity_question(manifest, strategy)
    assert question is not None
    mappings = question["all_media_capacity"]["option_mappings"]
    assert len(mappings) == 2  # a keep-everything (fast) mapping exists
    assert mappings[1]["strategy"]["media_scope"] == "all"


@pytest.mark.asyncio
async def test_budget_exhausted_reapplies_the_creators_earlier_answer(monkeypatch) -> None:
    manifest = _five_clip_manifest()
    strategy = _five_clip_all_scope_strategy(manifest)
    question = _all_media_capacity_question(manifest, strategy)
    assert question is not None
    subset_option = question["options"][0]  # "strongest clips" mapping, chosen once before

    session = _base_session(
        manifest=manifest,
        events=[
            _event(1, role="user", event_type="user_message", payload={"message": "use my clips"}),
            _event(
                2,
                role="assistant",
                event_type="assistant_question",
                payload=question,
            ),
            _event(
                3,
                role="user",
                event_type="user_message",
                payload={"message": subset_option},
            ),
            _event(
                4,
                role="assistant",
                event_type="assistant_strategy",
                payload={"message": "Applied.", "proposal_summary": "ok", "plan_hash": "x"},
            ),
        ],
        question_count=2,
        question_budget=2,
    )

    # Sanity: the pending-question lookup no longer finds this question
    # (it's not the tail event anymore), but the historical lookup does.
    assert creator_routes._latest_all_media_capacity_question(session.events) is None
    historical_kind = _historical_all_media_capacity_choice_kind(session.events, manifest)
    assert historical_kind == "strongest_subset"

    session = await _drive_planning_turn(
        monkeypatch,
        manifest=manifest,
        session=session,
        model_strategy=strategy,
        model_summary="Building your edit.",
        # Keep the latest turn neutral: this regression verifies reuse of the
        # earlier subset answer, while an explicit new "all" instruction is a
        # separate override path covered below.
        user_message="Please redo this with the same direction",
    )

    resolved = session.active_plan["edit_plan"]["strategy"]
    assert resolved["media_scope"] == "selected"
    summary = session.active_plan["summary"]
    assert "earlier" in summary.lower()


@pytest.mark.asyncio
async def test_budget_exhausted_reapplies_the_kind_not_the_stale_values(monkeypatch) -> None:
    """KRI-129 bug: history must carry only the KIND of choice ("strongest
    subset"), never the stale VALUES (target duration, ids) recorded when the
    earlier question was asked.

    Repro (matches the reported incident shape): turn 1 answers the capacity
    question at a 5-second target with the "strongest clips" mapping; a later
    turn -- budget exhausted -- explicitly asks for 6 seconds instead. The
    resolved duration must be 6 (and the subset recomputed for 6s), never the
    stale 5 recorded against the first question.
    """

    manifest = _five_clip_manifest()
    original_strategy = _five_clip_all_scope_strategy(manifest, target_duration_s=5.0)
    question = _all_media_capacity_question(manifest, original_strategy)
    assert question is not None
    subset_option = question["options"][0]  # "strongest clips" mapping, answered at 5s
    stale_subset_ids = question["all_media_capacity"]["option_mappings"][0]["strategy"][
        "selected_media_ids"
    ]

    session = _base_session(
        manifest=manifest,
        events=[
            _event(
                1,
                role="user",
                event_type="user_message",
                payload={"message": "use all my clips at 5 seconds"},
            ),
            _event(2, role="assistant", event_type="assistant_question", payload=question),
            _event(3, role="user", event_type="user_message", payload={"message": subset_option}),
            _event(
                4,
                role="assistant",
                event_type="assistant_strategy",
                payload={"message": "Applied.", "proposal_summary": "ok", "plan_hash": "x"},
            ),
        ],
        question_count=2,
        question_budget=2,
    )

    new_target_strategy = _five_clip_all_scope_strategy(manifest, target_duration_s=6.0)

    session = await _drive_planning_turn(
        monkeypatch,
        manifest=manifest,
        session=session,
        model_strategy=new_target_strategy,
        model_summary="Building your edit.",
        # The regression changes duration only.  An explicit new "all" scope
        # is a deliberate override of historical subset preference and has its
        # own guard; it must not be conflated with stale-value protection.
        user_message="actually keep it at 6 seconds",
    )

    resolved = session.active_plan["edit_plan"]["strategy"]
    assert resolved["media_scope"] == "selected"
    # The stale 5-second answer picked 3 clips (floor(5/1.4)); the fresh 6-
    # second mapping picks 4 (floor(6/1.4)). Never the stale duration or ids.
    assert resolved["target_duration_s"] == 6
    assert len(resolved["selected_media_ids"]) == 4
    assert set(resolved["selected_media_ids"]) != set(stale_subset_ids)
    summary = session.active_plan["summary"]
    assert "earlier" in summary.lower()


@pytest.mark.asyncio
async def test_budget_exhausted_new_explicit_all_overrides_earlier_subset(monkeypatch) -> None:
    manifest = _five_clip_manifest()
    strategy = _five_clip_all_scope_strategy(manifest)
    question = _all_media_capacity_question(manifest, strategy)
    assert question is not None
    subset_option = question["options"][0]
    session = _base_session(
        manifest=manifest,
        events=[
            _event(1, role="user", event_type="user_message", payload={"message": "use my clips"}),
            _event(2, role="assistant", event_type="assistant_question", payload=question),
            _event(
                3,
                role="user",
                event_type="user_message",
                payload={"message": subset_option},
            ),
            _event(
                4,
                role="assistant",
                event_type="assistant_strategy",
                payload={"message": "Applied.", "proposal_summary": "ok", "plan_hash": "x"},
            ),
        ],
        question_count=2,
        question_budget=2,
    )

    session = await _drive_planning_turn(
        monkeypatch,
        manifest=manifest,
        session=session,
        model_strategy=strategy,
        model_summary="Building your edit.",
        user_message="actually use all the clips",
    )

    resolved = session.active_plan["edit_plan"]["strategy"]
    assert resolved["media_scope"] == "all"
    assert resolved["direction"] == "fast_montage"
    assert "earlier" not in session.active_plan["summary"].lower()


@pytest.mark.asyncio
async def test_budget_exhausted_latest_narrowing_message_overrides_stale_history(
    monkeypatch,
) -> None:
    """KRI-129: the creator's NEWEST words override a contradicted history.

    History says "keep everything" (answered once before); the latest
    message narrows ("just use the best ones") instead. The historical
    "keep everything" kind must be ignored -- the summary must not claim an
    earlier choice was reapplied when the newest words say otherwise.
    """

    manifest = _five_clip_manifest()
    strategy = _five_clip_all_scope_strategy(manifest)
    question = _all_media_capacity_question(manifest, strategy)
    assert question is not None
    keep_everything_option = question["options"][1]  # "...include everything..." mapping

    session = _base_session(
        manifest=manifest,
        events=[
            _event(
                1, role="user", event_type="user_message", payload={"message": "use all my clips"}
            ),
            _event(2, role="assistant", event_type="assistant_question", payload=question),
            _event(
                3,
                role="user",
                event_type="user_message",
                payload={"message": keep_everything_option},
            ),
            _event(
                4,
                role="assistant",
                event_type="assistant_strategy",
                payload={"message": "Applied.", "proposal_summary": "ok", "plan_hash": "x"},
            ),
        ],
        question_count=2,
        question_budget=2,
    )

    # Sanity: history alone (unconstrained by a contradicting latest message)
    # really does classify as "keep_everything".
    assert (
        creator_routes._historical_all_media_capacity_choice_kind(session.events, manifest)
        == "keep_everything"
    )

    session = await _drive_planning_turn(
        monkeypatch,
        manifest=manifest,
        session=session,
        model_strategy=strategy,
        model_summary="Building your edit.",
        user_message="actually just use the best ones",
    )

    summary = session.active_plan["summary"]
    # Never claims to reapply "your earlier choice" -- the newest words
    # contradicted that history, so it was not consulted.
    assert "earlier" not in summary.lower()
    assert "don't fit" in summary.lower()


def test_latest_message_narrows_media_scope_detects_the_documented_cues() -> None:
    manifest = _five_clip_manifest()
    assert (
        creator_routes._latest_message_narrows_media_scope("just use the best ones", manifest)
        is True
    )
    assert (
        creator_routes._latest_message_narrows_media_scope(
            "only the selected clips please", manifest
        )
        is True
    )
    assert (
        creator_routes._latest_message_narrows_media_scope("make the intro title red", manifest)
        is False
    )


@pytest.mark.asyncio
async def test_budget_exhausted_with_no_history_prefers_keeping_everything(monkeypatch) -> None:
    manifest = _five_clip_manifest()
    strategy = _five_clip_all_scope_strategy(manifest)

    session = _base_session(
        manifest=manifest,
        events=[
            _event(
                1,
                role="user",
                event_type="user_message",
                payload={"message": "use all my clips"},
            ),
        ],
        question_count=2,
        question_budget=2,
    )

    session = await _drive_planning_turn(
        monkeypatch,
        manifest=manifest,
        session=session,
        model_strategy=strategy,
        model_summary="Building your edit.",
        user_message="Please redo this using all the clips",
    )

    resolved = session.active_plan["edit_plan"]["strategy"]
    # Keep-everything (faster pacing), never the clip-dropping subset.
    assert resolved["media_scope"] == "all"
    assert resolved["direction"] == "fast_montage"
    summary = session.active_plan["summary"]
    assert "kept everything" in summary.lower()


# --- (3) an explanatory sentence must be said once, not every turn --------


@pytest.mark.asyncio
async def test_explanatory_sentence_is_not_repeated_on_an_unrelated_later_turn(
    monkeypatch,
) -> None:
    """KRI-129: `creator_request` re-accumulates every past user message, so
    a resolution keyed off it (an explicit "all" scope that keeps re-
    resolving to the same outcome) can legitimately re-fire turn after turn.
    Repro: turn 1 "use all the videos" gets the explanatory sentence; turn 2
    "make the intro title red" must NOT repeat it, since the resolved
    outcome (scope, ids, duration) is unchanged from what was already
    communicated.
    """

    manifest = _five_clip_manifest()
    strategy = _five_clip_all_scope_strategy(manifest)

    session = _base_session(
        manifest=manifest,
        events=[
            _event(
                1,
                role="user",
                event_type="user_message",
                payload={"message": "use all my clips"},
            ),
        ],
        question_count=2,
        question_budget=2,
    )

    session = await _drive_planning_turn(
        monkeypatch,
        manifest=manifest,
        session=session,
        model_strategy=strategy,
        model_summary="Building your edit.",
        user_message="use all the videos",
    )
    first_summary = session.active_plan["summary"]
    assert "don't fit" in first_summary.lower()  # sanity: it fired once

    previous_active_plan = session.active_plan
    session.status = "planning"  # a new turn starting, as in the real flow

    session = await _drive_planning_turn(
        monkeypatch,
        manifest=manifest,
        session=session,
        model_strategy=strategy,
        model_summary="Building your edit.",
        user_message="make the intro title red",
        previous_active_plan=previous_active_plan,
    )

    second_summary = session.active_plan["summary"]
    assert "don't fit" not in second_summary.lower()
    # The resolved outcome itself is unaffected by the suppression.
    resolved = session.active_plan["edit_plan"]["strategy"]
    assert resolved["media_scope"] == "all"
    assert resolved["direction"] == "fast_montage"


@pytest.mark.asyncio
async def test_explanatory_sentence_still_fires_when_the_outcome_actually_changes(
    monkeypatch,
) -> None:
    """The dedup in the test above must not become a blanket suppression:
    a genuinely different resolved outcome (here: a different target
    duration) still gets its sentence."""

    manifest = _five_clip_manifest()
    strategy_5s = _five_clip_all_scope_strategy(manifest, target_duration_s=5.0)
    strategy_6s = _five_clip_all_scope_strategy(manifest, target_duration_s=6.0)

    session = _base_session(
        manifest=manifest,
        events=[
            _event(
                1,
                role="user",
                event_type="user_message",
                payload={"message": "use all my clips at 5 seconds"},
            ),
        ],
        question_count=2,
        question_budget=2,
    )

    session = await _drive_planning_turn(
        monkeypatch,
        manifest=manifest,
        session=session,
        model_strategy=strategy_5s,
        model_summary="Building your edit.",
        user_message="use all the videos at 5 seconds",
    )
    previous_active_plan = session.active_plan
    assert previous_active_plan["edit_plan"]["strategy"]["target_duration_s"] == 5
    session.status = "planning"  # a new turn starting, as in the real flow

    session = await _drive_planning_turn(
        monkeypatch,
        manifest=manifest,
        session=session,
        model_strategy=strategy_6s,
        model_summary="Building your edit.",
        user_message="actually make it 6 seconds, use all the videos",
        previous_active_plan=previous_active_plan,
    )

    resolved = session.active_plan["edit_plan"]["strategy"]
    assert resolved["target_duration_s"] == 6
    summary = session.active_plan["summary"]
    assert "don't fit" in summary.lower()


# --- (4) explicit "selected" scope with real ids survives into the brief ---


def test_guided_selected_scope_with_five_ids_survives_normalize_and_seeding() -> None:
    fixture, manifest = _fixture_manifest()
    selected_ids = [ref.media_id for ref in manifest.media][:5]
    strategy = CreativeStrategy(
        direction="guided_story",
        render_program="guided",
        media_scope="selected",
        selected_media_ids=selected_ids,
        target_duration_s=fixture["duration_s"],
        audio_strategy="licensed_music",
    )

    normalized = normalize_creator_strategy_media(manifest, strategy)
    assert normalized.selected_media_ids == selected_ids

    plan = compile_strategy_to_plan(manifest, strategy)
    assert plan.strategy.selected_media_ids == selected_ids

    item = SimpleNamespace(edit_proposal=None)
    _seed_guided_specialist_brief(
        item, plan, summary="Group by sport", creator_request=fixture["creator_request"]
    )
    assert item.edit_proposal["brief"]["media_scope"] == "selected"
    assert item.edit_proposal["brief"]["selected_media_ids"] == selected_ids


def test_guided_selected_scope_unknown_ids_rejected_unless_repaired() -> None:
    fixture, manifest = _fixture_manifest()
    valid_ids = [ref.media_id for ref in manifest.media][:5]
    strategy = CreativeStrategy(
        direction="guided_story",
        render_program="guided",
        media_scope="selected",
        selected_media_ids=[*valid_ids, "not-a-real-id"],
        target_duration_s=fixture["duration_s"],
        audio_strategy="licensed_music",
    )

    with pytest.raises(ValueError, match="selected_media_ids must reference manifest media"):
        normalize_creator_strategy_media(manifest, strategy)

    repaired = normalize_creator_strategy_media(manifest, strategy, repair_model_output=True)
    assert repaired.selected_media_ids == valid_ids


# --- (5) main_creator: regex is evidence, never a veto ----------------------


def _main_creator_manifest():
    from app.agents._schemas.creator_agent import (
        CapabilityAvailability,
        CreatorMediaRef,
        ResolvedCreatorManifest,
    )

    available = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="montage",
        render_program="guided",
        media=[
            CreatorMediaRef(media_id=f"clip-{index:02d}", kind="video", duration_s=5.0)
            for index in range(8)
        ],
        capabilities={
            "edit_format:montage": available,
            "draft_guided_proposal": available,
            "dispatch_render": available,
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def _main_creator_raw(*, media_scope) -> str:
    return json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": {
                    "direction": "guided_story",
                    "edit_format": "montage",
                    "audio_strategy": "licensed_music",
                    "media_scope": media_scope,
                    "render_program": "guided",
                    "selected_media_ids": [],
                    "target_duration_s": 24,
                    "rationale": "Build a concise visual arc.",
                },
                "summary": "A concise visual story.",
            }
        }
    )


@pytest.mark.parametrize("model_scope", ["all", "selected", None])
def test_main_creator_keeps_model_scope_when_regex_is_silent(model_scope) -> None:
    manifest = _main_creator_manifest()
    agent_input = MainCreatorInput(
        user_message="Create an edit of the best moments.",
        media_context=[{"media_id": media.media_id} for media in manifest.media],
        capability_manifest=manifest,
    )

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _main_creator_raw(media_scope=model_scope), agent_input
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.media_scope == model_scope


def test_main_creator_regex_still_wins_over_the_model() -> None:
    manifest = _main_creator_manifest()
    agent_input = MainCreatorInput(
        user_message="Use all uploaded media.",
        media_context=[{"media_id": media.media_id} for media in manifest.media],
        capability_manifest=manifest,
    )

    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _main_creator_raw(media_scope="selected"), agent_input
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.media_scope == "all"


# --- (6) legacy default path is untouched when scope is never mentioned ----


def test_no_scope_mention_leaves_legacy_default_behavior_byte_identical() -> None:
    manifest = _sixteen_clip_manifest(one_short=False)
    base = CreativeStrategy(
        direction="fast_montage",
        edit_format="montage",
        pacing="balanced",
        target_duration_s=30,
        rationale="A focused edit from strong footage.",
    )

    strategy = _apply_explicit_render_intent(
        base, "Make this feel energetic and fun.", manifest=manifest
    )

    assert strategy.media_scope is None
    # No capacity question can ever apply to an unset scope.
    assert _all_media_capacity_question(manifest, strategy) is None


# --- KRI-129 part 6: a duration cap on a genuine "selected" scope is said --


def _two_clip_selected_manifest():
    """A short + a long clip; only the short two get selected below, so a
    once-only montage's combined source capacity (7s) is well under a 20s
    target -- `normalize_creator_strategy_media` caps the duration without
    changing the math."""

    return resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-a", "kind": "video", "duration_s": 3.0},
            {"media_id": "clip-b", "kind": "video", "duration_s": 4.0},
            {"media_id": "clip-c", "kind": "video", "duration_s": 20.0},
        ],
        guided_capability_enabled=True,
    )


def test_normalize_caps_duration_for_a_genuine_selected_scope() -> None:
    """Pins the existing, untouched cap math this section's tests rely on."""

    manifest = _two_clip_selected_manifest()
    strategy = CreativeStrategy(
        direction="guided_story",
        render_program="guided",
        edit_format="montage",
        media_scope="selected",
        selected_media_ids=["clip-a", "clip-b"],
        target_duration_s=20,
        audio_strategy="licensed_music",
        video_reuse_policy="once",
    )

    normalized = normalize_creator_strategy_media(manifest, strategy)

    assert normalized.target_duration_s == 7


@pytest.mark.asyncio
async def test_selected_scope_duration_cap_is_explained_in_the_summary(monkeypatch) -> None:
    manifest = _two_clip_selected_manifest()
    session = _base_session(
        manifest=manifest,
        events=[],
        question_count=0,
        question_budget=2,
    )
    model_strategy = CreativeStrategy(
        direction="guided_story",
        render_program="guided",
        edit_format="montage",
        media_scope="selected",
        selected_media_ids=["clip-a", "clip-b"],
        target_duration_s=20,
        audio_strategy="licensed_music",
        video_reuse_policy="once",
        rationale="A quick two-clip cut.",
    )

    session = await _drive_planning_turn(
        monkeypatch,
        manifest=manifest,
        session=session,
        model_strategy=model_strategy,
        model_summary="Building your edit.",
        user_message="Use clip a and clip b, keep it to 20 seconds",
    )

    resolved = session.active_plan["edit_plan"]["strategy"]
    assert resolved["target_duration_s"] == 7
    summary = session.active_plan["summary"]
    assert "your selected clips add up to 7 seconds" in summary.lower()
    assert "so i made the edit" in summary.lower()


def test_capacity_question_is_not_asked_while_a_clip_length_is_still_unknown() -> None:
    """A registered clip has no duration until its analysis finishes. Capacity
    is then undecidable, not infeasible: the creator must not be told that
    "all 2 clips cannot fit" a 20-second edit."""

    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": f"clip-{index}", "kind": "video"} for index in range(2)],
        guided_capability_enabled=True,
    )
    assert all(ref.duration_s is None for ref in manifest.media)
    strategy = CreativeStrategy(
        direction="fast_montage",
        edit_format="montage",
        media_scope="all",
        selected_media_ids=[ref.media_id for ref in manifest.media],
        target_duration_s=20,
        audio_strategy="licensed_music",
        rationale="Name the dish on each clip.",
    )

    assert creator_routes._all_media_capacity_question(manifest, strategy) is None

    # The same shape with known, genuinely infeasible durations still asks.
    assert (
        creator_routes._all_media_capacity_question(
            _five_clip_manifest(), _five_clip_all_scope_strategy(_five_clip_manifest())
        )
        is not None
    )
