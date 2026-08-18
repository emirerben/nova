"""plans/014 — synchronous plan-item render registration (real-DB integration).

Unlike the repo's usual AsyncMock route-test idiom, these run against the real
`nova_test` Postgres (the same service CI provides — see ci.yml): the behaviors
under test — FOR-UPDATE serialization, cross-session identity-map staleness,
publish-failure containment — are exactly the class a mocked session cannot
prove (a mock would pass identically on broken code). Auth goes through the
REAL header path (X-User-Id + internal key), not a dependency override: the
override masked a MissingGreenlet 500 in review (the session-attached User row
expired by db.expire_all()). Every test seeds fresh UUID rows and never
truncates, so the file is xdist-safe.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import sync_session
from app.main import app
from app.models import ContentPlan, Job, Persona, PlanItem, PlanItemAsset, User
from app.schemas.edit_proposal import (
    ApprovedProposalSnapshot,
    EditProposal,
    EditProposalSnapshot,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)
from app.storage import ObjectMetadata
from app.tasks.content_plan_build import dispatch_item_render_for

_ENQUEUE = "app.services.job_dispatch.enqueue_orchestrator_sync"

# Safety rail: these tests WRITE real rows. Refuse to run against anything but
# a *_test database (an inherited DATABASE_URL — e.g. a sourced .env pointing
# at the dev DB — must fail loudly, not pollute silently), and skip cleanly on
# machines without the nova_test Postgres instead of erroring 8 times.
_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _reset_async_pool() -> None:
    """Dispose the app's async engine pool.

    TestClient spins up a fresh event loop PER REQUEST; an asyncpg connection
    checked back into the global pool stays bound to the loop that created it,
    so the next request (new loop) that draws it 500s. Disposing forces the
    next request to open a fresh connection. Called around every test AND
    between same-test requests.
    """
    import asyncio  # noqa: PLC0415

    from app.database import engine  # noqa: PLC0415

    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _fresh_async_pool():
    _reset_async_pool()
    yield
    _reset_async_pool()


def _seed_item(*, clips: list[str] | None = None) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert user + persona + plan + item; return (user_id, item_id)."""
    user_id = uuid.uuid4()
    item_id = uuid.uuid4()
    with sync_session() as s:
        s.add(User(id=user_id, email=f"{user_id}@test.local"))
        s.flush()
        persona = Persona(
            user_id=user_id,
            persona_status="ready",
            persona={"content_mode": "travel", "tone": "direct"},
        )
        s.add(persona)
        s.flush()
        plan = ContentPlan(user_id=user_id, persona_id=persona.id)
        s.add(plan)
        s.flush()
        s.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan.id,
                position=1,
                idea="test idea",
                item_status="awaiting_clips",
                clip_gcs_paths=(
                    clips if clips is not None else [f"users/{user_id}/plan/{item_id}/a.mp4"]
                ),
            )
        )
        s.commit()
    return user_id, item_id


def _auth(user_id: uuid.UUID) -> dict[str, str]:
    """REAL auth headers (proxy contract) — get_current_user loads the actual
    User row on the request session, so identity-map bugs reproduce here."""
    return {
        "X-User-Id": str(user_id),
        "Authorization": f"Bearer {settings.internal_api_key}",
    }


def _jobs_for(item_id: uuid.UUID) -> list[Job]:
    with sync_session() as s:
        rows = s.execute(select(Job).where(Job.content_plan_item_id == item_id)).scalars().all()
        s.expunge_all()
        return list(rows)


