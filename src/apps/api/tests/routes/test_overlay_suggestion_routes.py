"""Route tests for the overlay auto-placement SUGGESTION endpoints (plans/005 PR1b,
review C1). Mock-DB style, mirroring test_plan_item_assets.py.

The four routes layered on `_load_owned_item` + `_owned_item_render_job`:
  POST   /{item}/variants/{v}/suggest-overlays          — enqueue the matcher
  GET    /{item}/variants/{v}/overlay-suggestions       — read (DESTRUCTIVE stale-clear)
  POST   /{item}/variants/{v}/overlay-suggestions/apply — burn N into the video
  POST   /{item}/variants/{v}/overlay-suggestions/dismiss — clear pending

Both `_load_owned_item` and `_owned_item_render_job` run a `select(PlanItem)`
(the latter calls the former), so every handler executes TWO PlanItem selects
before its body; `apply` runs a THIRD for the response reload. `db.get()` always
returns the ContentPlan used for the ownership check. The count query in
suggest-overlays reads `.scalar_one()`, so the fake result supports both.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app

SETTINGS = "app.config.settings"
MATCH_TASK = "app.tasks.autoplace.match_overlay_suggestions"
STALE = "app.services.transcript_source.persisted_hash_is_stale"
APPLY_HELPER = "app.services.overlay_apply.apply_suggestions_to_variant"


@pytest.fixture(autouse=True)
def _no_real_broker_publish():
    """suggest_overlays dispatches match_overlay_suggestions.apply_async. Without
    a patch this publishes a REAL Celery message to the shared redis broker (a
    sibling worktree worker would consume it with garbage args). Patch the
    dispatch so tests are isolated AND the contract is assertable."""
    with patch(f"{MATCH_TASK}.apply_async") as m:
        yield m


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _scalar_result(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value)
    return r


def _owned_item(user_id: uuid.UUID, *, job):
    """A fully-populated item so `plan_item_response` (apply route) can serialize
    it without touching the DB again."""
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.current_job = job
    item.current_job_id = job.id if job else None
    item.day_index = 1
    item.theme = "t"
    item.idea = "i"
    item.position = 1
    item.scheduled_date = None
    item.notes = None
    item.scenes = []
    item.filming_suggestion = None
    item.rationale = None
    item.filming_guide = []
    item.clip_gcs_paths = []
    item.clip_assignments = []
    item.user_edited = False
    item.conformance = None
    item.source_idea_seed_id = None
    item.source_idea_seed_text = None
    item.voiceover_gcs_path = None
    item.voiceover_bed_level = None
    item.voiceover_caption_style = None
    item.edit_format = None
    plan = MagicMock()
    plan.user_id = user_id
    return item, plan


def _job(variants: list[dict]) -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.status = "variants_ready"
    job.assembly_plan = {"variants": variants}
    return job


def _db(execute_results: list, plan) -> AsyncMock:
    """db.execute yields the given fake results in order; db.get (ContentPlan
    ownership check) always returns `plan`."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_results)
    db.get = AsyncMock(return_value=plan)
    return db


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _variant(**over) -> dict:
    v = {
        "variant_id": "original_text",
        "music_track_id": None,
        "text_mode": None,
        "render_status": "ready",
    }
    v.update(over)
    return v


# ── flag gating ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    [
        ("post", "/suggest-overlays", None),
        ("get", "/overlay-suggestions", None),
        ("post", "/overlay-suggestions/apply", {"suggestions": []}),
        ("post", "/overlay-suggestions/dismiss", None),
    ],
)
def test_all_suggestion_routes_404_when_flag_off(
    client: TestClient, method: str, path_suffix: str, body: dict | None
):
    user = _user()
    _override(user, AsyncMock())
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", False):
        resp = getattr(client, method)(
            f"/plan-items/{uuid.uuid4()}/variants/original_text{path_suffix}",
            **({"json": body} if body is not None else {}),
        )
    assert resp.status_code == 404


# ── suggest-overlays ──────────────────────────────────────────────────────────


