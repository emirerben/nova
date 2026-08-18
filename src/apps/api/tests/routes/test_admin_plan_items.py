"""Tests for routes/admin_plan_items.py — the plan-item triage endpoint.

Covers:
  - auth gate (X-Admin-Token), mirroring tests/routes/test_admin_jobs.py
  - payload shape for an item with clips, pool assets, a job, and an
    edit_proposal carrying a conversation, an active conversation_attempt,
    a draft, and a last_approved snapshot — asserts every creator-authored
    or internal-secret field is redacted (no marker string anywhere in the
    serialized response)
  - an unparseable (schema-invalid) edit_proposal envelope surfaces a flag
    + top-level key names only, never values
  - 404 for both an unknown id and a malformed (non-UUID) id
  - the endpoint never writes (no db.commit)
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

VALID_TOKEN = "test-admin-token"

# Distinctive marker strings standing in for real creator-typed text (or, for
# the attempt token, an internal write-fence secret). If any of these leak
# into the serialized response, the redaction contract is broken.
SECRET_USER_TURN = "SECRET_CREATOR_WORDS_the vibe should feel like a rainy Tokyo night"
SECRET_AGENT_TURN = "SECRET_AGENT_REPLY_here is a proposed direction for your story"
SECRET_DRAFT_TITLE = "SECRET_CREATOR_DRAFT_TITLE"
SECRET_DRAFT_GOAL = "SECRET_CREATOR_DRAFT_GOAL"
SECRET_APPROVED_TITLE = "SECRET_CREATOR_APPROVED_TITLE"
SECRET_BRIEF_GOAL = "SECRET_BRIEF_GOAL_shot on a rooftop at golden hour"
SECRET_USER_NOTE = "SECRET_USER_NOTE_this is the clip from the Tokyo trip"
SECRET_ATTEMPT_TOKEN = "SECRET_ATTEMPT_TOKEN_do-not-leak-me"  # noqa: S105 - test fixture, not a real secret
SECRET_MALFORMED_VALUE = "SECRET_MALFORMED_VALUE_should_never_appear"


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _media_ref(media_id: str) -> dict:
    return {
        "lane": "clip",
        "media_id": media_id,
        "gcs_path": f"users/u1/plan/p1/pool/{media_id}.mp4",
        "generation": "12345",
        "kind": "video",
        "source_filename": f"{media_id}.mp4",
        "duration_s": 5.0,
        "aspect": 1.78,
        "content_hash": None,
        "user_context": "",
        "analysis": {},
    }


def _edit_proposal_dict() -> dict:
    """A full EditProposal envelope exercising every field the redaction
    contract must strip or reduce to a summary: conversation, an active
    conversation_attempt (token), a draft, and a last_approved snapshot."""
    return {
        "schema_version": 1,
        "proposal_version": 3,
        "generation_attempt_id": "attempt-1",
        "media_digest": _digest("current-media"),
        "status": "draft",
        "brief": {
            "direction": "guided_story",
            "goal": SECRET_BRIEF_GOAL,
            "pace": "balanced",
            "duration_s": 24,
        },
        "conversation": [
            {
                "role": "user",
                "phase": "briefing",
                "content": SECRET_USER_TURN,
                "suggestions": ["make it punchier"],
            },
            {
                "role": "agent",
                "phase": "briefing",
                "content": SECRET_AGENT_TURN,
                "suggestions": [],
            },
        ],
        "brief_ready": True,
        "conversation_attempt": {
            "token": SECRET_ATTEMPT_TOKEN,
            "expected_proposal_version": 2,
            "reserved_proposal_version": 3,
            "started_at": "2026-08-10T09:00:00+00:00",
            "placeholder": False,
        },
        "draft": {
            "direction": "guided_story",
            "goal": SECRET_DRAFT_GOAL,
            "pace": "balanced",
            "duration_s": 30,
            "title": SECRET_DRAFT_TITLE,
            "media": [_media_ref("m1")],
            "story_beats": [
                {
                    "beat_id": "b1",
                    "topic": "Opening",
                    "thought": "",
                    "thought_source": "ai_draft",
                    "media_ids": ["m1"],
                    "layout": "fullscreen",
                    "duration_s": 5.0,
                }
            ],
            "output_orientation": "portrait",
            "output_orientation_reason": "auto-selected",
        },
        "last_approved": {
            "proposal_version": 2,
            "media_digest": _digest("approved-media"),
            "approved_at": "2026-08-01T12:00:00+00:00",
            "snapshot": {
                "direction": "guided_story",
                "goal": "an earlier goal",
                "pace": "balanced",
                "duration_s": 20,
                "title": SECRET_APPROVED_TITLE,
                "media": [_media_ref("m1"), _media_ref("m2")],
                "story_beats": [
                    {
                        "beat_id": "b1",
                        "topic": "Opening",
                        "thought": "",
                        "thought_source": "ai_draft",
                        "media_ids": ["m1"],
                        "layout": "fullscreen",
                        "duration_s": 5.0,
                    },
                    {
                        "beat_id": "b2",
                        "topic": "Closing",
                        "thought": "",
                        "thought_source": "ai_draft",
                        "media_ids": ["m2"],
                        "layout": "fullscreen",
                        "duration_s": 5.0,
                    },
                ],
                "output_orientation": "portrait",
                "output_orientation_reason": "auto-selected",
            },
        },
        "failure": {
            "code": "conversation_failed",
            "message": "the edit-guide agent call errored: upstream timeout",
            "retryable": True,
        },
    }


def _plan_item_row(**overrides) -> SimpleNamespace:
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        content_plan_id=uuid.uuid4(),
        day_index=1,
        theme="a theme",
        position=1,
        idea="a creator idea",
        filming_suggestion=None,
        rationale=None,
        edit_format="montage",
        montage_preset="classic",
        landscape_fit="fit",
        content_mode=None,
        smart_captions_enabled=False,
        smart_sound_design_enabled=True,
        clip_gcs_paths=[],
        filming_guide=[],
        clip_assignments=[],
        edit_proposal=None,
        conformance=None,
        item_status="idea",
        current_job_id=None,
        source_idea_seed_id=None,
        voiceover_gcs_path=None,
        voiceover_bed_level=None,
        voiceover_caption_style=None,
        voiceover_script=None,
        voiceover_script_recorded_version=None,
        scheduled_date=None,
        notes=None,
        scenes=[],
        user_edited=False,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _pool_asset_row(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        kind="image",
        status="ready",
        error_code=None,
        error_detail=None,
        duration_s=None,
        aspect=1.5,
        source_filename="photo.jpg",
        analysis_attempt_count=0,
        created_at=datetime.now(UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _job_row(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        status="processing",
        mode="generative",
        created_at=datetime.now(UTC),
        failure_reason="upstream_timeout",
        error_detail="agent call timed out after 60s",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _db_gen(item, assets, jobs):
    db_holder: dict[str, AsyncMock] = {}

    async def _gen():
        db = AsyncMock()
        item_res = MagicMock()
        item_res.scalar_one_or_none.return_value = item
        assets_res = MagicMock()
        assets_res.scalars.return_value.all.return_value = assets
        jobs_res = MagicMock()
        jobs_res.scalars.return_value.all.return_value = jobs
        db.execute = AsyncMock(side_effect=[item_res, assets_res, jobs_res])
        db_holder["db"] = db
        yield db

    return _gen, db_holder


# ── Auth ─────────────────────────────────────────────────────────────────────


class TestAdminPlanItemsAuth:
    def test_missing_token_unauthorized(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.get(f"/admin/plan-items/{uuid.uuid4()}/debug")
        assert res.status_code in (401, 422)

    def test_wrong_token_401(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            res = client.get(
                f"/admin/plan-items/{uuid.uuid4()}/debug",
                headers={"X-Admin-Token": "wrong"},
            )
        assert res.status_code == 401

    def test_valid_token_200(self, client):
        item = _plan_item_row()
        gen, _holder = _db_gen(item, [], [])
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = gen
            try:
                res = client.get(
                    f"/admin/plan-items/{item.id}/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200


# ── Payload shape + redaction ────────────────────────────────────────────────


class TestPlanItemDebugPayload:
    def test_full_payload_shape_and_redaction(self, client):
        job = _job_row(status="processing")
        item = _plan_item_row(
            edit_format="talking_head",
            content_mode="existing_footage",
            montage_preset="masonry",
            current_job_id=job.id,
            voiceover_gcs_path="users/u1/plan/p1/voiceover.m4a",
            scheduled_date=None,
            clip_gcs_paths=["users/u1/plan/p1/clip1.mp4", "users/u1/plan/p1/clip2.mp4"],
            clip_assignments=[
                {
                    "media_id": "m1",
                    "gcs_path": "users/u1/plan/p1/clip1.mp4",
                    "shot_id": "shot-1",
                    "user_note": SECRET_USER_NOTE,
                    "machine_matched": False,
                    "kind": "video",
                    "duration_s": 5.5,
                    "aspect": 1.78,
                    "generation": "111",
                    "analysis": {"analysis_version": 5, "summary": "a runner on a trail"},
                },
                {
                    "media_id": "m2",
                    "gcs_path": "users/u1/plan/p1/clip2.mp4",
                    "shot_id": None,
                    "user_note": "",
                    "machine_matched": True,
                    # No analysis yet — has_analysis must be False.
                },
            ],
            edit_proposal=_edit_proposal_dict(),
        )
        assets = [
            _pool_asset_row(status="failed", error_code="analysis_unreadable"),
            _pool_asset_row(status="ready", kind="video", duration_s=8.2),
        ]
        jobs = [job]

        gen, holder = _db_gen(item, assets, jobs)
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = gen
            try:
                res = client.get(
                    f"/admin/plan-items/{item.id}/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)

        assert res.status_code == 200
        body = res.json()
        raw_text = res.text

        # No creator-typed content, and no internal secret, anywhere in the
        # serialized body.
        assert SECRET_USER_TURN not in raw_text
        assert SECRET_AGENT_TURN not in raw_text
        assert SECRET_DRAFT_TITLE not in raw_text
        assert SECRET_DRAFT_GOAL not in raw_text
        assert SECRET_APPROVED_TITLE not in raw_text
        assert SECRET_BRIEF_GOAL not in raw_text
        assert SECRET_USER_NOTE not in raw_text
        assert SECRET_ATTEMPT_TOKEN not in raw_text

        # Read-only: the route must never commit.
        holder["db"].commit.assert_not_awaited()

        # ── item core ──
        assert body["item"]["id"] == str(item.id)
        assert body["item"]["item_status"] == "generating"
        assert body["item"]["edit_format"] == "talking_head"
        assert body["item"]["content_mode"] == "existing_footage"
        assert body["item"]["montage_preset"] == "masonry"
        assert body["item"]["current_job_id"] == str(item.current_job_id)
        assert body["item"]["has_voiceover"] is True
        assert body["item"]["voiceover_gcs_path"] == "users/u1/plan/p1/voiceover.m4a"

        # ── clips ──
        assert body["clip_gcs_paths"]["count"] == 2
        assert body["clip_gcs_paths"]["paths"] == [
            "users/u1/plan/p1/clip1.mp4",
            "users/u1/plan/p1/clip2.mp4",
        ]
        assignments = body["clip_assignments"]
        assert len(assignments) == 2
        a1 = next(a for a in assignments if a["media_id"] == "m1")
        assert a1["gcs_path"] == "users/u1/plan/p1/clip1.mp4"
        assert a1["kind"] == "video"
        assert a1["duration_s"] == 5.5
        assert a1["aspect"] == 1.78
        assert a1["generation"] == "111"
        assert a1["has_analysis"] is True
        assert a1["analysis_version"] == 5
        assert "user_note" not in a1
        a2 = next(a for a in assignments if a["media_id"] == "m2")
        assert a2["has_analysis"] is False
        assert a2["analysis_version"] is None
        assert "user_note" not in a2

        # ── pool assets ──
        assert len(body["pool_assets"]) == 2
        failed_asset = next(a for a in body["pool_assets"] if a["status"] == "failed")
        assert failed_asset["error_code"] == "analysis_unreadable"

        # ── jobs ──
        assert len(body["jobs"]) == 1
        assert body["jobs"][0]["id"] == str(job.id)
        assert body["jobs"][0]["status"] == "processing"
        assert body["jobs"][0]["failure_reason"] == "upstream_timeout"
        assert body["jobs"][0]["error_detail"] == "agent call timed out after 60s"

        # ── edit_proposal ──
        proposal = body["edit_proposal"]
        assert body["edit_proposal_unparseable"] is False
        assert body["edit_proposal_raw_keys"] is None
        assert proposal["status"] == "draft"
        assert proposal["proposal_version"] == 3
        assert proposal["schema_version"] == 1
        assert "goal" not in proposal["brief"]
        assert proposal["brief"]["goal_length"] == len(SECRET_BRIEF_GOAL)
        assert proposal["brief"]["direction"] == "guided_story"
        assert proposal["brief"]["pace"] == "balanced"
        assert proposal["brief_ready"] is True
        assert proposal["generation_attempt_id"] == "attempt-1"
        assert proposal["failure"]["code"] == "conversation_failed"
        assert "timeout" in proposal["failure"]["message"]

        # conversation_attempt: token is gone, presence + versions survive.
        attempt = proposal["conversation_attempt"]
        assert "token" not in attempt
        assert attempt["has_conversation_attempt"] is True
        assert attempt["expected_proposal_version"] == 2
        assert attempt["reserved_proposal_version"] == 3
        assert attempt["placeholder"] is False

        # draft reduced to a summary — only beat_count + duration_s survive.
        assert proposal["draft"] == {"beat_count": 1, "duration_s": 30}

        # last_approved reduced to a summary — no title/goal/media text.
        assert proposal["last_approved"]["beat_count"] == 2
        assert proposal["last_approved"]["media_count"] == 2
        assert proposal["last_approved"]["proposal_version"] == 2
        assert "goal" not in proposal["last_approved"]
        assert "title" not in proposal["last_approved"]

        # conversation turns are redacted to role/phase/length/has_suggestions.
        conv = proposal["conversation"]
        assert len(conv) == 2
        assert conv[0] == {
            "role": "user",
            "phase": "briefing",
            "length": len(SECRET_USER_TURN),
            "has_suggestions": True,
        }
        assert conv[1] == {
            "role": "agent",
            "phase": "briefing",
            "length": len(SECRET_AGENT_TURN),
            "has_suggestions": False,
        }

    def test_no_edit_proposal_returns_null(self, client):
        item = _plan_item_row(edit_proposal=None)
        gen, _holder = _db_gen(item, [], [])
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = gen
            try:
                res = client.get(
                    f"/admin/plan-items/{item.id}/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200
        body = res.json()
        assert body["edit_proposal"] is None
        assert body["edit_proposal_unparseable"] is False
        assert body["edit_proposal_raw_keys"] is None

    def test_unparseable_edit_proposal_surfaces_flag_and_keys_only(self, client):
        """A corrupted/legacy JSONB envelope fails EditProposal validation —
        parse_edit_proposal fails closed and returns None. The endpoint must
        still tell the operator an envelope exists (unparseable=True) and
        which top-level keys it had, but never any value from it."""
        malformed = {
            "not_a_real_field": SECRET_MALFORMED_VALUE,
            "status": "not-a-valid-status",
        }
        item = _plan_item_row(edit_proposal=malformed)
        gen, _holder = _db_gen(item, [], [])
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN
            app.dependency_overrides[get_db] = gen
            try:
                res = client.get(
                    f"/admin/plan-items/{item.id}/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 200
        assert SECRET_MALFORMED_VALUE not in res.text
        body = res.json()
        assert body["edit_proposal"] is None
        assert body["edit_proposal_unparseable"] is True
        assert body["edit_proposal_raw_keys"] == sorted(malformed.keys())


# ── Not found ────────────────────────────────────────────────────────────────


class TestPlanItemDebugNotFound:
    def test_unknown_id_returns_404(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                db = AsyncMock()
                item_res = MagicMock()
                item_res.scalar_one_or_none.return_value = None
                db.execute = AsyncMock(return_value=item_res)
                yield db

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get(
                    f"/admin/plan-items/{uuid.uuid4()}/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 404

    def test_malformed_id_returns_404(self, client):
        with patch("app.routes.admin.settings") as s:
            s.admin_api_key = VALID_TOKEN

            async def _gen():
                yield AsyncMock()

            app.dependency_overrides[get_db] = _gen
            try:
                res = client.get(
                    "/admin/plan-items/not-a-uuid/debug",
                    headers={"X-Admin-Token": VALID_TOKEN},
                )
            finally:
                app.dependency_overrides.pop(get_db, None)
        assert res.status_code == 404