def _approve_clip_proposal(item_id: uuid.UUID, *, path: str, generation: str = "42") -> None:
    media_id = str(uuid.uuid4())
    media = MediaRef(
        lane="clip",
        media_id=media_id,
        gcs_path=path,
        generation=generation,
        kind="video",
    )
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        goal="Share what stood out",
        pace="balanced",
        duration_s=24,
        title="What I noticed",
        media=[media],
        story_beats=[
            StoryBeat(
                beat_id="coast",
                topic="Coast",
                thought="The water set the pace.",
                media_ids=[media_id],
                duration_s=4,
            )
        ],
    )
    digest = canonical_media_digest([media])
    proposal = EditProposal(
        proposal_version=3,
        generation_attempt_id=str(uuid.uuid4()),
        media_digest=digest,
        status="approved",
        draft=snapshot,
        last_approved=ApprovedProposalSnapshot(
            proposal_version=3,
            media_digest=digest,
            approved_at=datetime.now(UTC),
            snapshot=snapshot,
        ),
    )
    with sync_session() as s:
        item = s.get(PlanItem, item_id)
        assert item is not None
        item.clip_assignments = [{"media_id": media_id, "gcs_path": path, "shot_id": None}]
        item.edit_proposal = proposal.model_dump(mode="json")
        s.commit()


def _approve_asset_proposal(
    user_id: uuid.UUID, item_id: uuid.UUID, *, path: str, generation: str = "51"
) -> str:
    asset_id = uuid.uuid4()
    media = MediaRef(
        lane="asset",
        media_id=str(asset_id),
        gcs_path=path,
        generation=generation,
        kind="image",
    )
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        goal="Tell the story with photos",
        pace="balanced",
        duration_s=18,
        title="Corfu in details",
        media=[media],
        story_beats=[
            StoryBeat(
                beat_id="town",
                topic="Town",
                thought="The old streets hold the story.",
                media_ids=[str(asset_id)],
                duration_s=4,
            )
        ],
    )
    digest = canonical_media_digest([media])
    proposal = EditProposal(
        proposal_version=3,
        generation_attempt_id=str(uuid.uuid4()),
        media_digest=digest,
        status="approved",
        draft=snapshot,
        last_approved=ApprovedProposalSnapshot(
            proposal_version=3,
            media_digest=digest,
            approved_at=datetime.now(UTC),
            snapshot=snapshot,
        ),
    )
    with sync_session() as s:
        item = s.get(PlanItem, item_id)
        assert item is not None
        item.clip_gcs_paths = []
        item.edit_proposal = proposal.model_dump(mode="json")
        s.add(
            PlanItemAsset(
                id=asset_id,
                plan_item_id=item_id,
                user_id=user_id,
                gcs_path=path,
                gcs_generation=generation,
                kind="image",
                status="ready",
            )
        )
        s.commit()
    return str(asset_id)


def _link_job(item_id: uuid.UUID, user_id: uuid.UUID, *, status: str) -> uuid.UUID:
    """Create a Job with the given status and point item.current_job_id at it."""
    with sync_session() as s:
        job = Job(
            user_id=user_id,
            status=status,
            mode="content_plan",
            raw_storage_path="",
            content_plan_item_id=item_id,
        )
        s.add(job)
        s.flush()
        db_item = s.get(PlanItem, item_id)
        assert db_item is not None
        db_item.current_job_id = job.id
        s.commit()
        return job.id


