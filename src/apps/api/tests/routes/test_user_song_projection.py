"""KRI-374: the editor learns a creator's own song exists via ``variants[].user_song``.

Additive and display-only: the variant keeps ``music_track_id: null`` / reference-only
playback (so the catalog-music controls never reach the song), and variants without a
creator song are byte-identical to before.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.routes import generative_jobs as gj
from app.services.phone_editor import PHONE_EDITOR_SAVED_PLAN_FIELD
from app.services.user_song_projection import attach_user_song, user_song_title
from tests.routes.test_generative_jobs import _resign_job
from tests.routes.test_phone_song_editor_commit import (  # noqa: F401  (autouse fixture)
    _phone_profile,
    background_job,
    lipsync_job,
)


@pytest.fixture(autouse=True)
def _signing(monkeypatch):
    monkeypatch.setattr(gj, "signed_get_url", lambda path, ttl: f"https://signed.test/{path}")
    monkeypatch.setattr(gj.storage, "signed_download_url", lambda *a, **kw: "https://download.test")


def _public(job, filename="My Song.m4a"):
    variants = gj._variants_for_response(job)
    attach_user_song(variants, job, song_filename=filename)
    return [gj.GenerativeVariant.model_validate(v).model_dump(mode="json") for v in variants]


@pytest.mark.parametrize(
    "make_job,mode", [(background_job, "background"), (lipsync_job, "lipsync")]
)
def test_a_variant_with_a_creator_song_carries_title_mode_and_window(make_job, mode):
    job, result = make_job()
    (variant,) = _public(job)
    song = result.user_song
    expected = {
        "title": "My Song",
        "mode": mode,
        "duration_s": song.duration_s,
        "window_start_s": song.window_start_s,
        "window_end_s": song.window_end_s,
        "volume": 1.0,
    }
    if mode == "lipsync":
        # KRI-561: the editor needs each pinned take's song offset to trim the song by cutting
        # the video. Background songs carry no `takes` key (their response is unchanged).
        expected["takes"] = {m: t.delta_s for m, t in song.takes.items()}
    assert variant["user_song"] == expected
    print(json.dumps(variant["user_song"]))
    # Display-only: the catalog-music surface is untouched.
    assert variant.get("music_track_id") is None


def test_a_saved_volume_shows_up_in_the_projection():
    job, _result = background_job()
    variant = job.assembly_plan["variants"][0]
    saved = copy.deepcopy(job.assembly_plan["guided_story_execution_plan"])
    saved["user_song"]["volume"] = 0.35
    variant[PHONE_EDITOR_SAVED_PLAN_FIELD] = saved
    (public,) = _public(job)
    assert public["user_song"]["volume"] == 0.35


def test_a_variant_without_a_creator_song_has_no_user_song_key():
    job = _resign_job()
    visible = gj._variants_for_response(job)
    before = copy.deepcopy(visible)
    attach_user_song(visible, job, song_filename="Whatever.mp3")
    assert visible == before
    public = gj.GenerativeVariant.model_validate(visible[0]).model_dump(mode="json")
    assert "user_song" not in public


def test_the_window_follows_a_saved_editor_plan_after_a_rewindow():
    job, result = background_job()
    variant = job.assembly_plan["variants"][0]
    saved = copy.deepcopy(job.assembly_plan["guided_story_execution_plan"])
    saved["user_song"]["window_end_s"] = saved["user_song"]["window_start_s"] + 4.0
    variant[PHONE_EDITOR_SAVED_PLAN_FIELD] = saved
    (public,) = _public(job)
    assert public["user_song"]["window_start_s"] == result.user_song.window_start_s
    assert public["user_song"]["window_end_s"] == pytest.approx(
        result.user_song.window_start_s + 4.0
    )
    assert public["user_song"]["window_end_s"] != result.user_song.window_end_s


@pytest.mark.parametrize(
    "filename,title",
    [
        ("My Song.m4a", "My Song"),
        ("  Night Drive.final.MP3 ", "Night Drive.final"),
        ("Song v1.2", "Song v1.2"),
        ("No Extension", "No Extension"),
        ("   ", None),
        ("", None),
        (None, None),
        (".m4a", None),
    ],
)
def test_title_strips_the_extension_and_is_none_when_blank(filename, title):
    assert user_song_title(filename) == title


@pytest.mark.asyncio
async def test_status_reads_the_title_once_and_only_when_a_song_exists():
    job, _ = lipsync_job()
    job.content_plan_item_id = "item-1"
    queried = MagicMock()
    queried.scalar_one_or_none.return_value = "Take Me Home.wav"
    db = SimpleNamespace(execute=AsyncMock(return_value=queried))
    variants = gj._variants_for_response(job)
    await gj._attach_user_song(variants, db, job=job)
    assert db.execute.await_count == 1
    assert variants[0]["user_song"]["title"] == "Take Me Home"

    plain = _resign_job()
    plain_db = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("no query")))
    plain_variants = gj._variants_for_response(plain)
    await gj._attach_user_song(plain_variants, plain_db, job=plain)
    assert all("user_song" not in v for v in plain_variants)


def test_the_thread_projection_carries_the_song_from_the_loaded_item():
    from app.routes.creation_threads import _job_projection

    job, _ = lipsync_job()
    job.current_phase = None
    job.failure_reason = None
    job.status = "awaiting_device"
    projected = _job_projection(job, item=SimpleNamespace(song_filename="Night Drive.m4a"))
    assert projected["variants"][0]["user_song"]["title"] == "Night Drive"
    assert projected["variants"][0]["user_song"]["mode"] == "lipsync"


def test_user_song_is_a_declared_optional_field_omitted_when_none():
    assert "user_song" in gj.GenerativeVariant.model_fields
    bare = gj.GenerativeVariant(variant_id="v").model_dump(mode="json")
    assert "user_song" not in bare
