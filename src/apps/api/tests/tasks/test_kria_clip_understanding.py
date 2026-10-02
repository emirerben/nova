"""KRI-219: background clip analysis for Kria threads (mocked analyzer, real test DB)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.database import sync_session
from app.models import ContentPlan, Persona, PlanItem, User
from app.tasks import kria_clip_understanding as task
from app.tasks.kria_clip_understanding import analyze_kria_clips, enqueue_clip_understanding


def _seed(assignments: list[dict]) -> uuid.UUID:
    user_id, persona_id, plan_id, item_id = (uuid.uuid4() for _ in range(4))
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        db.add(Persona(id=persona_id, user_id=user_id, persona_status="ready", persona={}))
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=user_id, persona_id=persona_id))
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="x",
                item_status="awaiting_clips",
                clip_assignments=assignments,
            )
        )
        db.commit()
    return item_id


def _row(i: int, **extra) -> dict:  # noqa: ANN003
    return {
        "gcs_path": f"users/u/analysis-proxy-ios-{i}.mp4",
        "media_id": f"m{i}",
        "kind": "video",
        "storage_generation": "7",
        **extra,
    }


def _analyzer(calls: list[str], *, fail: set[str] = frozenset()):  # noqa: ANN202
    def fake(raw, _pool, **_kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(raw["media_id"])
        if raw["media_id"] in fail:
            raise RuntimeError("provider boom")
        return (
            {
                **raw,
                "generation": "7",
                "analysis": {
                    "understanding": {"kind": "video", "summary": f"saw {raw['media_id']}"}
                },
            },
            MagicMock(),
        )

    return fake


@pytest.fixture(autouse=True)
def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")


def _stored(item_id: uuid.UUID) -> dict[str, dict]:
    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        return {r["media_id"]: r.get("analysis") or {} for r in item.clip_assignments}


def test_analyses_only_clips_without_understanding_and_keeps_facts(monkeypatch) -> None:
    done = _row(2, analysis={"understanding": {"kind": "video", "summary": "already"}})
    item_id = _seed(
        [_row(1, analysis={"clip_facts": [{"kind": "capture_time", "value": "x"}]}), done]
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.creator_clip_analysis.analyze_clip_assignment", _analyzer(calls)
    )
    out = analyze_kria_clips.apply(args=[str(item_id)]).get()
    assert calls == ["m1"] and out["analyzed"] == 1
    stored = _stored(item_id)
    assert stored["m1"]["understanding"]["summary"] == "saw m1"
    assert stored["m1"]["clip_facts"], "stored facts must survive the merge"
    assert stored["m2"]["understanding"]["summary"] == "already"


def test_second_run_is_idempotent(monkeypatch) -> None:
    item_id = _seed([_row(1)])
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.creator_clip_analysis.analyze_clip_assignment", _analyzer(calls)
    )
    analyze_kria_clips.apply(args=[str(item_id)]).get()
    analyze_kria_clips.apply(args=[str(item_id)]).get()
    assert calls == ["m1"]


def test_one_failing_clip_does_not_stop_the_rest(monkeypatch) -> None:
    item_id = _seed([_row(1), _row(2), _row(3)])
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.creator_clip_analysis.analyze_clip_assignment", _analyzer(calls, fail={"m2"})
    )
    out = analyze_kria_clips.apply(args=[str(item_id)]).get()
    assert calls == ["m1", "m2", "m3"] and out["analyzed"] == 2 and out["failed"] == 1
    stored = _stored(item_id)
    assert "understanding" in stored["m1"] and "understanding" not in stored["m2"]


def test_budget_stop_ends_the_run_without_retrying(monkeypatch) -> None:
    from app.agents._runtime import AiBudgetExceededError

    item_id = _seed([_row(1), _row(2)])
    calls: list[str] = []

    def over_budget(raw, _pool, **_kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(raw["media_id"])
        raise AiBudgetExceededError(scope="monthly", reset_at="x", cached_behavior_available=False)

    monkeypatch.setattr("app.services.creator_clip_analysis.analyze_clip_assignment", over_budget)
    out = analyze_kria_clips.apply(args=[str(item_id)]).get()
    assert calls == ["m1"] and out["status"] == "done" and out["failed"] == 1


def test_a_replaced_object_generation_is_not_overwritten(monkeypatch) -> None:
    item_id = _seed([_row(1)])

    def stale(raw, _pool, **_kw):  # noqa: ANN001, ANN003, ANN202
        return (
            {**raw, "generation": "8", "analysis": {"understanding": {"summary": "old"}}},
            MagicMock(),
        )

    monkeypatch.setattr("app.services.creator_clip_analysis.analyze_clip_assignment", stale)
    analyze_kria_clips.apply(args=[str(item_id)]).get()
    assert "understanding" not in _stored(item_id)["m1"]


@pytest.mark.parametrize("flag,key", [(False, "k"), (True, "")])
def test_flag_or_missing_gemini_key_does_nothing(monkeypatch, flag, key) -> None:
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", flag)
    monkeypatch.setattr(settings, "gemini_api_key", key)
    item_id = _seed([_row(1)])
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.creator_clip_analysis.analyze_clip_assignment", _analyzer(calls)
    )
    assert analyze_kria_clips.apply(args=[str(item_id)]).get()["status"] == "disabled"
    assert calls == []
    sent = MagicMock()
    monkeypatch.setattr(task.analyze_kria_clips, "apply_async", sent)
    enqueue_clip_understanding(item_id)
    sent.assert_not_called()


def test_enqueue_goes_to_the_analysis_queue_and_never_raises(monkeypatch) -> None:
    sent = MagicMock()
    monkeypatch.setattr(task.analyze_kria_clips, "apply_async", sent)
    item_id = uuid.uuid4()
    enqueue_clip_understanding(item_id)
    assert sent.call_args.kwargs["queue"] == settings.pool_asset_analysis_queue
    sent.side_effect = RuntimeError("broker down")
    enqueue_clip_understanding(item_id)  # swallowed


def test_task_is_registered_and_routed() -> None:
    from app.worker import celery_app

    assert "app.tasks.kria_clip_understanding" in celery_app.conf.include
    assert celery_app.conf.task_routes["tasks.analyze_kria_clips"] == {
        "queue": settings.pool_asset_analysis_queue
    }