def test_generate_item_mints_job_synchronously(client: TestClient) -> None:
    """The POST itself mints the Job (status flips before the response), with
    the orchestrator enqueued once onto the throttled plan-jobs queue and the
    celery_task_id == job id correlation intact."""
    user_id, item_id = _seed_item()
    with patch(_ENQUEUE) as enqueue:
        resp = client.post(f"/plan-items/{item_id}/generate", headers=_auth(user_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "generating"
    assert body["current_job_id"] is not None

    jobs = _jobs_for(item_id)
    assert len(jobs) == 1
    job = jobs[0]
    assert str(job.id) == body["current_job_id"]
    assert job.status == "queued"
    assert job.celery_task_id == str(job.id)
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["queue"] == "plan-jobs"
    assert enqueue.call_args.args[1] == str(job.id)


def test_generate_response_carries_fresh_job_id(client: TestClient) -> None:
    """Identity-map pin (plans/014 A2 + review P1): the route's async session
    loaded the item AND the auth User row before the helper's sync session
    committed the Job. The response must reflect the committed truth
    (db.expire_all()) without tripping a sync lazy-load on the expired User
    (owner_id captured pre-expire — MissingGreenlet regression)."""
    user_id, item_id = _seed_item()
    with patch(_ENQUEUE):
        resp = client.post(f"/plan-items/{item_id}/generate", headers=_auth(user_id))
    assert resp.status_code == 200
    body = resp.json()
    with sync_session() as s:
        db_item = s.get(PlanItem, item_id)
        assert db_item is not None
        db_job_id = db_item.current_job_id
    assert db_job_id is not None
    assert body["current_job_id"] == str(db_job_id)
    assert body["status"] == "generating"


def test_generate_item_duplicate_post_returns_active_job(client: TestClient) -> None:
    """D5 pin: a duplicate Generate (lost-response retry, second tab) is an
    idempotent 200 carrying the ACTIVE job — not a 409 the frontend renders as
    an error — and mints no second Job row."""
    user_id, item_id = _seed_item()
    with patch(_ENQUEUE) as enqueue:
        first = client.post(f"/plan-items/{item_id}/generate", headers=_auth(user_id))
        _reset_async_pool()
        second = client.post(f"/plan-items/{item_id}/generate", headers=_auth(user_id))
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["current_job_id"] == first.json()["current_job_id"]
    assert second.json()["status"] == "generating"
    assert len(_jobs_for(item_id)) == 1
    enqueue.assert_called_once()  # the duplicate must not re-enqueue either


def test_generate_item_concurrent_posts_mint_single_job() -> None:
    """C1 pin: two genuinely concurrent dispatches serialize on the item row's
    SELECT … FOR UPDATE — one mints, the other observes the fresh
    current_job_id inside the lock and reports already_active. Without the
    lock both would mint (jobs.content_plan_item_id has no uniqueness
    constraint)."""
    _user_id, item_id = _seed_item()
    outcomes: list[tuple[str, str | None]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _run() -> None:
        barrier.wait()
        try:
            result = dispatch_item_render_for(str(item_id))
            outcomes.append((result.outcome, result.job_id))
        except BaseException as exc:  # noqa: BLE001 — surfaced as the test failure
            errors.append(exc)

    with patch(_ENQUEUE):
        threads = [threading.Thread(target=_run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert not any(t.is_alive() for t in threads), "dispatch thread hung (lock deadlock?)"
    assert not errors, f"dispatch thread raised: {errors!r}"
    assert sorted(o for o, _ in outcomes) == ["already_active", "dispatched"]
    jobs = _jobs_for(item_id)
    assert len(jobs) == 1
    # Both callers converge on the SAME job id.
    assert {j for _, j in outcomes} == {str(jobs[0].id)}


def test_generate_item_invalid_clips_422(client: TestClient) -> None:
    """A clip path outside the allowlist used to fail SILENTLY inside the task
    (15 min of frozen 'Starting…'); now it is an immediate 422."""
    user_id, item_id = _seed_item(clips=["not-allowed/x.mp4"])
    with patch(_ENQUEUE) as enqueue:
        resp = client.post(f"/plan-items/{item_id}/generate", headers=_auth(user_id))
    assert resp.status_code == 422
    enqueue.assert_not_called()
    assert _jobs_for(item_id) == []


def test_task_side_enforcement_rejects_missing_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock-owning dispatcher is the final proposal gate, not only the route."""

    monkeypatch.setattr(settings, "guided_edit_enforcement_enabled", True)
    _user_id, item_id = _seed_item()
    with patch(_ENQUEUE) as enqueue:
        result = dispatch_item_render_for(str(item_id))
    assert result.outcome == "proposal_required"
    enqueue.assert_not_called()
    assert _jobs_for(item_id) == []


def test_bypass_guided_edit_gate_refuses_when_pool_asset_present() -> None:
    """P2-4 (2026-08-18 adversarial review): draft_edit_proposal's clip-only

    montage fallback checks zero-registered-pool-assets in a SEPARATE
    transaction from this dispatch. The lock-owning dispatcher must
    independently re-count under ITS OWN lock and refuse the bypass — never
    silently drop a pool asset that appeared in the gap behind a montage.
    """

    user_id, item_id = _seed_item()
    with sync_session() as s:
        s.add(
            PlanItemAsset(
                plan_item_id=item_id,
                user_id=user_id,
                gcs_path=f"users/{user_id}/plan/{item_id}/pool/photo.jpg",
                gcs_generation="1",
                kind="image",
                status="ready",
            )
        )
        s.commit()

    with patch(_ENQUEUE) as enqueue:
        result = dispatch_item_render_for(str(item_id), bypass_guided_edit_gate=True)

    assert result.outcome == "guided_edit_bypass_unsafe"
    enqueue.assert_not_called()
    assert _jobs_for(item_id) == []


def test_bypass_guided_edit_gate_dispatches_when_pool_is_genuinely_empty() -> None:
    """The bypass still works for the real clip-only fallback case (no pool

    assets at all) — the TOCTOU guard above must not block the legitimate path.
    """

    _user_id, item_id = _seed_item()
    with patch(_ENQUEUE):
        result = dispatch_item_render_for(str(item_id), bypass_guided_edit_gate=True)

    assert result.outcome == "dispatched"
    assert len(_jobs_for(item_id)) == 1


def test_approved_proposal_exact_media_is_snapshotted_into_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generate freezes the approval and immutable media identity into the Job."""

    monkeypatch.setattr(settings, "guided_edit_enforcement_enabled", True)
    _user_id, item_id = _seed_item()
    path = f"users/test/plan/{item_id}/a.mp4"
    with sync_session() as s:
        item = s.get(PlanItem, item_id)
        assert item is not None
        item.clip_gcs_paths = [path]
        s.commit()
    _approve_clip_proposal(item_id, path=path)
    metadata = ObjectMetadata(
        path=path,
        generation="42",
        etag="etag",
        size=100,
        content_type="video/mp4",
    )

    with patch("app.storage.object_metadata", return_value=metadata), patch(_ENQUEUE):
        result = dispatch_item_render_for(str(item_id))

    assert result.outcome == "dispatched"
    job = _jobs_for(item_id)[0]
    guided = job.assembly_plan["guided_edit"]
    assert guided["proposal_version"] == 3
    assert guided["approved_proposal"]["title"] == "What I noticed"
    assert guided["media_identities"] == [
        {
            "lane": "clip",
            "media_id": guided["approved_proposal"]["media"][0]["media_id"],
            "gcs_path": path,
            "generation": "42",
            "kind": "video",
        }
    ]


def test_capability_snapshots_approved_asset_only_story_without_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approved pool-only edit must not fall back during staged rollout."""

    monkeypatch.setattr(settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(settings, "guided_edit_enforcement_enabled", False)
    user_id, item_id = _seed_item(clips=[])
    path = f"users/{user_id}/plan/{item_id}/pool/town.jpg"
    media_id = _approve_asset_proposal(user_id, item_id, path=path)
    metadata = ObjectMetadata(
        path=path,
        generation="51",
        etag="etag",
        size=100,
        content_type="image/jpeg",
    )

    with patch("app.storage.object_metadata", return_value=metadata), patch(_ENQUEUE):
        result = dispatch_item_render_for(str(item_id))

    assert result.outcome == "dispatched"
    job = _jobs_for(item_id)[0]
    assert job.all_candidates["clip_paths"] == [path]
    assert job.raw_storage_path == path
    assert job.assembly_plan["guided_edit"]["media_identities"] == [
        {
            "lane": "asset",
            "media_id": media_id,
            "gcs_path": path,
            "generation": "51",
            "kind": "image",
        }
    ]


def test_generate_item_publish_failure_marks_job_failed(client: TestClient) -> None:
    """A1 pin: a broker-publish failure must never strand a forever-'queued'
    ghost (the reaper deliberately excludes queued). The minted Job flips
    terminal, the route 502s, and a retry mints a FRESH job."""
    user_id, item_id = _seed_item()

    def _central_recovery_then_raise(_task, job_id, **_kwargs) -> None:
        # Mirror enqueue_orchestrator_sync's post-broker queued-only recovery.
        # The caller must report publish_failed, not mistake this helper-owned
        # processing_failed row for a worker claim.
        with sync_session() as s:
            row = s.get(Job, uuid.UUID(job_id), with_for_update=True)
            assert row is not None
            row.status = "processing_failed"
            row.failure_reason = "dispatch_publish_failed"
            s.commit()
        raise RuntimeError("redis down")

    with patch(_ENQUEUE, side_effect=_central_recovery_then_raise):
        resp = client.post(f"/plan-items/{item_id}/generate", headers=_auth(user_id))
    assert resp.status_code == 502

    jobs = _jobs_for(item_id)
    assert len(jobs) == 1
    assert jobs[0].status == "processing_failed"
    assert jobs[0].failure_reason == "dispatch_publish_failed"

    # The failed job is terminal → a retry passes the active-render re-check
    # and mints a fresh job (the user-visible retry button works).
    _reset_async_pool()
    with patch(_ENQUEUE):
        retry = client.post(f"/plan-items/{item_id}/generate", headers=_auth(user_id))
    assert retry.status_code == 200
    assert retry.json()["status"] == "generating"
    assert len(_jobs_for(item_id)) == 2


def test_publish_raised_but_worker_claimed_reports_dispatched() -> None:
    """CX1 pin (ambiguous publish): apply_async raising does not prove the
    message was lost. If the worker already flipped the job to `processing`,
    the containment's conditional UPDATE (WHERE status='queued') must NO-OP —
    the running render is not marked failed and the caller sees dispatched."""
    _user_id, item_id = _seed_item()

    def _deliver_then_raise(*_a, **_kw) -> None:
        # Simulate: message actually reached the broker; the worker claimed the
        # job (status → processing) before our exception handler ran.
        with sync_session() as s:
            row = s.execute(select(Job).where(Job.content_plan_item_id == item_id)).scalars().one()
            row.status = "processing"
            s.commit()
        raise RuntimeError("socket dropped after delivery")

    with patch(_ENQUEUE, side_effect=_deliver_then_raise):
        result = dispatch_item_render_for(str(item_id))

    assert result.outcome == "dispatched"
    jobs = _jobs_for(item_id)
    assert len(jobs) == 1
    assert jobs[0].status == "processing"  # running render NOT clobbered to failed


def test_dispatch_missing_row_branches() -> None:
    """Guard-clause coverage: malformed id and unknown item are missing_row.

    (A dangling current_job_id is unconstructable — plan_items.current_job_id
    carries an FK to jobs.id, so the helper's `current is None` fall-through is
    pure defense; verified by the FK violation when this test tried to seed it.)
    """
    assert dispatch_item_render_for("not-a-uuid").outcome == "missing_row"
    assert dispatch_item_render_for(str(uuid.uuid4())).outcome == "missing_row"


def test_dispatch_empty_clips_is_invalid_clips() -> None:
    """The no-clips guard in _dispatch_item_render must report invalid_clips.
    Reachable in prod via the legacy task path (generate_plan_item_videos has
    no clip pre-check — clips can be detached between route check and task
    run), so it is not pure defense."""
    _user_id, item_id = _seed_item(clips=[])
    with patch(_ENQUEUE) as enqueue:
        result = dispatch_item_render_for(str(item_id))
    assert result.outcome == "invalid_clips"
    enqueue.assert_not_called()
    assert _jobs_for(item_id) == []


def test_task_swallows_not_dispatched_outcomes() -> None:
    """generate_plan_item_videos logs-and-returns on a non-dispatch outcome —
    it must never raise (Celery would retry-loop a permanently-missing row)."""
    from app.tasks.content_plan_build import generate_plan_item_videos  # noqa: PLC0415

    generate_plan_item_videos.run(str(uuid.uuid4()))  # missing_row → warning, no raise


def test_added_template_job_is_terminal_for_generate() -> None:
    """CX3 pin: /me/jobs/{id}/add-to-plan can link a template/music job to a
    plan item. Those statuses must count as terminal — the item reads 'ready'
    (not perpetual 'generating') and a new Generate mints a fresh render
    instead of reporting already_active forever."""
    from app.routes.plan_items import derive_item_status  # noqa: PLC0415

    user_id, item_id = _seed_item()
    _link_job(item_id, user_id, status="template_ready")

    with sync_session() as s:
        db_item = s.get(PlanItem, item_id)
        assert db_item is not None
        assert derive_item_status(db_item) == "ready"

    with patch(_ENQUEUE):
        result = dispatch_item_render_for(str(item_id))
    assert result.outcome == "dispatched"
    assert len(_jobs_for(item_id)) == 2  # fresh render minted alongside the pinned job


def test_activation_continues_after_publish_failure() -> None:
    """C7 pin: the helper NEVER raises on publish failure, so a multi-item
    dispatch loop (activation seed) keeps going after one item's publish
    blows up — the failure is contained to that item's Job row."""
    _uid_a, item_a = _seed_item()
    _uid_b, item_b = _seed_item()
    with patch(_ENQUEUE, side_effect=[RuntimeError("redis blip"), None]):
        outcomes = [
            dispatch_item_render_for(str(item_a)).outcome,
            dispatch_item_render_for(str(item_b)).outcome,
        ]
    assert outcomes == ["publish_failed", "dispatched"]
    assert _jobs_for(item_a)[0].status == "processing_failed"
    assert _jobs_for(item_b)[0].status == "queued"


def test_plan_terminal_covers_orchestrator_no_rerun_statuses() -> None:
    """CA4 drift guard: every status the orchestrator's redelivery guard treats
    as terminal must also be terminal for dispatch — otherwise a finished
    render reads as already_active and the item can never re-generate."""
    from app.services.job_status import PLAN_ITEM_JOB_TERMINAL  # noqa: PLC0415
    from app.tasks.generative_build import _NO_RERUN_STATUSES  # noqa: PLC0415

    assert _NO_RERUN_STATUSES <= PLAN_ITEM_JOB_TERMINAL


def test_generate_item_kill_switch_falls_back_to_task(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Flag off ⇒ legacy ROUTE contract: .delay() dispatch, no Job row minted
    in-request, and the double-generate guard is the legacy 409. (The task
    itself keeps the lock + publish containment — deliberately safer.)"""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "PLAN_SYNC_DISPATCH_ENABLED", False, raising=False)
    user_id, item_id = _seed_item()
    with (
        patch("app.tasks.content_plan_build.generate_plan_item_videos") as task,
        patch(_ENQUEUE) as enqueue,
    ):
        task.delay = MagicMock()
        resp = client.post(f"/plan-items/{item_id}/generate", headers=_auth(user_id))
    assert resp.status_code == 200
    assert resp.json()["status"] == "awaiting_clips"  # registration still async
    task.delay.assert_called_once_with(str(item_id), 0)
    enqueue.assert_not_called()
    assert _jobs_for(item_id) == []

    # Legacy 409 when a render is already active (queued job linked).
    _link_job(item_id, user_id, status="queued")
    _reset_async_pool()
    resp = client.post(f"/plan-items/{item_id}/generate", headers=_auth(user_id))
    assert resp.status_code == 409
