"""KRI-842: the compiler's per-clip sound, the tail geometry and the editor Save round trip.

Ways this could go wrong, written down before the code (the job-level cases are in
`tests/tasks/test_kri842_narrated_clip_sound.py`, the receipts in
`tests/kria/test_kri842_clip_sound_checks.py`):

FM1  The gain lands on the wrong clip when steps and clips differ: the same footage in two
     steps (both get it), or matched by position instead of by media.
FM2  Another clip stays audible under the voice.
FM4  The duck or the normalisation changes a level: the faint clip louder than the bed, a
     bed of 0 ignoring the ask, the duck switched on with nothing audible under the voice.
FM5  The tail is rejected by the tiling check, cut off by a stretched last step, has an id the
     editor projection cannot read, or sits in footage the clip does not have.
FM8  The tail is too short, too long, or the clip too short to have one.
FM9  Captions, title and the voice's fade follow the longer timeline.
FM12 An editor Save re-cut silently drops the gains or the tail while the receipt still says
     built: the gain follows its slot (not its index), a new slot is silent, and the receipt is
     re-read from the saved recipe.
"""

from __future__ import annotations

import pytest

pytest.importorskip("app.pipeline.phone_captions")

from app.config import settings
from app.pipeline.phone_clip_sound import (
    BUILT,
    EDITED,
    LINE_PAST_END,
    LINE_TOO_EARLY,
    LINE_TOO_LONG,
    NO_ROOM,
    NOT_POSSIBLE,
    reconcile_clip_sound,
    tail_window,
)
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_narrated_plan import (
    MAX_VOICE_TAIL_S,
    NarratedVoiceTail,
    compile_phone_narrated_plan,
    replace_narrated_captions,
    replace_narrated_title,
)
from app.pipeline.phone_voiceover_cut import replace_voiceover_cut
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_voiceover_timeline import narrated_timings_and_assignments
from tests.pipeline.test_phone_narrated_plan import (
    _narration,
    _step,
    _three_bindings,
    _three_steps,
)

VOICE_S = 12.0
BINDINGS = _three_bindings()
POOL = [b.proxy_path for b in BINDINGS]


def compile_(steps=None, **kwargs):
    return compile_phone_narrated_plan(
        steps or _three_steps(),
        BINDINGS,
        _narration(duration_s=VOICE_S),
        voiceover_duration_s=VOICE_S,
        mix=kwargs.pop("mix", 0.75),
        **kwargs,
    )


def video(recipe):
    return next(track for track in recipe.tracks if track.id == "narrated").clips


def verified(monkeypatch, recipe):
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )


# --- nothing asked: the recipe is the one this compiler always made ------------------------


def test_without_a_clip_sound_ask_the_recipe_is_unchanged_byte_for_byte():
    plain = compile_()
    explicit = compile_(under_voice_media_ids=frozenset(), voice_tail=None)

    assert plain.model_dump_json() == explicit.model_dump_json()
    assert plain.audio.original_volume == pytest.approx(0.25)
    assert [clip.volume for clip in video(plain)] == [1.0, 1.0, 1.0]
    assert plain.duration == pytest.approx(VOICE_S)


# --- the gain goes by media, and nothing else is audible under the voice -------------------


def test_the_gain_follows_the_media_in_every_step_that_plays_it():
    """FM1/FM2: c0 plays in two steps (3 steps != 2 clips); both are faint, c1 stays silent."""
    steps = [
        _step("s0", "c0", start_s=0.0, end_s=4.0),
        _step("s1", "c1", start_s=4.0, end_s=8.0),
        _step("s2", "c0", start_s=8.0, end_s=12.0, source_start_s=5.0),
    ]

    recipe = compile_(steps, under_voice_media_ids=frozenset({"c0"}))

    assert [clip.volume for clip in video(recipe)] == [
        pytest.approx(0.25),
        0.0,
        pytest.approx(0.25),
    ]
    assert recipe.audio.original_volume == 1.0
    assert recipe.duration == pytest.approx(VOICE_S)


