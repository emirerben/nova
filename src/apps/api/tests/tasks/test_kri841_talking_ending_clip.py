"""KRI-841: a phone Talking edit can END on a clip that plays its own sound.

"Add the X video at the very end with its own sound" must end the video on that
clip with its audio audible, and that clip must never also be placed (muted)
mid-talk as an overlay card, a full-screen insert, a reaction beat or a footage
cutaway.

These tests drive the REAL chain `_run_generative_job` -> `_run_phone_subtitled_job`
-> grounding -> `compile_phone_subtitled_plan` -> recipe validation, on the creator's
real footage (word timings synthesized evenly inside each spoken line):

  * Cafe T2 (Turkish prompt, ~/Downloads/kria-ecuador-test/T2_turkish_subtitles_talking/
    README.md): Caro talks 24.6 s; Visuals = Cotopaxi photo, oblea photo, the 6.4 s bite
    video WITH sound. "En sona Atakan'in ilk isirik videosunu kendi sesiyle ekle."
  * Don Julio T3 (English prompt, ~/Downloads/kria-julio-test/T3_spanish_subtitles_cutaways/
    README.md): three FOOTAGE clips - Lucas 12.9 s, an overhead table shot 2.0 s with no
    speech, and a 3.4 s cheers clip whose only word is "Cheers!". "At the very end, add the
    cheers video with its own sound."

Only transport is replaced (Whisper words, probe/ingest, the DB session, the Visuals pool
rows, Visuals binding, face sampling, trace writes). The Talking clip classifier, footage
cutaway scheduler, overlay + reaction-beat grounding, the compiler and recipe validation
are all real.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

import app.services.phone_overlay_grounding as ogm
import app.services.phone_reaction_grounding as rgm
import app.services.phone_visuals as phone_visuals_mod
from app.agents._schemas.creator_agent import CreativeStrategy
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    defers_caption_words_to_phone_render,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
    reply_from_receipts,
    requirements_to_check_at_draft,
)
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import CheckedStrategy, RefusedStrategy, check_strategy_for_runtime_v2
from app.pipeline.phone_subtitled_lanes import (
    PHONE_SUBTITLED_LANES_FIELD,
    PhoneSubtitledLanes,
    SubtitledEndingClip,
    SubtitledOverlayCard,
)
from app.pipeline.phone_subtitled_plan import (
    PhoneCutaway,
    compile_phone_subtitled_plan,
)
from app.pipeline.transcribe import Transcript, Word
from app.routes import generative_jobs as gj
from app.services import creator_capabilities as capabilities
from app.services import length_policy_state
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    build_render_contract,
    check_recipe_duration,
    read_render_contract,
    verify_phone_recipe,
)
from app.services.device_render import device_status
from app.services.length_policy_state import read_length_policy, stamp_length_policy
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD, PhoneVisualBinding
from app.services.phone_subtitled_editor import lanes_from_recipe
from app.services.sfx_catalog import SfxEntry
from app.tasks import generative_build as gb
from tests._prod_profile import PROD_VERIFIED_FEATURES, apply_prod_profile
from tests.pipeline.test_phone_subtitled_plan import _binding
from tests.routes.test_phone_subtitled_editor_commit import _enable_subtitled_editor
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _TALKING_HEAD_FEATURES,
    _assert_receipt_cutaways_match_recipe,
    _beat_dict,
    _enable_beats,
    _fake_resolve_sfx,
    _lane_request,
    _setup_subtitled,
    _setup_talking_multi,
)

# --------------------------------------------------------------------------- kit data


def _words_from(lines):
    """Whisper-like word timings: each token evenly inside its line's span."""
    out = []
    for start, end, text in lines:
        tokens = text.split()
        step = (end - start) / len(tokens)
        for i, token in enumerate(tokens):
            out.append(
                Word(
                    text=token,
                    start_s=round(start + i * step, 3),
                    end_s=round(start + (i + 1) * step, 3),
                    confidence=1.0,
                )
            )
    return out


CARO = _words_from(
    [
        (0.2, 2.2, "Hi, I'm Karolina, I'm from Ecuador."),
        (2.2, 5.2, "And this is the Oblea de la Casa."),
        (5.2, 8.7, "It's a traditional Colombian dessert."),
        (8.7, 15.4, "It's waffle cookie with bread, no, with blackberry jam,"),
        (15.4, 22.5, "cheese, dulce de leche, leche condensada and whipped cream."),
        (22.7, 23.7, "Nice, delicious."),
        (23.7, 24.4, "That's it."),
    ]
)
LUCAS = _words_from(
    [
        (0.0, 1.7, "What's your name?"),
        (2.0, 2.5, "Lucas."),
        (2.8, 3.6, "Lucas, yeah."),
        (4.2, 6.1, "Here we have traditional Argentinian sauces:"),
        (6.9, 7.9, "salsa criolla,"),
        (7.9, 9.4, "chimichurri and sarabella"),
        (10.7, 11.3, "Thank you."),
    ]
)
CHEERS = [Word(text="Cheers!", start_s=0.9, end_s=1.9, confidence=1.0)]

CAFE_TALK_S = 24.6
BITE_S = 6.4
JULIO_TALK_S = 12.9
CHEERS_S = 3.4

# Real pool rows carry a bare UUID; the creator's manifest names it "asset-<uuid>".
_NS = uuid.UUID("00000000-0000-4000-8000-000000000841")
COTOPAXI = str(uuid.uuid5(_NS, "cotopaxi"))
OBLEA = str(uuid.uuid5(_NS, "oblea"))
BITE = str(uuid.uuid5(_NS, "bite"))
_POOL = {
    COTOPAXI: {"kind": "image", "dur": None, "name": "T2_02_visual.jpg"},
    OBLEA: {"kind": "image", "dur": None, "name": "T2_03_visual.jpg"},
    BITE: {"kind": "video", "dur": BITE_S, "name": "T2_04_visual.mp4"},
}


def _pool_rows(ids=None):
    rows = []
    for asset_id in ids or _POOL:
        spec = _POOL[asset_id]
        rows.append(
            {
                "id": asset_id,
                "gcs_path": f"users/u1/plan/item1/pool/{asset_id}."
                f"{'mp4' if spec['kind'] == 'video' else 'jpg'}",
                "gcs_generation": "7",
                "kind": spec["kind"],
                "source_filename": spec["name"],
                "duration_s": spec["dur"],
                "aspect": 0.5625,
                "user_context": "",
                "analysis": {},
                "capture": None,
            }
        )
    return rows


def _visual(asset_id: str) -> PhoneVisualBinding:
    spec = _POOL[asset_id]
    extra = (
        {"duration_s": spec["dur"], "width": 1080, "height": 1920, "orientation_degrees": 0}
        if spec["kind"] == "video"
        else {}
    )
    row = _pool_rows([asset_id])[0]
    return PhoneVisualBinding(
        media_id=asset_id,
        gcs_path=row["gcs_path"],
        generation="7",
        sha256="a" * 64,
        byte_count=100,
        kind=spec["kind"],
        **extra,
    )


def _bind(_open, *, job_id, pins):  # noqa: ARG001
    return tuple(_visual(media_id) for media_id in pins)


def _ending_strategy(media_id: str, *, keep_audio: bool = True) -> dict:
    return {"media_id": media_id, "keep_audio": keep_audio}


# --------------------------------------------------------------------------- the check


