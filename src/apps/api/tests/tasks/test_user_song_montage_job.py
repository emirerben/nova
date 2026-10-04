"""KRI-374 D2: a phone montage with the creator's own song, through the worker.

Drives ``_run_generative_job`` -> ``_run_phone_unified_montage_job`` -> the song
planners -> ``_run_phone_guided_job`` -> ``compile_phone_guided_plan`` for both modes,
with the same fixture shape as ``test_unified_montage_dispatch`` and synthetic song
analysis / alignment (no network, no aligner).
"""

from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint, SongRenderAsset
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.lipsync_montage import lipsync_sync_error_s
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.schemas.user_song import SongAlignment
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.tasks import generative_build as gb
from tests._prod_profile import PROD_VERIFIED_FEATURES
from tests.pipeline.user_song_helpers import (
    SONG_DURATION_S,
    SONG_GENERATION,
    alignment,
    ambiguous,
    analysis,
    confident,
    song_bed,
    unmatched,
)

ITEM_ID = uuid.uuid4()
CLIP_S = 20.0
CLIPS = ("clip-a", "clip-b", "clip-c")


def _bindings():
    return tuple(
        PhoneSourceBinding(
            media_id=media_id,
            proxy_path=f"users/u/analysis-proxy-{media_id}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256=f"{i + 1:x}".rjust(64, "a"),
                byte_count=1000 + i,
                duration_s=CLIP_S,
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for i, media_id in enumerate(CLIPS)
    )


@pytest.fixture
def harness(monkeypatch):
    def build(*, sync: str, rows=(), strategy_extra=None, song_analysis=None, song_alignment=None):
        bindings = _bindings()
        assignments = [
            {
                "gcs_path": b.proxy_path,
                "media_id": b.media_id,
                "storage_generation": b.generation,
                "duration_s": CLIP_S,
            }
            for b in bindings
        ]
        user_id = uuid.uuid4()
        snapshot = {
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            "creator_generation_id": "generation",
        }
        job = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user_id,
            assembly_plan=copy.deepcopy(snapshot),
            status="queued",
            all_candidates={
                "clip_paths": [b.proxy_path for b in bindings],
                "edit_format": "montage",
                "landscape_fit": "fill",
                "creator_strategy": {
                    "render_program": "guided",
                    "direction": "guided_story",
                    "audio_strategy": "user_song",
                    "song_sync": sync,
                    **(strategy_extra or {}),
                },
                "user_song": {
                    "gcs_path": "users/u/creation-threads/t/song.m4a",
                    "generation": SONG_GENERATION,
                    "duration_s": SONG_DURATION_S,
                    "sync": sync,
                },
            },
            error_detail=None,
            failure_reason=None,
            content_plan_item_id=ITEM_ID,
        )
        session = Mock()

        @contextmanager
        def sessions():
            yield session

        monkeypatch.setattr(gb, "_sync_session", sessions)
        monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 3))
        monkeypatch.setattr(
            gb, "_load_unified_montage_inputs", lambda _job_id: (user_id, assignments, None)
        )
        monkeypatch.setattr(gb, "_load_unified_montage_visuals", lambda *_a, **_k: [])

        def fake_guided_plan(_job_id, guided):
            plan = compile_execution_plan(guided, track=None)
            job.assembly_plan["guided_story_execution_plan"] = plan
            return plan, None

        monkeypatch.setattr(gb, "_guided_execution_plan", fake_guided_plan)
        monkeypatch.setattr(gb.settings, "phone_rendering_enabled", True)
        monkeypatch.setattr(gb.settings, "user_song_montage_enabled", True)
        monkeypatch.setattr(gb.settings, "phone_render_verified_features", PROD_VERIFIED_FEATURES)
        monkeypatch.setattr(gb, "_job_plan_item_id", lambda _job_id: ITEM_ID)
        monkeypatch.setattr(
            gb, "_resolve_phone_song_bed", lambda *_a, **_k: song_bed(plan_item_id=str(ITEM_ID))
        )
        monkeypatch.setattr(gb, "_run_phone_voiceover_montage_job", Mock())
        monkeypatch.setattr(gb, "_run_guided_story_job", Mock())
        # The inline-compute fallbacks are the only seams to the song tables.
        from app.tasks import user_song

        info = song_analysis if song_analysis is not None else analysis()
        monkeypatch.setattr(user_song, "ensure_song_analysis", lambda _item: info)
        monkeypatch.setattr(
            user_song,
            "ensure_song_alignment",
            lambda _item: (
                info,
                song_alignment if song_alignment is not None else alignment(*rows),
            ),
        )
        speech = Mock(side_effect=AssertionError("a song edit never takes the speech montage"))
        monkeypatch.setattr(
            "app.services.phone_speech_montage_job.run_phone_speech_montage_job", speech
        )
        return job, snapshot, bindings

    return build