def test_the_faint_clip_uses_the_bed_level_or_the_default_bed_when_it_is_zero():
    """FM4/FM15."""
    assert [
        c.volume for c in video(compile_(under_voice_media_ids=frozenset({"c1"}), mix=0.6))
    ] == [
        0.0,
        pytest.approx(0.4),
        0.0,
    ]
    silent_bed = compile_(under_voice_media_ids=frozenset({"c1"}), mix=1.0)
    assert [c.volume for c in video(silent_bed)] == [0.0, pytest.approx(0.25), 0.0]
    ducked = compile_(under_voice_media_ids=frozenset({"c1"}), mix=1.0, duck_footage_bed=True)
    assert [c.volume for c in video(ducked)] == [0.0, pytest.approx(0.25 * 0.6), 0.0]
    assert ducked.audio.duck_original_during_music
    assert "audioDucking" in ducked.required_capabilities


def test_the_duck_is_off_when_nothing_is_audible_under_the_voice():
    """FM4: a tail alone over a silent bed has nothing to duck; the tail itself is never ducked
    (the duck follows the voice, which is silent by then)."""
    tail = NarratedVoiceTail(media_id="c2", source_start_s=4.0, duration_s=2.0)

    silent = compile_(voice_tail=tail, mix=1.0, duck_footage_bed=True)
    bed = compile_(voice_tail=tail, mix=0.75, duck_footage_bed=True)

    assert not silent.audio.duck_original_during_music
    assert "audioDucking" not in silent.required_capabilities
    assert [c.volume for c in video(silent)] == [0.0, 0.0, 0.0, 1.0]
    assert bed.audio.duck_original_during_music
    assert [c.volume for c in video(bed)][:3] == [pytest.approx(0.25 * 0.6)] * 3


# --- the tail ------------------------------------------------------------------------------


def test_the_tail_starts_where_the_voice_ends_and_nothing_else_moves():
    """FM5/FM9."""
    tail = NarratedVoiceTail(media_id="c2", source_start_s=4.0, duration_s=2.0)

    recipe = compile_(
        voice_tail=tail,
        caption_cues=[{"text": "Hi", "start_s": 0.0, "end_s": 2.0}],
        opening_title="A title",
    )

    clips = video(recipe)
    assert [c.id for c in clips] == ["step-0-s0", "step-1-s1", "step-2-s2", "step-3-s2-tail"]
    assert [c.timeline_start for c in clips] == pytest.approx([0.0, 4.0, 8.0, 12.0])
    assert clips[-1].volume == 1.0 and clips[-1].rate == 1.0
    assert (clips[-1].source_start, clips[-1].source_duration) == (4.0, 2.0)
    # The steps still tile the voice, the voice keeps its own length and fade.
    voice = next(t for t in recipe.tracks if t.id == "narration").clips[0]
    assert voice.source_duration == pytest.approx(VOICE_S)
    assert voice.audio_fade_out == pytest.approx(0.5)
    assert recipe.duration == pytest.approx(VOICE_S + 2.0)
    # Voiced clips keep today's bed level when no clip was singled out.
    assert [c.volume for c in clips[:3]] == [pytest.approx(0.25)] * 3
    assert recipe.audio.original_volume == 1.0
    # Captions are not stretched into the tail, the title still fades out where it did.
    captions = [layer for layer in recipe.text_layers if layer.id.startswith("caption")]
    assert captions and max(layer.end for layer in captions) <= 2.0 + 1e-6
    assert any(layer.id.startswith("title-") for layer in recipe.text_layers)


def test_the_tail_survives_every_swap_a_save_makes_and_the_editor_still_reads_it(monkeypatch):
    tail = NarratedVoiceTail(media_id="c2", source_start_s=4.0, duration_s=2.0)
    recipe = compile_(voice_tail=tail, opening_title="A title")
    verified(monkeypatch, recipe)
    validate_phone_pilot_recipe(recipe)

    recaptioned = replace_narrated_captions(
        recipe, caption_cues=[{"text": "New", "start_s": 0.0, "end_s": 3.0}]
    )
    retitled = replace_narrated_title(recaptioned, [])

    assert video(retitled) == video(recipe)
    assert retitled.audio == recipe.audio
    projected = narrated_timings_and_assignments(recipe, list(BINDINGS), POOL)
    assert projected is not None
    timings, assignments, slots = projected
    assert [(t["step_id"], t["start_s"], t["end_s"]) for t in timings][-1] == (
        "s2-tail",
        12.0,
        14.0,
    )
    assert assignments[-1]["clip_id"] == "clip_2" and assignments[-1]["source_start_s"] == 4.0
    assert [slot["slot_id"] for slot in slots][-1] == "s2-tail"


