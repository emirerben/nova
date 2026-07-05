"""Plan 007 Fix 1: the zero-click autoplace chain after _finalize_job.

Pins the eng-review decisions:
  CRITICAL-1 — hook gated on content_plan_item_id (public generative jobs never chain)
  CRITICAL-3 — autoplace_attempted marker: one dispatch per render generation, ever
  G2-A       — speech-bearing variants only (music_track_id None)
  G3-A       — OVERLAY_AUTOAPPLY_ENABLED drives auto_apply; autoplace flag gates all
Plus the shared apply helper (G1-A): overlap drop, suggestion clearing, no commit.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from app.services.overlay_apply import apply_suggestions_to_variant
from app.tasks.generative_build import _maybe_autoplace_after_finalize

JOB_ID = str(uuid.uuid4())


def _job(variants: list[dict], *, item_id=None) -> MagicMock:
    job = MagicMock()
    job.id = uuid.UUID(JOB_ID)
    job.user_id = uuid.uuid4()
    job.content_plan_item_id = item_id
    job.assembly_plan = {"variants": variants}
    return job


def _db_for(job, ready_assets: int = 2) -> MagicMock:
    db = MagicMock()
    db.get.return_value = job
    result = MagicMock()
    result.scalar_one.return_value = ready_assets
    db.execute.return_value = result
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=db)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, db


def _variant(vid="original_text", *, track=None, attempted=False, status="ready") -> dict:
    v = {
        "variant_id": vid,
        "render_status": status,
        "video_path": f"generative-jobs/x/{vid}.mp4",
        "music_track_id": track,
    }
    if attempted:
        v["autoplace_attempted"] = True
    return v


CHAIN = "app.tasks.generative_build"


def _run(job, *, autoplace=True, autoapply=True, ready_assets=2):
    ctx, db = _db_for(job, ready_assets)
    dispatched = []
    task = MagicMock()
    task.apply_async = MagicMock(side_effect=lambda **kw: dispatched.append(kw))
    with (
        patch(f"{CHAIN}._sync_session", return_value=ctx),
        patch("app.config.settings") as mock_settings,
        patch("app.tasks.autoplace.match_overlay_suggestions", task),
    ):
        mock_settings.overlay_autoplace_enabled = autoplace
        mock_settings.overlay_autoapply_enabled = autoapply
        mock_settings.autoplace_queue = "autoplace-jobs"
        _maybe_autoplace_after_finalize(JOB_ID)
    return dispatched, db


def test_flag_off_is_byte_identical() -> None:
    job = _job([_variant()], item_id=uuid.uuid4())
    dispatched, db = _run(job, autoplace=False)
    assert dispatched == []
    db.commit.assert_not_called()


def test_public_generative_job_never_chains() -> None:
    job = _job([_variant()], item_id=None)  # no content_plan_item_id
    dispatched, _ = _run(job)
    assert dispatched == []


def test_empty_pool_never_chains() -> None:
    job = _job([_variant()], item_id=uuid.uuid4())
    dispatched, _ = _run(job, ready_assets=0)
    assert dispatched == []


def test_song_variants_skipped_speech_dispatched() -> None:
    job = _job(
        [
            _variant("original_text"),
            _variant("song_text", track="track-1"),
            _variant("song_lyrics", track="track-1"),
        ],
        item_id=uuid.uuid4(),
    )
    dispatched, db = _run(job)
    assert len(dispatched) == 1
    assert dispatched[0]["args"][1] == "original_text"
    assert dispatched[0]["kwargs"] == {"auto_apply": True}
    db.commit.assert_called_once()  # attempted marker persisted


def test_attempted_marker_prevents_refire() -> None:
    job = _job([_variant(attempted=True)], item_id=uuid.uuid4())
    dispatched, _ = _run(job)
    assert dispatched == []


def test_marker_set_before_dispatch() -> None:
    job = _job([_variant()], item_id=uuid.uuid4())
    _run(job)
    v = job.assembly_plan["variants"][0]
    assert v["autoplace_attempted"] is True


def test_autoapply_flag_off_dispatches_suggest_only() -> None:
    job = _job([_variant()], item_id=uuid.uuid4())
    dispatched, _ = _run(job, autoapply=False)
    assert len(dispatched) == 1
    assert dispatched[0]["kwargs"] == {"auto_apply": False}


# ── shared apply helper (G1-A) ────────────────────────────────────────────────


def _envelope(start: float, end: float, *, sfx: bool = False) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "asset_id": "a1",
        "confidence_tier": "confident",
        "reason": "r",
        "overlay": {
            "id": uuid.uuid4().hex,
            "kind": "image",
            "src_gcs_path": "users/u/plan/i/pool/x.png",
            "start_s": start,
            "end_s": end,
        },
        "sfx": (
            {
                "id": uuid.uuid4().hex,
                "at_s": start,
                "gain": 1.0,
                "src_gcs_path": "sound-effects/pop.mp3",
            }
            if sfx
            else None
        ),
    }


def test_helper_merges_clears_and_dispatches_once() -> None:
    variant = {
        "variant_id": "original_text",
        "media_overlays": None,
        "sound_effects": None,
        "overlay_suggestions": ["stale"],
        "overlay_suggest_status": "ready",
        "overlay_suggest_hash": "h",
        "overlay_suggest_wishlist": None,
    }
    job = _job([variant], item_id=uuid.uuid4())
    with (
        patch("app.routes.generative_jobs.dispatch_set_media_overlays") as dispatch,
        patch("app.config.settings.sound_effects_enabled", True),
    ):
        result = apply_suggestions_to_variant(
            job, "original_text", [_envelope(3.0, 6.0, sfx=True)], user_id="u"
        )
    assert result["applied"] == 1 and result["dispatched"] is True
    dispatch.assert_called_once()
    v = job.assembly_plan["variants"][0]
    assert v["overlay_suggestions"] is None
    assert v["overlay_suggest_status"] is None
    assert len(v["sound_effects"]) == 1


def test_helper_drops_sfx_when_flag_off() -> None:
    """Review C15: partial rollout (autoapply on / SOUND_EFFECTS off) must not
    persist SFX children that never bake and are invisible in the FE."""
    variant = {"variant_id": "original_text", "media_overlays": None, "sound_effects": None}
    job = _job([variant], item_id=uuid.uuid4())
    with (
        patch("app.routes.generative_jobs.dispatch_set_media_overlays"),
        patch("app.config.settings.sound_effects_enabled", False),
    ):
        result = apply_suggestions_to_variant(
            job, "original_text", [_envelope(3.0, 6.0, sfx=True)], user_id="u"
        )
    assert result["applied"] == 1 and result["sfx"] == 0
    assert job.assembly_plan["variants"][0]["sound_effects"] is None


def test_helper_drops_cross_tenant_sfx() -> None:
    """Review C5/C16: a crafted SFX src_gcs_path under another user's namespace
    is rejected (glossary paths and this user's paths pass)."""
    variant = {"variant_id": "original_text", "media_overlays": None, "sound_effects": None}
    job = _job([variant], item_id=uuid.uuid4())
    env = _envelope(3.0, 6.0, sfx=True)
    env["sfx"]["src_gcs_path"] = "users/someone-else/plan/i/voiceover.mp3"
    with (
        patch("app.routes.generative_jobs.dispatch_set_media_overlays"),
        patch("app.config.settings.sound_effects_enabled", True),
    ):
        result = apply_suggestions_to_variant(job, "original_text", [env], user_id="u")
    assert result["applied"] == 1 and result["sfx"] == 0
    assert job.assembly_plan["variants"][0]["sound_effects"] is None


def test_helper_drops_overlapping_edited_card() -> None:
    variant = {
        "variant_id": "original_text",
        "media_overlays": [{"start_s": 2.0, "end_s": 7.0}],
        "sound_effects": None,
        "overlay_suggestions": None,
    }
    job = _job([variant], item_id=uuid.uuid4())
    with patch("app.routes.generative_jobs.dispatch_set_media_overlays") as dispatch:
        result = apply_suggestions_to_variant(
            job,
            "original_text",
            [_envelope(3.0, 6.0), _envelope(10.0, 13.0)],
            user_id="u",
        )
    assert result["applied"] == 1 and result["dropped"] == 1
    dispatch.assert_called_once()


def test_helper_all_dropped_means_no_dispatch() -> None:
    variant = {
        "variant_id": "original_text",
        "media_overlays": [{"start_s": 2.0, "end_s": 7.0}],
        "sound_effects": None,
    }
    job = _job([variant], item_id=uuid.uuid4())
    with patch("app.routes.generative_jobs.dispatch_set_media_overlays") as dispatch:
        result = apply_suggestions_to_variant(
            job, "original_text", [_envelope(3.0, 6.0)], user_id="u"
        )
    assert result["dispatched"] is False
    dispatch.assert_not_called()