def assert_ends_on_clip_with_sound(recipe, *, asset_id, speaker_end, clip_duration, tol=1e-3):
    """The recipe's LAST main-track clip is ``asset_id``, audible, starting where the talk
    ends, closing the video - and that asset is placed nowhere else."""
    main = next(t for t in recipe.tracks if t.id == "subtitled")
    last = max(main.clips, key=lambda c: c.timeline_start)
    assert last.source_asset_id == asset_id, (
        f"the video does not end on {asset_id}; it ends on {last.source_asset_id}"
    )
    assert last.volume >= 0.99, f"the ending clip is muted (volume={last.volume})"
    assert last.timeline_start == pytest.approx(speaker_end, abs=tol)
    assert recipe.duration == pytest.approx(speaker_end + clip_duration, abs=tol)
    assert last.timeline_start + last.source_duration / last.rate == pytest.approx(
        recipe.duration, abs=tol
    )
    placements = [
        (track.id, clip.id)
        for track in recipe.tracks
        for clip in track.clips
        if clip.source_asset_id == asset_id
    ]
    assert placements == [("subtitled", last.id)], (
        f"{asset_id} is also placed elsewhere in the talk: {placements}"
    )


def _recipe(job):
    return device_status(job, "subtitled").request.recipe


def _caption_end(recipe) -> float:
    return max(layer.end for layer in recipe.text_layers if layer.id.startswith("caption-"))


def _assets_in_tracks(recipe) -> set[str]:
    return {clip.source_asset_id for track in recipe.tracks for clip in track.clips}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # `_match_placements` only calls the Gemini agent when a key is set.
    monkeypatch.setattr(ogm.settings, "gemini_api_key", "", raising=False)
    # No real video to sample faces from.
    monkeypatch.setattr(ogm, "sample_face_regions", lambda *a, **k: ([], {"attempted": 0}))
    # Pipeline-trace persistence tries a (nonexistent) local Postgres.
    import app.services.pipeline_trace as pt

    monkeypatch.setattr(pt, "record_pipeline_event", lambda *a, **k: None)


# --------------------------------------------------------------------------- Cafe T2


def _cafe_job(
    monkeypatch,
    *,
    ending: dict | None = None,
    visual_videos: bool = True,
    beats: bool = True,
    overlay_display: str | None = "fullscreen_images",
    lane_request: dict | None = None,
    extra_beats: list | None = None,
):
    job, _snapshot, _session, _binding_ = _setup_subtitled(
        monkeypatch, transcript_words=CARO, duration_s=CAFE_TALK_S
    )
    _enable_beats(
        monkeypatch,
        extra_features=("visualVideos", "authoredText") if visual_videos else ("authoredText",),
    )
    monkeypatch.setattr(gb.settings, "phone_subtitled_video_overlays_enabled", True)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _bind)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    monkeypatch.setattr(rgm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    monkeypatch.setattr(
        rgm, "_load_sfx_entries", lambda *a, **k: [SfxEntry(id="sfx-ding", name="Ding")]
    )
    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _fake_resolve_sfx)
    if overlay_display:
        job.all_candidates["overlay_display"] = overlay_display
    strategy: dict = {"edit_format": "subtitled", "audio_strategy": "original_audio"}
    if beats:
        strategy["reaction_beats"] = [
            _beat_dict(
                "ecuador",
                "Ecuador",
                visual_id=f"asset-{COTOPAXI}",
                visual_role="photo",
            ),
            _beat_dict("delicious", "delicious", sound="sfx-ding"),
            *(extra_beats or []),
        ]
    if ending is not None:
        strategy["ending_clip"] = ending
    job.all_candidates["creator_strategy"] = strategy
    if lane_request is not None:
        job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = lane_request
    return job


def test_cafe_t2_ends_on_the_bite_with_its_own_sound_and_never_places_it_mid_talk(monkeypatch):
    job = _cafe_job(monkeypatch, ending=_ending_strategy(f"asset-{BITE}"))
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    variant = job.assembly_plan["variants"][0]
    assert_ends_on_clip_with_sound(
        recipe, asset_id=f"visual-{BITE}", speaker_end=CAFE_TALK_S, clip_duration=BITE_S
    )
    assert recipe.duration == pytest.approx(31.0, abs=1e-3)
    assert variant["duration_s"] == pytest.approx(31.0, abs=1e-3)
    # The talk is captioned, the ending is not.
    assert _caption_end(recipe) <= CAFE_TALK_S + 1e-3
    # The other grounding still works: the Cotopaxi beat and the ding keep their places.
    assert [entry["beat_id"] for entry in variant["phone_beat_receipt"]["placed"]] == [
        "ecuador",
        "delicious",
    ]
    assert f"visual-{COTOPAXI}" in _assets_in_tracks(recipe)
    assert "sfx" in {track.id for track in recipe.tracks}
    # The generic grounding never offered the ending video to the matcher.
    assert BITE not in {
        entry["media_id"]
        for entry in variant["phone_overlay_receipt"]["placed"]
        + variant["phone_overlay_receipt"]["unplaced"]
    }
    assert variant["phone_lane_receipt"]["applied"][-1] == "ending_clip"
    assert variant["phone_lane_receipt"]["dropped"] == []
    receipt = variant["phone_ending_clip"]
    assert receipt == {
        "version": 1,
        "requested_media_id": f"asset-{BITE}",
        "source": "visual",
        "status": "applied",
        "reason": None,
        "keep_audio": True,
        "has_audio": None,  # a Visuals pool row records no audio fact
        "volume": 1.0,
        "start_s": pytest.approx(CAFE_TALK_S, abs=1e-3),
        "end_s": pytest.approx(31.0, abs=1e-3),
        "recipe_duration_s": pytest.approx(31.0, abs=1e-3),
    }
    # The editor reads the bound video back from the job's pinned visuals.
    assert BITE in {row["media_id"] for row in job.assembly_plan[PHONE_VISUALS_FIELD]}
    assert {"visualVideos", "audioMix"} <= recipe.required_capabilities


def test_cafe_t2_bite_is_not_placed_mid_talk_in_any_overlay_layout(monkeypatch):
    for layout in (None, "fullscreen", "fullscreen_videos"):
        job = _cafe_job(
            monkeypatch,
            ending=_ending_strategy(f"asset-{BITE}"),
            overlay_display=layout,
            beats=False,
        )
        gb._run_generative_job(str(job.id))
        assert_ends_on_clip_with_sound(
            _recipe(job), asset_id=f"visual-{BITE}", speaker_end=CAFE_TALK_S, clip_duration=BITE_S
        )


def test_cafe_t2_without_visual_videos_verified_reports_dropped_and_places_nothing(monkeypatch):
    job = _cafe_job(monkeypatch, ending=_ending_strategy(f"asset-{BITE}"), visual_videos=False)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    variant = job.assembly_plan["variants"][0]
    assert recipe.duration == pytest.approx(CAFE_TALK_S, abs=1e-3)
    assert f"visual-{BITE}" not in _assets_in_tracks(recipe)  # not mid-talk either
    receipt = variant["phone_ending_clip"]
    assert receipt["status"] == "dropped"
    assert receipt["reason"] == "visual_videos_not_verified"
    assert receipt["source"] == "visual"
    assert receipt["volume"] == 0.0
    assert receipt["start_s"] is None and receipt["end_s"] is None
    assert receipt["recipe_duration_s"] == pytest.approx(CAFE_TALK_S, abs=1e-3)