@pytest.mark.parametrize(
    "tail",
    [
        NarratedVoiceTail(media_id="elsewhere", source_start_s=0.0, duration_s=1.0),
        NarratedVoiceTail(media_id="c2", source_start_s=9.5, duration_s=1.5),  # clip is 10 s
    ],
    ids=["media not in the edit", "past the footage"],
)
def test_a_tail_the_footage_cannot_play_is_refused(tail):
    with pytest.raises(UnsupportedPhonePlan):
        compile_(voice_tail=tail)


def test_a_tail_longer_than_the_most_a_clip_may_play_on_is_not_a_tail():
    with pytest.raises(ValueError):
        NarratedVoiceTail(media_id="c2", source_start_s=0.0, duration_s=MAX_VOICE_TAIL_S + 0.1)


# --- where the tail can sit (FM8) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # N1: the line at 3.42-4.40 in a 4.77 s clip, 2.2 s voiced: ends in the pause before it.
        (
            dict(clip_s=4.77, voiced_s=2.202, voiced_start_s=0.0, line=(3.42, 4.40)),
            (0.918, 3.12, 1.58),
        ),
        # No line: the rest of a long clip, at most 3 s.
        (dict(clip_s=20.0, voiced_s=4.0, voiced_start_s=1.0, line=None), (1.0, 5.0, 3.0)),
        # No line, little left: the voiced part moves earlier to leave the 0.8 s minimum.
        (dict(clip_s=6.0, voiced_s=4.0, voiced_start_s=1.5, line=None), (1.15, 5.15, 0.8)),
        # Not enough footage before the line: the voiced part starts at 0, the tail follows
        # it while the line has not begun.
        (
            dict(clip_s=10.0, voiced_s=3.2, voiced_start_s=0.0, line=(3.4, 4.0)),
            (0.0, 3.2, 1.1),
        ),
    ],
)
def test_tail_window(kwargs, expected):
    assert tail_window(**kwargs) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        # The whole clip is the voiced part: nothing left.
        (dict(clip_s=4.77, voiced_s=4.5, voiced_start_s=0.0, line=None), NO_ROOM),
        # The voiced part would still be playing when the line starts: its own reason, never
        # "no footage left".
        (dict(clip_s=10.0, voiced_s=3.8, voiced_start_s=0.0, line=(3.4, 4.0)), LINE_TOO_EARLY),
        # The line ends past the end of the footage.
        (dict(clip_s=4.0, voiced_s=1.0, voiced_start_s=0.0, line=(4.2, 4.6)), LINE_PAST_END),
        # A line longer than the most a tail may play.
        (dict(clip_s=30.0, voiced_s=2.0, voiced_start_s=0.0, line=(10.0, 17.5)), LINE_TOO_LONG),
    ],
)
def test_no_tail_where_the_clip_has_no_room_for_one(kwargs, reason):
    assert tail_window(**kwargs) == reason


# --- editor Save (FM12) --------------------------------------------------------------------


def n1_like_recipe():
    """Two quiet clips, the faint c1, and c2 playing on after the voice."""
    return compile_(
        under_voice_media_ids=frozenset({"c1"}),
        voice_tail=NarratedVoiceTail(media_id="c2", source_start_s=4.0, duration_s=2.0),
    )


def slots_of(recipe):
    projected = narrated_timings_and_assignments(recipe, list(BINDINGS), POOL)
    assert projected is not None
    return projected[2]


def cut(recipe, slots):
    return replace_voiceover_cut(
        recipe, archetype="narrated", slots=slots, bindings=BINDINGS, pool=POOL
    )


RECEIPT = {
    "version": 1,
    "under_voice": [{"requirement_id": "r1", "media_id": "c1", "status": BUILT, "gain": 0.25}],
    "after_voice": {
        "requirement_id": "r2",
        "media_id": "c2",
        "status": BUILT,
        "line": "bye bye",
        "line_start_s": 4.2,
        "line_end_s": 5.0,
        "source_start_s": 4.0,
        "source_end_s": 6.0,
        "tail_start_s": 12.0,
        "tail_end_s": 14.0,
    },
    "not_built": [],
}


