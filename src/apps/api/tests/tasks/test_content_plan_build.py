"""Unit test for persona threading in generate_plan_item_videos (mock DB).

Locks that the per-item plan task loads the creator's persona + the item's
theme/idea and forwards them to the shared build_generative_job — the data path
that makes content-plan hooks persona-coherent (intro_writer threading).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.models import ContentPlan, PlanItem
from app.models import Persona as PersonaRow
from app.tasks.content_plan_build import generate_plan_item_videos


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
