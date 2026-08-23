from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.config as config_mod
import app.routes.admin_edit_feedback as edit_feedback_route
from app.main import app
from app.routes.admin_edit_feedback import (
    SaveAnnotationRequest,
    SaveAnnotationsBulkRequest,
    _current_annotations,
    _decode_cursor,
    _decode_stratified_cursor,
    _next_chronological_cursor,
    _playback,
    _review_state,
    _stratified_cursor,
    _stratified_review_order,
    _timeline,
    save_edit_feedback_annotations_bulk,
)

VALID_TOKEN = "test-admin-token"


@dataclass
class _Annotation:
    id: uuid.UUID
    dimension: str
    created_at: datetime
    supersedes_annotation_id: uuid.UUID | None = None


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "admin_api_key", VALID_TOKEN, raising=False)
    return TestClient(app, raise_server_exceptions=False)


def test_admin_list_requires_backend_admin_token(client):
    response = client.get("/admin/edit-feedback")
    assert response.status_code in {401, 422}


def test_annotation_requires_rationale_for_substantive_rating():
    with pytest.raises(ValidationError, match="rationale is required"):
        SaveAnnotationRequest(dimension="hook", rating="bad")
    payload = SaveAnnotationRequest(dimension="hook", rating="not_applicable")
    assert payload.rationale is None


def test_annotation_accepts_granular_response_cuts_and_captions_dimensions():
    for dimension in ("ai_guidance_and_response", "cuts", "captions"):
        payload = SaveAnnotationRequest(
            dimension=dimension,
            rating="mixed",
            rationale=f"Specific feedback about {dimension}",
        )
        assert payload.dimension == dimension

    with pytest.raises(ValidationError, match="unknown review dimension"):
        SaveAnnotationRequest(
            dimension="captions_and_text",
            rating="mixed",
            rationale="Combined ratings are no longer accepted",
        )


def test_annotation_range_requires_both_ordered_bounds():
    with pytest.raises(ValidationError, match="both start and end"):
        SaveAnnotationRequest(dimension="hook", rating="good", rationale="works", frame_start_s=1)
    with pytest.raises(ValidationError, match="end must be after start"):
        SaveAnnotationRequest(
            dimension="hook",
            rating="good",
            rationale="works",
            frame_start_s=2,
            frame_end_s=1,
        )


def test_bulk_annotations_require_distinct_dimensions():
    annotation = SaveAnnotationRequest(
        dimension="hook",
        rating="good",
        rationale="No issue noted in this review pass.",
    )
    with pytest.raises(ValidationError, match="unique dimensions"):
        SaveAnnotationsBulkRequest(annotations=[annotation, annotation])

    payload = SaveAnnotationsBulkRequest(
        annotations=[
            annotation,
            SaveAnnotationRequest(
                dimension="cuts",
                rating="good",
                rationale="No issue noted in this review pass.",
            ),
        ]
    )
    assert [row.dimension for row in payload.annotations] == ["hook", "cuts"]


def test_bulk_annotations_commit_once(monkeypatch):
    artifact = SimpleNamespace(id=uuid.uuid4())

    class FakeResult:
        def scalar_one(self):
            return artifact.id

    class FakeSession:
        commit_count = 0
        added = []

        def execute(self, _query):
            return FakeResult()

        def add_all(self, rows):
            self.added = rows

        def commit(self):
            self.commit_count += 1

        def refresh(self, _row):
            return None

    class FakeSessionContext:
        def __init__(self, db):
            self.db = db

        def __enter__(self):
            return self.db

        def __exit__(self, *_args):
            return False

    db = FakeSession()
    monkeypatch.setattr("app.database.sync_session", lambda: FakeSessionContext(db))
    monkeypatch.setattr(edit_feedback_route, "_load_eligible_artifact", lambda *_args: artifact)
    monkeypatch.setattr(
        edit_feedback_route,
        "_new_annotation_row",
        lambda _db, _artifact, req: SimpleNamespace(
            id=uuid.uuid4(),
            dimension=req.dimension,
            rating=req.rating,
            rationale=req.rationale,
            frame_start_ms=None,
            frame_end_ms=None,
            reviewer_identity="emir",
            created_at=datetime.now(UTC),
            supersedes_annotation_id=None,
        ),
    )
    request = SaveAnnotationsBulkRequest(
        annotations=[
            SaveAnnotationRequest(
                dimension=dimension,
                rating="good",
                rationale="No issue noted in this review pass.",
            )
            for dimension in ("hook", "cuts")
        ]
    )

    response = save_edit_feedback_annotations_bulk(artifact.id, request)

    assert db.commit_count == 1
    assert [row.dimension for row in db.added] == ["hook", "cuts"]
    assert [row.dimension for row in response.annotations] == ["hook", "cuts"]


