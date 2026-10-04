"""KRI-374: `_detect_music_beats` lifted into app/pipeline/music_beats.py."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.pipeline import music_beats
from app.services.plan_item_media import (
    PROTECTED_PLAN_ITEM_MEDIA_FIELDS,
    mutate_plan_item_media,
)
from app.tasks import music_orchestrate


def test_the_catalog_task_delegates_to_the_shared_detector() -> None:
    with patch.object(music_orchestrate, "detect_music_beats", return_value=[0.5, 1.0]) as shared:
        assert music_orchestrate._detect_music_beats("a.mp3", 0.2) == [0.5, 1.0]
    shared.assert_called_once_with("a.mp3", 0.2)


def test_ffmpeg_failure_returns_no_beats() -> None:
    failed = SimpleNamespace(returncode=1, stdout="", stderr="boom")
    with patch.object(music_beats.subprocess, "run", return_value=failed):
        assert music_beats.detect_music_beats("a.mp3") == []


def test_too_few_frames_returns_no_beats() -> None:
    sparse = SimpleNamespace(returncode=0, stdout="frame:0 pts_time:0.0\n", stderr="")
    with patch.object(music_beats.subprocess, "run", return_value=sparse):
        assert music_beats.detect_music_beats("a.mp3") == []


def test_song_identity_fields_are_protected_to_the_media_facade() -> None:
    assert {"song_gcs_path", "song_generation", "song_duration_s", "song_filename"} <= (
        PROTECTED_PLAN_ITEM_MEDIA_FIELDS
    )


def test_replacing_a_song_drops_its_cached_analysis_and_alignment() -> None:
    item = SimpleNamespace(
        clip_gcs_paths=[],
        clip_assignments=[],
        voiceover_gcs_path=None,
        voiceover_generation=None,
        voiceover_duration_s=None,
        edit_format="montage",
        audio_mode="song",
        song_gcs_path="users/u/old.m4a",
        song_generation=1,
        song_duration_s=10.0,
        song_filename="old.m4a",
        song_analysis={"status": "ready"},
        song_alignment={"takes": {}},
        speech_cleanup_enabled=False,
        speech_cleanup_notice=None,
        edit_proposal=None,
    )

    mutate_plan_item_media(
        item,
        detector_policy="p",
        song_gcs_path="users/u/new.m4a",
        song_generation=2,
    )
    assert (item.song_analysis, item.song_alignment) == (None, None)

    item.song_analysis, item.song_alignment = {"status": "ready"}, {"takes": {}}
    # Same song re-stated (e.g. metadata-only write): the caches survive.
    mutate_plan_item_media(
        item,
        detector_policy="p",
        song_gcs_path="users/u/new.m4a",
        song_generation=2,
        song_filename="renamed.m4a",
    )
    assert item.song_analysis == {"status": "ready"}
    assert item.song_alignment == {"takes": {}}
