from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from time import sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.tasks.creator_preparation as preparation
from app.agents._runtime import RunContext
from app.schemas.edit_proposal import MediaRef

CREATOR_ID = str(uuid4())
ATTEMPT_ID = uuid4()
TOKEN = "lease-token"


def _analysis(subject: str) -> dict:
    return {
        "source": "clip_metadata",
        "subject": subject,
        "analysis_version": 8,
    }


def _raw(
    name: str,
    *,
    generation: str = "42",
    media_id: str | None = None,
    asset_id: str | None = None,
    asset_status: str | None = None,
    analysis: dict | None = None,
) -> dict:
    value = {
        "media_id": media_id or name,
        "gcs_path": f"users/{CREATOR_ID}/{name}.mp4",
        "storage_generation": generation,
        "generation": generation,
        "kind": "video",
        "analysis": _analysis(name) if analysis is None else analysis,
    }
    if asset_id is not None:
        value.update(
            asset_id=asset_id,
            asset_status=asset_status or "ready",
        )
    return value


def _context() -> RunContext:
    return RunContext(
        creator_id=CREATOR_ID,
        creator_agent_session_id=str(uuid4()),
        request_id="preparation-test",
    )


def _ref(raw: dict, *, lane: str = "clip") -> MediaRef:
    return MediaRef(
        lane=lane,
        media_id=str(raw["media_id"]),
        gcs_path=str(raw["gcs_path"]),
        generation=str(raw["generation"] or raw["storage_generation"]),
        kind="video",
        analysis=dict(raw.get("analysis") or {}),
    )


def _install_graph_mocks(monkeypatch, sources: list[dict], checkpoints: list[dict]):
    graph = (
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        sources,
        [],
    )

    @contextmanager
    def session():
        yield SimpleNamespace()

    monkeypatch.setattr(preparation, "sync_session", session)
    monkeypatch.setattr(preparation, "_locked", lambda *_args, **_kwargs: graph)
    monkeypatch.setattr(preparation, "owns_attempt", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        preparation,
        "_checkpoint",
        lambda _identifier, _token, analyzed: checkpoints.append(dict(analyzed)),
    )


def test_analyze_sources_caps_concurrency_and_checkpoints_each_completion(monkeypatch) -> None:
    sources = [_raw(f"clip-{index}") for index in range(7)]
    checkpoints: list[dict] = []
    _install_graph_mocks(monkeypatch, sources, checkpoints)
    active = 0
    maximum = 0
    lock = Lock()

    def analyze(raw, pool, *, run_context, require_semantic):
        nonlocal active, maximum
        assert run_context.creator_id == CREATOR_ID
        assert require_semantic is True
        with lock:
            active += 1
            maximum = max(maximum, active)
        sleep(0.015)
        with lock:
            active -= 1
        return dict(raw), _ref(raw)

    monkeypatch.setattr(preparation, "analyze_clip_assignment", analyze)

    preparation._analyze_sources(ATTEMPT_ID, TOKEN, sources, _context())

    assert maximum == 3
    assert len(checkpoints) == len(sources)
    assert {row["media_id"] for row in checkpoints} == {row["media_id"] for row in sources}


def test_all_ready_cached_sources_still_call_helper_without_paid_analysis(monkeypatch) -> None:
    sources = [_raw("cached-1"), _raw("cached-2")]
    checkpoints: list[dict] = []
    _install_graph_mocks(monkeypatch, sources, checkpoints)
    calls: list[tuple[str, str, str]] = []

    def analyze(raw, pool, *, run_context, require_semantic):
        calls.append((raw["media_id"], raw["generation"], run_context.creator_id))
        assert require_semantic is True
        return dict(raw), _ref(raw)

    monkeypatch.setattr(preparation, "analyze_clip_assignment", analyze)

    preparation._analyze_sources(ATTEMPT_ID, TOKEN, sources, _context())

    assert calls == [("cached-1", "42", CREATOR_ID), ("cached-2", "42", CREATOR_ID)]
    assert len(checkpoints) == 2


