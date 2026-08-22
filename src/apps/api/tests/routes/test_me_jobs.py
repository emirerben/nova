"""Route tests for the per-user library surface and editor promotion.

Mock-DB style, mirroring test_plan_item_variant_edit.py. The library is strictly
scoped to the authenticated user (no user_id input to forge), so these assert:
derived status + preview-url extraction across job modes, keyset pagination, and
that plan attachment and editor promotion link both FK sides only when the job
and plan belong to the caller (404 — not 403 — on cross-user references).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _job(
    *,
    user_id: uuid.UUID,
    status: str = "variants_ready",
    assembly_plan: dict | None = None,
    mode: str | None = "generative",
    job_type: str = "default",
    content_plan_item_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    all_candidates: dict | None = None,
    failure_reason: str | None = None,
) -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.user_id = user_id
    job.status = status
    job.assembly_plan = assembly_plan if assembly_plan is not None else {}
    job.mode = mode
    job.job_type = job_type
    job.content_plan_item_id = content_plan_item_id
    job.content_plan_ownership_epoch = None
    job.all_candidates = all_candidates if all_candidates is not None else {}
    job.failure_reason = failure_reason
    job.created_at = created_at or datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    job.finished_at = (
        datetime(2026, 5, 30, 12, 1, 0, tzinfo=UTC)
        if status in {"variants_ready", "variants_ready_partial", "done", "clips_ready"}
        else None
    )
    return job


def _scalar(value) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    return r


def _scalars(rows: list) -> MagicMock:
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    return r


def _rows(rows: list) -> MagicMock:
    """A result whose `.all()` yields tuple rows (the batched feedback lookup)."""
    r = MagicMock()
    r.all = MagicMock(return_value=rows)
    return r


def _db(execute_results: list) -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_results)
    return db


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


client = TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


# ── GET /me/jobs ──────────────────────────────────────────────────────────────


def test_list_returns_users_jobs_with_derived_status_and_preview(monkeypatch) -> None:
    user = _user()
    ready_variant_job = _job(
        user_id=user.id,
        status="variants_ready",
        assembly_plan={
            "variants": [
                {"variant_id": "song_lyrics", "render_status": "failed", "output_url": None},
                {
                    "variant_id": "song_text",
                    "render_status": "ready",
                    "output_url": "gs://x/a.mp4",
                    "video_path": "generative-jobs/j/song-text.mp4",
                },
            ]
        },
    )
    single_output_job = _job(
        user_id=user.id,
        status="template_ready",
        mode=None,
        job_type="template",
        assembly_plan={
            "output_url": "gs://x/tpl.mp4",
            "output_path": "template-jobs/j/output.mp4",
        },
    )
    monkeypatch.setattr(
        "app.routes.me.signed_download_url",
        lambda path, filename, expiration_minutes: f"https://download.example/{filename}",
    )
    monkeypatch.setattr(
        "app.routes.me.signed_get_url",
        lambda path, ttl: f"https://resigned.example/{path}",
    )
    db = _db([_scalars([ready_variant_job, single_output_job]), _rows([]), _scalars([])])
    _override(user, db)

    resp = client.get("/me/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert [j["status"] for j in body["jobs"]] == ["ready", "ready"]
    # Re-signed fresh from `video_path`/`output_path`, not the stale stored URL.
    assert (
        body["jobs"][0]["output_url"] == "https://resigned.example/generative-jobs/j/song-text.mp4"
    )
    assert body["jobs"][0]["download_url"].endswith(".mp4")
    assert body["jobs"][1]["output_url"] == "https://resigned.example/template-jobs/j/output.mp4"
    assert body["jobs"][1]["download_url"].endswith(".mp4")
    assert body["jobs"][1]["mode"] == "template"  # falls back to job_type
    assert body["next_cursor"] is None


def test_list_generating_job_has_no_preview_url() -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        status="processing",
        assembly_plan={"variants": [{"variant_id": "song_text", "render_status": "rendering"}]},
    )
    db = _db([_scalars([job]), _rows([]), _scalars([])])
    _override(user, db)

    resp = client.get("/me/jobs")
    assert resp.status_code == 200
    j = resp.json()["jobs"][0]
    assert j["status"] == "generating"
    assert j["output_url"] is None


def test_list_query_excludes_manual_drafts() -> None:
    user = _user()
    db = _db([_scalars([])])
    _override(user, db)

    resp = client.get("/me/jobs")

    assert resp.status_code == 200
    statement = db.execute.await_args_list[0].args[0]
    assert "draft" in statement.compile().params.values()


def test_list_exposes_only_structured_failure_taxonomy() -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        status="variants_failed",
        failure_reason="encoder_timeout",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "later",
                    "rank": 2,
                    "render_status": "failed",
                    "error_class": "unknown",
                    "error": "private worker trace",
                },
                {
                    "variant_id": "first",
                    "rank": 1,
                    "render_status": "failed",
                    "error_class": "timeout",
                    "error": "ffmpeg command and bucket path",
                },
            ]
        },
    )
    db = _db([_scalars([job]), _rows([]), _scalars([])])
    _override(user, db)

    resp = client.get("/me/jobs")
    assert resp.status_code == 200
    item = resp.json()["jobs"][0]
    assert item["failure_reason"] == "encoder_timeout"
    assert item["error_class"] == "timeout"
    assert "error" not in item


def test_list_keeps_playback_when_download_signing_fails(monkeypatch) -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        status="variants_ready",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": "ready",
                    "output_url": "https://play.example/video.mp4",
                    "video_path": "generative-jobs/j/song-text.mp4",
                }
            ]
        },
    )
    monkeypatch.setattr(
        "app.routes.me.signed_download_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("signer down")),
    )
    monkeypatch.setattr(
        "app.routes.me.signed_get_url",
        lambda path, ttl: "https://play.example/video.mp4",
    )
    db = _db([_scalars([job]), _rows([]), _scalars([])])
    _override(user, db)

    resp = client.get("/me/jobs")
    assert resp.status_code == 200
    item = resp.json()["jobs"][0]
    assert item["output_url"] == "https://play.example/video.mp4"
    assert item["download_url"] is None


def test_list_resigns_playback_url_from_video_path(monkeypatch) -> None:
    """The persisted per-variant `output_url` is a 1-day-TTL signature minted at
    render time; the library must re-sign fresh from `video_path` on every read
    (mirrors `_variants_for_response` in routes/generative_jobs.py) so playback
    never 400s past 24h."""
    user = _user()
    stale_variant = {
        "variant_id": "song_text",
        "render_status": "ready",
        "output_url": "https://stale.example/out.mp4?Expires=old",
        "video_path": "generative-jobs/x/out.mp4",
    }
    job = _job(
        user_id=user.id,
        status="variants_ready",
        assembly_plan={"variants": [dict(stale_variant)]},
    )
    resign = MagicMock(return_value="https://fresh.example/resigned.mp4")
    monkeypatch.setattr("app.routes.me.signed_get_url", resign)
    db = _db([_scalars([job]), _rows([]), _scalars([])])
    _override(user, db)

    resp = client.get("/me/jobs")

    assert resp.status_code == 200
    item = resp.json()["jobs"][0]
    assert item["output_url"] == "https://fresh.example/resigned.mp4"
    resign.assert_called_once_with("generative-jobs/x/out.mp4", 360)
    # The raw variant dict on the job must never be mutated with the fresh URL.
    assert job.assembly_plan["variants"][0]["output_url"] == stale_variant["output_url"]


def test_list_resigns_template_job_playback_url_from_output_path(monkeypatch) -> None:
    """Same re-sign contract for the single-output (template/music) job shape."""
    user = _user()
    stale_output_url = "https://stale.example/tpl.mp4?Expires=old"
    job = _job(
        user_id=user.id,
        status="template_ready",
        mode=None,
        job_type="template",
        assembly_plan={
            "output_url": stale_output_url,
            "output_path": "dev-user/x/out.mp4",
        },
    )
    resign = MagicMock(return_value="https://fresh.example/resigned-tpl.mp4")
    monkeypatch.setattr("app.routes.me.signed_get_url", resign)
    db = _db([_scalars([job]), _rows([]), _scalars([])])
    _override(user, db)

    resp = client.get("/me/jobs")

    assert resp.status_code == 200
    item = resp.json()["jobs"][0]
    assert item["output_url"] == "https://fresh.example/resigned-tpl.mp4"
    resign.assert_called_once_with("dev-user/x/out.mp4", 360)
    assert job.assembly_plan["output_url"] == stale_output_url


def test_list_keeps_stored_playback_url_when_resign_fails(monkeypatch) -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        status="variants_ready",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": "ready",
                    "output_url": "https://stored.example/video.mp4",
                    "video_path": "generative-jobs/x/out.mp4",
                }
            ]
        },
    )
    monkeypatch.setattr(
        "app.routes.me.signed_get_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("signer down")),
    )
    db = _db([_scalars([job]), _rows([]), _scalars([])])
    _override(user, db)

    resp = client.get("/me/jobs")

    assert resp.status_code == 200
    item = resp.json()["jobs"][0]
    assert item["output_url"] == "https://stored.example/video.mp4"


def test_list_does_not_resign_without_output_path() -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        status="done",
        assembly_plan={"output_url": "https://stored.example/no-path.mp4"},
    )
    db = _db([_scalars([job]), _rows([]), _scalars([])])
    _override(user, db)

    resp = client.get("/me/jobs")

    assert resp.status_code == 200
    item = resp.json()["jobs"][0]
    assert item["output_url"] == "https://stored.example/no-path.mp4"


def test_list_forged_user_id_query_param_is_ignored() -> None:
    """No user_id input exists on the route, so a forged ?user_id is inert —
    the scope always comes from the authenticated dependency."""
    user = _user()
    own_job = _job(user_id=user.id, status="done", assembly_plan={"output_url": "gs://x/own.mp4"})
    db = _db([_scalars([own_job]), _rows([]), _scalars([])])
    _override(user, db)

    resp = client.get(f"/me/jobs?user_id={uuid.uuid4()}")
    assert resp.status_code == 200
    # The DB query was built from user.id, not the forged param: we return the
    # rows the (overridden) session yields, and the route never reads ?user_id.
    assert [j["id"] for j in resp.json()["jobs"]] == [str(own_job.id)]


def test_list_paginates_with_next_cursor() -> None:
    user = _user()
    older = _job(user_id=user.id, created_at=datetime(2026, 5, 29, tzinfo=UTC))
    newer = _job(user_id=user.id, created_at=datetime(2026, 5, 30, tzinfo=UTC))
    # limit=1 → route fetches limit+1=2 rows, returns 1, emits cursor from it.
    db = _db([_scalars([newer, older]), _rows([]), _scalars([])])
    _override(user, db)

    resp = client.get("/me/jobs?limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["id"] == str(newer.id)
    assert body["next_cursor"] == newer.created_at.isoformat()


def test_list_rejects_bad_cursor() -> None:
    user = _user()
    db = _db([])
    _override(user, db)
    resp = client.get("/me/jobs?cursor=not-a-date")
    assert resp.status_code == 400


def test_list_populates_feedback_signal_from_batch_lookup() -> None:
    user = _user()
    liked = _job(user_id=user.id, status="done", assembly_plan={"output_url": "gs://x/a.mp4"})
    none = _job(user_id=user.id, status="done", assembly_plan={"output_url": "gs://x/b.mp4"})
    # The batched second query returns (job_id, signal) tuples for thumbed jobs only.
    db = _db([_scalars([liked, none]), _rows([(liked.id, "up")]), _scalars([])])
    _override(user, db)

    resp = client.get("/me/jobs")
    assert resp.status_code == 200
    by_id = {j["id"]: j["feedback_signal"] for j in resp.json()["jobs"]}
    assert by_id[str(liked.id)] == "up"
    assert by_id[str(none.id)] is None


# ── POST /me/jobs/{id}/add-to-plan ────────────────────────────────────────────


def test_add_to_plan_links_both_fk_sides() -> None:
    user = _user()
    job = _job(user_id=user.id, status="done", assembly_plan={"output_url": "gs://x/o.mp4"})
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user.id
    plan.persona_id = uuid.uuid4()
    plan.ownership_quarantined_at = None
    persona = MagicMock(id=plan.persona_id, user_id=user.id)
    item = MagicMock()
    item.id = uuid.uuid4()
    db = _db(
        [
            _scalar(job),
            _scalar(plan),
            _scalar(persona),
            _scalar(item),
            _scalar(job),
        ]
    )
    _override(user, db)

    resp = client.post(f"/me/jobs/{job.id}/add-to-plan", json={"day_index": 3})
    assert resp.status_code == 200
    assert resp.json()["content_plan_item_id"] == str(item.id)
    # Both sides of the circular FK pair were set, then committed.
    assert item.current_job_id == job.id
    assert job.content_plan_item_id == item.id
    db.commit.assert_awaited_once()


def test_add_to_plan_404_when_job_not_owned() -> None:
    user = _user()
    job = _job(user_id=uuid.uuid4())  # a different user's job
    db = _db([_scalar(job)])
    _override(user, db)
    resp = client.post(f"/me/jobs/{job.id}/add-to-plan", json={"day_index": 1})
    assert resp.status_code == 404


def test_add_to_plan_404_when_no_plan() -> None:
    user = _user()
    job = _job(user_id=user.id)
    db = _db([_scalar(job), _scalar(None)])  # no content plan
    _override(user, db)
    resp = client.post(f"/me/jobs/{job.id}/add-to-plan", json={"day_index": 1})
    assert resp.status_code == 404


def test_add_to_plan_404_when_day_missing() -> None:
    user = _user()
    job = _job(user_id=user.id)
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user.id
    plan.persona_id = uuid.uuid4()
    plan.ownership_quarantined_at = None
    persona = MagicMock(id=plan.persona_id, user_id=user.id)
    db = _db([_scalar(job), _scalar(plan), _scalar(persona), _scalar(None)])  # day not found
    _override(user, db)
    resp = client.post(f"/me/jobs/{job.id}/add-to-plan", json={"day_index": 99})
    assert resp.status_code == 404


def test_add_to_plan_400_on_bad_job_id() -> None:
    user = _user()
    db = _db([])
    _override(user, db)
    resp = client.post("/me/jobs/not-a-uuid/add-to-plan", json={"day_index": 1})
    assert resp.status_code == 400


# ── POST /me/jobs/{id}/open-in-editor ──────────────────────────────────────


def _plan(user_id: uuid.UUID) -> MagicMock:
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user_id
    plan.persona_id = uuid.uuid4()
    plan.ownership_epoch = 7
    plan.ownership_quarantined_at = None
    return plan


def _persona(plan: MagicMock, user_id: uuid.UUID) -> MagicMock:
    return MagicMock(id=plan.persona_id, user_id=user_id)


def _ready_variants() -> list[dict]:
    return [
        {
            "variant_id": "rank-three",
            "rank": 3,
            "render_status": "ready",
            "output_url": "https://play.example/three.mp4",
        },
        {
            "variant_id": "rank-one",
            "rank": 1,
            "render_status": "ready",
            "output_url": "https://play.example/one.mp4",
        },
        {
            "variant_id": "failed",
            "rank": 0,
            "render_status": "failed",
        },
    ]


def test_open_in_editor_creates_unscheduled_item_and_copies_job_metadata() -> None:
    user = _user()
    clip_paths = [
        "generative-jobs/u/sources/a.mp4",
        "generative-jobs/u/sources/b.mov",
    ]
    job = _job(
        user_id=user.id,
        assembly_plan={"variants": _ready_variants()},
        all_candidates={
            "clip_paths": clip_paths,
            "edit_format": "day_vlog",
            "montage_preset": "masonry",
            "landscape_fit": "fit",
            "filming_guide": [{"what": "Arrival", "how": "Wide", "duration_s": 3}],
            "clip_notes": {clip_paths[0]: "Use this as the hook"},
            "voiceover_gcs_path": "users/u/voiceovers/final.m4a",
            "voiceover_bed_level": 0.35,
            "voiceover_caption_style": "word",
            "persona": {"theme": "Lisbon diary", "idea": "Make it feel intimate"},
        },
    )
    plan = _plan(user.id)
    existing = MagicMock(id=uuid.uuid4(), position=4)
    db = _db(
        [
            _scalar(job),
            _scalar(plan),
            _scalar(_persona(plan, user.id)),
            _scalars([existing]),
            _scalar(job),
        ]
    )
    _override(user, db)

    resp = client.post(
        f"/me/jobs/{job.id}/open-in-editor",
        json={"title": "  My Lisbon cut  "},
    )

    assert resp.status_code == 200
    assert resp.json()["variant_id"] == "rank-one"
    created = db.add.call_args.args[0]
    assert resp.json()["plan_item_id"] == str(created.id)
    assert created.content_plan_id == plan.id
    assert created.day_index is None
    assert created.position == 5
    assert created.content_mode == "existing_footage"
    assert created.theme == "My Lisbon cut"
    assert created.idea == "My Lisbon cut"
    assert created.notes == "Make it feel intimate"
    assert created.edit_format == "day_vlog"
    assert created.montage_preset == "masonry"
    assert created.landscape_fit == "fit"
    assert created.clip_gcs_paths == clip_paths
    assert created.clip_assignments == [
        {
            "gcs_path": clip_paths[0],
            "shot_id": None,
            "user_note": "Use this as the hook",
        },
        {"gcs_path": clip_paths[1], "shot_id": None},
    ]
    assert created.voiceover_gcs_path == "users/u/voiceovers/final.m4a"
    assert created.audio_mode == "voiceover"
    assert created.voiceover_bed_level == 0.35
    assert created.voiceover_caption_style == "word"
    assert created.current_job_id == job.id
    assert job.mode == "content_plan"
    assert job.content_plan_item_id == created.id
    assert job.content_plan_ownership_epoch == plan.ownership_epoch
    db.commit.assert_awaited_once()


def test_open_in_editor_is_idempotent_for_existing_link() -> None:
    user = _user()
    item = MagicMock()
    item.id = uuid.uuid4()
    item.position = 2
    item.current_job_id = None  # repair a legacy one-sided circular link
    job = _job(
        user_id=user.id,
        content_plan_item_id=item.id,
        assembly_plan={"variants": _ready_variants()},
    )
    plan = _plan(user.id)
    db = _db(
        [
            _scalar(job),
            _scalar(plan.id),
            _scalar(plan),
            _scalar(_persona(plan, user.id)),
            _scalars([item]),
            _scalar(job),
        ]
    )
    _override(user, db)

    resp = client.post(f"/me/jobs/{job.id}/open-in-editor", json={})

    assert resp.status_code == 200
    assert resp.json() == {
        "plan_item_id": str(item.id),
        "variant_id": "rank-one",
    }
    assert item.current_job_id == job.id
    assert job.mode == "content_plan"
    assert job.content_plan_ownership_epoch == plan.ownership_epoch
    db.add.assert_not_called()
    db.commit.assert_awaited_once()


def test_open_in_editor_duplicate_does_not_write_when_both_links_exist() -> None:
    user = _user()
    plan = _plan(user.id)
    item = MagicMock()
    item.id = uuid.uuid4()
    item.position = 2
    job = _job(
        user_id=user.id,
        mode="content_plan",
        content_plan_item_id=item.id,
        assembly_plan={"variants": _ready_variants()},
    )
    job.content_plan_ownership_epoch = plan.ownership_epoch
    item.current_job_id = job.id
    db = _db(
        [
            _scalar(job),
            _scalar(plan.id),
            _scalar(plan),
            _scalar(_persona(plan, user.id)),
            _scalars([item]),
            _scalar(job),
        ]
    )
    _override(user, db)

    resp = client.post(f"/me/jobs/{job.id}/open-in-editor")

    assert resp.status_code == 200
    assert resp.json()["plan_item_id"] == str(item.id)
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


def test_open_in_editor_cross_user_job_is_404_without_plan_lookup() -> None:
    user = _user()
    job = _job(
        user_id=uuid.uuid4(),
        assembly_plan={"variants": _ready_variants()},
    )
    db = _db([_scalar(job)])
    _override(user, db)

    resp = client.post(f"/me/jobs/{job.id}/open-in-editor", json={})

    assert resp.status_code == 404
    assert db.execute.await_count == 1


def test_open_in_editor_provisions_minimal_plan_for_first_time_creator() -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        assembly_plan={"variants": _ready_variants()},
    )
    db = _db(
        [
            _scalar(job),
            _scalar(None),
            _scalar(user),
            _scalar(None),
            _scalar(None),
            _scalars([]),
            _scalar(job),
        ]
    )
    _override(user, db)

    resp = client.post(f"/me/jobs/{job.id}/open-in-editor", json={})

    assert resp.status_code == 200
    added = [call.args[0] for call in db.add.call_args_list]
    assert len(added) == 3
    persona, plan, item = added
    assert persona.user_id == user.id
    assert persona.persona_status == "ready"
    assert plan.user_id == user.id
    assert plan.persona_id == persona.id
    assert plan.plan_status == "ready"
    assert item.content_plan_id == plan.id
    assert job.content_plan_item_id == item.id
    assert job.content_plan_ownership_epoch == 0
    db.commit.assert_awaited_once()


def test_open_in_editor_unfinished_job_has_stable_409() -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        status="processing",
        assembly_plan={
            "variants": [{"variant_id": "one", "rank": 1, "render_status": "rendering"}]
        },
    )
    db = _db([_scalar(job)])
    _override(user, db)

    resp = client.post(f"/me/jobs/{job.id}/open-in-editor", json={})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Video is not ready to open in the editor."
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


def test_open_in_editor_promotes_first_ready_while_sibling_renders() -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        status="rendering",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "ready",
                    "rank": 0,
                    "render_status": "ready",
                    "output_url": "https://play.example/ready.mp4",
                },
                {"variant_id": "still-rendering", "rank": 1, "render_status": "rendering"},
            ]
        },
    )
    plan = _plan(user.id)
    db = _db(
        [
            _scalar(job),
            _scalar(plan),
            _scalar(_persona(plan, user.id)),
            _scalars([]),
            _scalar(job),
        ]
    )
    _override(user, db)

    resp = client.post(f"/me/jobs/{job.id}/open-in-editor", json={})

    assert resp.status_code == 200
    assert resp.json()["variant_id"] == "ready"
    created = db.add.call_args.args[0]
    assert resp.json()["plan_item_id"] == str(created.id)
    assert job.mode == "content_plan"
    assert job.content_plan_item_id == created.id
    assert job.content_plan_ownership_epoch == plan.ownership_epoch
    db.commit.assert_awaited_once()


def test_open_in_editor_promotes_before_worker_finalization() -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        status="variants_ready",
        assembly_plan={"variants": _ready_variants()},
    )
    job.finished_at = None
    plan = _plan(user.id)
    db = _db(
        [
            _scalar(job),
            _scalar(plan),
            _scalar(_persona(plan, user.id)),
            _scalars([]),
            _scalar(job),
        ]
    )
    _override(user, db)

    resp = client.post(f"/me/jobs/{job.id}/open-in-editor", json={})

    assert resp.status_code == 200
    assert resp.json()["variant_id"] == "rank-one"
    assert job.mode == "content_plan"
    db.commit.assert_awaited_once()


def test_open_in_editor_all_failed_variants_have_same_stable_409() -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        status="variants_failed",
        assembly_plan={"variants": [{"variant_id": "one", "rank": 1, "render_status": "failed"}]},
    )
    db = _db([_scalar(job)])
    _override(user, db)

    resp = client.post(f"/me/jobs/{job.id}/open-in-editor", json={})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Video is not ready to open in the editor."


def test_open_in_editor_cancelled_job_with_retained_ready_variant_is_409() -> None:
    user = _user()
    job = _job(
        user_id=user.id,
        status="cancelled",
        assembly_plan={"variants": _ready_variants()},
    )
    db = _db([_scalar(job)])
    _override(user, db)

    resp = client.post(f"/me/jobs/{job.id}/open-in-editor", json={})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Video is not ready to open in the editor."
    assert db.execute.await_count == 1


def test_retry_failed_job_reuses_owned_job_and_preserves_inputs(monkeypatch) -> None:
    user = _user()
    inputs = {
        "clip_paths": [f"dev-user/{user.id}/generative/a/clip.mp4"],
        "persona": {"idea": "Keep it warm"},
        "voiceover_gcs_path": f"voiceover-uploads/direct/{user.id}/a/voice.webm",
    }
    job = _job(
        user_id=user.id,
        status="processing_failed",
        assembly_plan={"variants": []},
        all_candidates=inputs,
    )
    job.error_detail = "encoder timeout"
    job.current_phase = "render_variants"
    job.phase_log = [{"name": "analyze_clips"}]
    db = _db([_scalar(job), _scalar(job)])
    _override(user, db)
    enqueue = AsyncMock()
    monkeypatch.setattr("app.services.job_dispatch.enqueue_orchestrator", enqueue)

    resp = client.post(f"/me/jobs/{job.id}/retry")

    assert resp.status_code == 200
    assert resp.json() == {"job_id": str(job.id), "status": "queued"}
    assert job.status == "queued"
    assert job.all_candidates == inputs
    assert job.error_detail is None
    assert job.failure_reason is None
    assert job.current_phase is None
    assert job.phase_log == []
    db.commit.assert_awaited_once()
    enqueue.assert_awaited_once()


def test_retry_active_job_is_409_without_enqueue(monkeypatch) -> None:
    user = _user()
    job = _job(user_id=user.id, status="processing")
    db = _db([_scalar(job), _scalar(job)])
    _override(user, db)
    enqueue = AsyncMock()
    monkeypatch.setattr("app.services.job_dispatch.enqueue_orchestrator", enqueue)

    resp = client.post(f"/me/jobs/{job.id}/retry")

    assert resp.status_code == 409
    enqueue.assert_not_awaited()


def test_retry_publish_failure_returns_503_and_restores_retryable_terminal(monkeypatch) -> None:
    user = _user()
    job = _job(user_id=user.id, status="processing_failed")
    recovery = MagicMock(rowcount=1)
    db = _db([_scalar(job), _scalar(job), recovery])
    _override(user, db)
    enqueue = AsyncMock(side_effect=RuntimeError("broker down"))
    monkeypatch.setattr("app.services.job_dispatch.enqueue_orchestrator", enqueue)

    resp = client.post(f"/me/jobs/{job.id}/retry")

    assert resp.status_code == 503
    assert resp.json()["detail"] == (
        "The render queue is temporarily unavailable. Please try again."
    )
    assert db.commit.await_count == 2
    recovery_call = db.execute.await_args_list[-1].args[0]
    compiled = str(recovery_call.compile(compile_kwargs={"literal_binds": True}))
    assert "processing_failed" in compiled
    assert "dispatch_publish_failed" in compiled


def test_retry_cross_user_job_is_404_without_lock_or_enqueue(monkeypatch) -> None:
    user = _user()
    job = _job(user_id=uuid.uuid4(), status="processing_failed")
    db = _db([_scalar(job)])
    _override(user, db)
    enqueue = AsyncMock()
    monkeypatch.setattr("app.services.job_dispatch.enqueue_orchestrator", enqueue)

    resp = client.post(f"/me/jobs/{job.id}/retry")

    assert resp.status_code == 404
    assert db.execute.await_count == 1
    enqueue.assert_not_awaited()


def test_open_in_editor_400_on_bad_job_id() -> None:
    user = _user()
    db = _db([])
    _override(user, db)

    resp = client.post("/me/jobs/not-a-uuid/open-in-editor", json={})

    assert resp.status_code == 400