def test_suggest_overlays_music_variant_guard(client: TestClient):
    """A variant with music_track_id set → 400 (auto-placement is speech-only)."""
    user = _user()
    job = _job([_variant(variant_id="song_text", music_track_id="track-1")])
    item, plan = _owned_item(user.id, job=job)
    # Two PlanItem selects (load + render_job), no count query (guard fires first).
    db = _db([_scalar_result(item), _scalar_result(item)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(f"/plan-items/{item.id}/variants/song_text/suggest-overlays")
    assert resp.status_code == 400
    assert "song or lyric variants" in resp.json()["detail"]


def test_suggest_overlays_lyric_variant_guard(client: TestClient):
    """text_mode == 'lyrics' → 400 even without a music_track_id."""
    user = _user()
    job = _job([_variant(variant_id="song_lyrics", text_mode="lyrics")])
    item, plan = _owned_item(user.id, job=job)
    db = _db([_scalar_result(item), _scalar_result(item)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(f"/plan-items/{item.id}/variants/song_lyrics/suggest-overlays")
    assert resp.status_code == 400
    assert "song or lyric variants" in resp.json()["detail"]


@pytest.mark.parametrize("archetype", ["narrated", "subtitled"])
def test_suggest_overlays_caption_archetype_guard(client: TestClient, archetype: str):
    """OV-5 (plan 010): manual lanes are open on caption archetypes, but the AI
    suggest route stays rejected pending a speech-content quality eval."""
    user = _user()
    job = _job([_variant(variant_id=archetype, resolved_archetype=archetype)])
    item, plan = _owned_item(user.id, job=job)
    db = _db([_scalar_result(item), _scalar_result(item)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(f"/plan-items/{item.id}/variants/{archetype}/suggest-overlays")
    assert resp.status_code == 400
    assert "edit format" in resp.json()["detail"]


def test_suggest_overlays_happy_path_commits_and_enqueues(
    client: TestClient, _no_real_broker_publish
):
    user = _user()
    job = _job([_variant()])
    item, plan = _owned_item(user.id, job=job)
    # load + render_job (2 PlanItem selects) + ready_count query (>0).
    db = _db(
        [_scalar_result(item), _scalar_result(item), _scalar_result(1)],
        plan,
    )
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(f"{SETTINGS}.autoplace_queue", "autoplace-jobs"),
    ):
        resp = client.post(f"/plan-items/{item.id}/variants/original_text/suggest-overlays")
    assert resp.status_code == 200
    assert resp.json()["status"] == "matching"
    # persist-first: status committed BEFORE enqueue.
    assert job.assembly_plan["variants"][0]["overlay_suggest_status"] == "matching"
    assert db.commit.await_count >= 1
    # Dispatched exactly once, to the autoplace queue, with job/variant/user args.
    _no_real_broker_publish.assert_called_once()
    call = _no_real_broker_publish.call_args
    assert call.kwargs["args"] == [str(job.id), "original_text", str(user.id)]
    assert call.kwargs["queue"] == "autoplace-jobs"


def test_suggest_overlays_reverts_and_503_when_enqueue_fails(
    client: TestClient, _no_real_broker_publish
):
    """Broker down after 'matching' committed → revert status, surface 503."""
    user = _user()
    job = _job([_variant()])
    item, plan = _owned_item(user.id, job=job)
    db = _db(
        [_scalar_result(item), _scalar_result(item), _scalar_result(2)],
        plan,
    )
    _override(user, db)
    _no_real_broker_publish.side_effect = RuntimeError("broker unreachable")
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(f"{SETTINGS}.autoplace_queue", "autoplace-jobs"),
    ):
        resp = client.post(f"/plan-items/{item.id}/variants/original_text/suggest-overlays")
    assert resp.status_code == 503
    # Status reverted to None (not left stuck at "matching").
    assert job.assembly_plan["variants"][0]["overlay_suggest_status"] is None
    # Committed twice: the persist-first write and the revert write.
    assert db.commit.await_count >= 2


def test_suggest_overlays_zero_ready_assets_400(client: TestClient, _no_real_broker_publish):
    user = _user()
    job = _job([_variant()])
    item, plan = _owned_item(user.id, job=job)
    db = _db(
        [_scalar_result(item), _scalar_result(item), _scalar_result(0)],
        plan,
    )
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(f"/plan-items/{item.id}/variants/original_text/suggest-overlays")
    assert resp.status_code == 400
    assert "at least one visual" in resp.json()["detail"]
    # Guard fires before any enqueue.
    _no_real_broker_publish.assert_not_called()


def test_suggest_overlays_variant_not_found_404(client: TestClient):
    user = _user()
    job = _job([_variant()])
    item, plan = _owned_item(user.id, job=job)
    db = _db([_scalar_result(item), _scalar_result(item)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(f"/plan-items/{item.id}/variants/ghost/suggest-overlays")
    assert resp.status_code == 404
    assert "Variant not found" in resp.json()["detail"]


# ── get overlay-suggestions (read-time stale-clear) ───────────────────────────


def test_get_suggestions_stale_clears_and_commits(client: TestClient):
    """overlay_suggestions present AND persisted hash stale → suggestions/status/
    hash nulled, committed, response stale_cleared=True."""
    user = _user()
    variant = _variant(
        overlay_suggestions=[{"id": "s1"}],
        overlay_suggest_status="ready",
        overlay_suggest_hash="old-hash",
    )
    job = _job([variant])
    item, plan = _owned_item(user.id, job=job)
    db = _db([_scalar_result(item), _scalar_result(item)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(STALE, return_value=True),
    ):
        resp = client.get(f"/plan-items/{item.id}/variants/original_text/overlay-suggestions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stale_cleared"] is True
    assert body["suggestions"] == []
    assert body["status"] is None
    # The persisted variant was actually nulled + committed.
    v = job.assembly_plan["variants"][0]
    assert v["overlay_suggestions"] is None
    assert v["overlay_suggest_status"] is None
    assert v["overlay_suggest_hash"] is None
    assert db.commit.await_count >= 1


def test_get_suggestions_not_stale_returns_set(client: TestClient):
    """Hash matches → no clear, suggestions returned, stale_cleared False, no commit."""
    user = _user()
    variant = _variant(
        overlay_suggestions=[{"id": "s1"}, {"id": "s2"}],
        overlay_suggest_status="ready",
        overlay_suggest_hash="fresh-hash",
        overlay_suggest_wishlist=["a b-roll of a dog"],
    )
    job = _job([variant])
    item, plan = _owned_item(user.id, job=job)
    db = _db([_scalar_result(item), _scalar_result(item)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(STALE, return_value=False),
    ):
        resp = client.get(f"/plan-items/{item.id}/variants/original_text/overlay-suggestions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stale_cleared"] is False
    assert body["status"] == "ready"
    assert len(body["suggestions"]) == 2
    assert body["wishlist"] == ["a b-roll of a dog"]
    db.commit.assert_not_awaited()


# ── apply overlay-suggestions ─────────────────────────────────────────────────


def test_apply_no_fault_400_when_not_dispatched(client: TestClient):
    """dispatched=False (concurrent update) → 400 no-fault copy, no commit."""
    user = _user()
    job = _job([_variant()])
    item, plan = _owned_item(user.id, job=job)
    # load + render_job = 2 PlanItem selects (no response reload — 400 short-circuits).
    db = _db([_scalar_result(item), _scalar_result(item)], plan)
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(
            APPLY_HELPER,
            return_value={"applied": 0, "dropped": 0, "sfx": 0, "dispatched": False},
        ),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/variants/original_text/overlay-suggestions/apply",
            json={"suggestions": [{"id": "s1"}]},
        )
    assert resp.status_code == 400
    assert "just updated" in resp.json()["detail"]
    db.commit.assert_not_awaited()


def test_apply_dispatched_commits_and_returns_item(client: TestClient):
    """dispatched=True → 200 + commit awaited; response reload runs a 3rd select."""
    user = _user()
    job = _job([_variant()])
    item, plan = _owned_item(user.id, job=job)
    # load + render_job + response reload = 3 PlanItem selects.
    db = _db(
        [_scalar_result(item), _scalar_result(item), _scalar_result(item)],
        plan,
    )
    _override(user, db)
    with (
        patch(f"{SETTINGS}.overlay_autoplace_enabled", True),
        patch(
            APPLY_HELPER,
            return_value={"applied": 2, "dropped": 0, "sfx": 1, "dispatched": True},
        ) as helper,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/variants/original_text/overlay-suggestions/apply",
            json={"suggestions": [{"id": "s1"}, {"id": "s2"}]},
        )
    assert resp.status_code == 200
    assert resp.json()["id"] == str(item.id)
    helper.assert_called_once()
    assert db.commit.await_count >= 1


# ── dismiss overlay-suggestions ───────────────────────────────────────────────


def test_dismiss_nulls_fields_and_commits(client: TestClient):
    user = _user()
    variant = _variant(
        overlay_suggestions=[{"id": "s1"}],
        overlay_suggest_status="ready",
        overlay_suggest_hash="h",
        overlay_suggest_wishlist=["x"],
    )
    job = _job([variant])
    item, plan = _owned_item(user.id, job=job)
    db = _db([_scalar_result(item), _scalar_result(item)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(
            f"/plan-items/{item.id}/variants/original_text/overlay-suggestions/dismiss"
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    v = job.assembly_plan["variants"][0]
    assert v["overlay_suggestions"] is None
    assert v["overlay_suggest_status"] is None
    assert v["overlay_suggest_hash"] is None
    assert v["overlay_suggest_wishlist"] is None
    assert db.commit.await_count >= 1


# ── ownership ─────────────────────────────────────────────────────────────────


def test_dismiss_404_when_not_owner(client: TestClient):
    user = _user()
    job = _job([_variant()])
    item, plan = _owned_item(uuid.uuid4(), job=job)  # plan owned by someone else
    db = _db([_scalar_result(item)], plan)
    _override(user, db)
    with patch(f"{SETTINGS}.overlay_autoplace_enabled", True):
        resp = client.post(
            f"/plan-items/{item.id}/variants/original_text/overlay-suggestions/dismiss"
        )
    assert resp.status_code == 404
