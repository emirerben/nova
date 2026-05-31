"""Unit test for persona threading in generate_plan_item_videos (mock DB).

Locks that the per-item plan task loads the creator's persona + the item's
theme/idea and forwards them to the shared build_generative_job — the data path
that makes content-plan hooks persona-coherent (intro_writer threading).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.agents._schemas.content_plan import PlanItemSpec
from app.models import ContentPlan, PlanItem
from app.models import Persona as PersonaRow
from app.tasks.content_plan_build import generate_plan_item_videos, regenerate_content_plan


def _session_with(item, plan, persona_row) -> MagicMock:
    session = MagicMock()

    def _get(model, _pk):
        return {PlanItem: item, ContentPlan: plan, PersonaRow: persona_row}.get(model)

    session.get = MagicMock(side_effect=_get)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def test_persona_forwarded_to_build_generative_job() -> None:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/u/plan/i/a.mp4"]
    item.theme = "first 5am workout"
    item.idea = "film the dark early start"

    plan = MagicMock()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()

    persona_row = MagicMock()
    persona_row.persona = {
        "tone": "no-excuses gym motivation",
        "content_pillars": ["morning routines", "discipline"],
    }

    job = MagicMock()
    job.id = uuid.uuid4()

    ctx = _session_with(item, plan, persona_row)
    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        generate_plan_item_videos.run(str(item.id))

    mock_build.assert_called_once()
    kwargs = mock_build.call_args.kwargs
    assert kwargs["mode"] == "content_plan"
    assert kwargs["persona_tone"] == "no-excuses gym motivation"
    assert kwargs["persona_pillars"] == ["morning routines", "discipline"]
    assert kwargs["item_theme"] == "first 5am workout"
    assert kwargs["item_idea"] == "film the dark early start"


def test_missing_persona_falls_back_to_empty() -> None:
    # A plan item whose persona row is gone must NOT block the render — the task
    # passes empty persona fields and the builder omits the key downstream.
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/u/plan/i/a.mp4"]
    item.theme = "first 5am workout"
    item.idea = ""

    plan = MagicMock()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()

    job = MagicMock()
    job.id = uuid.uuid4()

    ctx = _session_with(item, plan, None)  # persona row missing
    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        generate_plan_item_videos.run(str(item.id))

    kwargs = mock_build.call_args.kwargs
    assert kwargs["persona_tone"] == ""
    assert kwargs["persona_pillars"] == []
    assert kwargs["item_theme"] == "first 5am workout"


# ── regenerate_content_plan: the "their say" invariant ────────────────────────


def _plan_item(day: int, *, user_edited: bool, current_job_id: uuid.UUID | None) -> MagicMock:
    it = MagicMock()
    it.day_index = day
    it.user_edited = user_edited  # explicit — a bare MagicMock attr is truthy
    it.current_job_id = current_job_id
    it.theme = f"old theme {day}"
    it.idea = f"old idea {day}"
    return it


def _valid_persona() -> dict:
    return {
        "summary": "you film calm morning routines",
        "content_pillars": ["mornings", "discipline"],
        "tone": "warm and steady",
        "audience": "people who want a calmer start",
        "posting_cadence": "3-4 posts/week",
        "sample_topics": ["sunrise walk"],
    }


def test_regenerate_preserves_user_edited_and_in_flight_items() -> None:
    """The load-bearing invariant: regenerate replaces ONLY a day that is neither
    hand-edited nor already rendering. Day 1 (user_edited) and day 3 (current_job)
    are kept verbatim; only day 2 is deleted and re-inserted from fresh AI output."""
    user_id = uuid.uuid4()
    edited = _plan_item(1, user_edited=True, current_job_id=None)
    regenerable = _plan_item(2, user_edited=False, current_job_id=None)
    in_flight = _plan_item(3, user_edited=False, current_job_id=uuid.uuid4())

    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user_id
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.items = [edited, regenerable, in_flight]

    persona_row = MagicMock()
    persona_row.persona = _valid_persona()

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk: {ContentPlan: plan, PersonaRow: persona_row}.get(model)
    )
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)

    # Fresh AI output proposes all three days; only the regenerable one may land.
    output = MagicMock()
    output.items = [
        PlanItemSpec(day_index=1, theme="NEW 1", idea="new idea 1"),
        PlanItemSpec(day_index=2, theme="NEW 2", idea="new idea 2"),
        PlanItemSpec(day_index=3, theme="NEW 3", idea="new idea 3"),
    ]
    agent = MagicMock()
    agent.run = MagicMock(return_value=output)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch(
            "app.services.feedback_summary.rollup_user_feedback",
            return_value="liked: 3, disliked: 1",
        ),
    ):
        regenerate_content_plan.run(str(plan.id))

    # The feedback summary was persisted on the plan.
    assert plan.preference_summary == "liked: 3, disliked: 1"
    # ONLY the regenerable day-2 item was deleted (protected days untouched).
    deleted = [c.args[0] for c in session.delete.call_args_list]
    assert deleted == [regenerable]
    # ONLY a single new item was added, for day 2, from the fresh AI output.
    added = [c.args[0] for c in session.add.call_args_list]
    assert len(added) == 1
    assert added[0].day_index == 2
    assert added[0].theme == "NEW 2"
