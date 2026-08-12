"""Ownership and epoch fences for the pre-Job transcript analysis task."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models import ContentPlan, PlanItem
from app.services.content_plan_persona import require_plan_persona_owned
from app.tasks import transcript_analyze as task_module


class _Session:
    def __init__(self, item, plan):  # noqa: ANN001
        self.item = item
        self.plan = plan

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, model, _pk, **_kwargs):
        return {PlanItem: self.item, ContentPlan: self.plan}.get(model)


def _owned_rows(monkeypatch: pytest.MonkeyPatch):
    item = SimpleNamespace(
        id=uuid.uuid4(),
        content_plan_id=uuid.uuid4(),
        clip_gcs_paths=["users/owner/plan/item/owned.mp4"],
    )
    plan = SimpleNamespace(
        id=item.content_plan_id,
        user_id=uuid.uuid4(),
        persona_id=uuid.uuid4(),
        ownership_epoch=5,
        ownership_quarantined_at=None,
    )
    persona = SimpleNamespace(id=plan.persona_id, user_id=plan.user_id)
    monkeypatch.setattr(task_module, "sync_session", lambda: _Session(item, plan))
    monkeypatch.setattr(
        task_module,
        "load_owned_plan_persona_sync",
        lambda _db, current_plan, *, for_update=False: require_plan_persona_owned(
            current_plan, persona
        ),
    )
    return item, plan, persona


def test_fence_locks_plan_persona_item_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        persona_id=uuid.uuid4(),
        ownership_epoch=8,
        ownership_quarantined_at=None,
    )
    item = SimpleNamespace(id=uuid.uuid4(), content_plan_id=plan.id)
    events: list[str] = []

    class _FenceSession:
        def get(self, model, _pk, **kwargs):  # noqa: ANN001
            assert kwargs == {"with_for_update": True}
            events.append(model.__name__)
            return {ContentPlan: plan, PlanItem: item}[model]

    monkeypatch.setattr(
        task_module,
        "load_owned_plan_persona_sync",
        lambda *_a, **_kw: events.append("Persona") or object(),
    )

    assert task_module._lock_owned_plan_item(
        _FenceSession(),
        plan_id=plan.id,
        item_id=item.id,
        expected_epoch=8,
    ) == (plan, item)
    assert events == ["ContentPlan", "Persona", "PlanItem"]


@pytest.mark.parametrize("fence", ["mismatch", "quarantine"])
def test_invalid_owner_exits_before_signing_probe_agent_or_redis(
    monkeypatch: pytest.MonkeyPatch,
    fence: str,
) -> None:
    item, plan, persona = _owned_rows(monkeypatch)
    if fence == "mismatch":
        persona.user_id = uuid.uuid4()
    else:
        plan.ownership_quarantined_at = object()
    probe = MagicMock()
    summarize = MagicMock()
    persist = MagicMock()
    monkeypatch.setattr(task_module, "_probe_total_duration", probe)
    monkeypatch.setattr(task_module, "summarize_footage", summarize)
    monkeypatch.setattr(task_module, "put_analyze", persist)

    task_module.analyze_transcript_footage.run(
        "analysis-1",
        ["users/foreign/secret.mp4"],
        str(item.id),
    )

    probe.assert_not_called()
    summarize.assert_not_called()
    persist.assert_not_called()


@pytest.mark.parametrize("fence_change", ["epoch", "quarantine"])
def test_result_is_discarded_when_fence_changes_while_external_work_is_paused(
    monkeypatch: pytest.MonkeyPatch,
    fence_change: str,
) -> None:
    item, plan, _persona = _owned_rows(monkeypatch)

    def _probe(_paths):  # noqa: ANN001
        if fence_change == "epoch":
            plan.ownership_epoch += 1
        else:
            plan.ownership_quarantined_at = object()
        return 12.5

    persist = MagicMock()
    monkeypatch.setattr(task_module, "_probe_total_duration", _probe)
    monkeypatch.setattr(task_module, "summarize_footage", lambda _paths: "owned footage")
    monkeypatch.setattr(task_module, "put_analyze", persist)

    task_module.analyze_transcript_footage.run("analysis-2", [], str(item.id))

    persist.assert_not_called()


def test_task_uses_live_owned_item_paths_and_publishes_under_final_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, _plan, _persona = _owned_rows(monkeypatch)
    probe = MagicMock(return_value=7.25)
    summarize = MagicMock(return_value="owned footage")
    persist = MagicMock()
    monkeypatch.setattr(task_module, "_probe_total_duration", probe)
    monkeypatch.setattr(task_module, "summarize_footage", summarize)
    monkeypatch.setattr(task_module, "put_analyze", persist)

    task_module.analyze_transcript_footage.run(
        "analysis-3",
        ["users/foreign/secret.mp4"],
        str(item.id),
    )

    owned_paths = ["users/owner/plan/item/owned.mp4"]
    probe.assert_called_once_with(owned_paths)
    summarize.assert_called_once_with(owned_paths)
    persist.assert_called_once_with(
        str(item.id),
        "analysis-3",
        {"status": "ready", "duration_s": 7.25, "footage_summary": "owned footage"},
    )
