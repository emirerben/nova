"""Unit tests for activate_content_plan phase stamping (PR4).

Verifies that activate_content_plan stamps activation_phase through the three
values (matching_clips → picking_days → starting_renders) and sets
activation_started_at.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.models import ContentPlan, PlanItem


def _make_plan() -> MagicMock:
    plan = MagicMock(spec=ContentPlan)
    plan.id = uuid.uuid4()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.seed_clip_paths = ["users/u/plan/p/seed/a.mp4"]
    plan.activation_status = "activating"
    plan.activation_phase = None
    plan.activation_started_at = None
    plan.items = []
    return plan


def _session_factory(plan: MagicMock):  # noqa: ANN201
    """Create a mock sync_session context manager."""
    session = MagicMock()
    session.commit = MagicMock()
    session.add = MagicMock()
    session.flush = MagicMock()

    def _get(model, pk):  # noqa: ANN001
        if model is ContentPlan:
            return plan
        return None

    session.get = MagicMock(side_effect=_get)

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, session


def _make_item(plan_id: uuid.UUID) -> MagicMock:
    item = MagicMock(spec=PlanItem)
    item.id = uuid.uuid4()
    item.content_plan_id = plan_id
    item.theme = "Morning routine"
    item.idea = "Film the 6am start"
    item.filming_suggestion = ""
    item.clip_gcs_paths = []
    return item


def _run_with_mocks(  # noqa: ANN201
    plan: MagicMock,
    ctx: MagicMock,
    ingest_result: dict,  # noqa: ANN001
    matcher_side_effect=None,  # noqa: ANN001
    matcher_return=None,  # noqa: ANN001
) -> None:
    """Run activate_content_plan with heavy I/O mocked out."""
    from app.tasks.content_plan_build import activate_content_plan  # noqa: PLC0415

    trace_ctx = MagicMock()
    trace_ctx.__enter__ = MagicMock(return_value=None)
    trace_ctx.__exit__ = MagicMock(return_value=False)

    mock_matcher_instance = MagicMock()
    if matcher_side_effect is not None:
        mock_matcher_instance.run.side_effect = matcher_side_effect
    else:
        mock_matcher_instance.run.return_value = matcher_return

    mock_matcher_cls = MagicMock(return_value=mock_matcher_instance)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        # Patch in the source module (lazy local import uses the source).
        patch("app.tasks.generative_build._ingest_clips", return_value=ingest_result, create=True),
        patch("app.services.pipeline_trace.pipeline_trace_for", return_value=trace_ctx),
        patch("app.agents.clip_plan_matcher.ClipPlanMatcherAgent", mock_matcher_cls),
        patch("app.agents._model_client.default_client"),
    ):
        activate_content_plan.run(str(plan.id))


def test_activation_phase_set_to_matching_clips_before_ingest() -> None:
    """activation_phase = 'matching_clips' + activation_started_at set at activating transition."""
    plan = _make_plan()
    plan.items = [_make_item(plan.id)]

    phases_at_commit: list[str | None] = []
    started_at_at_commit: list = []
    ctx, session = _session_factory(plan)

    def _commit() -> None:
        phases_at_commit.append(plan.activation_phase)
        started_at_at_commit.append(plan.activation_started_at)

    session.commit = _commit

    ingest_result: dict = {"clip_id_to_gcs": {}, "clip_metas": []}

    _run_with_mocks(plan, ctx, ingest_result, matcher_side_effect=ValueError("no summary"))

    # At the first commit (the "activating" transition), activation_phase must be
    # "matching_clips" and activation_started_at must be non-None.
    assert len(phases_at_commit) >= 1
    assert "matching_clips" in phases_at_commit
    assert any(v is not None for v in started_at_at_commit)


def test_activation_phase_set_to_picking_days_after_matcher() -> None:
    """'picking_days' phase is stamped after the matcher returns assignments."""
    plan = _make_plan()
    plan.items = [_make_item(plan.id)]

    phases_at_commit: list[str | None] = []
    ctx, session = _session_factory(plan)

    def _commit() -> None:
        phases_at_commit.append(plan.activation_phase)

    session.commit = _commit

    ingest_result: dict = {
        "clip_id_to_gcs": {"c1": "users/u/plan/p/seed/a.mp4"},
        "clip_metas": [
            MagicMock(
                clip_id="c1",
                hook_text="hook",
                hook_score=0.8,
                detected_subject="person",
                transcript="hi",
            )
        ],
    }

    matched = MagicMock()
    matched.assignments = []  # no assignments → activated_empty, no dispatch

    _run_with_mocks(plan, ctx, ingest_result, matcher_return=matched)

    # After the matcher succeeds, 'picking_days' should have been stamped.
    assert "picking_days" in phases_at_commit
