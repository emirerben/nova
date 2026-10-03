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


@pytest.fixture(autouse=True)
def _sent(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    sent = MagicMock()
    monkeypatch.setattr(task.analyze_kria_clips, "apply_async", sent)
    return sent


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
    # each failed attempt is retried once (2 calls) and a clip settles after 3 attempts
    assert sorted(calls) == ["m1"] + ["m2"] * 6 + ["m3"]
    assert out["analyzed"] == 2 and out["failed"] == 3
    stored = _stored(item_id)
    assert "understanding" in stored["m1"] and "understanding" not in stored["m2"]
    assert stored["m2"]["understanding_attempts"] == 3, "persistent failure must settle"


def test_budget_stop_ends_the_run_and_resumes_later(monkeypatch, _sent) -> None:
    from app.agents._runtime import AiBudgetExceededError

    item_id = _seed([_row(1), _row(2)])
    calls: list[str] = []

    def over_budget(raw, _pool, **_kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(raw["media_id"])
        raise AiBudgetExceededError(scope="monthly", reset_at="x", cached_behavior_available=False)

    monkeypatch.setattr("app.services.creator_clip_analysis.analyze_clip_assignment", over_budget)
    out = analyze_kria_clips.apply(args=[str(item_id)]).get()
    assert set(calls) <= {"m1", "m2"} and out["status"] == "done" and out["stopped"]
    resumes = [c for c in _sent.call_args_list if c.kwargs.get("countdown", 0) >= 60]
    assert len(resumes) == 1, "a quota stop must schedule a resume, not end silently"
    assert resumes[0].kwargs["args"][1] == 1  # chain counter


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


def test_every_clip_is_analysed_beyond_the_old_24_cap(monkeypatch) -> None:
    item_id = _seed([_row(i) for i in range(47)])
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.creator_clip_analysis.analyze_clip_assignment", _analyzer(calls)
    )
    out = analyze_kria_clips.apply(args=[str(item_id)]).get()
    assert out["analyzed"] == 47 and len(set(calls)) == 47
    assert all("understanding" in a for a in _stored(item_id).values())


def test_clips_are_analysed_with_bounded_concurrency(monkeypatch) -> None:
    import threading
    import time

    live = peak = 0
    lock = threading.Lock()

    def slow(raw, _pool, **_kw):  # noqa: ANN001, ANN003, ANN202
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return _analyzer([])(raw, _pool)

    item_id = _seed([_row(i) for i in range(12)])
    monkeypatch.setattr("app.services.creator_clip_analysis.analyze_clip_assignment", slow)
    analyze_kria_clips.apply(args=[str(item_id)]).get()
    assert 2 <= peak <= 4, peak


def test_transient_failure_is_retried_once(monkeypatch) -> None:
    item_id = _seed([_row(1)])
    seen: list[str] = []

    def flaky(raw, _pool, **_kw):  # noqa: ANN001, ANN003, ANN202
        seen.append(raw["media_id"])
        if len(seen) == 1:
            raise RuntimeError("transient")
        return _analyzer([])(raw, _pool)

    monkeypatch.setattr("app.services.creator_clip_analysis.analyze_clip_assignment", flaky)
    out = analyze_kria_clips.apply(args=[str(item_id)]).get()
    assert out["analyzed"] == 1 and out["failed"] == 0 and len(seen) == 2


def test_analysed_but_empty_clip_settles_and_is_not_requeried(monkeypatch) -> None:
    item_id = _seed([_row(1)])
    calls: list[str] = []

    def blank(raw, _pool, **_kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(raw["media_id"])
        return {**raw, "generation": "7", "analysis": {"understanding": {"kind": "video"}}}, None

    monkeypatch.setattr("app.services.creator_clip_analysis.analyze_clip_assignment", blank)
    analyze_kria_clips.apply(args=[str(item_id)]).get()
    analyze_kria_clips.apply(args=[str(item_id)]).get()
    assert calls == ["m1"]


def test_understanding_incomplete_helper() -> None:
    from app.services.clip_understanding import understanding_incomplete

    assert understanding_incomplete(None)
    assert understanding_incomplete({"source": "probe_only"})
    assert not understanding_incomplete({"understanding": {"summary": "football match"}})
    assert not understanding_incomplete({"understanding_attempts": 3})


def test_analysis_prompt_still_demands_a_concrete_sport_activity() -> None:
    from pathlib import Path

    text = (Path(task.__file__).parents[2] / "prompts" / "analyze_clip.txt").read_text()
    assert "name the specific sport" in text
