"""KRI-290: `replace_voiceover_cut` swaps a phone Voiceover recipe's clip cut.

The video owns the clock: shorter footage cuts the voiceover at the video's end,
longer footage plays on after the voice ends, and growing the cut again plays
the voice (and a music bed) up to their natural length. Everything else keeps
its pinned shape.
"""

from __future__ import annotations

import pytest

pytest.importorskip("app.pipeline.phone_captions")

from app.config import settings
from app.kria.recipes import AssetFingerprint, MediaAsset, TimelineClip, TimelineTrack
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import LibraryRenderAsset, RenderAssetManifest, RenderFingerprint
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_narrated_plan import compile_phone_narrated_plan
from app.pipeline.phone_recipe_shared import PHONE_AUDIO_FADE_S, timeline_end_s
from app.pipeline.phone_voiceover_cut import replace_voiceover_cut, transition_overlap_s
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_voiceover_timeline import (
    narrated_timings_and_assignments,
    voiceover_ai_timeline,
)
from tests.pipeline.test_phone_narrated_plan import _binding, _narration, _step

_VERIFIED = [
    "basicComposition",
    "local1080Export",
    "narrationAudio",
    "audioMix",
    "positionedText",
    "animatedText",
    "variableSpeed",
    "crossfade",
    "musicBed",
]


@pytest.fixture
def verified(monkeypatch):
    monkeypatch.setattr(settings, "phone_render_verified_features", list(_VERIFIED))


_BINDINGS = (_binding("c0"), _binding("c1", duration_s=2.0), _binding("c2"), _binding("c3"))
_POOL = [b.proxy_path for b in _BINDINGS]


def _recipe(*, voice_s: float = 12.0, cues: list[dict] | None = None) -> EditRecipeV2:
    """A narrated recipe over c0 / c1 / c2 (c3 stays in the pool, unused).

    ``voice_s`` longer than 12 models a voiceover that was cut at compile time
    because the footage was shorter than it.
    """
    return compile_phone_narrated_plan(
        [
            _step("s0", "c0", start_s=0.0, end_s=4.0, source_start_s=1.5),
            _step("s1", "c1", start_s=4.0, end_s=8.0),
            _step("s2", "c2", start_s=8.0, end_s=12.0),
        ],
        _BINDINGS[:3],
        _narration(duration_s=voice_s),
        voiceover_duration_s=12.0,
        mix=0.7,
        caption_cues=cues if cues is not None else [{"text": "Hi", "start_s": 0.0, "end_s": 2.0}],
    )


def _montage(recipe: EditRecipeV2, *, music_s: float | None = None) -> EditRecipeV2:
    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}
    tracks = [
        track.model_copy(update={"id": "montage"}) if track.id == "narrated" else track
        for track in recipe.tracks
    ]
    assets, manifest = list(recipe.assets), list(recipe.asset_manifest.assets)
    if music_s is not None:
        print_ = RenderFingerprint(sha256="e" * 64, byte_count=4242)
        manifest.append(
            LibraryRenderAsset(
                id="music-song",
                catalog="music",
                catalog_id="song",
                generation="7",
                fingerprint=print_,
            )
        )
        assets.append(
            MediaAsset(
                id="music-song",
                relative_path="music-song",
                fingerprint=AssetFingerprint(hex=print_.sha256, byte_count=print_.byte_count),
                duration=music_s,
            )
        )
        tracks.append(
            TimelineTrack(
                id="music",
                kind="audio",
                clips=[
                    TimelineClip(
                        id="music-bed",
                        source_asset_id="music-song",
                        source_start=5.0,
                        source_duration=12.0,
                        timeline_start=0.0,
                        rate=1.0,
                        volume=0.3,
                        audio_fade_in=0.5,
                        audio_fade_out=0.5,
                    )
                ],
            )
        )
    fields.update(
        tracks=tracks, assets=assets, asset_manifest=RenderAssetManifest(assets=tuple(manifest))
    )
    return EditRecipeV2(**fields)


def _narrated_slots(recipe: EditRecipeV2) -> list[dict]:
    projected = narrated_timings_and_assignments(recipe, list(_BINDINGS), _POOL)
    assert projected is not None
    return projected[2]


def _montage_slots(recipe: EditRecipeV2) -> list[dict]:
    timeline = voiceover_ai_timeline(recipe, list(_BINDINGS), _POOL)
    assert timeline is not None
    return timeline["slots"]


