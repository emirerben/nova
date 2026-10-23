import pytest

pytest.importorskip("app.pipeline.phone_captions")

from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint
from app.pipeline.narrated_photo_sequence import (
    PhotoSequenceUnavailable,
    compile_narrated_photo_sequences,
    refresh_narrated_sequence_receipt,
)
from app.pipeline.phone_narrated_plan import (
    NarratedPhoneStep,
    compile_phone_narrated_plan,
    replace_editor_lanes,
)
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.pipeline.phone_subtitled_lanes import (
    PhoneSubtitledLanes,
    SubtitledLaneError,
    SubtitledOverlayCard,
)
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding


def _binding(media_id: str) -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"user/analysis-proxy-{media_id}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=4.0,
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )


def _visual(media_id: str, *, kind: str = "image", duration_s: float | None = None):
    return PhoneVisualBinding(
        media_id=media_id,
        gcs_path=f"users/owner/{media_id}.{'mp4' if kind == 'video' else 'jpg'}",
        generation="9",
        sha256=("b" if kind == "image" else "c") * 64,
        byte_count=2048,
        kind=kind,
        duration_s=duration_s,
        width=1080,
        height=1920,
    )


def _card(
    card_id: str,
    media_id: str,
    start_s: float,
    end_s: float,
    *,
    kind: str = "image",
    display_mode: str = "fullscreen",
) -> SubtitledOverlayCard:
    extension = "mp4" if kind == "video" else "jpg"
    return SubtitledOverlayCard(
        id=card_id,
        media_id=media_id,
        gcs_path=f"users/owner/{media_id}.{extension}",
        generation="9",
        start_s=start_s,
        end_s=end_s,
        kind=kind,
        display_mode=display_mode,
    )


def _recipe():
    steps = [
        NarratedPhoneStep(step_id="s0", media_id="main-0", start_s=0, end_s=4),
        NarratedPhoneStep(step_id="s1", media_id="main-1", start_s=4, end_s=8),
    ]
    return compile_phone_narrated_plan(
        steps,
        (_binding("main-0"), _binding("main-1")),
        PhoneNarrationBed(
            plan_item_id="item-1",
            generation="1",
            fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=99),
            duration_s=8,
        ),
        voiceover_duration_s=8,
        caption_cues=[{"text": "Hello there", "start_s": 0, "end_s": 2}],
        opening_title="A day out",
        opening_title_end_s=2,
    )


def _sequence_recipe(*, start=4.0, end=5.5, mode="fullscreen"):
    photo = _visual("photo")
    lanes = PhoneSubtitledLanes(
        overlays=[
            _card("beat-river-1", "photo", start, end, display_mode=mode).model_copy(
                update={"fade": False}
            )
        ]
    )
    recipe = replace_editor_lanes(_recipe(), lanes=lanes, visuals=(photo,))
    return recipe, lanes, (photo,)


def _sequence_beats(**overrides):
    return [
        {
            "beat_id": "river",
            "trigger": "the river",
            "visual_id": "photo",
            "display_mode": "fullscreen",
            "following_clip_id": "main-1",
            **overrides,
        }
    ]


@pytest.mark.parametrize("visual_id", ["photo", "asset-photo", "ASSET-photo"])
def test_photo_sequence_restarts_original_video_after_photo_and_survives_save(visual_id):
    pinned, lanes, visuals = _sequence_recipe()
    result, evidence = compile_narrated_photo_sequences(
        pinned,
        beats=_sequence_beats(visual_id=visual_id),
        bindings=(_binding("main-0"), _binding("main-1")),
    )
    main = result.tracks[0]
    assert len(main.clips) == 3
    assert main.clips[0] == pinned.tracks[0].clips[0]
    assert main.clips[-1].timeline_start == 5.5
    assert main.clips[-1].source_start == 0  # Not 1.5 consumed behind the photo.
    assert main.clips[-1].source_duration == 2.5
    assert result.tracks[1:] == pinned.tracks[1:]
    assert result.text_layers == pinned.text_layers
    assert result.audio == pinned.audio
    assert result.duration == pinned.duration == 8
    assert evidence["river"]["sequence_realized"] is True
    assert evidence["river"]["following_at_s"] == 5.5
    assert evidence["river"]["following_end_s"] == 8
    saved = replace_editor_lanes(result, lanes=lanes, visuals=visuals)
    assert saved.tracks[0] == main
    assert saved.tracks[2] == result.tracks[2]


