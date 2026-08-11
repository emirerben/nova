"""Task-side tenant and ownership-epoch regression tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.agents._schemas.content_plan import ContentPlanOutput, PlanItemSpec
from app.models import ContentPlan, PlanItem
from app.models import Persona as PersonaRow
from app.services.content_plan_persona import PlanPersonaOwnershipError
from app.tasks.content_plan_build import (
    activate_content_plan,
    dispatch_item_render_for,
    generate_content_plan,
    generate_ideas_into_plan,
    match_pool_clips,
    regenerate_content_plan,
    reroll_plan_item,
)


def _ctx(session: MagicMock) -> MagicMock:
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False
    return context


def _plan(*, status: str = "generating") -> MagicMock:
    plan = MagicMock(spec=ContentPlan)
    plan.id = uuid.uuid4()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.ownership_epoch = 7
    plan.ownership_quarantined_at = None
    plan.plan_status = status
    plan.generation_started_at = datetime.now(UTC)
    plan.events = None
    plan.horizon_days = 30
    plan.items = []
    plan.seed_clip_paths = [f"users/{plan.user_id}/plan/{plan.id}/seed/a.mp4"]
    plan.pool = {
        "status": "matching",
        "clips": [{"gcs_path": f"users/{plan.user_id}/plan-pool/{plan.id}/a.mp4"}],
    }
    return plan


def _persona(plan: MagicMock) -> MagicMock:
    persona = MagicMock(spec=PersonaRow)
    persona.id = plan.persona_id
    persona.user_id = plan.user_id
    persona.persona = {
        "summary": "A mobility creator documenting practical desk routines.",
        "content_pillars": ["mobility", "daily routines"],
        "tone": "direct and encouraging",
        "audience": "desk workers who want to move more",
        "posting_cadence": "three times a week",
        "sample_topics": ["desk stretches"],
    }
    persona.idea_seeds = [{"id": "seed-1", "text": "desk reset", "status": "pending"}]
    persona.tiktok_profile = None
    persona.style = None
    return persona


def test_dispatch_owner_mismatch_returns_typed_outcome_before_job_or_queue() -> None:
    plan = _plan(status="ready")
    item = MagicMock(spec=PlanItem)
    item.id = uuid.uuid4()
    item.content_plan_id = plan.id
    session = MagicMock()
    session.get.side_effect = lambda model, _pk, **_kwargs: item if model is PlanItem else None

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch(
            "app.tasks.content_plan_build._lock_owned_plan_persona",
            side_effect=PlanPersonaOwnershipError(plan),
        ),
        patch("app.services.generative_jobs.build_generative_job") as build_job,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync") as enqueue,
    ):
        result = dispatch_item_render_for(str(item.id))

    assert result.outcome == "invalid_persona"
    assert result.job_id is None
    build_job.assert_not_called()
    enqueue.assert_not_called()
    session.add.assert_not_called()
    session.flush.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize("task_name", ["generate", "regenerate"])
@pytest.mark.parametrize("quarantined", [False, True])
def test_plan_build_invalid_pair_never_calls_agent_or_mutates_foreign_seed(
    task_name: str,
    quarantined: bool,
) -> None:
    plan = _plan()
    if quarantined:
        plan.ownership_quarantined_at = datetime.now(UTC)
    foreign_persona = _persona(plan)
    foreign_persona.user_id = uuid.uuid4()
    before = [dict(seed) for seed in foreign_persona.idea_seeds]
    session = MagicMock()
    session.get.side_effect = lambda model, _pk, **_kwargs: plan if model is ContentPlan else None

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch(
            "app.tasks.content_plan_build.load_owned_plan_persona_sync",
            side_effect=PlanPersonaOwnershipError(plan),
        ),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as agent_cls,
    ):
        task = generate_content_plan if task_name == "generate" else regenerate_content_plan
        task.run(str(plan.id))

    agent_cls.assert_not_called()
    assert foreign_persona.idea_seeds == before
    session.add.assert_not_called()
    session.delete.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize("fence_change", ["epoch", "quarantine"])
def test_generate_discards_result_when_ownership_fence_changes(fence_change: str) -> None:
    plan = _plan()
    persona = _persona(plan)
    session = MagicMock()
    session.get.side_effect = lambda model, _pk, **_kwargs: {
        ContentPlan: plan,
        PersonaRow: persona,
    }.get(model)
    output = ContentPlanOutput(items=[PlanItemSpec(day_index=1, theme="mobility", idea="reset")])

    def _owned_loader(_session, current_plan, *, for_update=False):  # noqa: ANN001, ARG001
        if current_plan.ownership_quarantined_at is not None:
            raise PlanPersonaOwnershipError(current_plan)
        return persona

    def _run(*_args, **_kwargs):
        if fence_change == "epoch":
            plan.ownership_epoch += 1
        else:
            plan.ownership_quarantined_at = datetime.now(UTC)
        return output

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch(
            "app.tasks.content_plan_build.load_owned_plan_persona_sync",
            side_effect=_owned_loader,
        ),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as agent_cls,
        patch("app.tasks.content_plan_build._dedup_and_replace", return_value=output),
    ):
        agent_cls.return_value.run.side_effect = _run
        generate_content_plan.run(str(plan.id), plan.ownership_epoch)

    agent_cls.return_value.run.assert_called_once()
    assert persona.idea_seeds[0]["status"] == "pending"
    session.add.assert_not_called()
    session.delete.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.parametrize("task_name", ["activation", "pool", "reroll", "generate_one"])
def test_legacy_task_entry_mismatch_is_terminal_before_external_work(task_name: str) -> None:
    plan = _plan(status="generating" if task_name == "generate_one" else "ready")
    item = MagicMock(spec=PlanItem)
    item.id = uuid.uuid4()
    item.content_plan_id = plan.id
    item.item_status = "rerolling"
    session = MagicMock()
    session.get.side_effect = lambda model, _pk, **_kwargs: {
        ContentPlan: plan,
        PlanItem: item,
    }.get(model)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch(
            "app.tasks.content_plan_build.load_owned_plan_persona_sync",
            side_effect=PlanPersonaOwnershipError(plan),
        ),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as plan_agent,
        patch("app.agents.clip_plan_matcher.ClipPlanMatcherAgent") as matcher_agent,
        patch("app.tasks.generative_build._ingest_clips") as ingest,
    ):
        if task_name == "activation":
            activate_content_plan.run(str(plan.id))
        elif task_name == "pool":
            match_pool_clips.run(str(plan.id))
        elif task_name == "reroll":
            reroll_plan_item.run(str(item.id))
        else:
            generate_ideas_into_plan.run(str(plan.id))

    plan_agent.assert_not_called()
    matcher_agent.assert_not_called()
    ingest.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()
    assert item.item_status == "rerolling"