def test_cafe_t2_without_audio_mix_verified_drops_the_ending_instead_of_failing_the_job(
    monkeypatch,
):
    job = _cafe_job(
        monkeypatch,
        ending=_ending_strategy(f"asset-{BITE}"),
        beats=False,
        overlay_display=None,
    )
    # Only the ending video is in the pool, so nothing else needs the audio mix.
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows([BITE]))
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [f for f in gb.settings.phone_render_verified_features if f != "audioMix"],
    )
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    assert recipe.duration == pytest.approx(CAFE_TALK_S, abs=1e-3)
    assert f"visual-{BITE}" not in _assets_in_tracks(recipe)
    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"]) == ("dropped", "audio_mix_not_verified")


def test_cafe_t2_unknown_ending_media_is_dropped_not_fatal(monkeypatch):
    job = _cafe_job(monkeypatch, ending=_ending_strategy("asset-does-not-exist"))
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert receipt["status"] == "dropped"
    assert receipt["reason"] == "not_found"
    assert receipt["source"] == "unknown"
    assert _recipe(job).duration == pytest.approx(CAFE_TALK_S, abs=1e-3)


def test_cafe_t2_ending_naming_the_only_footage_clip_is_dropped_as_the_speaker(monkeypatch):
    job = _cafe_job(monkeypatch, ending=_ending_strategy("c0"))
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"], receipt["source"]) == (
        "dropped",
        "is_speaker",
        "footage",
    )
    assert _recipe(job).duration == pytest.approx(CAFE_TALK_S, abs=1e-3)


def test_cafe_t2_a_beat_pointing_at_the_ending_video_never_shows_it_mid_talk(monkeypatch):
    job = _cafe_job(
        monkeypatch,
        ending=_ending_strategy(f"asset-{BITE}"),
        extra_beats=[_beat_dict("bite-beat", "Colombian", visual_id=f"asset-{BITE}")],
    )
    gb._run_generative_job(str(job.id))

    recipe = _recipe(job)
    assert_ends_on_clip_with_sound(
        recipe, asset_id=f"visual-{BITE}", speaker_end=CAFE_TALK_S, clip_duration=BITE_S
    )
    beat_receipt = job.assembly_plan["variants"][0]["phone_beat_receipt"]
    assert "bite-beat" not in [entry["beat_id"] for entry in beat_receipt["placed"]]
    assert [entry["beat_id"] for entry in beat_receipt["unplaced"]] == ["bite-beat"]


def test_cafe_t2_a_closing_shot_of_the_ending_video_is_not_held_over_the_talk(monkeypatch):
    job = _cafe_job(monkeypatch, ending=_ending_strategy(f"asset-{BITE}"))
    job.all_candidates["creator_strategy"]["closing_media"] = {"visual_id": f"asset-{BITE}"}
    gb._run_generative_job(str(job.id))

    assert_ends_on_clip_with_sound(
        _recipe(job), asset_id=f"visual-{BITE}", speaker_end=CAFE_TALK_S, clip_duration=BITE_S
    )


def test_cafe_t2_a_photo_named_as_the_ending_is_dropped_and_not_shown_mid_talk(monkeypatch):
    job = _cafe_job(monkeypatch, ending=_ending_strategy(f"asset-{OBLEA}"))
    job.all_candidates["creator_strategy"]["closing_media"] = {"visual_id": f"asset-{OBLEA}"}
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    variant = job.assembly_plan["variants"][0]
    assert recipe.duration == pytest.approx(CAFE_TALK_S, abs=1e-3)
    assert f"visual-{OBLEA}" not in _assets_in_tracks(recipe)  # not held as the closing shot either
    assert variant["phone_beat_receipt"]["closing"] == {
        "status": "unplaced",
        "reason": "visual_is_ending_clip",
        "badge": "none",
    }
    receipt = variant["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"], receipt["source"]) == (
        "dropped",
        "not_a_video",
        "visual",
    )


def test_a_hand_authored_lane_ending_clip_wins_over_the_strategy_ending(monkeypatch):
    admin_ending = {
        "media_id": BITE,
        "gcs_path": _pool_rows([BITE])[0]["gcs_path"],
        "generation": "7",
    }
    job = _cafe_job(
        monkeypatch,
        ending=_ending_strategy(f"asset-{BITE}"),
        beats=False,
        overlay_display=None,
        lane_request=_lane_request(ending_clip=admin_ending),
    )
    gb._run_generative_job(str(job.id))

    recipe = _recipe(job)
    main = next(t for t in recipe.tracks if t.id == "subtitled")
    ending = main.clips[-1]
    assert ending.id == "clip-ending" and ending.volume == 0  # the admin path stays muted
    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"]) == ("dropped", "lane_request_ending_wins")


def test_a_job_without_a_strategy_ending_is_byte_identical(monkeypatch):
    job = _cafe_job(monkeypatch, ending=None)
    gb._run_generative_job(str(job.id))

    assert "phone_ending_clip" not in job.assembly_plan["variants"][0]


# --------------------------------------------------------------------------- Don Julio T3


def _enable_talking_lanes(monkeypatch, pool=()):
    """The cards / sounds / beats lanes on a multi-clip Talking edit (``pool`` = Visuals ids)."""
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        list(dict.fromkeys([*_TALKING_HEAD_FEATURES, "stillImages"])),
    )
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _bind)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows(list(pool)))


def _julio_job(monkeypatch, *, lanes_on: bool, enabled: bool = True, ending: dict | None = None):
    job, _snapshot, _bindings = _setup_talking_multi(
        monkeypatch,
        clips=[("lucas", JULIO_TALK_S, 12), ("overhead", 2.0, 0), ("cheers", CHEERS_S, 1)],
        enabled=enabled,
    )
    import app.pipeline.transcribe as transcribe_mod

    by_clip = {"lucas": LUCAS, "overhead": [], "cheers": CHEERS}

    def _transcribe(path, *a, **k):
        clip_id = path.rsplit("/", 1)[-1].split(".")[0]
        return Transcript(words=list(by_clip[clip_id]), language="en")

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper_cached", _transcribe, raising=False)
    if lanes_on:
        _enable_talking_lanes(monkeypatch)
    if ending is not None:
        job.all_candidates["creator_strategy"] = {
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "ending_clip": ending,
        }
    return job


@pytest.mark.parametrize("lanes_on", [False, True], ids=["lanes_off", "lanes_on"])
def test_julio_t3_ends_on_the_cheers_clip_with_its_sound(monkeypatch, lanes_on):
    job = _julio_job(monkeypatch, lanes_on=lanes_on, ending=_ending_strategy("cheers"))
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    variant = job.assembly_plan["variants"][0]
    assert_ends_on_clip_with_sound(
        recipe, asset_id="cheers", speaker_end=JULIO_TALK_S, clip_duration=CHEERS_S
    )
    assert recipe.duration == pytest.approx(16.3, abs=1e-3)
    assert variant["duration_s"] == pytest.approx(16.3, abs=1e-3)
    # The overhead shot is still a muted cutaway, inside Lucas's talk.
    cutaways = next(t for t in recipe.tracks if t.id == "talking-head-cutaways").clips
    assert [c.source_asset_id for c in cutaways] == ["overhead"]
    assert cutaways[0].volume == 0
    assert cutaways[0].timeline_start + cutaways[0].source_duration <= JULIO_TALK_S + 1e-3
    # Lucas keeps his own audio on the main track.
    main = next(t for t in recipe.tracks if t.id == "subtitled")
    assert [c.source_asset_id for c in main.clips] == ["lucas", "cheers"]
    assert main.clips[0].volume == 1
    # "Cheers!" is captioned where it is said (12.9 + 0.9 s onward).
    assert any(
        "Cheers" in cue["text"] and cue["start_s"] >= JULIO_TALK_S
        for cue in variant["caption_cues"]
    )
    receipt = variant["phone_ending_clip"]
    assert receipt == {
        "version": 1,
        "requested_media_id": "cheers",
        "source": "footage",
        "status": "applied",
        "reason": None,
        "keep_audio": True,
        "has_audio": True,  # the phone's own source receipt
        "volume": 1.0,
        "start_s": pytest.approx(JULIO_TALK_S, abs=1e-3),
        "end_s": pytest.approx(16.3, abs=1e-3),
        "recipe_duration_s": pytest.approx(16.3, abs=1e-3),
    }
    multi = variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    assert multi["unplaced"] == []
    _assert_receipt_cutaways_match_recipe(multi, recipe)
    # The ending clip alone must not switch off cards / sounds / beats for the speaker.
    assert "lanes_skipped" not in multi
    # ...and the creator is not told "Played your 2 talking clips back to back".
    from app.services.nova_steps import render_notes_from_talking_multi_clip_receipt

    assert multi["ending_media_id"] == "cheers"
    assert render_notes_from_talking_multi_clip_receipt(multi) == [
        "Showed your 1 other clip full screen while you talk"
    ]
    assert ("phone_lane_receipt" in variant) is lanes_on