@pytest.mark.parametrize(
    ("start", "end", "mode", "overrides", "reason"),
    [
        (4, 5.5, "pip", {}, "photo_not_placed"),
        (4, 5.5, "fullscreen", {"following_clip_id": "foreign"}, "following_clip_not_unique"),
        (4.2, 5.7, "fullscreen", {}, "following_clip_already_started"),
        (4, 4.5, "fullscreen", {}, "photo_hold_shortened"),
        (4, 8, "fullscreen", {"hold_s": 4}, "no_room_for_following_clip"),
    ],
)
def test_sequence_recovers_instead_of_claiming_wrong_output(start, end, mode, overrides, reason):
    pinned, _, _ = _sequence_recipe(start=start, end=end, mode=mode)
    before = pinned.model_dump(mode="json")
    with pytest.raises(PhotoSequenceUnavailable) as caught:
        compile_narrated_photo_sequences(
            pinned, beats=_sequence_beats(**overrides), bindings=(_binding("main-1"),)
        )
    assert caught.value.reason == reason
    assert pinned.model_dump(mode="json") == before


def test_sequence_preserves_later_pinned_ending_and_source_bounds():
    # Put the sequence in the first step; the second is the pinned ending.
    pinned, _, _ = _sequence_recipe(start=0, end=1.5)
    result, _ = compile_narrated_photo_sequences(
        pinned,
        beats=_sequence_beats(following_clip_id="main-0"),
        bindings=(_binding("main-0"), _binding("main-1")),
    )
    assert result.tracks[0].clips[-1] == pinned.tracks[0].clips[-1]
    assert all(c.source_start + c.source_duration <= 4 for c in result.tracks[0].clips)


def test_sequence_missing_photo_is_not_applied_after_lane_failure():
    with pytest.raises(PhotoSequenceUnavailable, match="full-screen photo"):
        compile_narrated_photo_sequences(
            _recipe(), beats=_sequence_beats(), bindings=(_binding("main-1"),)
        )


@pytest.mark.parametrize("wrong_part", ["photo", "video"])
def test_same_timing_wrong_media_is_rejected(wrong_part):
    recipe, _, _ = _sequence_recipe()
    requests = _sequence_beats()
    if wrong_part == "photo":
        requests[0]["visual_id"] = "bathtub-photo"
    else:
        requests[0]["following_clip_id"] = "main-0"
    # Counts, windows, narration and text are identical; identity is the only change.
    with pytest.raises(PhotoSequenceUnavailable) as caught:
        compile_narrated_photo_sequences(
            recipe, beats=requests, bindings=(_binding("main-0"), _binding("main-1"))
        )
    assert caught.value.reason == (
        "wrong_photo" if wrong_part == "photo" else "following_clip_already_started"
    )


def test_another_fullscreen_photo_cannot_hide_the_following_video():
    recipe, lanes, visuals = _sequence_recipe()
    second = _card("next", "photo", 5.5, 7)
    recipe = replace_editor_lanes(
        recipe,
        lanes=lanes.model_copy(update={"overlays": [*lanes.overlays, second]}),
        visuals=visuals,
    )
    with pytest.raises(PhotoSequenceUnavailable) as caught:
        compile_narrated_photo_sequences(
            recipe, beats=_sequence_beats(), bindings=(_binding("main-1"),)
        )
    assert caught.value.reason == "following_clip_covered"