def test_a_save_that_leaves_the_cut_alone_keeps_every_gain_and_the_tail():
    recipe = n1_like_recipe()

    assert cut(recipe, slots_of(recipe)) == recipe
    reconciled = reconcile_clip_sound(recipe, RECEIPT)
    assert reconciled["under_voice"][0]["status"] == BUILT
    assert reconciled["under_voice"][0]["gain"] == pytest.approx(0.25)
    assert reconciled["after_voice"]["status"] == BUILT
    assert reconciled["after_voice"]["tail_start_s"] == pytest.approx(12.0)
    assert reconciled["after_voice"]["tail_end_s"] == pytest.approx(14.0)


def test_a_gain_follows_its_slot_when_an_earlier_clip_is_deleted():
    """The clip id carries its index, so deleting s0 renames every later clip."""
    recipe = n1_like_recipe()
    slots = slots_of(recipe)

    edited = cut(recipe, slots[1:])  # s0 deleted: s1 (faint) is now index 0, c2 + its tail follow

    assert [(c.source_asset_id, c.volume) for c in video(edited)] == [
        ("c1", pytest.approx(0.25)),
        ("c2", 0.0),
        ("c2", 1.0),
    ]
    assert edited.audio.original_volume == 1.0


def test_a_slot_added_by_the_save_is_silent_under_the_voice():
    recipe = n1_like_recipe()
    slots = slots_of(recipe)
    extra = {
        "slot_id": "added",
        "clip_index": 0,
        "in_s": 5.0,
        "duration_s": 2.0,
        "transition_after": "cut",
    }

    edited = cut(recipe, [slots[0], extra, *slots[1:]])

    assert [c.volume for c in video(edited)] == [0.0, 0.0, pytest.approx(0.25), 0.0, 1.0]


def test_the_receipt_is_read_from_the_recipe_the_save_pinned():
    recipe = n1_like_recipe()
    slots = slots_of(recipe)

    # Deleting the tail: the voice now ends the video; nothing plays on.
    no_tail = reconcile_clip_sound(cut(recipe, slots[:-1]), RECEIPT)
    assert no_tail["after_voice"]["status"] == NOT_POSSIBLE
    assert no_tail["after_voice"]["reason_code"] == EDITED
    assert no_tail["under_voice"][0]["status"] == BUILT

    # Shortening an earlier clip pulls the tail under the voice: it is no longer "after".
    shorter = [dict(slot) for slot in slots]
    shorter[0]["duration_s"] = 1.0
    under_the_voice = reconcile_clip_sound(cut(recipe, shorter), RECEIPT)
    assert under_the_voice["after_voice"]["status"] == NOT_POSSIBLE

    # Cutting the tail so the line is cut off.
    clipped = [dict(slot) for slot in slots]
    clipped[-1]["in_s"], clipped[-1]["duration_s"] = 4.0, 0.5
    cut_off = reconcile_clip_sound(cut(recipe, clipped), RECEIPT)
    assert cut_off["after_voice"]["status"] == NOT_POSSIBLE

    # Deleting the faint clip: its sound no longer plays.
    gone = reconcile_clip_sound(cut(recipe, [slots[0], *slots[2:]]), RECEIPT)
    assert gone["under_voice"][0]["status"] == NOT_POSSIBLE
    assert gone["under_voice"][0]["reason_code"] == EDITED


def test_a_rebuilt_uniform_bed_no_longer_counts_as_one_clips_sound():
    """The authored timeline rebuilds every clip at the item's bed: "only this clip" is gone."""
    uniform = compile_()  # every clip at the bed, original_volume the bed

    reconciled = reconcile_clip_sound(uniform, RECEIPT)

    assert reconciled["under_voice"][0]["status"] == NOT_POSSIBLE
    assert reconciled["after_voice"]["status"] == NOT_POSSIBLE


def test_a_tail_left_at_the_quiet_bed_does_not_count_as_playing_on():
    """The authored restage rebuilds every clip at the item's bed level: audible, but not the
    normal-volume goodbye the creator asked for."""
    recipe = n1_like_recipe()
    clips = video(recipe)
    quiet_tail = clips[-1].model_copy(update={"volume": 0.25})
    tracks = [
        track.model_copy(update={"clips": [*clips[:-1], quiet_tail]})
        if track.id == "narrated"
        else track
        for track in recipe.tracks
    ]
    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}
    quiet = type(recipe)(**{**fields, "tracks": tracks})

    reconciled = reconcile_clip_sound(quiet, RECEIPT)

    assert reconciled["after_voice"]["status"] == NOT_POSSIBLE
    assert reconciled["after_voice"]["reason_code"] == EDITED