def _track(recipe: EditRecipeV2, track_id: str) -> TimelineTrack:
    return next(track for track in recipe.tracks if track.id == track_id)


def _cut(recipe, slots, archetype="narrated"):
    return replace_voiceover_cut(
        recipe, archetype=archetype, slots=slots, bindings=_BINDINGS, pool=_POOL
    )


def test_an_untouched_narrated_cut_round_trips_to_the_same_recipe():
    recipe = _recipe()
    assert _cut(recipe, _narrated_slots(recipe)) == recipe


def test_an_untouched_montage_cut_round_trips_to_the_same_recipe():
    recipe = _montage(_recipe())
    assert _cut(recipe, _montage_slots(recipe), "voiceover") == recipe


def test_shorter_footage_cuts_the_voice_at_the_video_end(verified):
    recipe = _recipe()
    slots = _narrated_slots(recipe)
    slots[2]["duration_s"] = 2.0  # the last clip now ends at 10 s, voice is 12 s

    edited = _cut(recipe, slots)

    assert timeline_end_s(_track(edited, "narrated").clips) == pytest.approx(10.0)
    voice = _track(edited, "narration").clips[0]
    assert voice.source_duration == pytest.approx(10.0)
    assert voice.audio_fade_out == pytest.approx(PHONE_AUDIO_FADE_S)
    # Nothing outruns the cut: the recipe is exactly as long as the video.
    assert edited.duration == pytest.approx(10.0)
    validate_phone_pilot_recipe(edited)


def test_longer_footage_plays_on_after_the_voice_ends(verified):
    recipe = _recipe()
    slots = _narrated_slots(recipe)
    slots[0]["duration_s"] = 6.0  # c0 has 8.5 s from its 1.5 s in-point

    edited = _cut(recipe, slots)

    clips = _track(edited, "narrated").clips
    assert [clip.timeline_start for clip in clips] == pytest.approx([0.0, 6.0, 10.0])
    assert edited.duration == pytest.approx(14.0)
    # The voiceover keeps its natural 12 s -- never stretched or looped.
    assert _track(edited, "narration").clips == _track(recipe, "narration").clips
    validate_phone_pilot_recipe(edited)


def test_extending_again_plays_more_of_a_voice_the_first_cut_had_cut_short():
    # The voice is 15 s but the first render's footage only covered 12 s.
    recipe = _recipe(voice_s=15.0)
    assert _track(recipe, "narration").clips[0].source_duration == pytest.approx(12.0)
    slots = _narrated_slots(recipe)
    slots[2]["duration_s"] = 6.0  # video now ends at 14 s
    longer = _cut(recipe, slots)
    assert _track(longer, "narration").clips[0].source_duration == pytest.approx(14.0)

    slots[2]["duration_s"] = 9.0  # 17 s of video, past the 15 s voice
    longest = _cut(recipe, slots)
    assert _track(longest, "narration").clips[0].source_duration == pytest.approx(15.0)
    assert longest.duration == pytest.approx(17.0)


def test_a_music_bed_follows_the_cut_up_to_its_own_length():
    recipe = _montage(_recipe(), music_s=20.0)  # 15 s of song after its 5 s start
    slots = _montage_slots(recipe)

    slots[2]["duration_s"] = 2.0
    shorter = _cut(recipe, slots, "voiceover")
    bed = _track(shorter, "music").clips[0]
    assert bed.source_duration == pytest.approx(10.0)
    assert bed.audio_fade_in == bed.audio_fade_out == pytest.approx(PHONE_AUDIO_FADE_S)

    slots[2]["duration_s"] = 9.0  # 17 s of video
    longer = _cut(recipe, slots, "voiceover")
    assert _track(longer, "music").clips[0].source_duration == pytest.approx(15.0)


def test_reorder_delete_and_a_new_pool_clip_rebuild_the_track():
    recipe = _montage(_recipe())
    slots = _montage_slots(recipe)
    new_clip = {
        "slot_id": "new-slot",
        "clip_index": 3,  # c3: in the pool, never used by the first cut
        "in_s": 0.5,
        "duration_s": 3.0,
        "transition_after": "cut",
    }
    edited = _cut(recipe, [slots[2], new_clip, slots[0]], "voiceover")  # c1 deleted

    clips = _track(edited, "montage").clips
    assert [clip.id for clip in clips] == [slots[2]["slot_id"], "new-slot", slots[0]["slot_id"]]
    assert [clip.timeline_start for clip in clips] == pytest.approx([0.0, 4.0, 7.0])
    asset_ids = {asset.id for asset in edited.assets}
    assert "c3" in asset_ids  # the newly used source is pinned
    assert "c1" not in asset_ids  # the deleted clip's source is not
    assert {asset.id for asset in edited.asset_manifest.assets} == asset_ids
    # Rate-1 clips only now: the slowed c1 clip is gone.
    assert "variableSpeed" not in edited.required_capabilities