def _song_clip(job):
    recipe = device_status(job, "guided_story").request.recipe
    (clip,) = next(t for t in recipe.tracks if t.id == "song").clips
    return recipe, clip


def test_background_song_montage_renders_with_the_song_as_the_only_audio(harness):
    job, _snapshot, _bindings = harness(sync="background")

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    record = job.assembly_plan["unified_montage"]
    assert record["user_song"]["mode"] == "background"
    plan = job.assembly_plan["guided_story_execution_plan"]
    assert plan["user_song"]["mode"] == "background"
    recipe, clip = _song_clip(job)
    assert recipe.audio.original_volume == 0.0
    assert clip.source_start == pytest.approx(plan["user_song"]["window_start_s"])
    asset = next(a for a in recipe.asset_manifest.assets if a.kind == "song")
    assert isinstance(asset, SongRenderAsset)
    assert asset.generation == str(SONG_GENERATION)
    assert recipe.duration <= SONG_DURATION_S


def test_lipsync_song_montage_places_confident_takes_on_the_song_clock(harness):
    job, _snapshot, _bindings = harness(
        sync="lipsync",
        rows=[confident("clip-a", 10), confident("clip-b", 28), confident("clip-c", 46)],
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    plan = job.assembly_plan["guided_story_execution_plan"]
    song = plan["user_song"]
    assert song["mode"] == "lipsync"
    assert set(song["takes"]) == set(CLIPS)
    recipe, clip = _song_clip(job)
    assert recipe.audio.original_volume == 0.0
    assert clip.source_start == pytest.approx(song["window_start_s"])
    video = next(t for t in recipe.tracks if t.kind == "video")
    for placed in video.clips:
        media_id = next(
            m["media_id"] for m in plan["story_timeline"] if m["moment_id"] == placed.id
        )
        pinned = song["takes"][media_id]
        assert (
            lipsync_sync_error_s(
                output_start_s=placed.timeline_start,
                source_start_s=placed.source_start,
                delta_s=pinned["delta_s"],
                window_start_s=song["window_start_s"],
            )
            <= 0.001
        )


def test_an_uncertain_take_is_not_placed_without_the_creators_answer(harness):
    job, _snapshot, _bindings = harness(
        sync="lipsync",
        rows=[
            confident("clip-a", 10),
            ambiguous("clip-b", 28, 70),
            confident("clip-c", 46),
        ],
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    song = job.assembly_plan["guided_story_execution_plan"]["user_song"]
    assert "clip-b" not in song["takes"]  # never placed at a guessed song time
    _recipe, clip = _song_clip(job)
    assert clip.source_start == pytest.approx(song["window_start_s"])


def test_the_creators_confirmed_order_places_the_uncertain_take(harness):
    job, _snapshot, _bindings = harness(
        sync="lipsync",
        rows=[
            confident("clip-a", 10),
            ambiguous("clip-b", 70, 28),  # the aligner's first guess (70) is wrong here
            confident("clip-c", 46),
        ],
        strategy_extra={
            "resolved_song_takes": [
                {
                    "media_id": "clip-a",
                    "delta_s": 10.0,
                    "status": "confident",
                    "confirmed_by_creator": False,
                },
                {
                    "media_id": "clip-b",
                    "delta_s": 28.0,
                    "status": "confident",
                    "confirmed_by_creator": True,
                },
                {
                    "media_id": "clip-c",
                    "delta_s": 46.0,
                    "status": "confident",
                    "confirmed_by_creator": False,
                },
            ]
        },
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    takes = job.assembly_plan["guided_story_execution_plan"]["user_song"]["takes"]
    assert takes["clip-b"]["delta_s"] == pytest.approx(28.0)
    assert takes["clip-b"]["confirmed_by_creator"] is True
    assert takes["clip-a"]["confirmed_by_creator"] is False


def test_a_take_the_creator_could_not_place_stays_broll(harness):
    job, _snapshot, _bindings = harness(
        sync="lipsync",
        rows=[confident("clip-a", 10), ambiguous("clip-b", 28, 70), confident("clip-c", 46)],
        strategy_extra={
            "resolved_song_takes": [
                {"media_id": "clip-a", "delta_s": 10.0, "status": "confident"},
                {
                    "media_id": "clip-b",
                    "delta_s": None,
                    "status": "unmatched",
                    "confirmed_by_creator": True,
                },
                {"media_id": "clip-c", "delta_s": 46.0, "status": "confident"},
            ]
        },
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    assert "clip-b" not in job.assembly_plan["guided_story_execution_plan"]["user_song"]["takes"]


def test_a_song_edit_skips_the_spoken_excerpt_montage(harness):
    job, _snapshot, _bindings = harness(sync="background")
    gb._run_generative_job(str(job.id))  # the speech runner is a failing Mock
    assert job.status == "awaiting_device"


# ── user-facing failures ─────────────────────────────────────────────────────


def _plan_error(harness, **kwargs):
    job, snapshot, _bindings = harness(**kwargs)
    with pytest.raises(UnsupportedPhonePlan) as caught:
        gb._run_phone_unified_montage_job(
            str(job.id), snapshot, job.all_candidates, ownership_epoch=3
        )
    return str(caught.value), caught.value


def test_no_matched_takes_says_how_to_film_them(harness):
    message, error = _plan_error(
        harness, sync="lipsync", rows=[unmatched("clip-a"), unmatched("clip-b")]
    )
    assert "film each take with the song playing" in message.lower()
    assert error.capability == "musicBed"


def test_a_stale_take_alignment_is_not_trusted(harness):
    # The alignment was computed for clip proxies that have since been replaced.
    stale = alignment(confident("clip-a", 10), confident("clip-b", 28))
    for row in stale.takes.values():
        row.proxy_generation = 99
    message, _error = _plan_error(harness, sync="lipsync", song_alignment=stale)
    assert "couldn't find where any of your clips" in message


def test_alignment_for_another_song_generation_is_refused(harness):
    other = SongAlignment(song_generation=SONG_GENERATION + 1, takes={})
    message, _error = _plan_error(harness, sync="lipsync", song_alignment=other)
    assert "couldn't find where any of your clips" in message


def test_a_replaced_song_fails_closed_before_planning(harness):
    stale = analysis().model_copy(update={"generation": SONG_GENERATION + 1})
    message, _error = _plan_error(harness, sync="background", song_analysis=stale)
    assert "replaced" in message


def test_an_unreadable_song_names_the_problem(harness):
    failed = analysis().model_copy(update={"status": "failed"})
    message, _error = _plan_error(harness, sync="background", song_analysis=failed)
    assert "couldn't read your song" in message


def test_the_dispatcher_maps_a_lipsync_decline_to_phone_plan_unsupported(harness, monkeypatch):
    job, _snapshot, _bindings = harness(sync="lipsync", rows=[unmatched("clip-a")])
    fail = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", fail)
    monkeypatch.setattr(gb, "mark_failed_phase", Mock())
    gb._run_generative_job(str(job.id))
    fail.assert_called_once()
    args, kwargs = fail.call_args
    assert kwargs["failure_reason"] == "phone_plan_unsupported"
    assert "couldn't find where any of your clips" in args[1]


def test_flag_off_after_dispatch_fails_closed_at_compile(harness, monkeypatch):
    job, _snapshot, _bindings = harness(sync="background")
    monkeypatch.setattr(gb.settings, "user_song_montage_enabled", False)
    fail = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", fail)
    monkeypatch.setattr(gb, "mark_failed_phase", Mock())
    gb._run_generative_job(str(job.id))
    fail.assert_called_once()
    assert fail.call_args.kwargs["failure_reason"] == "phone_plan_unsupported"
    assert "currently unavailable" in fail.call_args.args[1]


# ── _resolve_phone_song_bed ──────────────────────────────────────────────────


def _bed_session(monkeypatch, *, item):
    job = SimpleNamespace(id=uuid.uuid4(), content_plan_item_id=getattr(item, "id", None))

    def get(model, _key):
        return job if model.__name__ == "Job" else item

    session = SimpleNamespace(get=get)

    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(gb, "_sync_session", sessions)
    return job


def _item(**changes):
    base = {
        "id": ITEM_ID,
        "audio_mode": "song",
        "song_gcs_path": "users/u/creation-threads/t/song.m4a",
        "song_generation": SONG_GENERATION,
        "song_duration_s": SONG_DURATION_S,
    }
    return SimpleNamespace(**(base | changes))


def _user_song():
    from app.schemas.user_song import UserSongPlan

    return UserSongPlan(
        mode="background",
        plan_item_id=str(ITEM_ID),
        generation=SONG_GENERATION,
        duration_s=SONG_DURATION_S,
        window_start_s=4.0,
        window_end_s=20.0,
    )


@pytest.fixture
def inspect_spy(monkeypatch):
    from app.services import phone_voiceover

    calls = []

    def inspect(path, *, asset_id, plan_item_id, expected_generation=None):
        calls.append((path, asset_id, plan_item_id, expected_generation))
        return SongRenderAsset(
            id=asset_id,
            plan_item_id=plan_item_id,
            generation=str(expected_generation),
            fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=5_000_000),
        )

    monkeypatch.setattr(phone_voiceover, "inspect_song_asset", inspect)
    return calls


# The original (unpatched) `_resolve_phone_song_bed` is the real unit under test here.
_REAL_RESOLVE = gb._resolve_phone_song_bed


def test_song_bed_pins_the_current_generation_and_hash(monkeypatch, inspect_spy):
    job = _bed_session(monkeypatch, item=_item())
    bed = _REAL_RESOLVE(str(job.id), _user_song())
    assert inspect_spy == [
        (
            "users/u/creation-threads/t/song.m4a",
            f"song-{ITEM_ID}",
            str(ITEM_ID),
            str(SONG_GENERATION),
        )
    ]
    assert bed.generation == str(SONG_GENERATION)
    assert bed.fingerprint.sha256 == "c" * 64
    assert bed.duration_s == SONG_DURATION_S
    assert bed.plan_item_id == str(ITEM_ID)


@pytest.mark.parametrize(
    "changes",
    [
        {"song_generation": SONG_GENERATION + 1},
        {"song_duration_s": SONG_DURATION_S + 30},
        {"audio_mode": "kria"},
        {"song_gcs_path": None},
        {"song_generation": None},
    ],
)
def test_song_bed_fails_closed_when_the_song_changed_or_was_removed(
    monkeypatch, inspect_spy, changes
):
    job = _bed_session(monkeypatch, item=_item(**changes))
    with pytest.raises(UnsupportedPhonePlan) as caught:
        _REAL_RESOLVE(str(job.id), _user_song())
    assert caught.value.capability == "musicBed"
    assert inspect_spy == []


def test_song_bed_fails_closed_for_another_items_song(monkeypatch, inspect_spy):
    job = _bed_session(monkeypatch, item=_item(id=uuid.uuid4()))
    with pytest.raises(UnsupportedPhonePlan):
        _REAL_RESOLVE(str(job.id), _user_song())


@pytest.mark.parametrize("error", [FileNotFoundError, ValueError])
def test_song_bed_fails_closed_when_the_bytes_cannot_be_pinned(monkeypatch, error):
    from app.services import phone_voiceover

    job = _bed_session(monkeypatch, item=_item())

    def boom(*_a, **_k):
        raise error("gone")

    monkeypatch.setattr(phone_voiceover, "inspect_song_asset", boom)
    with pytest.raises(UnsupportedPhonePlan, match="replaced"):
        _REAL_RESOLVE(str(job.id), _user_song())


@pytest.mark.parametrize("sync", ["lipsync", "background"])
def test_the_creators_output_shape_reaches_the_song_snapshot(harness, sync):
    job, _snapshot, _bindings = harness(
        sync=sync,
        rows=[confident("clip-a", 10), confident("clip-b", 28), confident("clip-c", 46)],
    )
    job.all_candidates["creator_render_shape"] = {
        "output_orientation": "landscape",
        "landscape_fit": "fit",
    }

    gb._run_generative_job(str(job.id))

    plan = job.assembly_plan["guided_story_execution_plan"]
    assert plan["user_song"]["mode"] == sync
    assert plan["output_orientation"] == "landscape"
    assert plan["output_orientation_reason"] == "The creator selected this output format."


def test_the_worker_never_matches_a_library_track_for_a_creator_song(monkeypatch):
    """Production incident (first real attempt): `_guided_execution_plan` matched a
    library track for the montage, the compiler turned it into a reference-only
    `song_reference`, and the creator-song plan validator refused it, so the job
    failed with "The fast montage could not be compiled safely"."""
    from tests.pipeline.test_unified_montage_song import song_plan

    plan = song_plan()
    raw = plan.guided_edit()
    job = SimpleNamespace(
        id=uuid.uuid4(), status="queued", assembly_plan={"guided_edit": raw}, all_candidates={}
    )

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _model, _pk, **_kwargs):
            return job

        def commit(self):
            return None

    matched: list[int] = []

    def _match(*_args, **_kwargs):
        matched.append(1)
        return SimpleNamespace(
            id="library-track-1",
            title="Library Song",
            artist="Somebody",
            duration_s=200.0,
            track_config={"best_start_s": 0.0},
            beat_timestamps_s=[0.5 * i for i in range(1, 80)],
            audio_gcs_path="music/library-track-1/track.mp3",
        )

    monkeypatch.setattr(gb, "_sync_session", lambda: _Session())
    monkeypatch.setattr(gb, "_match_best_track", _match)

    compiled, track = gb._guided_execution_plan(str(job.id), raw)

    assert matched == [], "a creator's own song must never trigger library-track matching"
    assert track is None
    assert compiled["user_song"]["mode"] == "background"
    assert compiled.get("song_reference") is None
    assert compiled.get("music") is None
