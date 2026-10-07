"""KRI-470 PR-D: the shadow route event on REAL Postgres (the trace writer + the redelivery probe).

The mocked dispatch tests cannot prove the JSONB containment probe or that a redelivered task
really appends the event only once.  This drives ``shadow_route_check`` through the real
``record_pipeline_event`` against a committed Job row, then removes it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import sync_session
from app.models import Job, User
from app.services import render_route
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    PLAN_AUTHORITY_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.services.pipeline_trace import pipeline_trace_for

if not (make_url(settings.database_url).database or "").endswith("_test"):
    pytest.skip("requires a *_test database", allow_module_level=True)


@pytest.fixture
def committed_job():
    try:
        session = sync_session()
        session.execute(text("select 1"))
    except OperationalError:
        pytest.skip("nova_test Postgres not reachable")
    user_id, job_id = uuid.uuid4(), uuid.uuid4()
    session.add(User(id=user_id, email=f"{user_id}@test.local"))
    session.flush()
    session.add(
        Job(id=job_id, user_id=user_id, status="processing", raw_storage_path=f"t/{job_id}.mp4")
    )
    session.commit()
    try:
        yield job_id
    finally:
        session.rollback()
        session.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": str(job_id)})
        session.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(user_id)})
        session.commit()
        session.close()


def _events(job_id: uuid.UUID) -> list[dict]:
    with sync_session() as session:
        rows = session.execute(
            text("SELECT pipeline_trace FROM jobs WHERE id = :id"), {"id": str(job_id)}
        ).scalar()
    return [e for e in rows or [] if e["event"] == "route_mismatch"]


def _mismatching_job() -> tuple[dict, dict]:
    strategy = {"edit_format": "montage"}
    contract = build_render_contract(strategy, generation_id="g")
    assembly = {CONTRACT_FIELD: contract.model_dump(mode="json"), PHONE_SOURCES_FIELD: []}
    # a creator song is attached but the plan says library music: legacy label differs
    candidates = {
        REQUIREMENT_VERSION_FIELD: 1,
        PLAN_AUTHORITY_FIELD: 1,
        "edit_format": "montage",
        "creator_strategy": strategy,
        "clip_paths": ["a", "b"],
    }
    return assembly, candidates


def _check(job_id: uuid.UUID, legacy: str) -> None:
    assembly, candidates = _mismatching_job()
    with pipeline_trace_for(job_id):
        render_route.shadow_route_check(
            job_id=str(job_id),
            assembly=assembly,
            candidates=candidates,
            platform="phone",
            legacy_route=legacy,
            point="phone_dispatch",
        )


def test_a_redelivered_task_appends_the_mismatch_once(committed_job) -> None:
    _check(committed_job, "user_song_montage")
    _check(committed_job, "user_song_montage")  # redelivery: same decision, same contract
    events = _events(committed_job)
    assert len(events) == 1
    assert events[0]["data"]["legacy_route"] == "user_song_montage"
    assert events[0]["data"]["resolver_route"] == "unified_montage"


def test_a_different_mismatch_is_still_recorded(committed_job) -> None:
    _check(committed_job, "user_song_montage")
    _check(committed_job, "speech_montage")
    assert {e["data"]["legacy_route"] for e in _events(committed_job)} == {
        "user_song_montage",
        "speech_montage",
    }