def test_transitions_follow_the_native_preview_overlap():
    recipe = _montage(_recipe())
    slots = _montage_slots(recipe)
    slots[0].update(transition_after="dip_to_black", transition_duration_s=0.5)

    edited = _cut(recipe, slots, "voiceover")

    clips = _track(edited, "montage").clips
    assert clips[1].transition is not None
    assert clips[1].transition.kind == "fade_black"
    assert clips[1].transition.duration == pytest.approx(0.3)  # capped like the preview
    assert clips[1].timeline_start == pytest.approx(3.7)
    assert "crossfade" in edited.required_capabilities
    # The projection reads it back in the editor's own vocabulary.
    reread = _montage_slots(edited)
    assert reread[0]["transition_after"] == "dip_to_black"
    assert reread[0]["transition_duration_s"] == pytest.approx(0.3)


def test_transition_overlap_matches_the_native_preview():
    cut = {"transition_after": "cut"}
    fade = {"transition_after": "crossfade", "transition_duration_s": None}
    assert transition_overlap_s(cut, 4.0, 4.0) == 0.0
    assert transition_overlap_s(fade, 4.0, 4.0) == 0.3
    assert transition_overlap_s(fade, 0.5, 4.0) == 0.15
    assert transition_overlap_s(fade, 0.3, 4.0) == 0.0  # under 0.1 s is a hard cut


def test_text_that_ran_to_the_end_follows_the_cut():
    recipe = _montage(_recipe(cues=[{"text": "Bye", "start_s": 10.0, "end_s": 12.0}]))
    assert recipe.text_layers[-1].end == pytest.approx(12.0)
    slots = _montage_slots(recipe)

    slots[2]["duration_s"] = 6.0
    longer = _cut(recipe, slots, "voiceover")
    assert longer.text_layers[-1].end == pytest.approx(14.0)

    slots[2]["duration_s"] = 1.0  # the video now ends at 9 s, before the text starts
    shorter = _cut(recipe, slots, "voiceover")
    assert all(layer.start < 9.0 for layer in shorter.text_layers)
    assert all(layer.end <= shorter.duration for layer in shorter.text_layers)


def test_a_narrated_clip_still_slows_to_fill_a_window_its_footage_cannot_cover():
    recipe = _recipe()
    slots = _narrated_slots(recipe)
    slots[1]["duration_s"] = 3.0  # c1 has 2 s of footage

    clip = _track(_cut(recipe, slots), "narrated").clips[1]
    assert clip.source_duration / clip.rate == pytest.approx(3.0)
    assert clip.rate < 1


def test_a_new_clip_takes_the_cut_look_only_where_the_look_can_grade_it():
    recipe = _montage(_recipe())
    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}

    def graded_track(track):
        clips = [clip.model_copy(update={"look": "golden_hour"}) for clip in track.clips]
        return track.model_copy(update={"clips": clips}) if track.id == "montage" else track

    fields["tracks"] = [graded_track(track) for track in recipe.tracks]
    graded = EditRecipeV2(**fields)
    new_clip = {"slot_id": "new", "in_s": 0.0, "duration_s": 2.0, "transition_after": "cut"}

    portrait = _cut(graded, [*_montage_slots(graded), {**new_clip, "clip_index": 3}], "voiceover")
    assert {clip.look for clip in _track(portrait, "montage").clips} == {"golden_hour"}

    landscape = _binding("c4")
    landscape = landscape.model_copy(
        update={"original": landscape.original.model_copy(update={"width": 1920, "height": 1080})}
    )
    with pytest.raises(UnsupportedPhonePlan):
        replace_voiceover_cut(
            graded,
            archetype="voiceover",
            slots=[*_montage_slots(graded), {**new_clip, "clip_index": 4}],
            bindings=(*_BINDINGS, landscape),
            pool=[*_POOL, landscape.proxy_path],
        )