def test_julio_t3_with_the_flag_off_still_refuses_three_footage_clips(monkeypatch):
    job = _julio_job(monkeypatch, lanes_on=False, enabled=False, ending=_ending_strategy("cheers"))
    snapshot = job.assembly_plan
    with pytest.raises(ValueError, match="exactly one clip"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status != "awaiting_device"


def test_julio_t3_a_hand_authored_lane_ending_wins_over_a_footage_ending(monkeypatch):
    job = _julio_job(monkeypatch, lanes_on=True, ending=_ending_strategy("cheers"))
    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        ending_clip={
            "media_id": BITE,
            "gcs_path": _pool_rows([BITE])[0]["gcs_path"],
            "generation": "7",
        }
    )
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows([BITE]))
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        list(dict.fromkeys([*gb.settings.phone_render_verified_features, "visualVideos"])),
    )
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    main = next(t for t in recipe.tracks if t.id == "subtitled")
    assert [c.source_asset_id for c in main.clips] == ["lucas", f"visual-{BITE}"]
    assert main.clips[-1].volume == 0  # the admin ending stays muted
    assert "cheers" not in _assets_in_tracks(recipe)
    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"]) == ("dropped", "lane_request_ending_wins")


def test_julio_t3_ending_naming_the_speaker_is_dropped_and_the_edit_is_unchanged(monkeypatch):
    job = _julio_job(monkeypatch, lanes_on=False, ending=_ending_strategy("lucas"))
    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    recipe = _recipe(job)
    assert recipe.duration == pytest.approx(JULIO_TALK_S, abs=1e-3)
    assert (variant["phone_ending_clip"]["status"], variant["phone_ending_clip"]["reason"]) == (
        "dropped",
        "is_speaker",
    )


def test_julio_t3_a_muted_footage_ending_is_not_supported_and_says_so(monkeypatch):
    job = _julio_job(
        monkeypatch, lanes_on=False, ending=_ending_strategy("cheers", keep_audio=False)
    )
    gb._run_generative_job(str(job.id))

    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert receipt["status"] == "dropped"
    assert receipt["reason"] == "muted_footage_ending_unsupported"
    recipe = _recipe(job)
    assert recipe.duration == pytest.approx(JULIO_TALK_S, abs=1e-3)
    # Not at the end, so not used at all: never a muted cutaway over Lucas.
    assert "cheers" not in _assets_in_tracks(recipe)


def test_a_talking_clip_named_as_the_ending_moves_to_the_end_of_the_main_track(monkeypatch):
    job, _snapshot, _bindings = _setup_talking_multi(
        monkeypatch,
        clips=[("take1", 20.0, 40), ("take2", 15.0, 30), ("take3", 10.0, 20)],
    )
    _enable_talking_lanes(monkeypatch)
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "ending_clip": _ending_strategy("take2"),
    }
    gb._run_generative_job(str(job.id))

    recipe = _recipe(job)
    assert_ends_on_clip_with_sound(recipe, asset_id="take2", speaker_end=30.0, clip_duration=15.0)
    main = next(t for t in recipe.tracks if t.id == "subtitled")
    assert [c.source_asset_id for c in main.clips] == ["take1", "take3", "take2"]
    multi = job.assembly_plan["variants"][0][gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    # take3 is a real second speaker, so the card / sound / beat lanes stay off as before.
    assert multi["lanes_skipped"] == "multiple_speakers"
    assert job.assembly_plan["variants"][0]["phone_ending_clip"]["status"] == "applied"


def test_a_visuals_ending_survives_several_speakers_and_cards_stay_off(monkeypatch):
    """Two real speakers turn the cards / sounds / beats off (they are grounded on ONE speaker's
    timeline), but a Visuals ending plays after ALL of them and needs none of that."""
    job, _snapshot, _bindings = _setup_talking_multi(
        monkeypatch, clips=[("take1", 12.0, 24), ("take2", 8.0, 16)]
    )
    _enable_talking_lanes(monkeypatch, pool=[COTOPAXI, OBLEA, BITE])
    job.all_candidates["overlay_display"] = "fullscreen"
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "ending_clip": _ending_strategy(f"asset-{BITE}"),
    }
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    variant = job.assembly_plan["variants"][0]
    assert_ends_on_clip_with_sound(
        recipe, asset_id=f"visual-{BITE}", speaker_end=20.0, clip_duration=BITE_S
    )
    main = next(t for t in recipe.tracks if t.id == "subtitled")
    assert [c.source_asset_id for c in main.clips] == ["take1", "take2", f"visual-{BITE}"]
    # No card, full-screen insert or sound was grounded on the first speaker's timeline.
    assert _assets_in_tracks(recipe) == {"take1", "take2", f"visual-{BITE}"}
    assert {track.id for track in recipe.tracks} == {"subtitled"}
    # ...and the receipt still says so.
    assert variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]["lanes_skipped"] == "multiple_speakers"
    for absent in ("phone_overlay_receipt", "phone_beat_receipt", "overlay_transcript"):
        assert absent not in variant
    assert variant["phone_lane_receipt"] == {"applied": ["ending_clip"], "dropped": []}
    assert {row["media_id"] for row in job.assembly_plan[PHONE_VISUALS_FIELD]} == {BITE}
    receipt = variant["phone_ending_clip"]
    assert (receipt["status"], receipt["source"], receipt["volume"]) == ("applied", "visual", 1.0)
    assert receipt["recipe_duration_s"] == pytest.approx(26.4, abs=1e-3)


def test_several_speakers_ignore_a_hand_authored_lane_request_as_before(monkeypatch):
    job, _snapshot, _bindings = _setup_talking_multi(
        monkeypatch, clips=[("take1", 12.0, 24), ("take2", 8.0, 16)]
    )
    _enable_talking_lanes(monkeypatch, pool=[COTOPAXI, BITE])
    job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD] = _lane_request(
        overlays=[
            {
                "id": "card1",
                "media_id": COTOPAXI,
                "gcs_path": _pool_rows([COTOPAXI])[0]["gcs_path"],
                "generation": "7",
                "start_s": 1.0,
                "end_s": 3.0,
            }
        ]
    )
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "ending_clip": _ending_strategy(f"asset-{BITE}"),
    }
    gb._run_generative_job(str(job.id))

    recipe = _recipe(job)
    assert_ends_on_clip_with_sound(
        recipe, asset_id=f"visual-{BITE}", speaker_end=20.0, clip_duration=BITE_S
    )
    assert f"visual-{COTOPAXI}" not in _assets_in_tracks(recipe)  # cards stay off, as before


