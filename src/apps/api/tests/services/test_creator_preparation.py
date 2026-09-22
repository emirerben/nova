from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.creator_preparation import (
    finish_preparation,
    owns_attempt,
    public_preparation,
    retry_inputs,
    source_digest,
    source_snapshot,
)


def _assignment(path: str, *, media_id: str | None = None, kind: str = "video") -> dict:
    value = {"gcs_path": path, "kind": kind, "analysis": {"subject": "private"}}
    if media_id is not None:
        value["media_id"] = media_id
    return value


def _asset(asset_id: str, path: str, *, status: str = "ready", generation: str = "7"):
    return SimpleNamespace(
        id=asset_id,
        gcs_path=path,
        gcs_generation=generation,
        kind="image",
        analysis={"subject": "asset-private"},
        duration_s=1.5,
        aspect=1.0,
        status=status,
    )


def test_public_preparation_returns_only_allowlisted_progress_fields() -> None:
    session = SimpleNamespace(
        preparation={
            "attempt_id": "private-attempt-id",
            "status": "analyzing",
            "completed": 1,
            "total": 3,
            "message": "safe progress",
            "error_code": "provider_outcome_unknown",
            "retryable": True,
            "inputs": {"private": "creator instruction"},
            "source_digest": "private-digest",
        }
    )

    assert public_preparation(session) == {
        "status": "analyzing",
        "completed": 1,
        "total": 3,
        "message": "safe progress",
        "error_code": "provider_outcome_unknown",
        "retryable": True,
    }


@pytest.mark.parametrize("preparation", [None, [], "not-a-projection"])
def test_public_preparation_omits_invalid_projection(preparation) -> None:
    assert public_preparation(SimpleNamespace(preparation=preparation)) is None


def test_source_snapshot_preserves_assignment_then_pool_order_and_aliases() -> None:
    item = SimpleNamespace(
        clip_assignments=[
            _assignment("users/u/first.mp4"),
            _assignment("users/u/second.mp4", media_id="recorded-2", kind="image"),
        ],
        clip_gcs_paths=["legacy/ignored.mp4"],
    )

    sources = source_snapshot(item, [_asset("asset-9", "users/u/pool.png")])

    assert [source["gcs_path"] for source in sources] == [
        "users/u/first.mp4",
        "users/u/second.mp4",
        "users/u/pool.png",
    ]
    assert [source["media_id"] for source in sources] == [
        "clip-1",
        "recorded-2",
        "asset-asset-9",
    ]
    assert sources[0]["kind"] == "video"
    assert sources[1]["kind"] == "image"
    assert sources[2]["storage_generation"] == "7"
    assert sources[2]["generation"] == "7"
    assert sources[2]["analysis"] == {"subject": "asset-private"}


def test_source_snapshot_uses_legacy_paths_only_without_assignments_or_assets() -> None:
    item = SimpleNamespace(
        clip_assignments=[],
        clip_gcs_paths=["legacy/one.mp4", "legacy/two.mp4"],
    )

    assert source_snapshot(item, []) == [
        {"media_id": "legacy-clip-1", "gcs_path": "legacy/one.mp4", "kind": "video"},
        {"media_id": "legacy-clip-2", "gcs_path": "legacy/two.mp4", "kind": "video"},
    ]


def test_source_digest_is_stable_for_analysis_updates() -> None:
    original = [
        {
            "media_id": "clip-1",
            "asset_id": "asset-1",
            "gcs_path": "users/u/clip.mp4",
            "storage_generation": "12",
            "kind": "video",
            "analysis": {"subject": "old"},
        }
    ]
    updated = [{**original[0], "analysis": {"subject": "new", "duration": 4.2}}]

    assert source_digest(original) == source_digest(updated)


@pytest.mark.parametrize(
    "change",
    [
        {"gcs_path": "users/u/replaced.mp4"},
        {"storage_generation": "13"},
        {"kind": "image"},
        {"asset_id": "asset-2"},
    ],
)
def test_source_digest_changes_for_source_identity_changes(change) -> None:
    original = [
        {
            "media_id": "clip-1",
            "asset_id": "asset-1",
            "gcs_path": "users/u/clip.mp4",
            "storage_generation": "12",
            "kind": "video",
        }
    ]

    assert source_digest(original) != source_digest([{**original[0], **change}])


def _ownership_graph():
    creator_id = uuid4()
    plan_id = uuid4()
    item_id = uuid4()
    session_id = uuid4()
    sources = [
        {
            "media_id": "clip-1",
            "gcs_path": "users/u/clip.mp4",
            "storage_generation": "12",
            "kind": "video",
        }
    ]
    plan = SimpleNamespace(
        id=plan_id,
        user_id=creator_id,
        ownership_epoch=4,
        ownership_quarantined_at=None,
    )
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id)
    session = SimpleNamespace(
        id=session_id,
        creator_id=creator_id,
        plan_item_id=item_id,
        ownership_epoch=4,
        revision=8,
        status="planning",
    )
    attempt = SimpleNamespace(
        creator_id=creator_id,
        plan_item_id=item_id,
        ownership_epoch=4,
        session_revision=8,
        source_digest=source_digest(sources),
    )
    return attempt, session, plan, item, sources


