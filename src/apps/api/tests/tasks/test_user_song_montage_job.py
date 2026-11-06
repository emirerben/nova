"""KRI-374 D2: a phone montage with the creator's own song, through the worker.

Drives ``_run_generative_job`` -> ``_run_phone_unified_montage_job`` -> the song
planners -> ``_run_phone_guided_job`` -> ``compile_phone_guided_plan`` for both modes,
with the same fixture shape as ``test_unified_montage_dispatch`` and synthetic song
analysis / alignment (no network, no aligner).
"""

from __future__ import annotations

import copy
import json
import re
import sys
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint, SongRenderAsset
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.lipsync_montage import lipsync_sync_error_s
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, UserSongPlanDeclined
from app.schemas.user_song import SongAlignment, SongWord
from app.services.device_render import device_status
from app.services.phone_editor import PHONE_EDITOR_SAVED_PLAN_FIELD
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


def _bindings(durations=None):
    return tuple(
        PhoneSourceBinding(
            media_id=media_id,
            proxy_path=f"users/u/analysis-proxy-{media_id}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256=f"{i + 1:x}".rjust(64, "a"),
                byte_count=1000 + i,
                duration_s=(durations or {}).get(media_id, CLIP_S),
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for i, media_id in enumerate(CLIPS)
    )


@pytest.fixture
def harness(monkeypatch):
    def build(
        *,
        sync: str,
        rows=(),
        strategy_extra=None,
        song_analysis=None,
        song_alignment=None,
        durations=None,
        creator_request=None,
        with_song=True,
    ):
        bindings = _bindings(durations)
        assignments = [
            {
                "gcs_path": b.proxy_path,
                "media_id": b.media_id,
                "storage_generation": b.generation,
                "duration_s": (durations or {}).get(b.media_id, CLIP_S),
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
                    "audio_strategy": "user_song" if with_song else "original_audio",
                    **({"song_sync": sync} if with_song else {}),
                    **(strategy_extra or {}),
                },
                **(
                    {
                        "user_song": {
                            "gcs_path": "users/u/creation-threads/t/song.m4a",
                            "generation": SONG_GENERATION,
                            "duration_s": SONG_DURATION_S,
                            "sync": sync,
                        }
                    }
                    if with_song
                    else {}
                ),
                **({"creator_request": creator_request} if creator_request else {}),
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


def test_an_uncertain_take_is_placed_unconfirmed_without_the_creators_answer(harness):
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
    # KRI-471: placed by likelihood (no status gate), never marked creator-confirmed.
    # The tie between 28 and 70 breaks toward the cluster: 28 sits among a and c.
    assert set(song["takes"]) == set(CLIPS)
    assert song["takes"]["clip-b"]["confirmed_by_creator"] is False
    assert song["takes"]["clip-b"]["position_basis"] == "tie_break"
    assert song["takes"]["clip-b"]["delta_s"] == pytest.approx(28.0)
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
                    "order_index": 0,
                    "delta_s": 10.0,
                    "place": "pinned",
                    "position_basis": "aligner",
                    "confirmed_by_creator": False,
                },
                {
                    "media_id": "clip-b",
                    "order_index": 1,
                    "delta_s": 28.0,
                    "place": "pinned",
                    "position_basis": "creator_position",
                    "confirmed_by_creator": True,
                },
                {
                    "media_id": "clip-c",
                    "order_index": 2,
                    "delta_s": 46.0,
                    "place": "pinned",
                    "position_basis": "aligner",
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
    assert takes["clip-b"]["position_basis"] == "creator_position"
    assert takes["clip-a"]["confirmed_by_creator"] is False


def test_a_legacy_resolved_row_without_place_still_pins_the_confirmed_take(harness):
    job, _snapshot, _bindings = harness(
        sync="lipsync",
        rows=[confident("clip-a", 10), ambiguous("clip-b", 70, 28), confident("clip-c", 46)],
        strategy_extra={
            "resolved_song_takes": [
                {"media_id": "clip-a", "delta_s": 10.0, "status": "confident"},
                {
                    "media_id": "clip-b",
                    "delta_s": 28.0,
                    "status": "confident",
                    "confirmed_by_creator": True,
                },
                {"media_id": "clip-c", "delta_s": 46.0, "status": "confident"},
            ]
        },
    )
    gb._run_generative_job(str(job.id))
    takes = job.assembly_plan["guided_story_execution_plan"]["user_song"]["takes"]
    assert takes["clip-b"]["delta_s"] == pytest.approx(28.0)
    assert takes["clip-b"]["confirmed_by_creator"] is True


def test_a_take_the_creator_asked_to_keep_as_broll_stays_broll(harness):
    job, _snapshot, _bindings = harness(
        sync="lipsync",
        rows=[confident("clip-a", 10), ambiguous("clip-b", 28, 70), confident("clip-c", 46)],
        strategy_extra={
            "resolved_song_takes": [
                {"media_id": "clip-a", "delta_s": 10.0, "place": "pinned"},
                {
                    "media_id": "clip-b",
                    "delta_s": None,
                    "place": "broll",
                    "reason": "no_room",
                    "confirmed_by_creator": True,
                },
                {"media_id": "clip-c", "delta_s": 46.0, "place": "pinned"},
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


def _plan_error(harness, *, stamp=None, **kwargs):
    job, snapshot, _bindings = harness(**kwargs)
    if stamp is not None:
        from app.services.creator_render_contract import CONTRACT_FIELD

        _stamp_job(job, **stamp)
        snapshot[CONTRACT_FIELD] = job.assembly_plan[CONTRACT_FIELD]  # as dispatch hands it over
    with pytest.raises(UnsupportedPhonePlan) as caught:
        gb._run_phone_unified_montage_job(
            str(job.id), snapshot, job.all_candidates, ownership_epoch=3
        )
    return str(caught.value), caught.value


def _fallback_plan(job):
    record = job.assembly_plan["unified_montage"]
    return record["user_song"], job.assembly_plan["guided_story_execution_plan"]


def test_no_matched_takes_falls_back_to_the_song_as_background(harness):
    job, _snapshot, _bindings = harness(
        sync="lipsync", rows=[unmatched("clip-a"), unmatched("clip-b"), unmatched("clip-c")]
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt, plan = _fallback_plan(job)
    assert receipt["mode"] == "background"
    assert receipt["requested_mode"] == "lipsync"
    assert receipt["fallback_reason"] == "no_synced_takes"
    assert set(receipt["unmatched_ids"]) == set(CLIPS)
    assert plan["user_song"]["mode"] == "background"
    assert {m["media_id"] for m in plan["story_timeline"]} == set(CLIPS)
    recipe, _clip = _song_clip(job)  # song bed binds, compile + validator accept it
    assert recipe.audio.original_volume == 0.0


def test_a_stale_take_alignment_falls_back_to_background(harness):
    # The alignment was computed for clip proxies that have since been replaced.
    stale = alignment(confident("clip-a", 10), confident("clip-b", 28))
    for row in stale.takes.values():
        row.proxy_generation = 99
    job, _snapshot, _bindings = harness(sync="lipsync", song_alignment=stale)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    assert _fallback_plan(job)[0]["fallback_reason"] == "no_synced_takes"


def test_alignment_for_another_song_generation_falls_back_to_background(harness):
    other = SongAlignment(song_generation=SONG_GENERATION + 1, takes={})
    job, _snapshot, _bindings = harness(sync="lipsync", song_alignment=other)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    assert _fallback_plan(job)[0]["mode"] == "background"


def test_kri466_eight_unmatched_takes_never_fail_the_job(harness, monkeypatch):
    """Prod replay: 8 takes, none matched, no answer: the song stays as the beat bed."""
    ids = tuple(f"clip-{i}" for i in range(8))
    monkeypatch.setattr(sys.modules[__name__], "CLIPS", ids)
    job, _snapshot, _bindings = harness(sync="lipsync", rows=[unmatched(m) for m in ids])

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt, plan = _fallback_plan(job)
    assert receipt["mode"] == "background"
    assert receipt["fallback_reason"] == "no_synced_takes"
    assert set(receipt["unmatched_ids"]) == set(ids)
    assert {m["media_id"] for m in plan["story_timeline"]} == set(ids)


def test_kri466_eight_unmatched_takes_the_creator_ordered_are_stacked_from_the_first_line(
    harness, monkeypatch
):
    """The same eight takes after the creator answered: they play in her order."""
    ids = tuple(f"clip-{i}" for i in range(8))
    monkeypatch.setattr(sys.modules[__name__], "CLIPS", ids)
    durations = {m: 6.0 for m in ids}
    alignment_ = alignment(*[unmatched(m) for m in ids])
    from app.services.song_order import resolve_uncertain_takes, resolved_song_takes_payload

    resolved = resolve_uncertain_takes(
        alignment_, list(ids), durations, SONG_DURATION_S, first_line_s=2.0
    )
    job, _snapshot, _bindings = harness(
        sync="lipsync",
        song_alignment=alignment_,
        durations=durations,
        strategy_extra={"resolved_song_takes": resolved_song_takes_payload(resolved)},
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    plan = job.assembly_plan["guided_story_execution_plan"]
    song = plan["user_song"]
    assert song["mode"] == "lipsync"
    assert set(song["takes"]) == set(ids)
    assert {t["position_basis"] for t in song["takes"].values()} == {"creator_stack"}
    deltas = [song["takes"][m]["delta_s"] for m in ids]
    assert deltas == sorted(deltas)  # the creator's order is the play order
    assert song["window_end_s"] - song["window_start_s"] <= 120.0


# ── KRI-471 prod replay (job aed98bf6): 8 sung takes over one 226 s song ─────

_REPLAY_DURATIONS = {
    "t1": 7.2,
    "t2": 4.57,
    "t3": 7.23,
    "t4": 8.9,
    "t5": 8.87,
    "t6": 13.77,
    "t7": 7.63,
    "t8": 13.17,
}
_REPLAY_CANDIDATES = {
    "t1": [(0.72, 0.54)],
    "t2": [],
    "t3": [(11.56, 0.55)],
    "t4": [(15.77, 0.56)],
    "t5": [(24.02, 0.21)],
    "t6": [],
    "t7": [(151.96, 0.3), (138.24, 0.3), (56.42, 0.11), (70.96, 0.05)],
    "t8": [(173.24, 0.6), (145.79, 0.56), (159.54, 0.55), (49.79, 0.55), (63.52, 0.51)],
}


def _replay_alignment():
    from app.schemas.user_song import PlacementCandidate, TakeAlignment

    rows = []
    for media_id, cands in _REPLAY_CANDIDATES.items():
        if not cands:
            rows.append(unmatched(media_id))
            continue
        ranked = sorted(cands, key=lambda c: -c[1])
        rows.append(
            TakeAlignment(
                media_id=media_id,
                status="confident" if len(ranked) == 1 and ranked[0][1] >= 0.35 else "ambiguous",
                delta_s=ranked[0][0],
                confidence=ranked[0][1],
                likelihood=ranked[0][1],
                margin=1.0 if len(ranked) == 1 else (ranked[0][1] - ranked[1][1]) / ranked[0][1],
                candidates=[
                    PlacementCandidate(delta_s=d, likelihood=li, method="lyrics")
                    for d, li in ranked
                ],
            )
        )
    return alignment(*rows)


def _replay(harness, monkeypatch, *, answer: bool):
    ids = tuple(_REPLAY_DURATIONS)
    monkeypatch.setattr(sys.modules[__name__], "CLIPS", ids)
    song = analysis(duration_s=226.4)
    al = _replay_alignment()
    extra = None
    if answer:
        from app.services.song_order import resolve_uncertain_takes, resolved_song_takes_payload

        resolved = resolve_uncertain_takes(
            al, list(ids), _REPLAY_DURATIONS, 226.4, first_line_s=2.0
        )
        extra = {"resolved_song_takes": resolved_song_takes_payload(resolved)}
    job, _snapshot, _bindings = harness(
        sync="lipsync",
        song_alignment=al,
        song_analysis=song,
        durations=_REPLAY_DURATIONS,
        strategy_extra=extra,
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    plan = job.assembly_plan["guided_story_execution_plan"]
    return plan, job.assembly_plan["unified_montage"]["user_song"]


def _assert_every_take_accounted_for(plan, receipt):
    song = plan["user_song"]
    ids = set(_REPLAY_DURATIONS)
    in_edit = {m["media_id"] for m in plan["story_timeline"]}
    reasons = {d["media_id"]: d["reason"] for d in receipt["dropped"]}
    assert ids <= in_edit | set(reasons) | set(receipt["placed_outside_ids"]), "a take vanished"
    assert song["window_end_s"] - song["window_start_s"] <= 120.0
    for media_id in receipt["placed_outside_ids"]:
        assert reasons.get(media_id) in ("gap_unfillable", "outside_window")


def test_prod_replay_8_takes_without_an_answer(harness, monkeypatch):
    plan, receipt = _replay(harness, monkeypatch, answer=False)
    takes = plan["user_song"]["takes"]
    _assert_every_take_accounted_for(plan, receipt)
    # The clear takes sit where the aligner put them, unconfirmed.
    for media_id, delta in (("t1", 0.72), ("t3", 11.56), ("t4", 15.77)):
        assert takes[media_id]["delta_s"] == pytest.approx(delta)
        assert takes[media_id]["position_basis"] == "aligner"
    assert all(t["confirmed_by_creator"] is False for t in takes.values())
    # The weak take is placed but flagged; the chorus repeat lands in the cluster.
    assert "t5" in receipt["low_confidence_ids"]
    placed = {p["media_id"]: p for p in receipt["placed"]}
    if "t8" in placed:
        assert placed["t8"]["delta_s"] < 80
    # Nothing to place t2 / t6 by: they stay muted B-roll, never guessed.
    assert "t2" not in takes and "t6" not in takes
    assert {"t2", "t6"} <= set(receipt["broll_ids"]) | set(receipt["kept_broll_ids"])


def test_prod_replay_8_takes_with_the_creators_order(harness, monkeypatch):
    plan, receipt = _replay(harness, monkeypatch, answer=True)
    takes = plan["user_song"]["takes"]
    _assert_every_take_accounted_for(plan, receipt)
    # t2 and t6 are no longer filler: the answer stacks them between their neighbours.
    for media_id in ("t2", "t6"):
        assert takes[media_id]["position_basis"] == "creator_stack"
        assert takes[media_id]["confirmed_by_creator"] is True
    assert takes["t2"]["delta_s"] < takes["t3"]["delta_s"]
    assert takes["t6"]["delta_s"] > takes["t5"]["delta_s"]
    # The window is fuller than the unanswered one (3.9 -> 30.3 s).
    song = plan["user_song"]
    assert song["window_end_s"] - song["window_start_s"] > 35.0


def test_prod_replay_8_takes_sync_invariant_holds_for_stacked_takes(harness, monkeypatch):
    plan, _receipt = _replay(harness, monkeypatch, answer=True)
    song = plan["user_song"]
    for moment in plan["story_timeline"]:
        pinned = song["takes"].get(moment["media_id"])
        if pinned is None:
            continue
        assert (
            lipsync_sync_error_s(
                output_start_s=moment["output_start_s"],
                source_start_s=moment["source_start_s"],
                delta_s=pinned["delta_s"],
                window_start_s=song["window_start_s"],
            )
            <= 0.0015
        )


def test_a_replaced_song_fails_closed_before_planning(harness):
    stale = analysis().model_copy(update={"generation": SONG_GENERATION + 1})
    message, _error = _plan_error(harness, sync="background", song_analysis=stale)
    assert "replaced" in message


def test_an_unreadable_song_names_the_problem(harness):
    failed = analysis().model_copy(update={"status": "failed"})
    message, _error = _plan_error(harness, sync="background", song_analysis=failed)
    assert "couldn't read your song" in message


def test_the_dispatcher_maps_a_song_decline_to_user_song_plan_declined(harness, monkeypatch):
    stale = analysis().model_copy(update={"generation": SONG_GENERATION + 1})
    job, _snapshot, _bindings = harness(sync="background", song_analysis=stale)
    fail = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", fail)
    monkeypatch.setattr(gb, "mark_failed_phase", Mock())
    gb._run_generative_job(str(job.id))
    fail.assert_called_once()
    args, kwargs = fail.call_args
    assert kwargs["failure_reason"] == "user_song_plan_declined"
    assert "replaced" in args[1]


def test_a_failed_background_fallback_declines_with_the_no_synced_takes_copy(harness, monkeypatch):
    job, snapshot, _bindings = harness(sync="lipsync", rows=[unmatched("clip-a")])
    monkeypatch.setattr(
        "app.pipeline.unified_montage.plan_unified_montage",
        Mock(side_effect=ValueError("boom")),
    )
    with pytest.raises(UserSongPlanDeclined) as caught:
        gb._run_phone_unified_montage_job(
            str(job.id), snapshot, job.all_candidates, ownership_epoch=3
        )
    assert "sing along clearly" in str(caught.value)


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
        "origin": "creator",
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


# ── KRI-569: the song's lyrics as captions ───────────────────────────────────

CAPTION_REQUEST = "Full creator request (chronological):\nLipsync\nAdd captions"
KARAOKE = {"caption_style": "karaoke"}


def _lyrics_analysis(count: int = 230):
    """The synthetic song sings a word every half second from 2.0 s."""
    words = [
        SongWord(text=f"la{i}", start_s=2.0 + i * 0.5, end_s=2.0 + i * 0.5 + 0.4)
        for i in range(count)
    ]
    return analysis().model_copy(update={"words": words})


@pytest.fixture
def composer(monkeypatch):
    """Patch ONLY the model client: the composer says the request is already satisfied."""
    from app.agents.edit_copilot import EditCopilotAgent

    seen = []

    def run(self, input, *, ctx=None):
        seen.append(input)
        reply = {
            "intent": "describe",
            "confidence": 0.95,
            "reply": "Captions are in the caption lane.",
            "ops": [],
        }
        return self.parse(json.dumps(reply), input)

    monkeypatch.setattr(EditCopilotAgent, "run", run)
    return seen


def _caption_rows(plan):
    return [row for row in plan["text_elements"] if row["id"].startswith("song-caption-")]


def test_lipsync_with_captions_pins_the_song_lyrics_as_editable_caption_rows(harness, composer):
    job, _snapshot, _bindings = harness(
        sync="lipsync",
        rows=[confident("clip-a", 10), confident("clip-b", 28), confident("clip-c", 46)],
        song_analysis=_lyrics_analysis(),
        creator_request=CAPTION_REQUEST,
        strategy_extra=KARAOKE,
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    plan = job.assembly_plan["guided_story_execution_plan"]
    song = plan["user_song"]
    track = job.assembly_plan["guided_edit"]["approved_proposal"]["song_captions"]
    assert track["song_generation"] == SONG_GENERATION
    assert track["caption_style"] == "word"  # karaoke asks for one word at a time
    rows = _caption_rows(plan)
    assert rows
    # Output time is song time minus the window start, and nothing leaves the window.
    window = song["window_end_s"] - song["window_start_s"]
    first = rows[0]
    index = int(first["id"].removeprefix("song-caption-"))
    assert first["start_s"] == pytest.approx(
        track["words"][index]["start_s"] - song["window_start_s"]
    )
    assert all(0 <= row["start_s"] < row["end_s"] <= window + 1e-6 for row in rows)
    # They are the SAME rows narration captions use, so the caption panel edits them.
    assert all(row["source_params"]["source"] == "caption_cue" for row in rows)
    assert plan["editor_caption_meta"] == {"style": "word", "y_frac": 0.7}
    # The text composer saw a summary of the lane, not 100+ caption rows.
    (seen,) = composer
    lane = seen.variant_snapshot["caption_lane"]
    assert lane["origin"] == "song_lyrics" and lane["words"] == len(rows)
    assert not any(
        bar["id"].startswith("song-caption-") for bar in seen.variant_snapshot["text_bars"]
    )
    # The phone recipe carries them as text layers.
    recipe, _clip = _song_clip(job)
    drawn = " ".join(run.text for layer in recipe.text_layers for run in layer.runs)
    assert first["text"] in drawn
    record = job.assembly_plan["unified_montage"]
    assert record["captions"]["status"] == "placed"
    assert record["captions"]["style"] == "word"
    assert record["captions"]["words"] == len(rows)


def test_background_song_with_a_plain_captions_ask_shows_whole_lyric_lines(harness, composer):
    job, _snapshot, _bindings = harness(
        sync="background",
        song_analysis=_lyrics_analysis(),
        creator_request="Full creator request (chronological):\nAdd captions",
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    plan = job.assembly_plan["guided_story_execution_plan"]
    assert plan["editor_caption_meta"]["style"] == "sentence"
    rows = _caption_rows(plan)
    assert rows
    # A lyric line ends where the song's own line ends (no punctuation needed).
    ends = [row["id"] for row in rows if row["source_params"].get("line_end")]
    assert ends
    recipe, _clip = _song_clip(job)
    lines = {run.text for layer in recipe.text_layers for run in layer.runs}
    assert any(text.count("la") > 1 for text in lines)  # a whole line, not one word


def test_a_met_captions_ask_is_judged_from_the_real_plan_record(harness, composer):
    from app.kria.brief import BriefRequirement
    from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage

    job, _snapshot, _bindings = harness(
        sync="background", song_analysis=_lyrics_analysis(), creator_request=CAPTION_REQUEST
    )
    gb._run_generative_job(str(job.id))

    record = job.assembly_plan["unified_montage"]
    ask = BriefRequirement(
        id="r1", kind="style", scope="global", description="add captions to the lip-sync"
    )
    (receipt,) = build_receipts([ask], plan_facts_from_unified_montage(record))
    assert receipt.status == "met"


# --- Lyrics ride along hidden when nobody asked (the Captions toggle reveals them) ---
#
# Failure modes written first:
#   (a) a song with no words (or none in the window) declines a montage that never asked;
#   (b) the plan record says captions were "placed" while nothing is on screen;
#   (c) the editor document carries lyric rows without `enabled: false`, so they draw unasked;
#   (d) turning captions on in the editor leaves `editor_caption_meta` stale (still hidden);
#   (e) the text composer is told lyrics are on screen when they are off.

_LYRIC_RE = re.compile(r"\bla\d+\b")


def _drawn_lyrics(job):
    recipe = device_status(job, "guided_story").request.recipe
    return [
        run.text for layer in recipe.text_layers for run in layer.runs if _LYRIC_RE.search(run.text)
    ]


@pytest.mark.parametrize("sync", ["lipsync", "background"])
def test_without_a_captions_ask_the_lyrics_ride_along_hidden(harness, composer, sync):
    from app.kria.brief_checks import plan_facts_from_unified_montage
    from app.services.creation_text_composition import CreationTextComposer

    job, _snapshot, _bindings = harness(
        sync=sync,
        rows=[confident("clip-a", 10), confident("clip-b", 28), confident("clip-c", 46)],
        song_analysis=_lyrics_analysis(),
        creator_request="Full creator request (chronological):\nMake a fast montage",
        strategy_extra=KARAOKE,  # the model's own default is never a request
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    track = job.assembly_plan["guided_edit"]["approved_proposal"]["song_captions"]
    assert track["hidden"] is True
    assert track["caption_style"] == "word"  # the toggle reveals them in the planned style
    plan = job.assembly_plan["guided_story_execution_plan"]
    assert _caption_rows(plan)
    assert plan["editor_caption_meta"] == {"style": "word", "y_frac": 0.7, "enabled": False}
    assert _drawn_lyrics(job) == []
    record = job.assembly_plan["unified_montage"]
    assert record["captions"]["status"] == "available_hidden"
    assert plan_facts_from_unified_montage(record).montage_captions != "placed"
    (seen,) = composer
    assert seen.variant_snapshot["caption_lane"]["shown"] is False
    prompt = CreationTextComposer.__new__(CreationTextComposer).render_prompt(seen)
    assert "already on screen" not in prompt


def _editor_save(job, **sections):
    from app.routes import generative_jobs as gj

    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    payload = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        guided_revision_number=revision["revision_number"],
        **sections,
    )
    gj.require_guided_story_editor_commit(job, "guided_story", payload)
    gj.prepare_editor_commit(job, "guided_story", payload)


def test_turning_hidden_lyrics_on_draws_them_and_keeps_the_creators_edits_across_a_song_move(
    harness, composer, monkeypatch
):
    from app.routes import generative_jobs as gj

    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    job, _snapshot, _bindings = harness(
        sync="background",
        song_analysis=_lyrics_analysis(),
        creator_request="Full creator request (chronological):\nMake a fast montage",
    )
    gb._run_generative_job(str(job.id))
    assert _drawn_lyrics(job) == []
    variant = job.assembly_plan["variants"][0]
    assert gj._guided_v2_revision(job, variant)["caption_meta"]["enabled"] is False
    # A song move while hidden re-times the lyric rows but keeps them off.
    start = job.assembly_plan["guided_story_execution_plan"]["user_song"]["window_start_s"]
    _editor_save(job, user_song=gj.EditorCommitUserSong(window_start_s=start + 0.5))
    assert _drawn_lyrics(job) == []
    saved = job.assembly_plan["variants"][0][PHONE_EDITOR_SAVED_PLAN_FIELD]
    assert saved["editor_caption_meta"]["enabled"] is False
    assert _caption_rows(saved)

    # Show captions: a Save that touches only the caption settings.
    _editor_save(
        job,
        caption_meta=gj.EditorCommitCaptionMeta(enabled=True, style="sentence", color="#FFD400"),
    )
    saved = job.assembly_plan["variants"][0][PHONE_EDITOR_SAVED_PLAN_FIELD]
    assert saved["editor_caption_meta"]["enabled"] is True
    recipe = device_status(job, "guided_story").request.recipe
    lyric_runs = [
        run for layer in recipe.text_layers for run in layer.runs if _LYRIC_RE.search(run.text)
    ]
    assert lyric_runs
    assert all(
        (run.fill.red, round(run.fill.green, 2), run.fill.blue) == (1.0, 0.83, 0.0)
        for run in lyric_runs
    )
    assert any(run.text.count("la") > 1 for run in lyric_runs)  # whole lyric lines

    # Rewrite one lyric word and delete another (omitted from the Save).
    variant = job.assembly_plan["variants"][0]
    rows = copy.deepcopy(gj._guided_v2_revision(job, variant)["text_elements"])
    lyrics = [
        row for row in rows if row["id"].startswith("song-caption-") and not row.get("removed")
    ]
    edited, deleted = lyrics[3], lyrics[5]
    edited["text"] = "my own words"
    edited["word_timings"][0]["text"] = "my own words"
    _editor_save(
        job,
        text_elements=[row for row in rows if row is not deleted],
    )

    # Move the song a second later: timing follows the song, the creator's edits hold.
    start = job.assembly_plan["variants"][0][PHONE_EDITOR_SAVED_PLAN_FIELD]["user_song"][
        "window_start_s"
    ]
    _editor_save(job, user_song=gj.EditorCommitUserSong(window_start_s=start + 1.0))
    saved = job.assembly_plan["variants"][0][PHONE_EDITOR_SAVED_PLAN_FIELD]
    assert saved["user_song"]["window_start_s"] == pytest.approx(start + 1.0)
    assert saved["editor_caption_meta"]["enabled"] is True
    drawn = " ".join(_drawn_lyrics(job))
    assert "my own words" in drawn
    assert not re.search(rf"\b{deleted['text']}\b", drawn)
    assert _drawn_lyrics(job)


# --- The editor READ the iPhone loads (status variant + timeline), hidden vs asked ---
#
# Failure modes written first:
#   (f) the status variant drops the lyric rows or their caption_cue tag, so the phone's
#       Edit captions list is empty and Show captions has nothing to reveal;
#   (g) `caption_meta` loses `enabled: false` on read, so the phone draws lyrics unasked;
#   (h) the device clamp closes caption_cues / caption_meta / caption_editor_style, so the
#       Show captions toggle is disabled on a guided phone montage;
#   (i) a row carries its own `enabled: false` / `removed`, so turning captions on in the
#       panel still draws nothing (the phone compiler skips such rows per row);
#   (j) a no-op re-read changes the revision hash or writes to the job (reads as an edit);
#   (k) a ready render without the hidden rows' pixels fails the receipt check;
#   (l) an asked-for track grows a `hidden` key (its approval hash would change).


def _editor_read(job):
    from app.routes import generative_jobs as gj

    before = copy.deepcopy(job.assembly_plan)
    (variant,) = gj._variants_for_response(job)
    raw = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, raw)
    timeline = gj.dispatch_get_timeline(
        job, "guided_story", sign_url=lambda path, _ttl: f"https://signed.test/{path}"
    )
    assert job.assembly_plan == before, "reading the editor document wrote to the job"
    return variant, revision, timeline


def _receipt_without_hidden_pixels(plan):
    from tests.pipeline.test_guided_story import _ready_result

    result = _ready_result(plan)
    drawn = [
        row["id"]
        for row in plan["text_elements"]
        if row["source_params"].get("source") != "caption_cue"
    ]
    result["render_receipt"].update(
        expected_text_ids=drawn,
        actual_text_ids=drawn,
        text_stages=[{"element_id": element_id, "visible": True} for element_id in drawn],
    )
    return result


@pytest.mark.parametrize("asked", [False, True])
def test_the_phone_editor_read_offers_hidden_lyrics_behind_the_captions_toggle(
    harness, composer, monkeypatch, asked
):
    from app.pipeline.guided_story import GuidedStoryError, validate_ready_result
    from app.routes import generative_jobs as gj

    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(gj, "signed_get_url", lambda path, ttl: f"https://signed.test/{path}")
    job, _snapshot, _bindings = harness(
        sync="background",
        song_analysis=_lyrics_analysis(),
        creator_request=CAPTION_REQUEST
        if asked
        else "Full creator request (chronological):\nMake a fast montage",
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    track = job.assembly_plan["guided_edit"]["approved_proposal"]["song_captions"]
    assert ("hidden" in track) is (not asked)

    variant, revision, timeline = _editor_read(job)

    lyrics = [row for row in variant["text_elements"] if row["id"].startswith("song-caption-")]
    assert lyrics
    assert all(row["source_params"]["source"] == "caption_cue" for row in lyrics)
    assert not any(row.get("enabled") is False or row.get("removed") for row in lyrics)
    meta = variant["caption_meta"]
    assert meta["style"] == "sentence"
    assert meta.get("enabled") is (False if not asked else None)
    assert revision["caption_meta"] == meta
    capabilities = variant["editor_capabilities"]
    assert capabilities["caption_cues"]["editable"] is True, capabilities["caption_cues"]
    assert capabilities["caption_meta"]["editable"] is True, capabilities["caption_meta"]
    assert capabilities["caption_editor_style"] is True
    assert capabilities["text_elements"] is True

    again, revision_again, timeline_again = _editor_read(job)
    assert revision_again["state_hash"] == revision["state_hash"]
    assert timeline_again["revision_hash"] == timeline["revision_hash"]
    assert timeline["has_user_edits"] is False
    assert again["render_status"] == variant["render_status"]
    assert again["caption_meta"] == meta

    plan = job.assembly_plan["guided_story_execution_plan"]
    result = _receipt_without_hidden_pixels(plan)
    if asked:
        with pytest.raises(GuidedStoryError):
            validate_ready_result(plan, result, job_id="job-1", verify_storage=False)
    else:
        assert validate_ready_result(plan, result, job_id="job-1", verify_storage=False)


# --- Clip speed on a song montage whose lyrics are pinned hidden ---
#
# Failure modes written first:
#   (m) hidden lyric rows lock clip speed, so every background-song montage loses the
#       speed control it had before lyrics were pinned by default;
#   (n) captions turned on (stored, or in the same Save as a speed change) leave speed
#       open, so a speed change could desync drawn lyrics from the edit;
#   (o) the lip-sync lock (song-clock clips) opens because its lyrics are hidden;
#   (p) a narration/subtitled caption row (timed to source speech) opens with
#       `enabled: false`, because the exemption keys on the flag instead of the origin.


def _speed_save(job, rate):
    from app.routes import generative_jobs as gj

    variant = job.assembly_plan["variants"][0]
    first = gj.editor_deletion_timeline(job, variant)[0]
    return gj.TimelineSlotEdit(
        slot_id=first["slot_id"],
        clip_index=first["clip_index"],
        in_s=first["in_s"],
        duration_s=first["duration_s"],
        playback_rate=rate,
    )


def _speed(job):
    """The phone's speed control and the reason a Save would be refused with."""
    from app.routes import generative_jobs as gj

    (variant,) = gj._variants_for_response(job)
    editable = variant["editor_capabilities"]["clips"]["playback_rate"]["editable"]
    return editable, gj._phone_clip_speed_reason(job, job.assembly_plan["variants"][0])


def _refused(job, **sections):
    from fastapi import HTTPException

    from app.routes import generative_jobs as gj

    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    payload = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        guided_revision_number=revision["revision_number"],
        **sections,
    )
    with pytest.raises(HTTPException) as caught:
        gj.require_guided_story_editor_commit(job, "guided_story", payload)
    return caught.value.status_code, caught.value.detail


def test_hidden_song_lyrics_leave_clip_speed_editable_until_captions_are_shown(
    harness, composer, monkeypatch
):
    from app.routes import generative_jobs as gj

    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", PROD_VERIFIED_FEATURES)
    monkeypatch.setattr(gj, "signed_get_url", lambda path, ttl: f"https://signed.test/{path}")
    job, _snapshot, _bindings = harness(
        sync="background",
        song_analysis=_lyrics_analysis(),
        creator_request="Full creator request (chronological):\nMake a fast montage",
    )
    gb._run_generative_job(str(job.id))
    assert job.assembly_plan["guided_edit"]["approved_proposal"]["song_captions"]["hidden"]

    assert _speed(job) == (True, None)
    on = gj.EditorCommitCaptionMeta(enabled=True, style="sentence")
    assert _refused(job, caption_meta=on, timeline_slots=[_speed_save(job, 2.0)]) == (
        422,
        {"code": "caption_timing_locked"},
    )
    _editor_save(job, timeline_slots=[_speed_save(job, 2.0)])
    assert (
        gj.editor_deletion_timeline(job, job.assembly_plan["variants"][0])[0]["playback_rate"]
        == 2.0
    )

    _editor_save(job, caption_meta=on)
    assert _speed(job) == (False, "caption_timing_locked")
    assert _refused(job, timeline_slots=[_speed_save(job, 1.0)]) == (
        422,
        {"code": "caption_timing_locked"},
    )

    speech_row = {
        "id": "caption-0",
        "source_params": {"source": "caption_cue", "key": "0", "origin": "narration"},
    }
    hidden = {"enabled": False, "style": "sentence"}
    assert (
        gj._phone_speed_lane_reason({"text_elements": [speech_row], "caption_meta": hidden})
        == "caption_timing_locked"
    )


def test_hidden_song_lyrics_keep_a_lipsync_montage_speed_locked(harness, composer, monkeypatch):
    from app.routes import generative_jobs as gj

    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", PROD_VERIFIED_FEATURES)
    monkeypatch.setattr(gj, "signed_get_url", lambda path, ttl: f"https://signed.test/{path}")
    job, _snapshot, _bindings = harness(
        sync="lipsync",
        rows=[confident("clip-a", 10), confident("clip-b", 28), confident("clip-c", 46)],
        song_analysis=_lyrics_analysis(),
        creator_request="Full creator request (chronological):\nLipsync",
    )
    gb._run_generative_job(str(job.id))
    assert job.assembly_plan["guided_edit"]["approved_proposal"]["song_captions"]["hidden"]

    assert _speed(job) == (False, "user_song_lipsync_locked")


@pytest.mark.parametrize("song_analysis", [analysis, lambda: _late_lyrics_analysis()])
def test_a_song_without_words_to_caption_never_declines_a_montage_that_did_not_ask(
    harness, composer, song_analysis
):
    job, _snapshot, _bindings = harness(
        sync="background",
        song_analysis=song_analysis(),
        creator_request="Full creator request (chronological):\nMake a fast montage",
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert "song_captions" not in job.assembly_plan["guided_edit"]["approved_proposal"]
    assert "captions" not in job.assembly_plan["unified_montage"]


def test_available_by_default_off_is_byte_identical_to_before(harness, composer, monkeypatch):
    monkeypatch.setattr(gb.settings, "song_captions_available_by_default", False)
    job, _snapshot, _bindings = harness(
        sync="background",
        song_analysis=_lyrics_analysis(),
        creator_request="Full creator request (chronological):\nMake a fast montage",
        strategy_extra=KARAOKE,
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert "song_captions" not in job.assembly_plan["guided_edit"]["approved_proposal"]
    assert not _caption_rows(job.assembly_plan["guided_story_execution_plan"])
    assert "captions" not in job.assembly_plan["unified_montage"]
    assert "caption_lane" not in composer[0].variant_snapshot


def test_kill_switch_off_is_byte_identical_to_before(harness, composer, monkeypatch):
    monkeypatch.setattr(gb.settings, "song_captions_enabled", False)
    job, _snapshot, _bindings = harness(
        sync="background", song_analysis=_lyrics_analysis(), creator_request=CAPTION_REQUEST
    )

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert "song_captions" not in job.assembly_plan["guided_edit"]["approved_proposal"]
    assert not _caption_rows(job.assembly_plan["guided_story_execution_plan"])
    assert "caption_lane" not in composer[0].variant_snapshot


def test_a_song_with_no_words_asks_instead_of_rendering_without_captions(harness):
    message, error = _plan_error(
        harness, sync="background", creator_request=CAPTION_REQUEST, strategy_extra=KARAOKE
    )
    assert isinstance(error, UserSongPlanDeclined)
    assert "couldn't hear any words in your song" in message


def test_prod_job_430219e4_shape_asks_for_the_song_instead_of_phone_plan_unsupported(harness):
    """Lip-sync + 'Add captions', clips' own audio, NO uploaded song (the failed prod job)."""
    message, error = _plan_error(
        harness,
        sync="background",
        with_song=False,
        creator_request=CAPTION_REQUEST,
        strategy_extra={**KARAOKE, "media_scope": "all"},
    )
    assert isinstance(error, UserSongPlanDeclined)
    assert message == gb.SONG_CAPTIONS_NEED_SONG_MESSAGE


def _speech_runner_declines(monkeypatch):
    """A montage with no song is offered to the spoken-excerpt runner first; it passes."""
    monkeypatch.setattr(
        "app.services.phone_speech_montage_job.run_phone_speech_montage_job",
        Mock(return_value=False),
    )


def test_the_dispatcher_shows_the_ask_for_the_song_verbatim(harness, monkeypatch):
    job, _snapshot, _bindings = harness(
        sync="background", with_song=False, creator_request=CAPTION_REQUEST, strategy_extra=KARAOKE
    )
    _speech_runner_declines(monkeypatch)
    fail = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", fail)
    monkeypatch.setattr(gb, "mark_failed_phase", Mock())
    gb._run_generative_job(str(job.id))
    fail.assert_called_once()
    assert fail.call_args.kwargs["failure_reason"] == "user_song_plan_declined"
    assert fail.call_args.args[1] == gb.SONG_CAPTIONS_NEED_SONG_MESSAGE


def test_a_montage_without_a_song_or_a_lipsync_ask_is_not_rerouted(harness, composer, monkeypatch):
    job, _snapshot, _bindings = harness(
        sync="background",
        with_song=False,
        creator_request="Full creator request (chronological):\nAdd captions",
        strategy_extra=KARAOKE,
    )
    _speech_runner_declines(monkeypatch)
    gb._run_generative_job(str(job.id))  # reaches the existing composer path unchanged
    assert job.status == "awaiting_device"


def _late_lyrics_analysis():
    """The song only sings in its last seconds; the montage window sits near the start."""
    words = [
        SongWord(text=f"la{i}", start_s=100.0 + i * 0.5, end_s=100.4 + i * 0.5) for i in range(20)
    ]
    return analysis().model_copy(update={"words": words})


def test_an_instrumental_stretch_asks_instead_of_claiming_captions_it_never_placed(
    harness, composer
):
    message, error = _plan_error(
        harness,
        sync="background",
        song_analysis=_late_lyrics_analysis(),
        creator_request=CAPTION_REQUEST,
    )
    assert isinstance(error, UserSongPlanDeclined)
    assert "no sung words in the part of the song your video uses" in message


def test_user_song_flag_off_leaves_a_songless_lipsync_caption_ask_on_the_legacy_path(
    harness, composer, monkeypatch
):
    """USER_SONG_MONTAGE_ENABLED=false: the chat never asks for a song; neither does the worker."""
    job, _snapshot, _bindings = harness(
        sync="background", with_song=False, creator_request=CAPTION_REQUEST, strategy_extra=KARAOKE
    )
    _speech_runner_declines(monkeypatch)
    monkeypatch.setattr(gb.settings, "user_song_montage_enabled", False)
    fail = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", fail)
    monkeypatch.setattr(gb, "mark_failed_phase", Mock())

    gb._run_generative_job(str(job.id))

    assert not fail.called
    assert job.status == "awaiting_device"


# --- KRI-578: plan-authority jobs take the lyric-caption decision from the pinned strategy ---
#
# Failure modes written first:
#   (1) a stamped job still re-reads the saved request (a caption ask in the text turns captions
#       on, or a missing ask turns a pinned decision off);
#   (2) the pinned style is ignored (word vs whole lines comes from the request again);
#   (3) a pinned decision with the song gone renders silently without captions;
#   (4) the unstamped twins above (CAPTION_REQUEST / "Add captions") change behaviour.


def _stamp_job(job, **pin):
    """Pin the approved contract + plan-authority stamp, as dispatch does for a stamped job."""
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.services.creator_render_contract import (
        CONTRACT_FIELD,
        PLAN_AUTHORITY_FIELD,
        REQUIREMENT_VERSION_FIELD,
        build_render_contract,
    )
    from app.services.generative_jobs import CREATOR_RENDER_CONTRACT_VERSION

    strategy = dict(job.all_candidates["creator_strategy"], **pin)
    typed = CreativeStrategy.model_validate(strategy).model_dump(mode="json", exclude_none=True)
    contract = build_render_contract(typed, generation_id="generation")
    assert contract is not None
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    job.all_candidates.update(
        {
            PLAN_AUTHORITY_FIELD: 1,
            REQUIREMENT_VERSION_FIELD: 1,
            "creator_render_contract_version": CREATOR_RENDER_CONTRACT_VERSION,
            "creator_strategy": typed,
        }
    )


def test_a_stamped_lipsync_pinned_word_captions_render_without_a_caption_ask_in_the_request(
    harness, composer
):
    job, _snapshot, _bindings = harness(
        sync="lipsync",
        rows=[confident("clip-a", 10), confident("clip-b", 28), confident("clip-c", 46)],
        song_analysis=_lyrics_analysis(),
        creator_request="Full creator request (chronological):\nMake a fast montage",
    )
    _stamp_job(job, song_captions="word")

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert (
        job.assembly_plan["guided_edit"]["approved_proposal"]["song_captions"]["caption_style"]
        == "word"
    )
    assert _caption_rows(job.assembly_plan["guided_story_execution_plan"])
    assert job.assembly_plan["unified_montage"]["captions"]["style"] == "word"


def test_a_stamped_background_song_pinned_sentence_captions_show_whole_lyric_lines(
    harness, composer
):
    job, _snapshot, _bindings = harness(
        sync="background",
        song_analysis=_lyrics_analysis(),
        # A karaoke default and a "word by word" ask in the text must not beat the pin.
        creator_request="Full creator request (chronological):\nCaptions word by word",
        strategy_extra=KARAOKE,
    )
    _stamp_job(job, song_captions="sentence")

    gb._run_generative_job(str(job.id))

    plan = job.assembly_plan["guided_story_execution_plan"]
    assert plan["editor_caption_meta"]["style"] == "sentence"
    assert _caption_rows(plan)


def test_a_stamped_montage_ignores_a_captions_ask_in_the_request_text(harness, composer):
    """The planner pinned no lyric captions: the saved request cannot turn them on."""
    job, _snapshot, _bindings = harness(
        sync="background",
        song_analysis=_lyrics_analysis(),
        creator_request=CAPTION_REQUEST,
        strategy_extra=KARAOKE,
    )
    _stamp_job(job)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert job.assembly_plan["guided_edit"]["approved_proposal"]["song_captions"]["hidden"]
    plan = job.assembly_plan["guided_story_execution_plan"]
    assert plan["editor_caption_meta"]["enabled"] is False
    assert _drawn_lyrics(job) == []
    assert job.assembly_plan["unified_montage"]["captions"]["status"] == "available_hidden"


def test_a_stamped_pin_with_the_song_gone_asks_for_the_song(harness):
    message, error = _plan_error(
        harness,
        sync="background",
        with_song=False,
        creator_request="Full creator request (chronological):\nMake a fast montage",
        strategy_extra=KARAOKE,
        stamp={"song_captions": "word"},
    )
    assert isinstance(error, UserSongPlanDeclined)
    assert message == gb.SONG_CAPTIONS_NEED_SONG_MESSAGE