def test_several_speakers_with_the_lanes_kill_switch_off_drop_the_visuals_ending(monkeypatch):
    job, _snapshot, _bindings = _setup_talking_multi(
        monkeypatch, clips=[("take1", 12.0, 24), ("take2", 8.0, 16)]
    )
    _enable_talking_lanes(monkeypatch, pool=[BITE])
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", False)
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "ending_clip": _ending_strategy(f"asset-{BITE}"),
    }
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"]) == ("dropped", "media_lanes_disabled")
    assert f"visual-{BITE}" not in _assets_in_tracks(_recipe(job))


def _length_authority(text: str, seconds: float, *, policy_mode: str | None = None) -> dict:
    """The approved-length fields the device pin enforces, as dispatch stamps them."""
    brief = CreativeBrief(
        requirements=[
            BriefRequirement(
                id="length",
                kind="timing",
                scope="global",
                description=text,
                facts={"duration_s": seconds},
            )
        ]
    )
    contract = build_render_contract(
        {"edit_format": "subtitled", "audio_strategy": "original_audio"},
        brief=brief,
        generation_id="generation",
    )
    authority = {
        CONTRACT_FIELD: contract.model_dump(mode="json"),
        "creator_brief_binding": {"brief": brief.model_dump(mode="json")},
    }
    if policy_mode is not None:
        # The length-policy state the dispatcher stamps: its checks emit fleet observations.
        authority = stamp_length_policy(
            authority,
            {"edit_format": "subtitled", "audio_strategy": "original_audio"},
            contract,
            mode=policy_mode,
        )
    return authority


# Cafe: 24.6 s talk + 6.4 s bite = 31.0 s (+ 1.6 s brand ending on the phone).
@pytest.mark.parametrize(
    ("text", "seconds"),
    [("Keep it under 30 seconds", 30), ("Make it 25 seconds long", 25)],
    ids=["under_30_s", "25_s"],
)
def test_an_ending_that_would_break_the_stated_length_is_dropped_not_the_whole_render(
    monkeypatch, text, seconds
):
    job = _cafe_job(monkeypatch, ending=_ending_strategy(f"asset-{BITE}"))
    job.assembly_plan.update(_length_authority(text, seconds))
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device", job.error_detail  # not declined at the pin
    recipe = _recipe(job)
    assert recipe.duration == pytest.approx(CAFE_TALK_S, abs=1e-3)
    assert f"visual-{BITE}" not in _assets_in_tracks(recipe)
    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"]) == ("dropped", "over_length_limit")
    assert receipt["volume"] == 0.0 and receipt["recipe_duration_s"] == pytest.approx(CAFE_TALK_S)


@pytest.fixture
def length_observations(monkeypatch) -> list[tuple]:
    """Every fleet length-check observation emitted (it feeds the rollout / promotion proof)."""
    seen: list[tuple] = []
    monkeypatch.setattr(
        length_policy_state,
        "emit_length_check_observation",
        lambda *, check_mode, outcome: seen.append((check_mode, outcome)),
    )
    return seen


def test_the_workers_what_if_length_probe_emits_no_fleet_observation(
    monkeypatch, length_observations
):
    """Only the real pin counts a creator-length check; the probe that decides whether to drop
    the ending asks the same question twice and must not be mistaken for a creator decline."""
    job = _cafe_job(monkeypatch, ending=_ending_strategy(f"asset-{BITE}"))
    job.assembly_plan.update(
        _length_authority("Make it 25 seconds long", 25, policy_mode="enforce")
    )
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device", job.error_detail
    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"]) == ("dropped", "over_length_limit")
    # ...and exactly one observation: the pin's check of the recipe that was actually pinned.
    assert length_observations == [("enforce", "passed")]


def test_the_pin_still_emits_exactly_one_observation_per_length_check(length_observations):
    authority = _length_authority("Make it 25 seconds long", 25, policy_mode="enforce")
    contract = read_render_contract(authority)
    policy = read_length_policy(authority, contract)
    recipe = compile_phone_subtitled_plan(
        (_binding("c0", duration_s=CAFE_TALK_S),), caption_cues=[]
    )
    verify_phone_recipe(contract, recipe, length_policy=policy, source_audio={"c0": True})
    assert length_observations == [("enforce", "passed")]

    length_observations.clear()
    with pytest.raises(Exception, match="25 seconds"):
        check_recipe_duration(contract, 31.0, length_policy=policy, emit_observation=False)
    assert length_observations == []  # a probe: same verdict, no observation

    with pytest.raises(Exception, match="25 seconds"):
        check_recipe_duration(contract, 31.0, length_policy=policy)
    assert length_observations == [("enforce", "declined")]  # the default still emits


def test_the_length_check_blames_the_ending_only_when_the_edit_alone_would_pass():
    authority = _length_authority("Keep it under 30 seconds", 30)
    assert gb._ending_over_length_limit(authority, talk_s=24.6, ending_s=6.4) is True
    assert gb._ending_over_length_limit(authority, talk_s=24.6, ending_s=1.0) is False  # fits
    # Too long without the ending as well (29.0 + the 1.6 s brand ending): not its doing.
    assert gb._ending_over_length_limit(authority, talk_s=29.0, ending_s=6.4) is False
    assert gb._ending_over_length_limit({}, talk_s=24.6, ending_s=60.0) is False  # no contract


def test_a_roomy_stated_length_still_ends_on_the_bite(monkeypatch):
    job = _cafe_job(monkeypatch, ending=_ending_strategy(f"asset-{BITE}"))
    job.assembly_plan.update(_length_authority("Keep it under 60 seconds", 60))
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device", job.error_detail
    assert_ends_on_clip_with_sound(
        _recipe(job), asset_id=f"visual-{BITE}", speaker_end=CAFE_TALK_S, clip_duration=BITE_S
    )
    assert job.assembly_plan["variants"][0]["phone_ending_clip"]["status"] == "applied"


# Don Julio: 12.9 s talk + 3.4 s cheers = 16.3 s (+ 1.6 s).
@pytest.mark.parametrize("lanes_on", [False, True], ids=["lanes_off", "lanes_on"])
def test_a_footage_ending_that_would_break_the_stated_length_is_dropped_too(monkeypatch, lanes_on):
    job = _julio_job(monkeypatch, lanes_on=lanes_on, ending=_ending_strategy("cheers"))
    job.assembly_plan.update(_length_authority("Keep it under 15 seconds", 15))
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device", job.error_detail
    recipe = _recipe(job)
    assert recipe.duration == pytest.approx(JULIO_TALK_S, abs=1e-3)
    assert "cheers" not in _assets_in_tracks(recipe)  # not a muted cutaway over Lucas either
    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"]) == ("dropped", "over_length_limit")


def test_a_roomy_stated_length_still_ends_on_the_cheers_clip(monkeypatch):
    job = _julio_job(monkeypatch, lanes_on=True, ending=_ending_strategy("cheers"))
    job.assembly_plan.update(_length_authority("Keep it under 40 seconds", 40))
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device", job.error_detail
    assert_ends_on_clip_with_sound(
        _recipe(job), asset_id="cheers", speaker_end=JULIO_TALK_S, clip_duration=CHEERS_S
    )


