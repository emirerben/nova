"""KRI-1562: the editor Save of a phone Talking edit with the creator's song.

Real boundaries: the commit route's own validator (`gj.prepare_editor_commit`) stages the
`background_music` section, the real `compile_phone_subtitled_plan` recompiles, the real
`EditRecipeV2` and `validate_phone_pilot_recipe` check it, and the request is pinned. Only the
catalog hash of a newly chosen song (`render_library.inspect_music_track`) is stood in.

Failure modes:
F7  a later caption or sound Save drops the song or re-hashes it
F8  a removal Save leaves the song playing
F9  an app build below protocol 53 Saves a recipe that still plays the song (426), while a
    removal from that build goes through
F10 the editor offers `background_music` on a device variant whose Save refuses or drops it
    (flag off: closed everywhere; flag on: open only on a subtitled-path Talking edit)
F1  flag off: a Save is exactly what it was, and the section stays a 422
F15 the editor's preview gets the song (`background_music` with `preview_url`) from status
F16 a production job's render contract (camera audio required) accepts the music-role track
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.kria.render_assets import LibraryRenderAsset, RenderFingerprint
from app.routes import generative_jobs as gj
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.creator_render_contract import CONTRACT_FIELD, build_render_contract
from app.services.device_render import device_record, device_status
from tests.routes.test_phone_subtitled_editor_commit import _sfx_payload, phone_job

TRACK = SimpleNamespace(
    id="song-1",
    title="Calm",
    artist="Someone",
    analysis_status="ready",
    published_at=datetime(2026, 1, 1, tzinfo=UTC),
    archived_at=None,
    audio_gcs_path="music/song-1/audio.m4a",
    duration_s=60.0,
)
SPOKEN = {"clip-0": [[0.2, 1.4], [1.6, 2.9]]}


@pytest.fixture(autouse=True)
def _no_request_context():
    clear_request_context()
    yield
    clear_request_context()


def _flag(monkeypatch, on: bool):
    monkeypatch.setattr(gj.settings, "talking_music_enabled", on)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "musicBed", "speechDuck"],
    )


def _job(monkeypatch, *, on: bool = True):
    job = phone_job(monkeypatch)
    _flag(monkeypatch, on)
    variant = job.assembly_plan["variants"][0]
    variant["speech_source_windows"] = SPOKEN
    # A phone render the device already published: the editor's lanes open on it.
    variant["output_url"] = "https://signed.test/out.mp4"
    return job


def _hashes(monkeypatch) -> list[str]:
    calls: list[str] = []

    def inspect(track_id, path):
        calls.append(path)
        return LibraryRenderAsset(
            id=f"music-{track_id}",
            catalog="music",
            catalog_id=track_id,
            generation="5",
            fingerprint=RenderFingerprint(sha256="e" * 64, byte_count=4000),
        )

    monkeypatch.setattr("app.services.render_library.inspect_music_track", inspect)
    return calls


def _save(job, **sections):
    variant = job.assembly_plan["variants"][0]
    return gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation=gj.variant_render_baseline(variant), **sections),
        user_id="owner",
        plan_item_id="item",
        background_music_track=TRACK,
    )


def _add_song(job):
    return _save(
        job, background_music={"track_id": "song-1", "start_s": 10.0, "end_s": 18.0, "gain_db": -18}
    )


def _recipe(job):
    return device_status(job, "subtitled").request.recipe


def _music(recipe):
    return [track for track in recipe.tracks if track.role == "music"]


def test_adding_a_song_compiles_a_ducked_bed_on_the_transcript_windows(monkeypatch):
    job = _job(monkeypatch)
    hashed = _hashes(monkeypatch)
    _add_song(job)
    recipe = _recipe(job)
    [bed] = _music(recipe)
    assert bed.clips[0].source_start == 10.0
    assert [(w.start, w.end) for w in recipe.audio.speech_windows] == [(0.2, 2.9)]
    assert {"musicBed", "speechDuck"} <= recipe.required_capabilities
    assert hashed == ["music/song-1/audio.m4a"]
    assert job.assembly_plan["variants"][0]["smart_music_treatment"]["track_id"] == "song-1"


def test_a_later_sound_save_keeps_the_song_without_hashing_it_again(monkeypatch):
    job = _job(monkeypatch)
    hashed = _hashes(monkeypatch)
    _add_song(job)
    before = _music(_recipe(job))
    _save(job, sound_effects=[_sfx_payload(at_s=4.0)])
    after = _recipe(job)
    assert _music(after) == before
    assert after.audio.speech_windows
    assert len(hashed) == 1


def test_removing_the_song_takes_it_out_of_the_recipe(monkeypatch):
    job = _job(monkeypatch)
    _hashes(monkeypatch)
    _add_song(job)
    _save(job, background_music={"track_id": "song-1", "enabled": False})
    recipe = _recipe(job)
    assert _music(recipe) == []
    assert recipe.audio.speech_windows == []
    assert "musicBed" not in recipe.required_capabilities


def test_an_older_build_is_told_to_update_unless_it_removes_the_song(monkeypatch):
    job = _job(monkeypatch)
    _hashes(monkeypatch)
    set_client_protocol(52)
    with pytest.raises(HTTPException) as refused:
        _add_song(job)
    assert refused.value.status_code == 426
    assert refused.value.detail == {
        "code": "client_update_required",
        "feature": "speech_duck",
        "minimum_client_protocol": 53,
    }
    set_client_protocol(53)
    _add_song(job)
    set_client_protocol(52)
    with pytest.raises(HTTPException) as caption_save:
        _save(job, sound_effects=[_sfx_payload(at_s=4.0)])
    assert caption_save.value.status_code == 426
    _save(job, background_music={"track_id": "song-1", "enabled": False})
    assert _music(_recipe(job)) == []


def test_flag_off_keeps_the_section_refused_and_the_save_unchanged(monkeypatch):
    job = _job(monkeypatch, on=False)
    _hashes(monkeypatch)
    with pytest.raises(HTTPException) as refused:
        _add_song(job)
    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "unsupported_phone_edit"

    off = _job(monkeypatch, on=False)
    off.assembly_plan["variants"][0]["smart_music_treatment"] = {
        "track_id": "song-1",
        "src_gcs_path": "music/song-1/audio.m4a",
        "section_start_s": 10.0,
        "section_end_s": 18.0,
    }
    plain = _job(monkeypatch, on=False)
    _save(off, sound_effects=[_sfx_payload(at_s=4.0)])
    _save(plain, sound_effects=[_sfx_payload(at_s=4.0)])
    assert _recipe(off).model_dump_json() == _recipe(plain).model_dump_json()


def test_a_contract_that_requires_camera_audio_accepts_the_bed(monkeypatch):
    job = _job(monkeypatch)
    _hashes(monkeypatch)
    contract = build_render_contract({"audio_strategy": "original_audio"}, generation_id="first")
    assert contract is not None and contract.original_audio == "require"
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    _add_song(job)
    assert _music(_recipe(job))
    assert device_record(job, "subtitled")["contract_digest"]


# --- F10: the capability only opens where the Save compiles the song ---------------------------


def _capability(job, variant=None) -> bool:
    variant = variant or job.assembly_plan["variants"][0]
    return gj._device_clamped_editor_capabilities(job, variant)["background_music"]


@pytest.mark.parametrize(
    ("on", "authored", "expected"),
    [(False, False, False), (True, False, True), (True, True, False)],
)
def test_background_music_is_offered_only_where_a_save_compiles_it(
    monkeypatch, on, authored, expected
):
    monkeypatch.setattr(gj.settings, "smart_music_bed_enabled", True)
    job = _job(monkeypatch, on=on)
    variant = job.assembly_plan["variants"][0]
    if authored:
        variant["editor_timeline_mode"] = "authored"
    assert _capability(job) is expected


def test_an_offered_control_saves(monkeypatch):
    """The trap: advertised => a Save carrying it does not 422."""
    monkeypatch.setattr(gj.settings, "smart_music_bed_enabled", True)
    _hashes(monkeypatch)
    for on in (False, True):
        job = _job(monkeypatch, on=on)
        if _capability(job):
            _add_song(job)
            assert _music(_recipe(job))
        else:
            with pytest.raises(HTTPException):
                _add_song(job)


def test_an_older_build_is_not_offered_the_song(monkeypatch):
    monkeypatch.setattr(gj.settings, "smart_music_bed_enabled", True)
    job = _job(monkeypatch)
    set_client_protocol(52)
    assert _capability(job) is False
    set_client_protocol(53)
    assert _capability(job) is True


# --- F15: the editor preview reads the song from status --------------------------------------


@pytest.mark.asyncio
async def test_status_carries_the_song_for_the_editor_preview(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "app.routes.music._preview_audio_url", lambda path: f"https://signed.test/{path}"
    )
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "duration_s": 10.0,
        "smart_music_treatment": {
            "track_id": "song-1",
            "src_gcs_path": "music/song-1/audio.m4a",
            "section_start_s": 10.0,
            "section_end_s": 18.0,
            "gain_db": -18.0,
        },
    }
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [TRACK]))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    await gj._attach_music_previews([variant], db, job=SimpleNamespace())
    bed = variant["background_music"]
    assert (bed["track_id"], bed["start_s"], bed["end_s"], bed["gain_db"]) == (
        "song-1",
        10.0,
        18.0,
        -18.0,
    )
    assert bed["preview_url"] == "https://signed.test/music/song-1/audio.m4a"
