import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.pipeline.guided_story import song_reference_variant_fields
from app.routes import generative_jobs as gj
from app.schemas.user_song import UserSongPlan
from app.services.phone_editor import PHONE_EDITOR_SAVED_PLAN_FIELD, prepare_phone_editor_commit
from tests.routes.test_generative_jobs import _resign_job
from tests.routes.test_phone_editor_commit import phone_job

REFERENCE = {
    "schema_version": 1,
    "delivery": "external_platform",
    "track_id": "track-1",
    "title": "Song",
    "artist": None,
    "start_s": 10.0,
    "end_s": 13.0,
}


def test_status_preserves_reference_and_source_bed_but_strips_stale_song_urls(monkeypatch):
    monkeypatch.setattr(gj, "signed_get_url", lambda path, ttl: f"https://signed.test/{path}")
    monkeypatch.setattr(gj.storage, "signed_download_url", lambda *a, **kw: "https://download.test")
    job = _resign_job()
    stored = job.assembly_plan["variants"][0]
    stored.update(
        music_playback_mode="reference_only",
        song_reference=copy.deepcopy(REFERENCE),
        source_audio_preserved=False,
        music_track_id="stale-song",
        music_preview_url="https://stale.test/song.m4a",
        background_music={"preview_url": "https://stale.test/bed.m4a"},
        source_audio_options=[{"mix": "source_a", "audio_path": "generative-jobs/j/source.m4a"}],
    )
    before = copy.deepcopy(job.assembly_plan)
    visible = gj._variants_for_response(job)[0]
    public = gj.GenerativeVariant.model_validate(visible).model_dump(mode="json")
    assert public["song_reference"] == REFERENCE
    assert public["source_audio_preserved"] is False
    assert public["music_playback_mode"] == "reference_only"
    assert public["music_track_id"] is None
    assert public["music_preview_url"] is None
    assert public["background_music"] is None
    assert visible["source_audio_options"][0]["audio_url"] == (
        "https://signed.test/generative-jobs/j/source.m4a"
    )
    assert job.assembly_plan == before


@pytest.mark.asyncio
async def test_song_preview_attachment_never_looks_up_reference_only_tracks():
    source_options = [{"mix": "source_a", "audio_url": "https://source.test/speech.m4a"}]
    variant = {
        "variant_id": "guided_story",
        "music_playback_mode": "reference_only",
        "music_track_id": "stale-song",
        "music_preview_url": "https://stale.test/song.m4a",
        "smart_music_treatment": {"track_id": "stale-bed"},
        "background_music": {"preview_url": "https://stale.test/bed.m4a"},
        "source_audio_options": copy.deepcopy(source_options),
    }
    db = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("must not load song")))
    await gj._attach_music_previews([variant], db, job=SimpleNamespace())
    db.execute.assert_not_called()
    assert variant["music_track_id"] is None
    assert variant["music_preview_url"] is None
    assert variant["background_music"] is None
    assert variant["source_audio_options"] == source_options


@pytest.mark.parametrize(
    "section",
    [
        {"music_track_id": "another-song"},
        {"remove_music": True},
        {"music_window": {"start_s": 0, "alignment": "preserve_cuts"}},
        {"background_music": {"track_id": "another-song"}},
        {"mix": {"music_level": 0.5}},
    ],
)
def test_reference_only_song_mutations_fail_before_catalog_or_state_changes(section):
    job = _resign_job()
    job.assembly_plan["variants"][0].update(
        resolved_archetype="guided_story", music_playback_mode="reference_only"
    )
    before = copy.deepcopy(job.assembly_plan)
    request = gj.EditorCommitRequest(base_generation="first", **section)
    with pytest.raises(HTTPException) as error:
        gj.require_guided_story_editor_commit(job, "song_lyrics", request)
    assert error.value.status_code == 422
    assert error.value.detail == "song_added_when_posting"
    assert job.assembly_plan == before


def _creator_song_job(monkeypatch):
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    job = _resign_job()
    song = UserSongPlan(
        mode="background",
        plan_item_id="item-1",
        generation=3,
        duration_s=100,
        window_start_s=10,
        window_end_s=40,
    )
    job.assembly_plan["variants"][0].update(
        resolved_archetype="guided_story",
        music_playback_mode="reference_only",
        **{PHONE_EDITOR_SAVED_PLAN_FIELD: {"user_song": song.model_dump(mode="json")}},
    )
    return job