def test_a_talkative_ending_uploaded_first_still_ends_the_video_and_lucas_speaks(monkeypatch):
    """The ending clip is classified a speaker (12 words) AND uploaded before the real speaker."""
    job, _snapshot, _bindings = _setup_talking_multi(
        monkeypatch, clips=[("cheers", CHEERS_S, 12), ("lucas", JULIO_TALK_S, 30)]
    )
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "ending_clip": _ending_strategy("cheers"),
    }
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    assert_ends_on_clip_with_sound(
        recipe, asset_id="cheers", speaker_end=JULIO_TALK_S, clip_duration=CHEERS_S
    )
    main = next(t for t in recipe.tracks if t.id == "subtitled")
    assert [c.source_asset_id for c in main.clips] == ["lucas", "cheers"]
    multi = job.assembly_plan["variants"][0][gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    assert multi["speaker_media_ids"] == ["lucas", "cheers"]
    assert job.assembly_plan["variants"][0]["phone_ending_clip"]["status"] == "applied"


def test_the_ending_that_is_the_only_speaker_is_still_dropped_as_the_speaker(monkeypatch):
    job, _snapshot, _bindings = _setup_talking_multi(
        monkeypatch, clips=[("cheers", CHEERS_S, 12), ("lucas", JULIO_TALK_S, 1)]
    )
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "ending_clip": _ending_strategy("cheers"),
    }
    gb._run_generative_job(str(job.id))

    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"]) == ("dropped", "is_speaker")


def test_the_receipt_says_whether_the_ending_source_has_an_audio_track(monkeypatch):
    # Footage: known from the phone's own source receipt. Appended either way: it is the
    # brief check that tells the creator a silent clip has no sound of its own.
    job = _julio_job(monkeypatch, lanes_on=False, ending=_ending_strategy("cheers"))
    for row in job.assembly_plan[PHONE_SOURCES_FIELD]:
        if row["media_id"] == "cheers":
            row["original"]["has_audio"] = False
    gb._run_generative_job(str(job.id))
    assert_ends_on_clip_with_sound(
        _recipe(job), asset_id="cheers", speaker_end=JULIO_TALK_S, clip_duration=CHEERS_S
    )
    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["has_audio"]) == ("applied", False)

    audible = _julio_job(monkeypatch, lanes_on=False, ending=_ending_strategy("cheers"))
    gb._run_generative_job(str(audible.id))
    assert audible.assembly_plan["variants"][0]["phone_ending_clip"]["has_audio"] is True

    # A Visuals pool row records no audio fact (the bound receipt doesn't either): unknown.
    cafe = _cafe_job(monkeypatch, ending=_ending_strategy(f"asset-{BITE}"))
    gb._run_generative_job(str(cafe.id))
    assert cafe.assembly_plan["variants"][0]["phone_ending_clip"]["has_audio"] is None


def test_footage_cutaways_never_cover_the_ending_clip(monkeypatch):
    """Without the ending reserved, three 2 s cutaways spread over 12 s would land on it."""
    job, _snapshot, _bindings = _setup_talking_multi(
        monkeypatch,
        clips=[("take", 6.0, 12), ("a", 2.0, 0), ("b", 2.0, 0), ("c", 2.0, 0), ("end", 6.0, 1)],
    )
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "ending_clip": _ending_strategy("end"),
    }
    gb._run_generative_job(str(job.id))

    recipe = _recipe(job)
    assert_ends_on_clip_with_sound(recipe, asset_id="end", speaker_end=6.0, clip_duration=6.0)
    cutaways = next(t for t in recipe.tracks if t.id == "talking-head-cutaways").clips
    assert cutaways, "some footage should still cover the talk"
    for clip in cutaways:
        assert clip.timeline_start + clip.source_duration <= 6.0 + 1e-3
    multi = job.assembly_plan["variants"][0][gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    _assert_receipt_cutaways_match_recipe(multi, recipe)
    # Whatever found no room is reported, not dropped silently.
    placed = {row["media_id"] for row in multi["cutaways"]}
    assert placed | {row["media_id"] for row in multi["unplaced"]} == {"a", "b", "c"}


# --------------------------------------------------------------------------- negative controls


def _compile(lanes=None, *, cutaways=(), visuals=()):
    return compile_phone_subtitled_plan(
        (_binding("c0", duration_s=CAFE_TALK_S),),
        caption_cues=[],
        visuals=visuals,
        lanes=lanes,
        cutaways=cutaways,
    )


def test_the_check_rejects_the_recipe_main_builds_today_for_cafe_t2():
    """Today the bite is a muted picture-in-picture card mid-talk and nothing is appended."""
    bite = _visual(BITE)
    card = SubtitledOverlayCard(
        id="pip-1",
        media_id=BITE,
        gcs_path=bite.gcs_path,
        generation="7",
        start_s=16.19,
        end_s=20.19,
        kind="video",
    )
    recipe = _compile(PhoneSubtitledLanes(overlays=[card]), visuals=(bite,))
    with pytest.raises(AssertionError, match="does not end on|elsewhere"):
        assert_ends_on_clip_with_sound(
            recipe, asset_id=f"visual-{BITE}", speaker_end=CAFE_TALK_S, clip_duration=BITE_S
        )


def test_the_check_rejects_a_bite_appended_with_its_sound_off():
    """The admin ending-clip lane appends the video but muted."""
    bite = _visual(BITE)
    muted = SubtitledEndingClip(media_id=BITE, gcs_path=bite.gcs_path, generation="7")
    recipe = _compile(PhoneSubtitledLanes(ending_clip=muted), visuals=(bite,))
    with pytest.raises(AssertionError, match="muted"):
        assert_ends_on_clip_with_sound(
            recipe, asset_id=f"visual-{BITE}", speaker_end=CAFE_TALK_S, clip_duration=BITE_S
        )


def test_the_check_rejects_the_cheers_clip_as_a_mid_talk_cutaway():
    """Today (Don Julio): the cheers clip is a muted cutaway inside Lucas's talk."""
    cheers = _binding("cheers", duration_s=CHEERS_S)
    recipe = compile_phone_subtitled_plan(
        (_binding("lucas", duration_s=JULIO_TALK_S),),
        caption_cues=[],
        cutaways=(PhoneCutaway(binding=cheers, start_s=7.9, end_s=11.3),),
    )
    with pytest.raises(AssertionError, match="does not end on"):
        assert_ends_on_clip_with_sound(
            recipe, asset_id="cheers", speaker_end=JULIO_TALK_S, clip_duration=CHEERS_S
        )


# --------------------------------------------------------------------------- editor round trip


def test_a_default_ending_clip_stays_muted_and_serializes_as_before():
    ending = SubtitledEndingClip(media_id=BITE, gcs_path="p", generation="7")
    assert ending.volume == 0
    assert "volume" not in ending.model_dump(mode="json")  # persisted lane rows are unchanged
    audible = SubtitledEndingClip(media_id=BITE, gcs_path="p", generation="7", volume=1.0)
    assert audible.model_dump(mode="json")["volume"] == 1.0


def test_lanes_from_recipe_reads_the_ending_volume_back():
    bite = _visual(BITE)
    audible = SubtitledEndingClip(media_id=BITE, gcs_path=bite.gcs_path, generation="7", volume=1.0)
    recipe = _compile(PhoneSubtitledLanes(ending_clip=audible), visuals=(bite,))
    rebuilt = lanes_from_recipe(recipe, visuals=(bite,))
    assert rebuilt.ending_clip is not None and rebuilt.ending_clip.volume == 1.0
    again = _compile(rebuilt, visuals=(bite,))
    assert_ends_on_clip_with_sound(
        again, asset_id=f"visual-{BITE}", speaker_end=CAFE_TALK_S, clip_duration=BITE_S
    )


def test_an_editor_save_keeps_the_ending_sound_for_a_visual_ending(monkeypatch):
    job = _cafe_job(monkeypatch, ending=_ending_strategy(f"asset-{BITE}"))
    gb._run_generative_job(str(job.id))
    revision = device_status(job, "subtitled").request.identity.recipe_revision

    _enable_subtitled_editor(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(variant),
            caption_cues=list(variant["caption_cues"]),
        ),
        user_id="owner",
        plan_item_id="item",
    )

    after = _recipe(job)
    assert device_status(job, "subtitled").request.identity.recipe_revision == revision + 1
    assert_ends_on_clip_with_sound(
        after, asset_id=f"visual-{BITE}", speaker_end=CAFE_TALK_S, clip_duration=BITE_S
    )


