"""SFX suggestion chain after _finalize_job (word-level sound design, dark-flagged).

Mirrors test_autoplace_chain.py's guard suite for `_maybe_sfx_autoplace_after_finalize`:
  - SFX_AUTOPLACE_ENABLED off ⇒ byte-identical no-op (no dispatch, no commit)
  - public generative jobs (no content_plan_item_id) never chain
  - `sfx_autoplace_attempted` marker: one dispatch per render generation, ever,
    persisted under the row lock BEFORE apply_async
  - speech-plausibility: persisted word source OR no matched music track
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.tasks.generative_build import _maybe_sfx_autoplace_after_finalize

JOB_ID = str(uuid.uuid4())
CHAIN = "app.tasks.generative_build"


def _job(variants: list[dict], *, item_id=None) -> MagicMock:
    job = MagicMock()
    job.id = uuid.UUID(JOB_ID)
    job.user_id = uuid.uuid4()
    job.content_plan_item_id = item_id
    job.assembly_plan = {"variants": variants}
    return job


def _ctx_for(job) -> tuple[MagicMock, MagicMock]:
    db = MagicMock()
    db.get.return_value = job
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=db)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, db


def _variant(vid="original_text", *, track=None, attempted=False, status="ready", words=False):
    v = {
        "variant_id": vid,
        "render_status": status,
        "video_path": f"generative-jobs/x/{vid}.mp4",
        "music_track_id": track,
    }
    if attempted:
        v["sfx_autoplace_attempted"] = True
    if words:
        v["transcript"] = [{"word": "hello", "start_s": 0.5, "end_s": 1.0}]
    return v


def _run(job, *, enabled=True):
    ctx, db = _ctx_for(job)
    dispatched = []
    task = MagicMock()
    task.apply_async = MagicMock(side_effect=lambda **kw: dispatched.append(kw))
    with (
        patch(f"{CHAIN}._sync_session", return_value=ctx),
        patch("app.config.settings") as mock_settings,
        patch("app.tasks.autoplace.autoplace_sfx_suggestions", task),
    ):
        mock_settings.sfx_autoplace_enabled = enabled
        mock_settings.autoplace_queue = "autoplace-jobs"
        _maybe_sfx_autoplace_after_finalize(JOB_ID)
    return dispatched, db


def test_flag_off_is_byte_identical() -> None:
    job = _job([_variant()], item_id=uuid.uuid4())
    dispatched, db = _run(job, enabled=False)
    assert dispatched == []
    db.commit.assert_not_called()
    db.get.assert_not_called()  # flag gate fires before any DB read


def test_public_generative_job_never_chains() -> None:
    job = _job([_variant()], item_id=None)
    dispatched, db = _run(job)
    assert dispatched == []
    db.commit.assert_not_called()


def test_song_variant_without_words_skipped_speech_dispatched() -> None:
    job = _job(
        [
            _variant("original_text"),  # no track → whisper-plausible
            _variant("song_text", track="track-1"),  # track + no words → skip
            _variant("song_seq", track="track-1", words=True),  # persisted words → eligible
        ],
        item_id=uuid.uuid4(),
    )
    dispatched, db = _run(job)
    assert [d["args"][1] for d in dispatched] == ["original_text", "song_seq"]
    db.commit.assert_called_once()


def test_attempted_marker_prevents_refire() -> None:
    job = _job([_variant(attempted=True)], item_id=uuid.uuid4())
    dispatched, db = _run(job)
    assert dispatched == []
    db.commit.assert_not_called()


def test_marker_set_and_committed_before_dispatch() -> None:
    job = _job([_variant()], item_id=uuid.uuid4())
    order: list[str] = []
    ctx, db = _ctx_for(job)
    db.commit = MagicMock(side_effect=lambda: order.append("commit"))
    task = MagicMock()
    task.apply_async = MagicMock(side_effect=lambda **kw: order.append("dispatch"))
    with (
        patch(f"{CHAIN}._sync_session", return_value=ctx),
        patch("app.config.settings") as mock_settings,
        patch("app.tasks.autoplace.autoplace_sfx_suggestions", task),
    ):
        mock_settings.sfx_autoplace_enabled = True
        mock_settings.autoplace_queue = "autoplace-jobs"
        _maybe_sfx_autoplace_after_finalize(JOB_ID)
    assert order == ["commit", "dispatch"]
    assert job.assembly_plan["variants"][0]["sfx_autoplace_attempted"] is True


def test_unrendered_variant_never_dispatches() -> None:
    job = _job(
        [_variant(status="rendering"), {"variant_id": "failed", "render_status": "failed"}],
        item_id=uuid.uuid4(),
    )
    dispatched, _ = _run(job)
    assert dispatched == []
