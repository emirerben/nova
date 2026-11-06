"""KRI-1151: the real Talking worker plays a footage cutaway's own sound when the creator asked.

Drives `_run_generative_job` -> `_run_phone_subtitled_job` -> footage scheduler ->
`compile_phone_subtitled_plan` -> recipe validation on one speaker take plus one footage clip
(the thread 6fa36e51 shape: a stadium clip over the creator's talk). Only transport is
replaced; the brief binding is the thread's own brief with "stadium: sound on".

1. Flag off: the footage is a silent cutaway exactly as before, and no receipt row claims more.
2. Flag on + an "on" ask: the footage's file plays on an audio track, the speaker is muted
   under it, the variant records the row, and the finished-render check reads "met".
3. Flag on + an "off" ask for that clip: it stays silent and the row says met.
4. No ask: no sound lane and no record.
"""

from __future__ import annotations

import uuid

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import PlanFacts, check_requirement
from app.services.creator_render_contract import CONTRACT_FIELD, build_render_contract
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_talking_multi

_FEATURES = [
    "basicComposition",
    "local1080Export",
    "positionedText",
    "animatedText",
    "narrationAudio",
    "audioMix",
    "visualBlocks",
    "visualVideos",
    "alphaOverlay",
]
STADIUM = "street"


def _req(state: str, rid: str = "r2") -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="audio",
        scope=f"clip:{STADIUM}",
        status="open",
        description="Use the audio of the stadium clip",
        facts={"clip_sound": state},
        source_turn_id="t1",
    )


def _run(
    monkeypatch,
    *,
    flag: bool,
    state: str | None,
    windows: bool | None = None,
    audio_source_ids: tuple[str, ...] = (),
):
    job, snapshot, _bindings = _setup_talking_multi(
        monkeypatch, clips=[("take", 30.0, 60), (STADIUM, 6.0, 0)]
    )
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", _FEATURES)
    monkeypatch.setattr(gb.settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(
        gb.settings, "phone_clip_sound_windows_enabled", flag if windows is None else windows
    )
    monkeypatch.setattr(gb.settings, "phone_talking_cutaway_sound_enabled", flag)
    if audio_source_ids:
        strategy = {
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "montage_audio": {
                "preserve_source_audio": True,
                "source_media_ids": list(audio_source_ids),
            },
        }
        contract = build_render_contract(strategy, brief=None, generation_id="generation")
        job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    if state is not None:
        brief = CreativeBrief(version=1, requirements=[_req(state)])
        binding = BriefBinding.create(uuid.uuid4(), brief).model_dump(mode="json")
        job.assembly_plan["creator_brief_binding"] = binding
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    recipe = device_status(job, "subtitled").request.recipe
    return job, recipe, job.assembly_plan["variants"][0]


def _sound_track(recipe):
    return next((t for t in recipe.tracks if t.id == "talking-head-cutaway-audio"), None)


def test_flag_off_the_footage_stays_a_silent_cutaway(monkeypatch):
    _job, recipe, variant = _run(monkeypatch, flag=False, state="on")
    assert _sound_track(recipe) is None
    assert recipe.audio.mute_windows == []
    assert "clip_sound_toggles" not in variant


def test_an_on_ask_plays_the_footages_own_sound_over_a_muted_speaker(monkeypatch):
    _job, recipe, variant = _run(monkeypatch, flag=True, state="on")
    audio = _sound_track(recipe)
    cut = next(t for t in recipe.tracks if t.id == "talking-head-cutaways").clips[0]
    [clip] = audio.clips
    assert clip.source_asset_id == STADIUM
    assert (clip.timeline_start, clip.source_duration) == (cut.timeline_start, cut.source_duration)
    [window] = recipe.audio.mute_windows
    assert window.clip_ids == [
        c.id for c in next(t for t in recipe.tracks if t.id == "subtitled").clips
    ]
    assert window.start == clip.timeline_start
    assert window.end == pytest.approx(clip.timeline_start + clip.source_duration)
    [row] = variant["clip_sound_toggles"]["rows"]
    assert (row["requirement_id"], row["state"], row["status"]) == ("r2", "on", "met")
    receipt = check_requirement(
        _req("on"), PlanFacts(phone_talking=True, clip_sound_toggles=variant["clip_sound_toggles"])
    )
    assert receipt.status == "met"


def test_the_render_ready_review_rejudges_the_pinned_recipe_not_the_montage_clips(monkeypatch):
    from app.services.phone_clip_sound_toggle import rendered_toggle_receipt

    job, recipe, variant = _run(monkeypatch, flag=True, state="on")
    [row] = rendered_toggle_receipt(job.assembly_plan, variant, recipe)["rows"]
    assert (row["requirement_id"], row["status"]) == ("r2", "met")

    unmuted = recipe.model_copy(deep=True)
    unmuted.audio.mute_windows = []
    [row] = rendered_toggle_receipt(job.assembly_plan, variant, unmuted)["rows"]
    assert row["status"] == "partial"


def test_an_off_ask_keeps_the_footage_silent_and_says_so(monkeypatch):
    _job, recipe, variant = _run(monkeypatch, flag=True, state="off")
    assert _sound_track(recipe) is None
    [row] = variant["clip_sound_toggles"]["rows"]
    assert (row["state"], row["status"]) == ("off", "met")


def test_no_ask_means_no_sound_lane_and_no_record(monkeypatch):
    _job, recipe, variant = _run(monkeypatch, flag=True, state=None)
    assert _sound_track(recipe) is None
    assert "clip_sound_toggles" not in variant


def test_a_contract_that_names_the_speaker_as_the_only_camera_audio_keeps_the_footage_silent(
    monkeypatch,
):
    job, recipe, variant = _run(monkeypatch, flag=True, state="on", audio_source_ids=("take",))
    assert job.status == "awaiting_device", job.error_detail
    assert _sound_track(recipe) is None
    assert recipe.audio.mute_windows == []
    [row] = variant["clip_sound_toggles"]["rows"]
    assert (row["status"], row["reason_code"]) == ("not_possible", "contract_conflict")


def test_a_contract_that_lists_the_footage_among_the_camera_audio_still_plays_it(monkeypatch):
    job, recipe, variant = _run(
        monkeypatch, flag=True, state="on", audio_source_ids=("take", STADIUM)
    )
    assert job.status == "awaiting_device", job.error_detail
    assert _sound_track(recipe) is not None


def test_the_kill_switch_alone_keeps_the_footage_silent_while_mute_windows_stay_on(monkeypatch):
    job, recipe, variant = _run(monkeypatch, flag=False, state="on", windows=True)
    assert _sound_track(recipe) is None
    assert "clip_sound_toggles" not in variant


def test_the_finished_render_review_is_unchanged_while_the_lane_is_off(monkeypatch):
    from app.services.phone_clip_sound_toggle import rendered_toggle_receipt

    job, recipe, variant = _run(monkeypatch, flag=False, state="on")
    [row] = rendered_toggle_receipt(job.assembly_plan, variant, recipe)["rows"]
    assert (row["status"], row["reason_code"]) == ("not_possible", "not_in_edit")