def test_an_editor_save_keeps_the_ending_sound_for_a_footage_ending(monkeypatch):
    job = _julio_job(monkeypatch, lanes_on=False, ending=_ending_strategy("cheers"))
    gb._run_generative_job(str(job.id))

    _enable_subtitled_editor(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(variant),
            caption_cues=list(variant["caption_cues"]),
        ),
        user_id="owner",
        plan_item_id="item",
    )

    assert_ends_on_clip_with_sound(
        _recipe(job), asset_id="cheers", speaker_end=JULIO_TALK_S, clip_duration=CHEERS_S
    )


# --------------------------------------------------------------------------- the chain
#
# Nothing hand-written between the halves: the model's `ending_clip` goes through the server
# repair and the runtime's strategy check (implementer A), the repaired strategy goes into the
# real phone worker (above), and the variant the worker wrote goes through the brief receipts
# the creator reads - at the draft (before approval) and at render-ready.

_CHAIN_USER = uuid.uuid4()
_CHAIN_CAFE_PATHS = ("users/u/creation-threads/t/analysis-proxy-ios-A.mp4",)
_CHAIN_JULIO_PATHS = tuple(
    f"users/u/creation-threads/t/analysis-proxy-ios-{name}.mp4" for name in "ABC"
)
CAFE_ASK_TR = "En sona Atakan'ın ilk ısırık videosunu kendi sesiyle ekle"
JULIO_ASK_EN = "At the very end, add the cheers video with its own sound"


def _ending_ask(text: str, video: str, *, keep_audio: bool = True) -> BriefRequirement:
    return BriefRequirement(
        id="r4",
        kind="style",
        scope="global",
        description=text,
        facts={"ending_clip": video, "keep_audio": keep_audio},
    )


def _apply_world(
    monkeypatch, *, visual_videos=True, audio_mix=True, lanes=True, multi_clip=True
) -> None:
    """The settings BOTH halves read: production's, plus the four knobs the ask depends on.

    Applied after a worker harness is built, so one source of truth decides the draft check and
    the worker (a flag that disagrees between them is exactly the gap being tested)."""
    apply_prod_profile(monkeypatch)
    features = [
        feature
        for feature in PROD_VERIFIED_FEATURES
        if (visual_videos or feature != "visualVideos") and (audio_mix or feature != "audioMix")
    ]
    monkeypatch.setattr(settings, "phone_render_verified_features", features)
    monkeypatch.setattr(settings, "phone_subtitled_media_lanes_enabled", lanes)
    monkeypatch.setattr(settings, "talking_multi_clip_enabled", multi_clip)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    # Worker-only: no network (caption spelling pass) and the video-card gate the harness needs.
    monkeypatch.setattr(settings, "subtitled_caption_correction_enabled", False)
    monkeypatch.setattr(settings, "phone_subtitled_video_overlays_enabled", True)


def _chain_manifest(media: list[dict]):
    own = [m["media_id"] for m in media if not m["media_id"].startswith("asset-")]
    return capabilities.resolve_creator_manifest(
        item_id="item-841",
        edit_format="subtitled",
        media=media,
        phone_source_media_ids=own,
        phone_rendering_allowed=True,
    )


def _chain_strategy(manifest, **extra) -> CreativeStrategy:
    own = [m.media_id for m in manifest.media if not m.media_id.startswith("asset-")]
    return CreativeStrategy.model_validate(
        {
            "direction": "fast_montage",
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "render_program": "native",
            "target_duration_s": 24,
            "selected_media_ids": own,
            "rationale": "Keep the take whole and add what was asked.",
            **extra,
        }
    )


def _repaired(manifest, strategy: CreativeStrategy) -> dict:
    """What the runtime dispatches for the draft: the strategy after the server repair."""
    checked = check_strategy_for_runtime_v2(manifest, strategy)
    assert isinstance(checked, CheckedStrategy), getattr(checked, "question", checked)
    return checked.strategy.model_dump(mode="json", exclude_none=True)


def _draft_receipt(ask: BriefRequirement, strategy: dict, manifest, clip_paths, language="en"):
    """The receipt the creator's first draft reply is built from (`tasks/kria_runtime.py`)."""
    kept = requirements_to_check_at_draft(
        [ask],
        creator_id=_CHAIN_USER,
        strategy=strategy,
        item_edit_format="subtitled",
        clip_paths=clip_paths,
    )
    facts = plan_facts_from_strategy(strategy, manifest=manifest)
    facts = dataclasses.replace(
        facts,
        phone_talking=defers_caption_words_to_phone_render(
            creator_id=_CHAIN_USER,
            edit_format="subtitled",
            audio_strategy=strategy.get("audio_strategy"),
            clip_paths=clip_paths,
        ),
        caption_words_at_render=True,
    )
    receipts = build_receipts(kept, facts, include_unchecked=True, strict_order=True)
    brief = CreativeBrief(version=1, requirements=kept)
    with reply_language_for(language):
        reply = reply_from_receipts(brief, receipts, summary="Here's your draft.")
    return {r.requirement_id: r for r in receipts}["r4"], reply


def _render_ready_receipt(ask: BriefRequirement, variant: dict, language="en"):
    """The receipt the creator's render-ready reply is built from, off the worker's own variant."""
    facts = plan_facts_from_phone_variant(variant)
    brief = CreativeBrief(version=1, requirements=[ask])
    receipts = build_receipts(brief.live(), facts, include_unchecked=True)
    with reply_language_for(language):
        reply = reply_from_receipts(brief, receipts, summary="Your video is ready.")
    return {r.requirement_id: r for r in receipts}["r4"], reply


def _cafe_media(*, full: bool) -> list[dict]:
    media = [{"media_id": "c0", "kind": "video", "duration_s": CAFE_TALK_S}]
    if full:
        media += [
            {"media_id": f"asset-{COTOPAXI}", "kind": "image", "label": "T2_02_visual.jpg"},
            {"media_id": f"asset-{OBLEA}", "kind": "image", "label": "T2_03_visual.jpg"},
        ]
    media.append(
        {
            "media_id": f"asset-{BITE}",
            "kind": "video",
            "duration_s": BITE_S,
            "label": "T2_04_visual.mp4",
        }
    )
    return media


def _julio_media() -> list[dict]:
    return [
        {"media_id": "lucas", "kind": "video", "duration_s": JULIO_TALK_S},
        {"media_id": "overhead", "kind": "video", "duration_s": 2.0},
        {"media_id": "cheers", "kind": "video", "duration_s": CHEERS_S},
    ]


