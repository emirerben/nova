"""Unit tests for app/services/clip_prefetch.py.

What we lock in here:
  1. Path validation rejects everything that isn't a real upload-batch path
     produced by /presigned-urls or the Drive importer.
  2. Within one process, scheduling the same path twice deduplicates.
  3. The prefetch task is a no-op when the cache already has an entry
     (idempotency at the cache-key level, not just the in-flight set).
  4. Every external failure (download error, hash error, Gemini error)
     is swallowed — the task never raises.
  5. The cache write only happens on the full success path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import clip_prefetch

# Match the regex in `is_valid_prefetch_path` — must mirror the
# `{user_uuid}/batch-{batch_id}/clip_NNN.ext` shape produced by
# routes/presigned.py and routes/uploads.py drive batch import.
_VALID_PATH = (
    "00000000-0000-0000-0000-000000000001/batch-abc123def456/clip_001.mp4"
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Clear module-level dedup set + semaphore between tests."""
    clip_prefetch.reset_state_for_tests()
    yield
    clip_prefetch.reset_state_for_tests()


# ── path validation ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        # Happy path — exactly what the upload endpoints generate.
        (_VALID_PATH, True),
        # Different file extensions still match.
        (
            "00000000-0000-0000-0000-000000000001/batch-abc123def456/clip_999.mov",
            True,
        ),
        # Path traversal attempts.
        ("../../../etc/passwd", False),
        ("uploads/../../secrets/key.txt", False),
        # Arbitrary GCS object that isn't an upload-batch.
        ("templates/template-abc/video.mp4", False),
        # Wrong user-id shape.
        ("not-a-uuid/batch-abc123def456/clip_001.mp4", False),
        # Wrong batch-id shape.
        ("00000000-0000-0000-0000-000000000001/batch-xyz/clip_001.mp4", False),
        # Missing clip index.
        ("00000000-0000-0000-0000-000000000001/batch-abc123def456/raw.mp4", False),
        # Empty.
        ("", False),
    ],
)
def test_is_valid_prefetch_path_whitelist(path: str, expected: bool) -> None:
    assert clip_prefetch.is_valid_prefetch_path(path) is expected


# ── dedup ────────────────────────────────────────────────────────────────────


def _make_meta(clip_id: str = "c1") -> object:
    """Minimal ClipMeta-shaped object — what analyze_clip would return."""
    return SimpleNamespace(
        clip_id=clip_id,
        transcript="",
        hook_text="",
        hook_score=5.0,
        best_moments=[],
        analysis_degraded=False,
        clip_path="",
    )


async def _run_with_stubs(
    *,
    download_side_effect=None,
    hash_return: str | None = "deadbeef",
    cache_get_return=None,
    upload_side_effect=None,
    analyze_side_effect=None,
    set_cached_meta=None,
) -> dict:
    """Run schedule_prefetch + wait for the task, return what each stub saw."""
    cache_set = MagicMock() if set_cached_meta is None else set_cached_meta
    stubs = {
        "download_to_file": MagicMock(side_effect=download_side_effect),
        "compute_clip_hash": MagicMock(return_value=hash_return),
        "get_cached_meta": MagicMock(return_value=cache_get_return),
        "gemini_upload_and_wait": MagicMock(
            return_value=MagicMock(uri="gemini://ref"),
            side_effect=upload_side_effect,
        ),
        "analyze_clip": MagicMock(
            return_value=_make_meta(),
            side_effect=analyze_side_effect,
        ),
        "set_cached_meta": cache_set,
    }
    with (
        patch("app.storage.download_to_file", stubs["download_to_file"]),
        patch(
            "app.pipeline.clip_cache.compute_clip_hash",
            stubs["compute_clip_hash"],
        ),
        patch(
            "app.pipeline.clip_cache.get_cached_meta",
            stubs["get_cached_meta"],
        ),
        patch(
            "app.pipeline.clip_cache.set_cached_meta",
            stubs["set_cached_meta"],
        ),
        patch(
            "app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait",
            stubs["gemini_upload_and_wait"],
        ),
        patch(
            "app.pipeline.agents.gemini_analyzer.analyze_clip",
            stubs["analyze_clip"],
        ),
    ):
        scheduled = await clip_prefetch.schedule_prefetch(_VALID_PATH, "")
        # Yield control until every spawned task is done. asyncio.create_task
        # doesn't return a handle from schedule_prefetch, so we just drain
        # all currently-pending tasks.
        await _drain_tasks()
    return {"scheduled": scheduled, **stubs}


