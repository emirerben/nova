"""KRI-374 lane B: dispatch of a creator-uploaded song (user_song candidates + gate)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.tasks.content_plan_build import PHONE_GATE_MESSAGES, _dispatch_item_render
from tests.tasks.test_content_plan_build import _phone_dispatch_item

SONG_PATH = "users/u/creation-threads/t/song-1.m4a"
FEATURES = ["basicComposition", "musicBed", "audioMix"]


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    song: bool = True,
    cloud: bool = False,
    flag: bool = True,
    features: list[str] | None = None,
    creator_strategy: dict | None = None,
    voiceover: bool = False,
    orphan: bool = False,
):
    item = _phone_dispatch_item("montage")
    if cloud:
        item.clip_gcs_paths = ["users/u/plan/i/clip.mp4"]
        item.clip_assignments = [
            {"media_id": "m0", "gcs_path": item.clip_gcs_paths[0], "storage_generation": "1"}
        ]
    item.audio_mode = "song" if (song or orphan) else "kria"
    item.song_gcs_path = SONG_PATH if song else None
    item.song_generation = 1234567
    item.song_duration_s = 187.5
    if voiceover:
        item.voiceover_gcs_path = "users/u/plan/i/voice.m4a"
    plan = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), preference_summary="", ownership_epoch=0
    )
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={}, all_candidates={"clip_paths": []})
    session = MagicMock()
    session.execute.return_value.scalar_one.return_value = 0
    session.execute.return_value.all.return_value = []

    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(settings, "user_song_montage_enabled", flag)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", FEATURES if features is None else features
    )
    bind_mock = MagicMock(return_value=("bound-source",))
    approved = {"proposal_version": 1, "media_digest": "d" * 64, "snapshot": {"media": []}}
    with (
        patch(
            "app.services.smart_captions.resolve_smart_captions_context_sync",
            return_value=None,
        ),
        patch(
            "app.services.edit_proposals.validate_approved_proposal_media_sync",
            return_value=(None, approved),
        ),
        patch("app.services.phone_sources.bind_phone_sources", bind_mock),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        result = _dispatch_item_render(
            session,
            item,
            plan,
            {"tone": "direct", "content_pillars": []},
            ownership_epoch=0,
            **({"creator_strategy": creator_strategy} if creator_strategy else {}),
        )
    return result, job, build


def test_song_dispatch_carries_user_song_candidates_and_no_voiceover(monkeypatch) -> None:
    result, job, build = _run(monkeypatch)

    assert result.outcome == "dispatched"
    assert job.all_candidates["user_song"] == {
        "gcs_path": SONG_PATH,
        "generation": 1234567,
        "duration_s": 187.5,
        "sync": "background",
    }
    assert build.call_args.kwargs["voiceover_gcs_path"] is None


def test_song_dispatch_never_passes_a_stored_voiceover(monkeypatch) -> None:
    result, job, build = _run(monkeypatch, voiceover=True)

    assert result.outcome == "dispatched"
    assert build.call_args.kwargs["voiceover_gcs_path"] is None
    assert "voiceover_gcs_path" not in job.all_candidates


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        ({"song_sync": "lipsync"}, "lipsync"),
        ({"song_sync": "background"}, "background"),
        ({"song_sync": "nonsense"}, "background"),
        ({"audio_strategy": "user_song"}, "background"),
    ],
)
def test_sync_comes_from_the_approved_strategy(monkeypatch, strategy, expected) -> None:
    # `song_sync` / "user_song" are lane C's schema additions; this test is about
    # dispatch, so keep strategy validation out of it (the strategy is a plain dict
    # here, exactly as the confirmed strategy reaches dispatch).
    monkeypatch.setattr(
        "app.tasks.content_plan_build._creator_selected_clip_paths",
        lambda _item, clip_paths, _strategy, **_kwargs: clip_paths,
    )
    result, job, _build = _run(monkeypatch, creator_strategy=strategy)

    assert result.outcome == "dispatched"
    assert job.all_candidates["user_song"]["sync"] == expected


@pytest.mark.parametrize(
    "why",
    ["flag_off", "musicbed_unverified", "audiomix_unverified", "cloud_destination"],
)
def test_song_is_refused_when_it_cannot_render_on_the_phone(monkeypatch, why) -> None:
    kwargs: dict = {}
    if why == "flag_off":
        kwargs["flag"] = False
    elif why == "musicbed_unverified":
        kwargs["features"] = ["basicComposition", "audioMix"]
    elif why == "audiomix_unverified":
        kwargs["features"] = ["basicComposition", "musicBed"]
    else:
        kwargs["cloud"] = True
    with patch("app.tasks.content_plan_build.log") as log:
        result, job, build = _run(monkeypatch, **kwargs)

    assert result.outcome == "invalid_clips"
    assert result.reason == "user_song_unavailable"
    build.assert_not_called()
    assert "user_song" not in job.all_candidates
    assert log.warning.call_args.kwargs["phone_gate"] == "user_song_unavailable"


def test_a_song_mode_row_without_a_song_is_treated_as_no_song(monkeypatch) -> None:
    # audio_mode == "song" but song_gcs_path is NULL -> not a song; the gate never fires
    # even with the kill switch off.
    result, job, _build = _run(monkeypatch, song=False, orphan=True, flag=False)

    assert result.outcome == "dispatched"
    assert "user_song" not in job.all_candidates


def test_gate_message_is_registered_with_a_stable_code() -> None:
    code, message = PHONE_GATE_MESSAGES["user_song_unavailable"]
    assert code == "phone_user_song_unavailable"
    assert "song" in message.lower() and "iPhone" in message