def test_owns_attempt_accepts_current_owned_graph() -> None:
    assert owns_attempt(*_ownership_graph())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda attempt, session, plan, item: setattr(attempt, "ownership_epoch", 5),
        lambda attempt, session, plan, item: setattr(session, "creator_id", uuid4()),
        lambda attempt, session, plan, item: setattr(
            plan, "ownership_quarantined_at", datetime.now(UTC)
        ),
        lambda attempt, session, plan, item: setattr(session, "status", "cancelled"),
        lambda attempt, session, plan, item: setattr(session, "revision", 9),
    ],
)
def test_owns_attempt_rejects_stale_or_transferred_ownership(mutate) -> None:
    attempt, session, plan, item, sources = _ownership_graph()
    mutate(attempt, session, plan, item)

    assert not owns_attempt(attempt, session, plan, item, sources)


@pytest.mark.parametrize(
    "sources",
    [
        [],
        [{"media_id": "clip-1", "gcs_path": "users/u/new.mp4", "kind": "video"}],
    ],
)
def test_owns_attempt_rejects_removed_or_replaced_source(sources) -> None:
    attempt, session, plan, item, _current_sources = _ownership_graph()

    assert not owns_attempt(attempt, session, plan, item, sources)


@pytest.mark.asyncio
async def test_retry_inputs_returns_saved_original_request() -> None:
    attempt_id = uuid4()
    session_id = uuid4()
    creator_id = uuid4()
    attempt = SimpleNamespace(
        id=attempt_id,
        session_id=session_id,
        creator_id=creator_id,
        inputs={
            "user_message": "Make the beach clips feel cinematic.",
            "previous_active_plan": {"theme": "summer"},
            "private_source_text": "must not be replayed",
        },
    )
    session = SimpleNamespace(
        id=session_id,
        creator_id=creator_id,
        preparation={"attempt_id": str(attempt_id), "status": "failed"},
    )

    class FakeDB:
        async def get(self, model, identifier):
            assert identifier == attempt_id
            return attempt

    assert await retry_inputs(FakeDB(), session, " Retry preparing my clips. ") == {
        "user_message": "Make the beach clips feel cinematic.",
        "previous_active_plan": {"theme": "summer"},
    }


@pytest.mark.asyncio
async def test_finish_preparation_locks_and_settles_attempt_in_same_transaction() -> None:
    attempt_id = uuid4()
    session_id = uuid4()
    attempt = SimpleNamespace(
        id=attempt_id,
        session_id=session_id,
        status="running",
        lease_until=datetime.now(UTC),
        error_code="old-error",
    )
    session = SimpleNamespace(
        id=session_id,
        status="awaiting_confirmation",
        last_error=None,
        preparation={
            "attempt_id": str(attempt_id),
            "status": "resolving",
            "completed": 2,
            "total": 2,
        },
    )

    class FakeDB:
        def __init__(self):
            self.get_calls = []

        async def get(self, model, identifier, **kwargs):
            self.get_calls.append((model, identifier, kwargs))
            return attempt

    db = FakeDB()
    await finish_preparation(db, session)

    assert db.get_calls[0][1:] == (attempt_id, {"with_for_update": True})
    assert attempt.status == "completed"
    assert attempt.lease_until is None
    assert attempt.error_code is None
    assert session.preparation == {
        "attempt_id": str(attempt_id),
        "status": "ready",
        "completed": 2,
        "total": 2,
        "message": "Your clips are ready.",
        "error_code": None,
        "retryable": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,retryable",
    [
        ("provider_outcome_unknown", False),
        ("clip_media_unavailable", False),
        ("provider_quota_exceeded", True),
    ],
)
async def test_failed_preparation_preserves_recovery_policy(code, retryable):
    attempt_id, session_id = uuid4(), uuid4()
    attempt = SimpleNamespace(
        id=attempt_id, session_id=session_id, status="running", lease_until=datetime.now(UTC)
    )
    session = SimpleNamespace(
        id=session_id,
        status="briefing",
        last_error={"code": code},
        preparation={
            "attempt_id": str(attempt_id),
            "status": "resolving",
            "completed": 2,
            "total": 2,
        },
    )

    class FakeDB:
        async def get(self, *_args, **_kwargs):
            return attempt

    await finish_preparation(FakeDB(), session)
    assert session.preparation["error_code"] == code
    assert session.preparation["retryable"] is retryable
    assert attempt.status == "failed"