@pytest.mark.parametrize(
    "section",
    [
        {"volume": 0.5},
        {"window_start_s": 12.0},
        {"removed": True},
    ],
)
def test_the_creators_own_song_is_not_a_reference_only_mutation(monkeypatch, section):
    job = _creator_song_job(monkeypatch)
    request = gj.EditorCommitRequest(
        base_generation="first", user_song=gj.EditorCommitUserSong(**section)
    )
    gj.require_guided_story_editor_commit(job, "song_lyrics", request)


def test_library_sections_stay_refused_beside_the_creators_song(monkeypatch):
    job = _creator_song_job(monkeypatch)
    request = gj.EditorCommitRequest(
        base_generation="first",
        music_track_id="another-song",
        user_song=gj.EditorCommitUserSong(volume=0.5),
    )
    with pytest.raises(HTTPException) as error:
        gj.require_guided_story_editor_commit(job, "song_lyrics", request)
    assert error.value.detail == "song_added_when_posting"


def test_a_user_song_section_needs_a_creator_song(monkeypatch):
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    job = _resign_job()
    job.assembly_plan["variants"][0].update(
        resolved_archetype="guided_story", music_playback_mode="reference_only"
    )
    before = copy.deepcopy(job.assembly_plan)
    request = gj.EditorCommitRequest(
        base_generation="first", user_song=gj.EditorCommitUserSong(volume=0.5)
    )
    with pytest.raises(HTTPException) as error:
        gj.require_guided_story_editor_commit(job, "song_lyrics", request)
    assert error.value.status_code == 422
    assert error.value.detail == {"code": "user_song_unavailable"}
    assert job.assembly_plan == before


def test_a_user_song_section_is_refused_on_authored_and_legacy_paths(monkeypatch):
    request = gj.EditorCommitRequest(
        base_generation="first", user_song=gj.EditorCommitUserSong(volume=0.5)
    )
    job = _creator_song_job(monkeypatch)
    job.assembly_plan["variants"][0]["editor_timeline_mode"] = "authored"
    with pytest.raises(HTTPException) as error:
        gj.require_guided_story_editor_commit(job, "song_lyrics", request)
    assert error.value.detail == gj._GUIDED_STORY_EDIT_ERROR
    job = _creator_song_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", False)
    with pytest.raises(HTTPException) as error:
        gj.require_guided_story_editor_commit(job, "song_lyrics", request)
    assert error.value.detail == gj._GUIDED_STORY_EDIT_ERROR


def test_the_song_edit_wire_shape_is_strict():
    with pytest.raises(ValueError):
        gj.EditorCommitUserSong(volume=1.5)
    with pytest.raises(ValueError):
        gj.EditorCommitUserSong(window_start_s=-1)
    with pytest.raises(ValueError):
        gj.EditorCommitUserSong(window_start_s=float("inf"))
    with pytest.raises(ValueError):
        gj.EditorCommitUserSong.model_validate({"volume": 0.5, "level": 1})


def test_phone_revision_replaces_reference_timing_with_new_recipe(monkeypatch):
    job = phone_job(monkeypatch)
    plan = job.assembly_plan["guided_story_execution_plan"]
    plan.update(
        compiler_version=6,
        song_reference=copy.deepcopy(REFERENCE),
        song_reference_track_duration_s=120,
    )
    job.assembly_plan["variants"][0].update(song_reference_variant_fields(plan))
    job.assembly_plan["guided_edit"] = {}
    revised = copy.deepcopy(plan)
    revised["resolved_duration_s"] = 2
    revised["song_reference"]["end_s"] = 12
    revised["story_timeline"][0].update(source_end_s=4, output_end_s=2, duration_s=2)
    revised["beat_windows"][0].update(resolved_duration_s=2, end_s=2)

    def compile_revision(*_args, admitted_sources):
        assert admitted_sources == []
        return revised

    monkeypatch.setattr("app.services.phone_editor.compile_guided_runtime_plan", compile_revision)
    prepare_phone_editor_commit(
        job,
        "guided_story",
        prepare=lambda staged: {
            "has_render_section": True,
            "guided_revision": {"revision_number": 2},
            "sections": {"timeline": True},
            "generation": "second",
        },
    )
    variant = job.assembly_plan["variants"][0]
    assert variant["duration_s"] == 2
    assert variant["song_reference"]["end_s"] == 12
    assert variant["music_playback_mode"] == "reference_only"
    assert variant["music_track_id"] is None
    # KRI-184: no explicit choice on a reference-only plan keeps the clips' own sound.
    assert variant["source_audio_preserved"] is True