def test_correction_keeps_history_and_selects_new_leaf():
    now = datetime.now(UTC)
    original = _Annotation(uuid.uuid4(), "hook", now)
    correction = _Annotation(
        uuid.uuid4(),
        "hook",
        now + timedelta(seconds=1),
        supersedes_annotation_id=original.id,
    )
    current, superseded_by = _current_annotations([original, correction])
    assert current["hook"] is correction
    assert superseded_by[original.id] == correction.id


def test_review_state_requires_every_dimension():
    assert _review_state({}) == "unreviewed"
    assert _review_state({"hook": object()}) == "needs_correction"
    from app.services.edit_training_dataset import REQUIRED_REVIEW_DIMENSIONS

    complete = {dimension: object() for dimension in REQUIRED_REVIEW_DIMENSIONS}
    assert _review_state(complete) == "reviewed"


def test_stratified_queue_round_robins_formats_models_and_edited_lineage():
    def row(
        artifact_id: str,
        *,
        format: str,
        model: str,
        parent: bool = False,
    ):
        artifact = SimpleNamespace(
            id=artifact_id,
            parent_artifact_id=uuid.uuid4() if parent else None,
        )
        payload = SimpleNamespace(
            format=format,
            language="en",
            media_mix="video",
            prompt_version="p1",
            model_version=model,
            edit_count=2 if parent else 0,
        )
        return artifact, format, payload

    rows = [
        row("montage-1", format="montage", model="m1"),
        row("montage-2", format="montage", model="m1"),
        row("story-1", format="guided_story", model="m2"),
        row("montage-edited", format="montage", model="m1", parent=True),
    ]

    ordered = _stratified_review_order(rows)

    assert {item[0].id for item in ordered[:3]} == {
        "montage-1",
        "story-1",
        "montage-edited",
    }
    assert ordered[-1][0].id == "montage-2"


def test_timeline_clamps_events_to_exact_artifact_duration():
    events = _timeline(
        {
            "moment_stages": [
                {"moment_id": "a", "media_id": "m1", "start_s": 0, "duration_s": 1},
                {"moment_id": "b", "media_id": "m2", "start_s": 1, "end_s": 9},
            ]
        },
        2.5,
    )
    assert [(event.start_s, event.end_s) for event in events] == [(0, 1), (1, 2.5)]


def test_invalid_cursor_is_422():
    with pytest.raises(Exception) as exc:
        _decode_cursor("not-a-cursor")
    assert getattr(exc.value, "status_code", None) == 422


def test_stratified_cursor_round_trips_global_queue_offset():
    assert _decode_stratified_cursor(_stratified_cursor(75)) == 75


def test_chronological_cursor_uses_raw_boundary_even_when_page_filters_to_empty():
    now = datetime.now(UTC)
    raw = [
        (SimpleNamespace(id=uuid.uuid4(), created_at=now - timedelta(seconds=index)), None)
        for index in range(3)
    ]

    cursor = _next_chronological_cursor(raw, 2)

    assert cursor is not None
    assert _decode_cursor(cursor) == (raw[1][0].created_at, raw[1][0].id)


def test_playback_signs_the_retained_copy_fresh_on_every_read(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "app.storage.signed_get_url",
        lambda path, expiration_minutes: (
            calls.append((path, expiration_minutes)) or f"https://signed/{len(calls)}"
        ),
    )
    retention = SimpleNamespace(
        id=uuid.uuid4(),
        event_type="copy",
        status="succeeded",
        storage_path="users/creator/edit-feedback/artifact/final.mp4",
        storage_generation="42",
        created_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )

    first, identity = _playback(SimpleNamespace(), [retention])
    second, _ = _playback(SimpleNamespace(), [retention])

    assert first == "https://signed/1"
    assert second == "https://signed/2"
    assert identity.endswith(":42")
    assert calls == [
        (retention.storage_path, 15),
        (retention.storage_path, 15),
    ]
