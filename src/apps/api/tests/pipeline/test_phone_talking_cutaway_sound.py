"""KRI-1151: a Talking edit plays a chosen footage cutaway's own sound, speaker muted under it.

Incident (prod thread 6fa36e51, job 07efa44c, 2026-11-01): "Show the talks when I mention in
full screen and use their audio. Place the stadium full screen when I talk but keep my audio."
The draft said "Turning clip sound on or off by clip or by seconds works on a montage made on
your iPhone, not in this edit" and held the whole draft back.

These tests pin the recipe the device engine renders, not the planner's claim. The engine
already plays an audio-kind track from a video asset (speech-excerpt montage) and ramps recipe
mute windows (KRI-942), so the shape under test needs no new capability.

Ways this can fail, each pinned below:
1. The cutaway's sound is on the recipe but the speaker still plays under it (two voices).
2. The speaker is muted but the cutaway plays nothing (silence).
3. A cutaway file with no audio track gets an audio clip (the engine throws at export).
4. A clamped / dropped cutaway keeps a window that no longer exists.
5. An editor Save drops the sound (the recipe is rebuilt from the cutaway track alone).
6. A sound-off cutaway changes bytes for every existing Talking recipe.
7. The pilot gate lets the recipe through while mute windows are switched off.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.clip_sound_toggle_ask import requirement_sound_toggle
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_subtitled_plan import (
    CUTAWAY_AUDIO_TRACK_ID,
    CUTAWAY_TRACK_ID,
    PhoneCutaway,
    compile_phone_subtitled_plan,
    cutaways_from_recipe,
)
from app.pipeline.phone_talking_cutaway_sound import (
    judge_cutaway_sound,
    plan_cutaway_sound,
)
from app.services.phone_rollout import validate_phone_pilot_recipe
from tests.pipeline.test_phone_subtitled_plan import _CUES, _binding

STADIUM = "134CDBD9-2781-4407-921B-57D663D2B33B.mp4"
TALK = "5A1F0C22-7777-4B9E-8C11-0123456789AB.mp4"
SPEAKER = "speaker.mp4"

_VERIFIED = [
    "basicComposition",
    "local1080Export",
    "positionedText",
    "animatedText",
    "audioMix",
    "visualBlocks",
    "visualVideos",
    "alphaOverlay",
]


def _cutaway(media_id: str, start_s: float, end_s: float, *, sound: bool = False, **binding):
    return PhoneCutaway(
        binding=_binding(media_id, **binding), start_s=start_s, end_s=end_s, sound=sound
    )


def _recipe(*cutaways: PhoneCutaway, speaker_s: float = 20.0):
    return compile_phone_subtitled_plan(
        (_binding(SPEAKER, duration_s=speaker_s),), caption_cues=_CUES, cutaways=cutaways
    )


def _req(rid: str, state: str, scope: str = "global", **facts) -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="audio",
        scope=scope,
        status="open",
        description=f"{rid} {state}",
        facts={"clip_sound": state, **facts},
        source_turn_id="t1",
    )


def _asks(*reqs: BriefRequirement):
    return tuple(a for r in reqs if (a := requirement_sound_toggle(r)) is not None)


def test_a_sound_cutaway_plays_its_own_audio_over_a_muted_speaker():
    recipe = _recipe(_cutaway(STADIUM, 3.0, 6.0, sound=True), _cutaway(TALK, 10.0, 12.0))

    tracks = {t.id: t for t in recipe.tracks}
    audio = tracks[CUTAWAY_AUDIO_TRACK_ID]
    assert audio.kind == "audio"
    assert [
        (c.source_asset_id, c.timeline_start, c.source_start, c.source_duration, c.volume)
        for c in audio.clips
    ] == [(STADIUM, 3.0, 0.0, pytest.approx(3.0), 1)]
    # The picture clip on the overlay track stays silent: its sound comes from the audio track.
    assert all(c.volume == 0 for c in tracks[CUTAWAY_TRACK_ID].clips)
    speaker_ids = [c.id for c in tracks["subtitled"].clips]
    [window] = recipe.audio.mute_windows
    assert (window.start, window.end) == (3.0, pytest.approx(6.0))
    assert window.clip_ids == speaker_ids
    assert recipe.audio.original_volume == 1.0
    assert {"audioMix", "visualBlocks", "visualVideos"} <= recipe.required_capabilities
    recipe.model_validate(recipe.model_dump(mode="json"))


def test_a_cutaway_without_sound_leaves_every_existing_recipe_unchanged():
    plain = _recipe(_cutaway(STADIUM, 3.0, 6.0))
    assert CUTAWAY_AUDIO_TRACK_ID not in {t.id for t in plain.tracks}
    assert plain.audio.mute_windows == []
    assert "mute_windows" not in plain.model_dump(mode="json")["audio"]


def test_a_sound_cutaway_over_a_file_with_no_audio_fails_closed():
    silent = PhoneCutaway(
        binding=_binding(STADIUM).model_copy(
            update={"original": _binding(STADIUM).original.model_copy(update={"has_audio": False})}
        ),
        start_s=3.0,
        end_s=6.0,
        sound=True,
    )
    with pytest.raises(UnsupportedPhonePlan, match="no sound"):
        _recipe(silent)


def test_the_sound_window_follows_the_clamped_cutaway():
    recipe = _recipe(_cutaway(STADIUM, 8.0, 12.0, sound=True, duration_s=1.2))
    [clip] = next(t for t in recipe.tracks if t.id == CUTAWAY_AUDIO_TRACK_ID).clips
    assert clip.source_duration == pytest.approx(1.2)
    [window] = recipe.audio.mute_windows
    assert window.end == pytest.approx(9.2)


def test_a_cutaway_too_short_to_show_has_no_sound_either():
    recipe = _recipe(_cutaway(STADIUM, 19.8, 25.0, sound=True))
    assert CUTAWAY_AUDIO_TRACK_ID not in {t.id for t in recipe.tracks}
    assert recipe.audio.mute_windows == []


def test_sound_survives_a_pinned_recipe_round_trip():
    cutaways = (_cutaway(STADIUM, 3.0, 6.0, sound=True), _cutaway(TALK, 10.0, 12.0))
    recipe = _recipe(*cutaways)
    bindings = (cutaways[0].binding, cutaways[1].binding)
    again = cutaways_from_recipe(recipe, bindings)
    assert [c.sound for c in again] == [True, False]
    rebuilt = _recipe(*again)
    assert rebuilt.model_dump_json() == recipe.model_dump_json()


def test_the_pilot_gate_needs_mute_windows_switched_on(monkeypatch: pytest.MonkeyPatch):
    recipe = _recipe(_cutaway(STADIUM, 3.0, 6.0, sound=True))
    monkeypatch.setattr(settings, "phone_render_verified_features", _VERIFIED)
    monkeypatch.setattr(settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(settings, "phone_clip_sound_windows_enabled", False)
    with pytest.raises(ValueError, match="Muted sections"):
        validate_phone_pilot_recipe(recipe)
    monkeypatch.setattr(settings, "phone_clip_sound_windows_enabled", True)
    validate_phone_pilot_recipe(recipe)


# -- which cutaways the creator's asks turn on ------------------------------------------


def test_a_whole_video_on_ask_turns_every_cutaway_on_and_a_named_off_wins():
    asks = _asks(_req("r2", "on"), _req("r4", "off", scope=f"clip:{STADIUM}"))
    plan = plan_cutaway_sound(asks, cutaway_media_ids=[STADIUM, TALK], speaker_media_ids=[SPEAKER])
    assert plan.on_media_ids == frozenset({TALK})
    rows = {r["requirement_id"]: r for r in plan.rows}
    assert rows["r2"]["status"] == "built"
    assert rows["r4"]["status"] == "built"


def test_a_named_on_ask_turns_only_that_cutaway_on():
    asks = _asks(_req("r2", "on", scope=f"clip:{STADIUM}"))
    plan = plan_cutaway_sound(asks, cutaway_media_ids=[STADIUM, TALK], speaker_media_ids=[SPEAKER])
    assert plan.on_media_ids == frozenset({STADIUM})


def test_an_ask_naming_a_clip_beats_a_whole_video_ask_whatever_the_order():
    asks = _asks(_req("r2", "on"), _req("r5", "off", scope=f"clip:{TALK}"), _req("r6", "on"))
    plan = plan_cutaway_sound(asks, cutaway_media_ids=[STADIUM, TALK], speaker_media_ids=[SPEAKER])
    assert plan.on_media_ids == frozenset({STADIUM})


def test_an_on_ask_for_the_speaker_is_already_true():
    asks = _asks(_req("r2", "on", scope=f"clip:{SPEAKER}"))
    plan = plan_cutaway_sound(asks, cutaway_media_ids=[STADIUM], speaker_media_ids=[SPEAKER])
    assert plan.on_media_ids == frozenset()
    assert plan.rows[0]["status"] == "built"


def test_a_clip_that_is_not_in_the_edit_is_said_so():
    asks = _asks(_req("r2", "on", scope="clip:nonexistent-clip"))
    plan = plan_cutaway_sound(asks, cutaway_media_ids=[STADIUM], speaker_media_ids=[SPEAKER])
    assert plan.rows[0]["status"] == "not_possible"
    assert plan.rows[0]["reason_code"] == "not_in_edit"


@pytest.mark.parametrize(
    "facts",
    [
        {"sound_from_s": 4, "sound_to_s": 6},
        {"sound_line": "Super excited!"},
        {"sound_clip": "last"},
        {"sound_except": [{"sound_clip": "last"}]},
    ],
)
def test_asks_a_cutaway_cannot_express_are_not_built_and_say_why(facts):
    asks = _asks(_req("r2", "on", **facts))
    plan = plan_cutaway_sound(asks, cutaway_media_ids=[STADIUM], speaker_media_ids=[SPEAKER])
    assert plan.on_media_ids == frozenset()
    assert plan.rows[0]["status"] == "not_possible"
    assert plan.rows[0]["reason_code"] == "cutaway_only"


def test_a_whole_video_off_ask_is_not_this_lanes_to_judge():
    asks = _asks(_req("r9", "off"))
    plan = plan_cutaway_sound(asks, cutaway_media_ids=[STADIUM], speaker_media_ids=[SPEAKER])
    assert plan.rows == ()


# -- judging what the recipe really plays -----------------------------------------------


def test_the_judge_reads_the_recipe_not_the_plan():
    asks = _asks(_req("r2", "on", scope=f"clip:{STADIUM}"))
    plan = plan_cutaway_sound(asks, cutaway_media_ids=[STADIUM], speaker_media_ids=[SPEAKER])

    built = _recipe(_cutaway(STADIUM, 3.0, 6.0, sound=True))
    [row] = judge_cutaway_sound(plan, built)["rows"]
    assert row["status"] == "met"
    assert row["on_windows"] == [[3.0, 6.0]]

    silent = _recipe(_cutaway(STADIUM, 3.0, 6.0))
    [row] = judge_cutaway_sound(plan, silent)["rows"]
    assert row["status"] == "not_possible"
    assert row["reason_code"] == "not_built"

    unmuted = built.model_copy(deep=True)
    unmuted.audio.mute_windows = []
    [row] = judge_cutaway_sound(plan, unmuted)["rows"]
    assert row["status"] == "partial"


def test_the_judge_says_when_a_named_cutaway_never_made_it_onto_the_timeline():
    asks = _asks(_req("r2", "on", scope=f"clip:{STADIUM}"))
    plan = plan_cutaway_sound(asks, cutaway_media_ids=[STADIUM], speaker_media_ids=[SPEAKER])
    recipe = _recipe(_cutaway(TALK, 3.0, 6.0))
    [row] = judge_cutaway_sound(plan, recipe)["rows"]
    assert row["status"] == "not_possible"
    assert row["reason_code"] == "not_in_edit"


def test_the_judge_confirms_a_named_off_cutaway_stays_silent():
    asks = _asks(_req("r4", "off", scope=f"clip:{STADIUM}"))
    plan = plan_cutaway_sound(asks, cutaway_media_ids=[STADIUM], speaker_media_ids=[SPEAKER])
    [row] = judge_cutaway_sound(plan, _recipe(_cutaway(STADIUM, 3.0, 6.0)))["rows"]
    assert row["status"] == "met"
    assert row["off_windows"] == [[3.0, 6.0]]
    [row] = judge_cutaway_sound(plan, _recipe(_cutaway(STADIUM, 3.0, 6.0, sound=True)))["rows"]
    assert row["status"] == "not_possible"


# -- review round: captions follow the audible voice -------------------------------------

_SPOKEN = [
    {"text": "first line here", "start_s": 0.5, "end_s": 2.5},
    {"text": "muted words under cutaway", "start_s": 3.5, "end_s": 5.5},
    {"text": "after the cutaway", "start_s": 7.0, "end_s": 9.0},
]


def _captioned(*cutaways: PhoneCutaway):
    return compile_phone_subtitled_plan(
        (_binding(SPEAKER, duration_s=20.0),), caption_cues=_SPOKEN, cutaways=cutaways
    )


def _caption_texts(recipe) -> list[str]:
    return [" ".join(run.text for run in layer.runs) for layer in recipe.text_layers]


def test_captions_for_words_the_viewer_cannot_hear_are_not_shown():
    recipe = _captioned(_cutaway(STADIUM, 3.0, 6.0, sound=True))
    assert _caption_texts(recipe) == ["first line here", "after the cutaway"]


def test_a_silent_cutaway_keeps_every_caption_because_the_speaker_still_talks():
    recipe = _captioned(_cutaway(STADIUM, 3.0, 6.0))
    assert len(recipe.text_layers) == 3


def test_a_caption_mostly_outside_the_muted_stretch_stays():
    recipe = _captioned(_cutaway(STADIUM, 5.0, 6.0, sound=True))
    assert len(recipe.text_layers) == 3


def test_an_overridden_on_ask_is_not_judged_met():
    asks = _asks(
        _req("r2", "on", scope=f"clip:{STADIUM}"), _req("r4", "off", scope=f"clip:{STADIUM}")
    )
    plan = plan_cutaway_sound(asks, cutaway_media_ids=[STADIUM], speaker_media_ids=[SPEAKER])
    rows = {
        r["requirement_id"]: r
        for r in judge_cutaway_sound(plan, _recipe(_cutaway(STADIUM, 3.0, 6.0)))["rows"]
    }
    assert (rows["r2"]["status"], rows["r2"]["reason_code"]) == ("not_possible", "overridden")
    assert rows["r4"]["status"] == "met"