def test_mixed_pool_and_raw_preserve_aliases_and_exact_pool_generation(monkeypatch) -> None:
    pool = _raw("pool-image", media_id="asset-alias", asset_id="asset-1")
    raw = _raw("recorded", media_id="clip-alias")
    sources = [pool, raw]
    checkpoints: list[dict] = []
    _install_graph_mocks(monkeypatch, sources, checkpoints)
    seen_pool: dict[str, MediaRef] = {}

    def analyze(raw, pool, *, run_context, require_semantic):
        seen_pool.update(pool)
        return dict(raw), _ref(raw)

    monkeypatch.setattr(preparation, "analyze_clip_assignment", analyze)

    preparation._analyze_sources(ATTEMPT_ID, TOKEN, sources, _context())

    assert seen_pool[pool["gcs_path"]].lane == "asset"
    assert seen_pool[pool["gcs_path"]].media_id == "asset-alias"
    assert seen_pool[pool["gcs_path"]].generation == "42"
    assert {row["media_id"] for row in checkpoints} == {"asset-alias", "clip-alias"}


def test_empty_sources_and_all_pool_assets_never_use_invalid_pool_lane(monkeypatch) -> None:
    checkpoints: list[dict] = []
    _install_graph_mocks(monkeypatch, [], checkpoints)
    calls: list[dict] = []

    def analyze(raw, pool, *, run_context, require_semantic):
        calls.append(dict(raw))
        assert all(ref.lane == "asset" for ref in pool.values())
        return dict(raw), _ref(raw)

    monkeypatch.setattr(preparation, "analyze_clip_assignment", analyze)
    preparation._analyze_sources(ATTEMPT_ID, TOKEN, [], _context())
    assert calls == []

    pool_source = _raw("only-pool", media_id="asset-alias", asset_id="asset-1")
    checkpoints.clear()
    calls.clear()
    _install_graph_mocks(monkeypatch, [pool_source], checkpoints)
    preparation._analyze_sources(ATTEMPT_ID, TOKEN, [pool_source], _context())
    assert len(calls) == 1
    assert checkpoints[0]["media_id"] == "asset-alias"


def test_pending_pool_does_not_block_completed_raw_checkpoint(monkeypatch) -> None:
    raw = _raw("recorded")
    pending = _raw(
        "pending-pool",
        media_id="asset-pending",
        asset_id="asset-pending",
        asset_status="analyzing",
        generation="",
        analysis=None,
    )
    sources = [raw, pending]
    checkpoints: list[dict] = []
    _install_graph_mocks(monkeypatch, sources, checkpoints)
    monkeypatch.setattr(
        preparation,
        "analyze_clip_assignment",
        lambda value, pool, **_kwargs: (dict(value), _ref(value)),
    )

    with pytest.raises(preparation.PreparationPending):
        preparation._analyze_sources(ATTEMPT_ID, TOKEN, sources, _context())

    assert [row["media_id"] for row in checkpoints] == ["recorded"]


def test_failed_helper_stops_scheduling_and_has_no_late_checkpoints(monkeypatch) -> None:
    sources = [_raw(f"clip-{index}") for index in range(8)]
    checkpoints: list[dict] = []
    _install_graph_mocks(monkeypatch, sources, checkpoints)
    calls: list[str] = []
    lock = Lock()

    def analyze(raw, pool, **_kwargs):
        with lock:
            calls.append(raw["media_id"])
        if raw["media_id"] == "clip-1":
            raise RuntimeError("provider failed")
        sleep(0.02)
        return dict(raw), _ref(raw)

    monkeypatch.setattr(preparation, "analyze_clip_assignment", analyze)

    with pytest.raises(RuntimeError, match="provider failed"):
        preparation._analyze_sources(ATTEMPT_ID, TOKEN, sources, _context())

    call_count = len(calls)
    checkpoint_count = len(checkpoints)
    sleep(0.05)
    assert len(calls) == call_count
    assert len(checkpoints) == checkpoint_count
    assert set(calls).issubset({"clip-0", "clip-1", "clip-2"})