def test_chain_cafe_t2_the_models_ending_reaches_the_worker_and_both_replies_say_done(monkeypatch):
    job = _cafe_job(monkeypatch, ending=None, beats=False, overlay_display="fullscreen_images")
    _apply_world(monkeypatch)
    manifest = _chain_manifest(_cafe_media(full=True))
    # The model names the video by its label; the repair writes the manifest's canonical id.
    model = _chain_strategy(
        manifest,
        reaction_beats=[
            _beat_dict("ecuador", "Ecuador", visual_id=f"asset-{COTOPAXI}", visual_role="photo")
        ],
        ending_clip={"media_id": "T2_04_visual.mp4"},
    )
    dispatched = _repaired(manifest, model)
    assert dispatched["ending_clip"] == {"media_id": f"asset-{BITE}", "keep_audio": True}
    job.all_candidates["creator_strategy"] = dispatched
    ask = _ending_ask(CAFE_ASK_TR, "Atakan'ın ilk ısırık videosu")

    draft, _reply = _draft_receipt(ask, dispatched, manifest, _CHAIN_CAFE_PATHS, "tr")
    assert draft.status == "met", draft.reason
    assert not needs_creator_choice(draft)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    assert_ends_on_clip_with_sound(
        recipe, asset_id=f"visual-{BITE}", speaker_end=CAFE_TALK_S, clip_duration=BITE_S
    )
    variant = job.assembly_plan["variants"][0]
    assert variant["phone_ending_clip"]["status"] == "applied"
    ready, reply = _render_ready_receipt(ask, variant, "tr")
    assert ready.status == "met", ready.reason
    assert ready.verification == "checked"
    assert "Yapamadım" not in reply and "Couldn't" not in reply


def test_chain_don_julio_t3_the_models_ending_reaches_the_worker_and_both_replies_say_done(
    monkeypatch,
):
    job = _julio_job(monkeypatch, lanes_on=True, ending=None)
    _apply_world(monkeypatch)
    manifest = _chain_manifest(_julio_media())
    model = _chain_strategy(manifest, ending_clip={"media_id": "cheers"})
    dispatched = _repaired(manifest, model)
    assert dispatched["ending_clip"]["media_id"] == "cheers"
    job.all_candidates["creator_strategy"] = dispatched
    ask = _ending_ask(JULIO_ASK_EN, "the cheers video")

    draft, _reply = _draft_receipt(ask, dispatched, manifest, _CHAIN_JULIO_PATHS)
    assert draft.status == "met", draft.reason
    assert not needs_creator_choice(draft)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    assert_ends_on_clip_with_sound(
        _recipe(job), asset_id="cheers", speaker_end=JULIO_TALK_S, clip_duration=CHEERS_S
    )
    variant = job.assembly_plan["variants"][0]
    assert "lanes_skipped" not in variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    ready, reply = _render_ready_receipt(ask, variant)
    assert ready.status == "met", ready.reason
    assert ready.verification == "checked"
    assert "Couldn't" not in reply


# Whenever the phone worker would drop the ending (it says so on `phone_ending_clip`), the
# creator must already have heard "Couldn't" at the DRAFT, before approving.
_CHAIN_CASES = {
    "visual_videos_not_verified": ({"visual_videos": False}, "visual_videos_not_verified"),
    "audio_mix_not_verified": ({"audio_mix": False}, "audio_mix_not_verified"),
    "media_lanes_kill_switch_off": ({"lanes": False}, "media_lanes_disabled"),
}


@pytest.mark.parametrize("case", sorted(_CHAIN_CASES))
def test_chain_a_visuals_ending_the_worker_would_drop_is_a_couldnt_at_the_draft(monkeypatch, case):
    knobs, worker_reason = _CHAIN_CASES[case]
    job = _cafe_job(monkeypatch, ending=None, beats=False, overlay_display=None)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows([BITE]))
    _apply_world(monkeypatch, **knobs)
    manifest = _chain_manifest(_cafe_media(full=False))
    # The strategy exactly as the model wrote it, canonical id (what a kept repair dispatches):
    # the worker is fed THIS, so a flag the draft did not honour still reaches it.
    model = _chain_strategy(manifest, ending_clip={"media_id": f"asset-{BITE}"})
    job.all_candidates["creator_strategy"] = model.model_dump(mode="json", exclude_none=True)
    ask = _ending_ask(CAFE_ASK_TR, "Atakan'ın ilk ısırık videosu")

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"]) == ("dropped", worker_reason)
    ready, _ = _render_ready_receipt(ask, job.assembly_plan["variants"][0], "tr")
    assert ready.status == "not_possible"  # and the render-ready reply says so too

    # The draft the creator approves: the strategy after the server repair.
    dispatched = _repaired(manifest, model)
    draft, reply = _draft_receipt(ask, dispatched, manifest, _CHAIN_CAFE_PATHS, "tr")
    assert draft.status == "not_possible", (case, draft.reason)
    assert needs_creator_choice(draft)  # held back: "a simpler version?"
    assert f"Yapamadım: {ask.description}" in reply


def test_chain_control_a_draft_made_while_the_phone_could_play_it_says_done_then_the_worker_drops(
    monkeypatch,
):
    """Negative control for the consistency check above: if the draft did not honour the gate,
    the creator would hear "Done" and the worker would still drop the ending."""
    job = _cafe_job(monkeypatch, ending=None, beats=False, overlay_display=None)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows([BITE]))
    _apply_world(monkeypatch)
    manifest = _chain_manifest(_cafe_media(full=False))
    model = _chain_strategy(manifest, ending_clip={"media_id": f"asset-{BITE}"})
    ask = _ending_ask(CAFE_ASK_TR, "Atakan'ın ilk ısırık videosu")
    healthy, _ = _draft_receipt(ask, _repaired(manifest, model), manifest, _CHAIN_CAFE_PATHS, "tr")
    assert healthy.status == "met"

    _apply_world(monkeypatch, audio_mix=False)  # the verified feature set shrinks before the render
    job.all_candidates["creator_strategy"] = model.model_dump(mode="json", exclude_none=True)
    gb._run_generative_job(str(job.id))

    receipt = job.assembly_plan["variants"][0]["phone_ending_clip"]
    assert (receipt["status"], receipt["reason"]) == ("dropped", "audio_mix_not_verified")
    redrafted, _ = _draft_receipt(
        ask, _repaired(manifest, model), manifest, _CHAIN_CAFE_PATHS, "tr"
    )
    assert redrafted.status == "not_possible"  # the same draft, built under the render's settings


def test_chain_don_julio_with_multi_clip_off_is_refused_before_the_worker_could_drop_it(
    monkeypatch,
):
    job, _snapshot, _bindings = _setup_talking_multi(
        monkeypatch,
        clips=[("lucas", JULIO_TALK_S, 12), ("overhead", 2.0, 0), ("cheers", CHEERS_S, 1)],
        enabled=False,
    )
    _apply_world(monkeypatch, multi_clip=False)
    manifest = _chain_manifest(_julio_media())
    model = _chain_strategy(manifest, ending_clip={"media_id": "cheers"})
    job.all_candidates["creator_strategy"] = model.model_dump(mode="json", exclude_none=True)

    # The worker refuses the 2nd/3rd footage clip outright (nothing is rendered)...
    with pytest.raises(ValueError, match="exactly one clip"):
        gb._run_phone_subtitled_job(
            str(job.id), job.assembly_plan, job.all_candidates, ownership_epoch=3
        )
    # ...and the creator hears it before any draft exists: the runtime asks, it does not draft.
    checked = check_strategy_for_runtime_v2(manifest, model)
    assert isinstance(checked, RefusedStrategy)
    assert "?" in checked.question