def test_editor_receipt_rechecks_the_final_sequence_instead_of_keeping_stale_success():
    recipe, _, visuals = _sequence_recipe()
    compiled, evidence = compile_narrated_photo_sequences(
        recipe, beats=_sequence_beats(), bindings=(_binding("main-1"),)
    )
    receipt = {
        "placed": [
            {
                "beat_id": "river",
                "card_id": "beat-river-1",
                "trigger": "the river",
                "at_s": 4,
                "end_s": 5.5,
                "display_mode": "fullscreen",
                **evidence["river"],
            }
        ]
    }
    assert refresh_narrated_sequence_receipt(compiled, receipt)["placed"][0]["sequence_realized"]
    removed = replace_editor_lanes(compiled, lanes=PhoneSubtitledLanes(), visuals=visuals)
    refreshed = refresh_narrated_sequence_receipt(removed, receipt)
    assert refreshed["placed"][0]["sequence_realized"] is False
    assert receipt["placed"][0]["sequence_realized"] is True


def test_fullscreen_photo_then_video_is_a_sequence_below_pip_and_preserves_recipe():
    pinned = _recipe()
    photo = _visual("photo")
    video = _visual("video", kind="video", duration_s=3)
    pip = _visual("pip")
    lanes = PhoneSubtitledLanes(
        overlays=[
            _card("photo", "photo", 1, 3),
            _card("video", "video", 3, 6, kind="video"),
            _card("pip", "pip", 4, 5, display_mode="pip"),
        ]
    )

    swapped = replace_editor_lanes(pinned, lanes=lanes, visuals=(photo, video, pip))

    assert [track.id for track in swapped.tracks] == [
        "narrated",
        "narration",
        "subtitled-fullscreen",
        "subtitled-overlays",
    ]
    fullscreen = swapped.tracks[2]
    assert [clip.id for clip in fullscreen.clips] == [
        "subtitled-overlay-photo",
        "subtitled-overlay-video",
    ]
    assert [clip.timeline_start for clip in fullscreen.clips] == [1, 3]
    assert all(clip.visual_placement.width_fraction is None for clip in fullscreen.clips)
    assert fullscreen.clips[1].volume == 0
    assert swapped.tracks[3].clips[0].visual_placement.width_fraction is not None
    assert swapped.duration == pinned.duration
    assert swapped.audio == pinned.audio
    assert swapped.text_layers == pinned.text_layers
    assert "visualVideos" in swapped.required_capabilities


def test_fullscreen_overlap_is_rejected_without_changing_the_pinned_recipe():
    pinned = _recipe()
    first = _visual("first")
    second = _visual("second")
    lanes = PhoneSubtitledLanes(
        overlays=[_card("first", "first", 1, 4), _card("second", "second", 3, 5)]
    )

    with pytest.raises(SubtitledLaneError, match="must not overlap"):
        replace_editor_lanes(pinned, lanes=lanes, visuals=(first, second))
    assert [track.id for track in pinned.tracks] == ["narrated", "narration"]


def test_resaving_and_removing_fullscreen_cards_reclaims_assets_and_capabilities():
    pinned = _recipe()
    photo = _visual("photo")
    video = _visual("video", kind="video", duration_s=3)
    lanes = PhoneSubtitledLanes(
        overlays=[_card("photo", "photo", 1, 3), _card("video", "video", 3, 6, kind="video")]
    )
    first = replace_editor_lanes(pinned, lanes=lanes, visuals=(photo, video))
    saved_again = replace_editor_lanes(first, lanes=lanes, visuals=(photo, video))
    removed = replace_editor_lanes(saved_again, lanes=PhoneSubtitledLanes(), visuals=())

    assert [track.id for track in saved_again.tracks].count("subtitled-fullscreen") == 1
    assert [asset.id for asset in saved_again.assets].count("visual-photo") == 1
    assert [asset.id for asset in saved_again.assets].count("visual-video") == 1
    assert "visualVideos" in saved_again.required_capabilities
    assert [track.id for track in removed.tracks] == ["narrated", "narration"]
    assert not {"visual-photo", "visual-video"} & {asset.id for asset in removed.assets}
    assert "visualVideos" not in removed.required_capabilities
    assert removed.audio == pinned.audio
    assert removed.text_layers == pinned.text_layers
    assert removed.duration == pinned.duration