async def _drain_tasks() -> None:
    """Wait for all currently-pending tasks except the test's own."""
    me = asyncio.current_task()
    others = [t for t in asyncio.all_tasks() if t is not me]
    if others:
        await asyncio.gather(*others, return_exceptions=True)


@pytest.mark.asyncio
async def test_invalid_path_is_rejected_silently() -> None:
    scheduled = await clip_prefetch.schedule_prefetch("../../bad", "")
    assert scheduled is False


@pytest.mark.asyncio
async def test_duplicate_schedule_returns_false() -> None:
    # Patch enough that the inner task can't actually do work — we just want
    # to know whether schedule_prefetch returns True/False on the second call.
    with patch("app.storage.download_to_file", MagicMock()):
        first = await clip_prefetch.schedule_prefetch(_VALID_PATH, "")
        second = await clip_prefetch.schedule_prefetch(_VALID_PATH, "")
    assert first is True
    assert second is False


# ── prefetch body ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_success_writes_cache() -> None:
    stubs = await _run_with_stubs()
    assert stubs["set_cached_meta"].called
    # Cache write key signature: (clip_hash, filter_hint, meta)
    args, _ = stubs["set_cached_meta"].call_args
    assert args[0] == "deadbeef"
    assert args[1] == ""


@pytest.mark.asyncio
async def test_cache_hit_skips_gemini() -> None:
    """If the entry already exists in Redis, we must NOT burn another
    Gemini upload/analyse on the same clip."""
    stubs = await _run_with_stubs(cache_get_return=_make_meta())
    stubs["gemini_upload_and_wait"].assert_not_called()
    stubs["analyze_clip"].assert_not_called()
    stubs["set_cached_meta"].assert_not_called()


@pytest.mark.asyncio
async def test_download_failure_is_swallowed_and_skips_rest() -> None:
    stubs = await _run_with_stubs(download_side_effect=RuntimeError("404 not yet finalized"))
    # No subsequent work happens — download failure is terminal for this
    # prefetch attempt. Orchestrator will retry the download itself later.
    stubs["compute_clip_hash"].assert_not_called()
    stubs["gemini_upload_and_wait"].assert_not_called()
    stubs["set_cached_meta"].assert_not_called()


@pytest.mark.asyncio
async def test_hash_failure_is_swallowed() -> None:
    stubs = await _run_with_stubs(hash_return=None)
    # No cache read or write when the file can't be hashed (e.g. deleted
    # mid-prefetch). Gemini upload also skipped — without a hash we
    # couldn't cache the result anyway.
    stubs["get_cached_meta"].assert_not_called()
    stubs["gemini_upload_and_wait"].assert_not_called()


@pytest.mark.asyncio
async def test_gemini_upload_failure_is_swallowed_no_cache_write() -> None:
    stubs = await _run_with_stubs(upload_side_effect=RuntimeError("Gemini 503"))
    stubs["analyze_clip"].assert_not_called()
    stubs["set_cached_meta"].assert_not_called()


@pytest.mark.asyncio
async def test_gemini_analyze_failure_is_swallowed_no_cache_write() -> None:
    stubs = await _run_with_stubs(analyze_side_effect=RuntimeError("Gemini refused"))
    # Important: we should NOT poison the cache with a degraded fallback.
    # set_cached_meta has its own filter for that but the caller shouldn't
    # even reach it on the failure path.
    stubs["set_cached_meta"].assert_not_called()


@pytest.mark.asyncio
async def test_in_flight_count_returns_to_zero_after_completion() -> None:
    await _run_with_stubs()
    assert clip_prefetch.in_flight_count() == 0


@pytest.mark.asyncio
async def test_in_flight_count_returns_to_zero_after_failure() -> None:
    await _run_with_stubs(download_side_effect=RuntimeError("boom"))
    assert clip_prefetch.in_flight_count() == 0
