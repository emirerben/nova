import typing

import pytest

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.kria.device_render import recipe_digest
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.recipes import MediaCapability, MediaTransform
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderFingerprint, VisualRenderAsset
from app.pipeline.guided_story import GuidedStoryExecutionPlan, _narration_caption_elements
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.schemas.edit_proposal import EditProposalSnapshot, NarrationTrack, NarrationWord
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding


def test_golden_hour_compiles_only_exact_canvas_and_requires_capability():
    plan, bindings = fixture()
    plan.story_timeline[0].look_preset = "golden_hour"
    with pytest.raises(ValueError, match="exact-canvas"):
        compile_phone_guided_plan(plan, bindings)
    bindings[0].original.width = 1080
    bindings[0].original.height = 1920
    recipe = compile_phone_guided_plan(plan, bindings)
    assert recipe.tracks[0].clips[0].look == "golden_hour"
    assert "goldenHourLook" in recipe.required_capabilities
    bindings[0].original.orientation_degrees = 90
    with pytest.raises(ValueError, match="exact-canvas"):
        compile_phone_guided_plan(plan, bindings)


def fixture():
    binding = PhoneSourceBinding(
        media_id="source",
        proxy_path="user/analysis-proxy-source.mp4",
        generation="123",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=10,
            width=1920,
            height=1080,
            has_audio=True,
        ),
    )
    plan = GuidedStoryExecutionPlan.model_validate(
        {
            "compiler_version": 5,
            "proposal_version": 1,
            "media_digest": "b" * 64,
            "direction": "guided_story",
            "goal": "Show the scene",
            "pace": "balanced",
            "approved_duration_s": 3,
            "resolved_duration_s": 3,
            "selected_media_ids": ["source"],
            "editor_revision_number": 1,
            "story_timeline": [
                {
                    "moment_id": "moment",
                    "beat_id": "beat",
                    "topic": "Scene",
                    "media_id": "source",
                    "lane": "clip",
                    "kind": "video",
                    "gcs_path": binding.proxy_path,
                    "generation": "123",
                    "layout": "fullscreen",
                    "source_start_s": 2,
                    "source_end_s": 5,
                    "output_start_s": 0,
                    "output_end_s": 3,
                    "duration_s": 3,
                }
            ],
            "beat_windows": [
                {
                    "beat_id": "beat",
                    "approved_duration_s": 3,
                    "resolved_duration_s": 3,
                    "start_s": 0,
                    "end_s": 3,
                }
            ],
            "text_elements": [],
            "transition_policy": {"type": "none", "duration_s": 0},
            "typography": {"style_id": "guided_story_v2", "font": "Inter"},
        }
    )
    return plan, (binding,)


def test_shared_timing_and_original_metadata_preserved():
    plan, bindings = fixture()
    recipe = compile_phone_guided_plan(plan, bindings)
    assert recipe.duration == 3
    assert recipe.tracks[0].clips[0].source_start == 2
    assert recipe.assets[0].natural_size.width == 1920
    assert recipe.audio.original_volume == 0
    assert "analysis-proxy" not in recipe.model_dump_json()
    # KRI-94: PhoneSourceBinding.require_proxy already guarantees a reserved
    # analysis proxy for every phone-bound asset reaching this compiler; it
    # should say so rather than sit at its always-false default. Font assets
    # (not phone-bound originals) correctly stay False — none are in this
    # fixture, but scope the assertion to the bound source id regardless.
    assert next(
        a for a in recipe.assets if a.id == bindings[0].render_asset().id
    ).is_proxy_available
    plan.montage_audio = {"preserve_source_audio": True}
    plan.editor_audio_level = 0.4
    assert compile_phone_guided_plan(plan, bindings).audio.original_volume == 0.4


def test_tolerates_millisecond_rounding_noise_between_stored_timing_fields():
    # `story_timeline` moments persist source/output timestamps independently
    # rounded to milliseconds. `source_end_s - source_start_s` and the stored
    # `duration_s` are two separately-rounded quantities that can legitimately
    # differ by ~1ms of compounding rounding noise -- confirmed live (job
    # a994fddd: source_duration=10.288 vs moment_duration_s=10.287, a 1ms gap
    # that a microsecond-scale tolerance rejected outright even though the
    # plan was perfectly valid.
    plan, bindings = fixture()
    # source_end(5) - source_start(2) == 3 exactly
    plan.story_timeline[0] = plan.story_timeline[0].model_copy(update={"duration_s": 3.001})
    recipe = compile_phone_guided_plan(plan, bindings)
    assert recipe.tracks[0].clips[0].source_duration == pytest.approx(3)


def test_rejects_a_timing_mismatch_larger_than_rounding_noise():
    plan, bindings = fixture()
    # 100ms off -- a real timing-program mismatch, not rounding noise
    plan.story_timeline[0] = plan.story_timeline[0].model_copy(update={"duration_s": 3.1})
    with pytest.raises(ValueError, match="exact source window"):
        compile_phone_guided_plan(plan, bindings)


def test_source_window_shifts_start_to_preserve_full_duration_when_it_fits():
    # The proxy is measured by server-side ffprobe; the original by the
    # client's on-device AVFoundation. Planning saturates a clip's
    # proxy-measured capacity (and centers shorter windows within it) when
    # the target duration demands it, so these two independent measurements
    # of the same file routinely disagree for a clip carrying a fraction of
    # a beat (observed live: jobs aeb62e3c/0e84c6f8/031c8ff0). When the full
    # requested duration still fits somewhere in the device's real file,
    # shift the start rather than truncating — a truncated moment would
    # desync from the text/audio timed against its original duration.
    plan, bindings = fixture()
    bindings[0].original.duration_s = 4.85  # moment.source_end_s == 5, overrun == 0.15s
    recipe = compile_phone_guided_plan(plan, bindings)
    clip = recipe.tracks[0].clips[0]
    # A 0.05s export safety margin comes off the available window first
    # (4.85 - 0.05 = 4.8), so the full 3s duration still fits by shifting.
    assert clip.source_start == pytest.approx(1.8)
    assert clip.source_duration == pytest.approx(3)


def test_source_window_truncates_only_when_shifting_cannot_fit_it():
    plan, bindings = fixture()
    bindings[0].original.duration_s = 2.5  # shorter than the requested 3s duration itself
    recipe = compile_phone_guided_plan(plan, bindings)
    clip = recipe.tracks[0].clips[0]
    assert clip.source_start == 0
    # 2.5 - the 0.05s safety margin = 2.45 available to fit.
    assert clip.source_duration == pytest.approx(2.45)


def test_ordinary_plan_leaves_a_title_holding_to_the_end_untouched():
    # No clip needs a refit here, so the compiled timeline matches
    # `plan.resolved_duration_s` exactly (modulo the ordinary millisecond
    # rounding noise this module already tolerates elsewhere) and a title
    # that legitimately holds to the very end of the story must compile
    # byte-identically -- the shrink clamp below must never fire here.
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(
            id="title",
            text="Title",
            start_s=0,
            end_s=plan.resolved_duration_s,
            font_family="Inter",
            effect="static",
        )
    ]
    recipe = compile_phone_guided_plan(plan, bindings)
    assert recipe.duration == pytest.approx(plan.resolved_duration_s)
    assert recipe.text_layers[0].end == pytest.approx(plan.resolved_duration_s)
    assert recipe.text_layers[0].start == 0


def test_shrunk_refit_timeline_clamps_text_layers_instead_of_failing_validation():
    # `bindings[0].original.duration_s = 2.5` shrinks the compiled clip to
    # 2.45s (see test_source_window_truncates_only_when_shifting_cannot_fit_it)
    # while `moment.output_end_s`/`plan.resolved_duration_s` -- and therefore
    # `cursor`'s own consistency check just above -- still say 3s. A title
    # compiled to hold to that nominal 3s end used to make
    # `EditRecipeV2.validate_asset_manifest` raise "text layer exceeds the
    # timeline" (prod job 71d4b358-b927-4a57-ba37-f87f5e232085). It must now
    # compile by clamping the title to the recipe's real, shrunk duration.
    plan, bindings = fixture()
    bindings[0].original.duration_s = 2.5  # shorter than the requested 3s duration itself
    plan.text_elements = [
        TextElement(
            id="title",
            text="Title",
            start_s=0,
            end_s=plan.resolved_duration_s,  # hold to the (nominal) end of the story
            font_family="Inter",
            effect="static",
        )
    ]
    recipe = compile_phone_guided_plan(plan, bindings)  # must not raise
    assert recipe.duration == pytest.approx(2.45)
    assert recipe.text_layers[0].end <= recipe.duration
    assert recipe.text_layers[0].end == pytest.approx(2.4)  # 2.45 - the 0.05s safety margin
    assert recipe.text_layers[0].start == 0


def test_shrunk_refit_timeline_shrinks_a_layer_start_too_when_it_would_go_negative_length():
    # A layer whose window sits entirely inside the clipped-off tail must
    # have its `start` pulled back too, not just its `end`, so it keeps at
    # least one frame of duration instead of becoming inverted/degenerate.
    plan, bindings = fixture()
    bindings[0].original.duration_s = 2.5  # shrinks the compiled timeline to 2.45s
    plan.text_elements = [
        TextElement(
            id="late-label",
            text="Late",
            start_s=2.9,
            end_s=3.0,
            font_family="Inter",
            effect="static",
        )
    ]
    recipe = compile_phone_guided_plan(plan, bindings)  # must not raise
    layer = recipe.text_layers[0]
    assert layer.end == pytest.approx(2.4)
    assert layer.end - layer.start == pytest.approx(1.0 / 30.0)
    assert layer.start < layer.end


def test_source_window_never_fits_exactly_to_the_device_boundary():
    # Reading a track to its precise reported duration is a classic
    # AVFoundation edge case -- the on-device export failed even after the
    # refit landed exactly on binding.original.duration_s (job 22d1ce8a: the
    # server compiled successfully but the local export then failed with no
    # output file). The refit must always leave a small safety margin.
    plan, bindings = fixture()
    bindings[0].original.duration_s = 4.999  # triggers the refit, close to the boundary
    recipe = compile_phone_guided_plan(plan, bindings)
    clip = recipe.tracks[0].clips[0]
    end = clip.source_start + clip.source_duration
    assert bindings[0].original.duration_s - end == pytest.approx(0.05)


def test_source_window_rejects_when_essentially_nothing_is_available():
    plan, bindings = fixture()
    bindings[0].original.duration_s = 0.05  # below the 0.1s viable-content floor
    with pytest.raises(ValueError, match="exact source window"):
        compile_phone_guided_plan(plan, bindings)


def transition_fixture(kind="crossfade", duration=0.3):
    plan, bindings = fixture()
    first = plan.story_timeline[0]
    first.transition_after = kind
    first.transition_duration_s = duration
    plan.story_timeline.append(
        first.model_copy(
            update={
                "moment_id": "second",
                "source_start_s": 5,
                "source_end_s": 8,
                "output_start_s": 3 - duration,
                "output_end_s": 6 - duration,
                "transition_after": "cut",
            }
        )
    )
    plan.resolved_duration_s = 6 - duration
    return plan, bindings


@pytest.mark.parametrize(
    "kind,expected",
    [("crossfade", "crossfade"), ("dip_to_black", "fade_black"), ("flash", "fade_white")],
)
def test_guided_transitions_preserve_overlap_and_source_windows(kind, expected):
    plan, bindings = transition_fixture(kind)
    recipe = compile_phone_guided_plan(plan, bindings)
    first, second = recipe.tracks[0].clips
    assert first.transition is None
    assert second.transition.kind == expected
    assert second.transition.duration == 0.3
    assert (
        first.source_start,
        first.source_duration,
        second.source_start,
        second.source_duration,
    ) == (2, 3, 5, 3)
    assert second.timeline_start == 2.7
    assert recipe.duration == 5.7
    assert (
        "crossfade" if kind == "crossfade" else "clipTransitions"
    ) in recipe.required_capabilities


def test_guided_global_transition_policy_and_explicit_cut():
    plan, bindings = transition_fixture()
    plan.story_timeline[0].transition_after = None
    plan.transition_policy.type = "crossfade"
    plan.transition_policy.duration_s = 0.3
    assert (
        compile_phone_guided_plan(plan, bindings).tracks[0].clips[1].transition.kind == "crossfade"
    )
    plan.story_timeline[0].transition_after = "cut"
    plan.story_timeline[1].output_start_s = 3
    plan.story_timeline[1].output_end_s = 6
    plan.resolved_duration_s = 6
    assert compile_phone_guided_plan(plan, bindings).tracks[0].clips[1].transition is None


def test_guided_transition_uses_cloud_short_clip_clamp():
    plan, bindings = transition_fixture()
    first, second = plan.story_timeline
    first.duration_s = 0.5
    first.source_end_s = 2.5
    first.output_end_s = 0.5
    second.duration_s = 0.5
    second.source_end_s = 5.5
    second.output_start_s = 0.35
    second.output_end_s = 0.85
    plan.resolved_duration_s = 0.85
    recipe = compile_phone_guided_plan(plan, bindings)
    assert recipe.tracks[0].clips[1].transition.duration == 0.15
    assert recipe.duration == 0.85


@pytest.mark.parametrize("change", ["audio", "missing_overlap", "submillisecond"])
def test_guided_transition_unverified_audio_or_timing_fails_closed(change):
    plan, bindings = transition_fixture()
    if change == "audio":
        plan.montage_audio = {"preserve_source_audio": True}
    elif change == "missing_overlap":
        plan.story_timeline[1].output_start_s = 3
    else:
        plan.story_timeline[0].transition_duration_s = 0.2001
    with pytest.raises(ValueError):
        compile_phone_guided_plan(plan, bindings)


def test_approved_static_text_uses_shared_layout_and_bound_font():
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(
            id="title",
            text="This view",
            start_s=0.5,
            end_s=2.5,
            font_family="Inter-Bold",
            effect="none",
            size_px=64,
        )
    ]
    recipe = compile_phone_guided_plan(plan, bindings)
    assert len(recipe.text_layers) == 1
    layer = recipe.text_layers[0]
    assert (layer.start, layer.end) == (0.5, 2.5)
    assert " ".join(run.text for run in layer.runs) == "This view"
    font = next(a for a in recipe.asset_manifest.assets if a.id == layer.runs[0].font_asset_id)
    assert font.kind == "library" and font.catalog == "font"
    assert font.fingerprint.byte_count > 1000


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", "124"),
        ("source_end_s", 11),
        ("output_start_s", 0.1),
        ("layout", "supporting_card"),
        ("look_preset", "warm"),
        ("transition_after", "crossfade"),
        ("kind", "image"),
    ],
)
def test_cannot_silently_drop_treatments_or_rebind_sources(field, value):
    plan, bindings = fixture()
    plan.story_timeline[0] = plan.story_timeline[0].model_copy(update={field: value})
    with pytest.raises(ValueError):
        compile_phone_guided_plan(plan, bindings)


@pytest.mark.parametrize(
    "lane,expected_capability",
    [
        ("editor_sound_effects", "soundEffects"),
        ("editor_media_overlays", "mediaCards"),
        ("editor_visual_blocks", "visualBlocks"),
        ("editor_motion_scenes", "motionScenes"),
        ("editor_custom_effects", "customEffects"),
    ],
)
def test_editor_lanes_cannot_disappear(lane, expected_capability):
    plan, bindings = fixture()
    setattr(plan, lane, [{"id": "required"}])
    with pytest.raises(UnsupportedPhonePlan, match=lane) as excinfo:
        compile_phone_guided_plan(plan, bindings)
    # The reject stays a hard fail-closed regardless of this attribute — it
    # is informational for future error surfacing, not a gate by itself.
    assert excinfo.value.capability == expected_capability
    assert expected_capability in typing.get_args(MediaCapability)


def test_dissolve_seed_follows_cloud_combined_lane_indices():
    plan, bindings = fixture()
    for index, lane in enumerate(
        ("text_elements", "context_label_text_elements", "narration_label_text_elements")
    ):
        setattr(
            plan,
            lane,
            [
                TextElement(
                    id=f"label-{index}",
                    text="This view",
                    start_s=0.5,
                    end_s=2.5,
                    font_family="Inter-Bold",
                    effect="dissolve-out",
                    size_px=64,
                )
            ],
        )
    recipe = compile_phone_guided_plan(plan, bindings)
    assert [layer.dissolve_seed for layer in recipe.text_layers] == [101, 138, 175]


def test_sequence_blocks_draw_above_other_lanes_without_changing_dissolve_seed():
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(
            id="seq1",
            text="First",
            role="generative_sequence",
            effect="fade-in",
            start_s=0,
            end_s=2,
            fade_out_ms=500,
        ),
        TextElement(
            id="seq2",
            text="Second",
            role="generative_sequence",
            effect="static",
            start_s=1,
            end_s=3,
            fade_out_ms=250,
        ),
    ]
    plan.context_label_text_elements = [
        TextElement(id="context", text="Context", effect="dissolve-out", start_s=0, end_s=3)
    ]
    recipe = compile_phone_guided_plan(plan, bindings)
    assert [layer.id for layer in recipe.text_layers] == ["context-0", "text-0", "text-1"]
    assert recipe.text_layers[0].dissolve_seed == 101
    assert [(layer.fade.kind, layer.fade.out_ms) for layer in recipe.text_layers[1:]] == [
        ("sequence", 500),
        ("sequence", 250),
    ]


@pytest.mark.parametrize("kind", ["crossfade", "dip_to_black", "flash"])
def test_v6_source_audio_transitions_preserve_windows_and_apply_gain_once(kind):
    plan, bindings = transition_fixture(kind, duration=0.12)
    plan.compiler_version = 6
    plan.montage_audio = {"preserve_source_audio": True}
    plan.editor_audio_level = 0.4
    recipe = compile_phone_guided_plan(plan, bindings)
    first, second = recipe.tracks[0].clips
    assert recipe.audio.original_volume == 0.4
    assert first.volume == second.volume == 1
    assert first.source_start == 2
    assert second.source_start == 5
    assert second.timeline_start == 2.88
    assert recipe.duration == 5.88
    assert "analysis-proxy" not in recipe.model_dump_json()


def test_v6_audio_transitions_still_reject_speed_changes():
    plan, bindings = transition_fixture()
    plan.compiler_version = 6
    plan.montage_audio = {"preserve_source_audio": True}
    plan.story_timeline[0].source_end_s -= 0.5
    with pytest.raises(ValueError, match="exact source window"):
        compile_phone_guided_plan(plan, bindings)


def test_frame_clocked_v2_revision_transitions_compile_from_the_laid_out_overlap():
    # 2026-09-19 (job d9a965b0): guided-editor v2 re-clocks every position onto
    # 1/30 s frames but keeps the authored 0.12 s request; the laid-out overlap
    # is 4 frames (0.133333 s). Rejecting the frame clock as "not milliseconds"
    # made every timeline save on a phone variant a 422 unsupported_phone_edit.
    plan, bindings = transition_fixture("crossfade", duration=0.12)
    overlap = round(4 / 30, 6)
    second = plan.story_timeline[1]
    second.output_start_s = round(3 - overlap, 6)
    second.output_end_s = round(6 - overlap, 6)
    plan.resolved_duration_s = second.output_end_s

    recipe = compile_phone_guided_plan(plan, bindings)

    clip = recipe.tracks[0].clips[1]
    assert clip.transition.duration == pytest.approx(overlap)
    assert clip.timeline_start == pytest.approx(3 - overlap)
    assert recipe.duration == pytest.approx(6 - overlap)


def test_transition_overlap_contradicting_the_request_by_more_than_a_frame_rejects():
    plan, bindings = transition_fixture("crossfade", duration=0.3)
    # Laid out with a 0.3 s overlap while only 0.12 s was requested.
    plan.story_timeline[0].transition_duration_s = 0.12
    with pytest.raises(ValueError, match="approved overlap"):
        compile_phone_guided_plan(plan, bindings)


def test_one_frame_source_window_disagreement_fills_the_output_slot():
    # v2 quantizes duration_s and an approval-inherited source_end_s
    # independently (7.153 -> 7.166667 vs 7.133333). The output slot wins.
    plan, bindings = fixture()
    first = plan.story_timeline[0]
    first.source_end_s = round(first.source_start_s + first.duration_s - 1 / 30, 6)

    clip = compile_phone_guided_plan(plan, bindings).tracks[0].clips[0]

    assert clip.source_duration == pytest.approx(first.duration_s)


def test_source_window_disagreement_beyond_a_frame_still_rejects():
    plan, bindings = fixture()
    first = plan.story_timeline[0]
    first.source_end_s = round(first.source_start_s + first.duration_s - 0.1, 6)
    with pytest.raises(ValueError, match="exact source window"):
        compile_phone_guided_plan(plan, bindings)


# --- KRI-121: Visuals-pool photos as fullscreen phone stills -----------------

PHOTO_ID = "5b2f9d1e-8c3a-4f6b-9e21-7a0d4c3b2a10"


def photo_fixture(**photo):
    """``fixture()``'s 3s video cut to a 2s Visuals-pool photo.

    The guided worker emits a pool photo as an asset-lane still whose source
    window is ``(0, render_s)``; ``PhoneVisualBinding`` pins its pool bytes.
    """
    plan, bindings = fixture()
    visual = PhoneVisualBinding(
        media_id=PHOTO_ID,
        gcs_path=f"users/owner/plan/item/pool/{PHOTO_ID}.jpg",
        generation="777",
        sha256="c" * 64,
        byte_count=2048,
    )
    plan.selected_media_ids.append(PHOTO_ID)
    plan.story_timeline.append(
        plan.story_timeline[0].model_copy(
            update={
                "moment_id": "photo",
                "media_id": visual.media_id,
                "lane": "asset",
                "kind": "image",
                "gcs_path": visual.gcs_path,
                "generation": visual.generation,
                "source_start_s": 0,
                "source_end_s": 2,
                "output_start_s": 3,
                "output_end_s": 5,
                "duration_s": 2,
                **photo,
            }
        )
    )
    plan.resolved_duration_s = 5
    return plan, bindings, (visual,)


def test_pool_photo_compiles_to_a_pinned_fullscreen_still():
    plan, bindings, visuals = photo_fixture()
    recipe = compile_phone_guided_plan(plan, bindings, visuals)
    video, still = recipe.tracks[0].clips
    asset = visuals[0].render_asset()
    assert asset.id == f"visual-{PHOTO_ID}"
    assert (
        still.source_asset_id,
        still.source_start,
        still.source_duration,
        still.timeline_start,
        still.rate,
    ) == (asset.id, 0, 2, 3, 1)
    # hold_duration is an editor-media field the pilot gate rejects outright.
    assert still.hold_duration is None and still.look is None and still.transition is None
    assert (video.source_start, video.source_duration) == (2, 3)
    assert asset in recipe.asset_manifest.assets
    projected = next(a for a in recipe.assets if a.id == asset.id)
    assert projected.relative_path == asset.id
    assert (projected.fingerprint.hex, projected.fingerprint.byte_count) == ("c" * 64, 2048)
    # iOS reads a still's size and orientation from the decoded image itself.
    assert projected.duration is None and projected.natural_size is None
    assert not projected.is_proxy_available
    assert "stillImages" in recipe.required_capabilities
    assert recipe.duration == 5
    document = recipe.model_dump_json()
    assert "/pool/" not in document and visuals[0].gcs_path not in document
    assert EditRecipeV2.model_validate_json(document) == recipe


def test_explicit_unit_playback_rate_is_not_a_photo_treatment():
    plan, bindings, visuals = photo_fixture(playback_rate=1.0)
    assert compile_phone_guided_plan(plan, bindings, visuals).tracks[0].clips[1].rate == 1


def test_photo_without_pinned_visuals_fails_closed_naming_still_images():
    # The worker passes no visuals while stillImages is unverified, so the
    # flag-off path must keep rejecting the whole plan rather than drop the photo.
    plan, bindings, _ = photo_fixture()
    with pytest.raises(UnsupportedPhonePlan, match="unsupported phone photo") as excinfo:
        compile_phone_guided_plan(plan, bindings)
    assert excinfo.value.capability == "stillImages"
    assert "stillImages" in typing.get_args(MediaCapability)


def test_unused_visuals_leave_a_video_only_recipe_byte_identical():
    plan, bindings = fixture()
    _, _, visuals = photo_fixture()
    with_visuals = compile_phone_guided_plan(plan, bindings, visuals)
    without = compile_phone_guided_plan(plan, bindings)
    assert with_visuals == without
    assert recipe_digest(with_visuals) == recipe_digest(without)
    assert "stillImages" not in with_visuals.required_capabilities


@pytest.mark.parametrize(
    "kind,expected,capability",
    [
        ("crossfade", "crossfade", "crossfade"),
        ("dip_to_black", "fade_black", "clipTransitions"),
        ("flash", "fade_white", "clipTransitions"),
    ],
)
def test_photo_between_videos_keeps_transitions_in_and_out(kind, expected, capability):
    plan, bindings, visuals = photo_fixture(
        output_start_s=2.7, output_end_s=4.7, transition_after=kind, transition_duration_s=0.3
    )
    first = plan.story_timeline[0]
    first.transition_after = kind
    first.transition_duration_s = 0.3
    plan.story_timeline.append(
        first.model_copy(
            update={
                "moment_id": "last",
                "source_start_s": 5,
                "source_end_s": 8,
                "output_start_s": 4.4,
                "output_end_s": 7.4,
                "transition_after": "cut",
            }
        )
    )
    plan.resolved_duration_s = 7.4
    recipe = compile_phone_guided_plan(plan, bindings, visuals)
    video, still, last = recipe.tracks[0].clips
    assert video.transition is None
    assert (still.transition.kind, still.transition.duration) == (expected, 0.3)
    assert (last.transition.kind, last.transition.duration) == (expected, 0.3)
    assert (still.source_start, still.source_duration, still.timeline_start) == (0, 2, 2.7)
    assert (last.source_start, last.source_duration) == (5, 3)
    assert last.timeline_start == pytest.approx(4.4)
    assert recipe.duration == pytest.approx(7.4)
    assert {capability, "stillImages"} <= recipe.required_capabilities


@pytest.mark.parametrize("still", [True, False])
def test_fast_cut_snap_may_move_only_a_still_off_its_nominal_source_window(still):
    # guided_story's fast-cut path keeps an image's nominal cut.source_end_s
    # while beat-snapping duration_s by up to 0.15s. A still renders for
    # duration_s regardless; a video with the same drift is a different
    # timing program and must still fail closed.
    plan, bindings, visuals = photo_fixture()
    plan.story_timeline[1 if still else 0].source_end_s += 0.15
    if not still:
        with pytest.raises(UnsupportedPhonePlan, match="exact source window"):
            compile_phone_guided_plan(plan, bindings, visuals)
        return
    clip = compile_phone_guided_plan(plan, bindings, visuals).tracks[0].clips[1]
    assert (clip.source_start, clip.source_duration) == (0, 2)


@pytest.mark.parametrize(
    "change", [{"output_start_s": 3.1, "output_end_s": 5.1}, {"output_end_s": 5.2}]
)
def test_photo_output_window_must_still_match_the_approved_timeline(change):
    plan, bindings, visuals = photo_fixture(**change)
    plan.resolved_duration_s = change["output_end_s"]
    with pytest.raises(UnsupportedPhonePlan, match="exact source window"):
        compile_phone_guided_plan(plan, bindings, visuals)


@pytest.mark.parametrize(
    "field,value",
    [
        ("image_motion", "subtle_zoom_in"),
        ("look_preset", "golden_hour"),
        ("look_preset", "warm"),
        ("look_adjustments", {"brightness": 0.1}),
        ("source_crop", {"x": 0, "y": 0, "width": 0.5, "height": 0.5}),
        ("playback_rate", 2),
    ],
)
def test_photo_treatments_the_native_still_path_cannot_draw_fail_closed(field, value):
    plan, bindings, visuals = photo_fixture(**{field: value})
    with pytest.raises(UnsupportedPhonePlan, match="unsupported phone photo treatment"):
        compile_phone_guided_plan(plan, bindings, visuals)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", "778"),
        ("gcs_path", "users/owner/plan/item/pool/other.jpg"),
        ("media_id", "6c3e0a2f-9d4b-4a7c-8f32-8b1e5d4c3b21"),
    ],
)
def test_photo_cannot_rebind_to_other_pool_bytes(field, value):
    plan, bindings, visuals = photo_fixture(**{field: value})
    with pytest.raises(ValueError, match="pinned visual") as excinfo:
        compile_phone_guided_plan(plan, bindings, visuals)
    # A drifted identity is an integrity failure, not an unsupported feature.
    assert not isinstance(excinfo.value, UnsupportedPhonePlan)


def test_only_asset_lane_photos_take_the_still_path():
    # A clip-lane image still needs a bound device original.
    plan, bindings, visuals = photo_fixture(lane="clip")
    with pytest.raises(ValueError, match="immutable phone source"):
        compile_phone_guided_plan(plan, bindings, visuals)


def test_pool_video_needs_its_own_video_visual_not_a_bound_photo():
    # Only a photo is bound (visualVideos unverified): the pool video fails
    # closed by capability instead of borrowing the photo's pinned bytes.
    plan, bindings, visuals = photo_fixture(kind="video")
    with pytest.raises(UnsupportedPhonePlan, match="unsupported phone visual video") as excinfo:
        compile_phone_guided_plan(plan, bindings, visuals)
    assert excinfo.value.capability == "visualVideos"


def test_duplicate_visual_identities_are_rejected():
    plan, bindings, (visual,) = photo_fixture()
    other = visual.model_copy(update={"generation": "778"})
    with pytest.raises(ValueError, match="unique media identities"):
        compile_phone_guided_plan(plan, bindings, (visual, other))


def test_photo_reused_across_moments_shares_one_pinned_asset():
    plan, bindings, visuals = photo_fixture()
    plan.story_timeline.append(
        plan.story_timeline[1].model_copy(
            update={"moment_id": "photo-again", "output_start_s": 5, "output_end_s": 7}
        )
    )
    plan.resolved_duration_s = 7
    recipe = compile_phone_guided_plan(plan, bindings, visuals)
    asset_id = f"visual-{PHOTO_ID}"
    assert [clip.source_asset_id for clip in recipe.tracks[0].clips[1:]] == [asset_id] * 2
    assert [a.id for a in recipe.asset_manifest.assets if isinstance(a, VisualRenderAsset)] == [
        asset_id
    ]
    assert recipe.duration == 7


# --- KRI-121 round 2: supporting-card photos ---------------------------------


def test_supporting_card_photo_compiles_to_a_still_card_clip(monkeypatch):
    plan, bindings, visuals = photo_fixture(layout="supporting_card")
    recipe = compile_phone_guided_plan(plan, bindings, visuals)
    video, card = recipe.tracks[0].clips
    assert card.still_layout == "supporting_card" and video.still_layout is None
    # The card is a layout of the same pinned still, not a new timing program.
    assert (card.source_start, card.source_duration, card.timeline_start, card.rate) == (0, 2, 3, 1)
    assert card.look is None and card.hold_duration is None
    assert card.transform == MediaTransform()
    # Same pinned bytes and download as a fullscreen still: no new capability.
    fullscreen = compile_phone_guided_plan(*photo_fixture())
    assert recipe.required_capabilities == fullscreen.required_capabilities
    assert recipe.asset_manifest == fullscreen.asset_manifest
    clips = recipe.model_dump(mode="json")["tracks"][0]["clips"]
    assert "still_layout" not in clips[0]
    assert clips[1]["still_layout"] == "supporting_card"
    # Fullscreen stills keep the exact document round 1 already issued.
    assert "still_layout" not in fullscreen.model_dump_json()
    assert recipe_digest(recipe) != recipe_digest(fullscreen)
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


@pytest.mark.parametrize(
    "field,value",
    [
        ("image_motion", "subtle_zoom_in"),
        ("look_preset", "golden_hour"),
        ("look_preset", "warm"),
        ("look_adjustments", {"brightness": 0.1}),
        ("source_crop", {"x": 0, "y": 0, "width": 0.5, "height": 0.5}),
        ("playback_rate", 2),
    ],
)
def test_supporting_card_still_rejects_treatments_the_card_cannot_draw(field, value):
    plan, bindings, visuals = photo_fixture(layout="supporting_card", **{field: value})
    with pytest.raises(UnsupportedPhonePlan, match="unsupported phone photo treatment"):
        compile_phone_guided_plan(plan, bindings, visuals)


def test_photo_used_fullscreen_and_as_a_card_shares_one_pinned_asset():
    plan, bindings, visuals = photo_fixture()
    plan.story_timeline.append(
        plan.story_timeline[1].model_copy(
            update={
                "moment_id": "photo-card",
                "layout": "supporting_card",
                "output_start_s": 5,
                "output_end_s": 7,
            }
        )
    )
    plan.resolved_duration_s = 7
    recipe = compile_phone_guided_plan(plan, bindings, visuals)
    asset_id = f"visual-{PHOTO_ID}"
    stills = recipe.tracks[0].clips[1:]
    assert [clip.source_asset_id for clip in stills] == [asset_id] * 2
    assert [clip.still_layout for clip in stills] == [None, "supporting_card"]
    assert [a.id for a in recipe.asset_manifest.assets if isinstance(a, VisualRenderAsset)] == [
        asset_id
    ]
    assert [a.id for a in recipe.assets].count(asset_id) == 1
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


# --- KRI-121 round 2: Visuals-pool videos ------------------------------------

VIDEO_ID = "7d4f1b3a-0e5c-4a8d-b9f2-1c6e8a7b5d43"


def pool_video(**changes):
    """A pinned pool video as the worker probes it: 8s, 1920x1080 stored sideways."""
    return PhoneVisualBinding(
        **{
            "media_id": VIDEO_ID,
            "gcs_path": f"users/owner/plan/item/pool/{VIDEO_ID}.mov",
            "generation": "888",
            "sha256": "d" * 64,
            "byte_count": 4096,
            "kind": "video",
            "duration_s": 8,
            "width": 1920,
            "height": 1080,
            "orientation_degrees": 90,
        }
        | changes
    )


def append_pool_video(plan, visual, **moment):
    """Append a 2s asset-lane video moment reading ``visual`` from 1s to 3s."""
    start = plan.resolved_duration_s
    plan.selected_media_ids.append(visual.media_id)
    plan.story_timeline.append(
        plan.story_timeline[0].model_copy(
            update={
                "moment_id": "pool-video",
                "media_id": visual.media_id,
                "lane": "asset",
                "kind": "video",
                "gcs_path": visual.gcs_path,
                "generation": visual.generation,
                "layout": "fullscreen",
                "source_start_s": 1,
                "source_end_s": 3,
                "output_start_s": start,
                "output_end_s": start + 2,
                "duration_s": 2,
                **moment,
            }
        )
    )
    plan.resolved_duration_s = start + 2


def pool_video_fixture(visual=None, **moment):
    """``fixture()``'s bound 3s clip cut to a 2s Visuals-pool video."""
    plan, bindings = fixture()
    pinned = pool_video(**(visual or {}))
    append_pool_video(plan, pinned, **moment)
    return plan, bindings, (pinned,)


def test_pool_video_compiles_like_bound_footage_from_its_pinned_pool_bytes():
    plan, bindings, visuals = pool_video_fixture()
    recipe = compile_phone_guided_plan(plan, bindings, visuals)
    bound, pooled = recipe.tracks[0].clips
    asset = visuals[0].render_asset()
    assert (asset.id, asset.media_kind) == (f"visual-{VIDEO_ID}", "video")
    # Unlike a still, a pool video keeps the approved source window.
    assert (
        pooled.source_asset_id,
        pooled.source_start,
        pooled.source_duration,
        pooled.timeline_start,
        pooled.rate,
    ) == (asset.id, 1, 2, 3, 1)
    assert pooled.look is None and pooled.still_layout is None and pooled.hold_duration is None
    assert (bound.source_start, bound.source_duration) == (2, 3)
    assert asset in recipe.asset_manifest.assets
    projected = next(a for a in recipe.assets if a.id == asset.id)
    assert projected.relative_path == asset.id
    assert (projected.fingerprint.hex, projected.fingerprint.byte_count) == ("d" * 64, 4096)
    assert projected.duration == 8
    assert (projected.natural_size.width, projected.natural_size.height) == (1920, 1080)
    assert projected.orientation_degrees == 90
    # Its full bytes already sit in the pool: there is no analysis proxy.
    assert not projected.is_proxy_available
    assert next(a for a in recipe.assets if a.id == "source").is_proxy_available
    assert "visualVideos" in recipe.required_capabilities
    assert "stillImages" not in recipe.required_capabilities
    assert recipe.duration == 5
    document = recipe.model_dump_json()
    assert "/pool/" not in document and visuals[0].gcs_path not in document
    assert "still_layout" not in document
    manifest = recipe.model_dump(mode="json")["asset_manifest"]["assets"]
    assert [row.get("media_kind") for row in manifest] == [None, "video"]
    assert EditRecipeV2.model_validate_json(document) == recipe


def test_photo_and_pool_video_each_require_their_own_capability():
    plan, bindings, photos = photo_fixture()
    video = pool_video()
    append_pool_video(plan, video)
    recipe = compile_phone_guided_plan(plan, bindings, (*photos, video))
    assert {"stillImages", "visualVideos"} <= recipe.required_capabilities
    assert [
        a.media_kind for a in recipe.asset_manifest.assets if isinstance(a, VisualRenderAsset)
    ] == ["image", "video"]
    assert recipe.duration == 7


@pytest.mark.parametrize("bind_photo", [False, True])
def test_pool_video_without_a_pinned_video_fails_closed_naming_visual_videos(bind_photo):
    # The worker binds videos only while visualVideos is verified; a verified
    # stillImages alone must not let a pool video through.
    plan, bindings, _ = pool_video_fixture()
    visuals = photo_fixture()[2] if bind_photo else ()
    with pytest.raises(UnsupportedPhonePlan, match="unsupported phone visual video") as excinfo:
        compile_phone_guided_plan(plan, bindings, visuals)
    assert excinfo.value.capability == "visualVideos"
    assert "visualVideos" in typing.get_args(MediaCapability)


def test_photo_without_a_pinned_photo_fails_closed_even_when_a_video_is_bound():
    plan, bindings, _ = photo_fixture()
    with pytest.raises(UnsupportedPhonePlan, match="unsupported phone photo") as excinfo:
        compile_phone_guided_plan(plan, bindings, (pool_video(),))
    assert excinfo.value.capability == "stillImages"


def test_unused_pool_videos_leave_a_video_only_recipe_byte_identical():
    plan, bindings = fixture()
    visuals = (*photo_fixture()[2], pool_video())
    with_visuals = compile_phone_guided_plan(plan, bindings, visuals)
    without = compile_phone_guided_plan(plan, bindings)
    assert with_visuals == without
    assert recipe_digest(with_visuals) == recipe_digest(without)
    assert not {"stillImages", "visualVideos"} & with_visuals.required_capabilities
    assert "media_kind" not in with_visuals.model_dump_json()


def pool_only_video_fixture(duration_s):
    """``fixture()``'s single moment re-sourced from a pool video, same window."""
    plan, _ = fixture()
    visual = pool_video(duration_s=duration_s)
    plan.selected_media_ids = [visual.media_id]
    plan.story_timeline[0] = plan.story_timeline[0].model_copy(
        update={
            "media_id": visual.media_id,
            "lane": "asset",
            "gcs_path": visual.gcs_path,
            "generation": visual.generation,
        }
    )
    return plan, (visual,)


@pytest.mark.parametrize(
    "duration_s,expected",
    [
        (10, (2, 3)),
        # Shifted to keep the full 3s: 4.85 - the 0.05s safety margin = 4.8.
        (4.85, (1.8, 3)),
        (4.999, (1.949, 3)),
        # Shorter than the moment itself: truncated to 2.5 - 0.05.
        (2.5, (0, 2.45)),
    ],
)
def test_pool_video_window_refits_against_the_probed_duration_like_bound_footage(
    duration_s, expected
):
    plan, visuals = pool_only_video_fixture(duration_s)
    pooled = compile_phone_guided_plan(plan, (), visuals)
    clip = pooled.tracks[0].clips[0]
    assert (clip.source_start, clip.source_duration) == pytest.approx(expected)
    plan, bindings = fixture()
    bindings[0].original.duration_s = duration_s
    bound = compile_phone_guided_plan(plan, bindings)
    reference = bound.tracks[0].clips[0]
    assert (clip.source_start, clip.source_duration) == (
        reference.source_start,
        reference.source_duration,
    )
    assert pooled.duration == bound.duration


def test_pool_video_window_rejects_when_essentially_nothing_is_available():
    plan, visuals = pool_only_video_fixture(0.05)
    with pytest.raises(UnsupportedPhonePlan, match="exact source window"):
        compile_phone_guided_plan(plan, (), visuals)


def test_pool_video_timing_must_preserve_its_source_window():
    # Only a still may drift off its nominal window; a pool video is footage.
    plan, bindings, visuals = pool_video_fixture(source_end_s=3.15)
    with pytest.raises(UnsupportedPhonePlan, match="exact source window"):
        compile_phone_guided_plan(plan, bindings, visuals)


@pytest.mark.parametrize("duration_s,projected", [(1800, 1800), (1800.5, None), (2400, None)])
def test_long_pool_recording_omits_the_informational_asset_duration(duration_s, projected):
    # The pool accepts recordings past MediaAsset.duration's 30-minute bound;
    # the device reads the real duration from the file's own track.
    plan, bindings, visuals = pool_video_fixture(visual={"duration_s": duration_s})
    recipe = compile_phone_guided_plan(plan, bindings, visuals)
    asset = next(a for a in recipe.assets if a.id == f"visual-{VIDEO_ID}")
    assert asset.duration == projected
    assert asset.natural_size is not None
    clip = recipe.tracks[0].clips[1]
    assert (clip.source_start, clip.source_duration) == (1, 2)
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_golden_hour_pool_video_compiles_only_exact_canvas_unrotated():
    exact = {"width": 1080, "height": 1920, "orientation_degrees": 0}
    plan, bindings, visuals = pool_video_fixture(visual=exact, look_preset="golden_hour")
    recipe = compile_phone_guided_plan(plan, bindings, visuals)
    assert recipe.tracks[0].clips[1].look == "golden_hour"
    assert {"goldenHourLook", "visualVideos"} <= recipe.required_capabilities
    for change in (
        {"orientation_degrees": 90},
        {"width": 1920, "height": 1080},
        # Sideways storage that displays as the canvas is still not exact.
        {"width": 1920, "height": 1080, "orientation_degrees": 90},
    ):
        plan, bindings, visuals = pool_video_fixture(
            visual=exact | change, look_preset="golden_hour"
        )
        with pytest.raises(UnsupportedPhonePlan, match="exact-canvas"):
            compile_phone_guided_plan(plan, bindings, visuals)


@pytest.mark.parametrize(
    "field,value",
    [
        ("layout", "supporting_card"),
        ("image_motion", "subtle_zoom_in"),
        ("look_preset", "warm"),
        ("look_adjustments", {"brightness": 0.1}),
        ("source_crop", {"x": 0, "y": 0, "width": 0.5, "height": 0.5}),
        ("playback_rate", 2),
    ],
)
def test_pool_video_treatments_the_recipe_cannot_carry_fail_closed(field, value):
    plan, bindings, visuals = pool_video_fixture(**{field: value})
    with pytest.raises(UnsupportedPhonePlan, match="unsupported phone moment treatment"):
        compile_phone_guided_plan(plan, bindings, visuals)


def test_explicit_unit_playback_rate_is_not_a_video_treatment():
    plan, bindings, visuals = pool_video_fixture(playback_rate=1.0)
    plan.story_timeline[0].playback_rate = 1.0
    recipe = compile_phone_guided_plan(plan, bindings, visuals)
    assert recipe_digest(recipe) == recipe_digest(compile_phone_guided_plan(*pool_video_fixture()))


@pytest.mark.parametrize(
    "field,value",
    [("source_crop", {"x": 0, "y": 0, "width": 0.5, "height": 0.5}), ("playback_rate", 2)],
)
def test_bound_footage_crop_and_retime_fail_closed_instead_of_silently_dropping(field, value):
    # Both compiled before, rendering uncropped 1x footage the creator never approved.
    plan, bindings = fixture()
    plan.story_timeline[0] = plan.story_timeline[0].model_copy(update={field: value})
    with pytest.raises(UnsupportedPhonePlan, match="unsupported phone moment treatment"):
        compile_phone_guided_plan(plan, bindings)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", "889"),
        ("gcs_path", "users/owner/plan/item/pool/other.mov"),
        ("media_id", "6c3e0a2f-9d4b-4a7c-8f32-8b1e5d4c3b21"),
    ],
)
def test_pool_video_cannot_rebind_to_other_pool_bytes(field, value):
    plan, bindings, visuals = pool_video_fixture(**{field: value})
    with pytest.raises(ValueError, match="pinned visual") as excinfo:
        compile_phone_guided_plan(plan, bindings, visuals)
    assert not isinstance(excinfo.value, UnsupportedPhonePlan)


@pytest.mark.parametrize("moment_kind", ["video", "image"])
def test_visual_bound_under_the_other_kinds_moment_is_an_integrity_failure(moment_kind):
    # Both kinds are bound, so the capability check passes; the moment then
    # names the identity of a visual of the other kind.
    plan, bindings, (photo,) = photo_fixture()
    video = pool_video()
    append_pool_video(plan, video)
    wrong = photo if moment_kind == "video" else video
    index = 2 if moment_kind == "video" else 1
    plan.story_timeline[index] = plan.story_timeline[index].model_copy(
        update={
            "media_id": wrong.media_id,
            "gcs_path": wrong.gcs_path,
            "generation": wrong.generation,
        }
    )
    with pytest.raises(ValueError, match="pinned visual kind") as excinfo:
        compile_phone_guided_plan(plan, bindings, (photo, video))
    assert not isinstance(excinfo.value, UnsupportedPhonePlan)


def test_pool_video_crossfades_into_a_photo():
    plan, bindings, (video,) = pool_video_fixture(
        transition_after="crossfade", transition_duration_s=0.3
    )
    _, _, (photo,) = photo_fixture()
    plan.selected_media_ids.append(photo.media_id)
    plan.story_timeline.append(
        plan.story_timeline[1].model_copy(
            update={
                "moment_id": "photo",
                "media_id": photo.media_id,
                "kind": "image",
                "gcs_path": photo.gcs_path,
                "generation": photo.generation,
                "layout": "supporting_card",
                "source_start_s": 0,
                "source_end_s": 2,
                "output_start_s": 4.7,
                "output_end_s": 6.7,
                "transition_after": "cut",
                "transition_duration_s": None,
            }
        )
    )
    plan.resolved_duration_s = 6.7
    recipe = compile_phone_guided_plan(plan, bindings, (video, photo))
    bound, pooled, card = recipe.tracks[0].clips
    assert bound.transition is None and pooled.transition is None
    assert (card.transition.kind, card.transition.duration) == ("crossfade", 0.3)
    assert (pooled.source_start, pooled.source_duration, pooled.timeline_start) == (1, 2, 3)
    assert (card.source_start, card.source_duration) == (0, 2)
    assert card.timeline_start == pytest.approx(4.7)
    assert card.still_layout == "supporting_card"
    assert recipe.duration == pytest.approx(6.7)
    assert {"crossfade", "stillImages", "visualVideos"} <= recipe.required_capabilities


def pool_video_crossfading_into_a_photo(visual_duration_s, source_start_s, source_end_s):
    """The bound clip, then a pool video window that crossfades (0.3s) into a 2s photo."""
    length = source_end_s - source_start_s
    plan, bindings, (video,) = pool_video_fixture(
        visual={"duration_s": visual_duration_s},
        source_start_s=source_start_s,
        source_end_s=source_end_s,
        output_end_s=3 + length,
        duration_s=length,
        transition_after="crossfade",
        transition_duration_s=0.3,
    )
    _, _, (photo,) = photo_fixture()
    start = 3 + length - 0.3
    plan.selected_media_ids.append(photo.media_id)
    plan.story_timeline.append(
        plan.story_timeline[1].model_copy(
            update={
                "moment_id": "photo",
                "media_id": photo.media_id,
                "kind": "image",
                "gcs_path": photo.gcs_path,
                "generation": photo.generation,
                "source_start_s": 0,
                "source_end_s": 2,
                "output_start_s": start,
                "output_end_s": start + 2,
                "duration_s": 2,
                "transition_after": "cut",
                "transition_duration_s": None,
            }
        )
    )
    plan.resolved_duration_s = start + 2
    return plan, bindings, (video, photo)


@pytest.mark.parametrize(
    "visual_duration_s, window, compiled",
    [
        # The planner used the whole file; bound at that same duration, no refit.
        (4, (0, 4), (0, 4)),
        # A refit that only shifts the start keeps the clip playing through the fade.
        (4.5, (0.6, 4.6), (0.45, 4)),
    ],
    ids=["reaches-the-end", "shifted"],
)
def test_pool_video_keeps_its_full_window_into_a_crossfade(visual_duration_s, window, compiled):
    plan, bindings, visuals = pool_video_crossfading_into_a_photo(visual_duration_s, *window)
    recipe = compile_phone_guided_plan(plan, bindings, visuals)
    _, pooled, card = recipe.tracks[0].clips
    assert (pooled.source_start, pooled.source_duration) == pytest.approx(compiled)
    assert pooled.timeline_start + pooled.source_duration == pytest.approx(
        card.timeline_start + card.transition.duration
    )


def test_refit_that_shortens_a_pool_video_before_a_crossfade_is_refused():
    # A 3.97s file under a 4s window refits to 3.92s and would end at 6.92,
    # before the photo's fade completes at 7.0: Composition.swift throws
    # invalidTimeline, so the phone would fail the export after approval.
    plan, bindings, visuals = pool_video_crossfading_into_a_photo(3.97, 0, 4)
    with pytest.raises(UnsupportedPhonePlan, match="transition needs the full source window"):
        compile_phone_guided_plan(plan, bindings, visuals)


def test_refit_that_shortens_bound_footage_before_a_transition_is_refused():
    plan, bindings = transition_fixture()
    bindings[0].original.duration_s = 2.5  # the first 3s window refits to 2.45s
    with pytest.raises(UnsupportedPhonePlan, match="transition needs the full source window"):
        compile_phone_guided_plan(plan, bindings)
    # At a cut the same shortened clip still compiles, as before.
    plan.story_timeline[0].transition_after = "cut"
    plan.story_timeline[1].output_start_s = 3
    plan.story_timeline[1].output_end_s = 6
    plan.resolved_duration_s = 6
    compile_phone_guided_plan(plan, bindings)


@pytest.mark.parametrize("window", [(1850, 1852), (1800.5, 1802.5), (1799, 1801)])
def test_pool_video_window_past_the_device_source_limit_is_refused_up_front(window):
    # Not the recipe's pydantic ValidationError: the job fails naming the reason.
    start, end = window
    plan, bindings, visuals = pool_video_fixture(
        visual={"duration_s": 2400}, source_start_s=start, source_end_s=end
    )
    with pytest.raises(UnsupportedPhonePlan, match="30-minute limit"):
        compile_phone_guided_plan(plan, bindings, visuals)


def test_pool_video_window_ending_at_the_device_source_limit_compiles():
    plan, bindings, visuals = pool_video_fixture(
        visual={"duration_s": 2400}, source_start_s=1798, source_end_s=1800
    )
    clip = compile_phone_guided_plan(plan, bindings, visuals).tracks[0].clips[1]
    assert (clip.source_start, clip.source_duration) == (1798, 2)


# --- KRI-121 round 2: projects with no device footage at all ------------------


def visuals_only_fixture(with_video):
    """A photo (then optionally a pool video) and no bound device original."""
    plan, _, (photo,) = photo_fixture(output_start_s=0, output_end_s=2)
    del plan.story_timeline[0]
    plan.selected_media_ids = [photo.media_id]
    plan.resolved_duration_s = 2
    visuals = (photo,)
    if with_video:
        video = pool_video()
        # `append_pool_video` copies the first moment, here the photo.
        append_pool_video(plan, video)
        visuals = (photo, video)
    return plan, visuals


@pytest.mark.parametrize("with_video", [False, True])
def test_visuals_only_plan_compiles_with_zero_bindings(with_video):
    plan, visuals = visuals_only_fixture(with_video)
    recipe = compile_phone_guided_plan(plan, (), visuals)
    clips = recipe.tracks[0].clips
    assert [clip.source_asset_id for clip in clips] == [
        visual.render_asset().id for visual in visuals
    ]
    assert all(isinstance(a, VisualRenderAsset) for a in recipe.asset_manifest.assets)
    assert not any(a.is_proxy_available for a in recipe.assets)
    assert ("visualVideos" in recipe.required_capabilities) == with_video
    assert "stillImages" in recipe.required_capabilities
    assert recipe.duration == (4 if with_video else 2)
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_round_two_leaves_already_issued_recipe_digests_unchanged():
    # Measured at c7bcdf4be (round 1). A device recipe is pinned by digest, so
    # adding still_layout / media_kind must not move a video-only or a
    # fullscreen-photo recipe that carries neither.
    assert (
        recipe_digest(compile_phone_guided_plan(*fixture()))
        == "5d9e6a37aae08622226ce4c2d60329e8f33bdc634d5fe103a0739ecb3a24f6db"
    )
    assert (
        recipe_digest(compile_phone_guided_plan(*photo_fixture()))
        == "06ed2d33f8522743f7aca40eb7abfd3a68fcaaa21600538de5688eb27bacda3d"
    )


# --- KRI-132 phone-voiceover-gate follow-up: guided-story narration lane ----
#
# The voiceover-timed guided story (execution contract `guided_voiceover_v1`)
# is the only lane that combines Visuals-pool media with a recorded
# voiceover. `plan.narration` pins a generation-pinned `NarrationTrack`; the
# compiler now projects it into a `VoiceoverRenderAsset` + a second
# `TimelineTrack(id="narration", kind="audio")`, hard-replacing the footage's
# own audio exactly like `_mix_pinned_narration` does on the cloud.

NARRATION_GCS_PATH = "voiceover-uploads/direct/owner/item/voice.m4a"
NARRATION_GENERATION = "voice-generation-9"
NARRATION_PLAN_ITEM_ID = "narration-item-1"
NARRATION_PHOTO_ID_1 = "9b1e5f2a-6c3d-4e8f-9a1b-2c3d4e5f6a71"
NARRATION_PHOTO_ID_2 = "9b1e5f2a-6c3d-4e8f-9a1b-2c3d4e5f6a72"


def narration_track(duration_s: float = 8.0) -> NarrationTrack:
    """Six ordered words comfortably inside an 8s track (canonical duration
    at 30fps is exactly 8.0s -- see `canonical_narration_duration_s`)."""
    words = [
        NarrationWord(text="This", start_s=0.1, end_s=0.4),
        NarrationWord(text="is", start_s=0.4, end_s=0.6),
        NarrationWord(text="a", start_s=0.6, end_s=0.7),
        NarrationWord(text="recorded", start_s=0.7, end_s=1.2),
        NarrationWord(text="voiceover", start_s=1.2, end_s=1.8),
        NarrationWord(text="story", start_s=1.8, end_s=2.2),
    ]
    return NarrationTrack(
        gcs_path=NARRATION_GCS_PATH,
        generation=NARRATION_GENERATION,
        duration_s=duration_s,
        words=words,
        language="en",
    )


def narration_bed(**changes) -> PhoneNarrationBed:
    return PhoneNarrationBed(
        **(
            {
                "plan_item_id": NARRATION_PLAN_ITEM_ID,
                "generation": NARRATION_GENERATION,
                "fingerprint": RenderFingerprint(sha256="d" * 64, byte_count=999_000),
                "duration_s": 8.0,
            }
            | changes
        )
    )


def narration_fixture():
    """Two video moments + two Visuals photos tiling the exact canonical
    narration duration (8s), a title, and pinned-narration captions -- the
    shape an approved voiceover-timed guided story actually compiles.
    Returns (plan, bindings, visuals, bed).
    """
    narration = narration_track()
    binding_1 = PhoneSourceBinding(
        media_id="source-1",
        proxy_path="user/analysis-proxy-source-1.mp4",
        generation="123",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=5,
            width=1920,
            height=1080,
            has_audio=True,
        ),
    )
    binding_2 = PhoneSourceBinding(
        media_id="source-2",
        proxy_path="user/analysis-proxy-source-2.mp4",
        generation="123",
        original=OriginalMediaDescriptor(
            sha256="b" * 64,
            byte_count=1000,
            duration_s=5,
            width=1920,
            height=1080,
            has_audio=True,
        ),
    )
    visual_1 = PhoneVisualBinding(
        media_id=NARRATION_PHOTO_ID_1,
        gcs_path=f"users/owner/plan/item/pool/{NARRATION_PHOTO_ID_1}.jpg",
        generation="777",
        sha256="c" * 64,
        byte_count=2048,
    )
    visual_2 = PhoneVisualBinding(
        media_id=NARRATION_PHOTO_ID_2,
        gcs_path=f"users/owner/plan/item/pool/{NARRATION_PHOTO_ID_2}.jpg",
        generation="778",
        sha256="e" * 64,
        byte_count=4096,
    )
    # `_narration_caption_elements` only reads `.narration` and `.font_family`
    # off its snapshot argument -- `model_construct` skips validating (and
    # therefore requiring) `EditProposalSnapshot`'s many unrelated fields.
    snapshot = EditProposalSnapshot.model_construct(narration=narration, font_family=None)
    title = TextElement(
        id="title",
        text="A day well spent",
        start_s=0,
        end_s=8,
        font_family="Inter-Bold",
        effect="static",
    ).model_dump(mode="json", exclude_none=True)
    captions = _narration_caption_elements(snapshot)
    assert len(captions) >= 6  # six distinct words, none merged as point timestamps
    plan = GuidedStoryExecutionPlan.model_validate(
        {
            "compiler_version": 6,
            "proposal_version": 1,
            "media_digest": "f" * 64,
            "direction": "guided_story",
            "goal": "Show the day",
            "pace": "balanced",
            "approved_duration_s": 8,
            "resolved_duration_s": 8,
            "selected_media_ids": [
                "source-1",
                "source-2",
                NARRATION_PHOTO_ID_1,
                NARRATION_PHOTO_ID_2,
            ],
            "story_timeline": [
                {
                    "moment_id": "moment-1",
                    "beat_id": "beat",
                    "topic": "Morning",
                    "media_id": "source-1",
                    "lane": "clip",
                    "kind": "video",
                    "gcs_path": binding_1.proxy_path,
                    "generation": "123",
                    "layout": "fullscreen",
                    "source_start_s": 0,
                    "source_end_s": 2,
                    "output_start_s": 0,
                    "output_end_s": 2,
                    "duration_s": 2,
                },
                {
                    "moment_id": "moment-2",
                    "beat_id": "beat",
                    "topic": "Afternoon",
                    "media_id": "source-2",
                    "lane": "clip",
                    "kind": "video",
                    "gcs_path": binding_2.proxy_path,
                    "generation": "123",
                    "layout": "fullscreen",
                    "source_start_s": 0,
                    "source_end_s": 2,
                    "output_start_s": 2,
                    "output_end_s": 4,
                    "duration_s": 2,
                },
                {
                    "moment_id": "moment-3",
                    "beat_id": "beat",
                    "topic": "Evening",
                    "media_id": NARRATION_PHOTO_ID_1,
                    "lane": "asset",
                    "kind": "image",
                    "gcs_path": visual_1.gcs_path,
                    "generation": visual_1.generation,
                    "layout": "fullscreen",
                    "source_start_s": 0,
                    "source_end_s": 2,
                    "output_start_s": 4,
                    "output_end_s": 6,
                    "duration_s": 2,
                },
                {
                    "moment_id": "moment-4",
                    "beat_id": "beat",
                    "topic": "Night",
                    "media_id": NARRATION_PHOTO_ID_2,
                    "lane": "asset",
                    "kind": "image",
                    "gcs_path": visual_2.gcs_path,
                    "generation": visual_2.generation,
                    "layout": "fullscreen",
                    "source_start_s": 0,
                    "source_end_s": 2,
                    "output_start_s": 6,
                    "output_end_s": 8,
                    "duration_s": 2,
                },
            ],
            "beat_windows": [
                {
                    "beat_id": "beat",
                    "approved_duration_s": 8,
                    "resolved_duration_s": 8,
                    "start_s": 0,
                    "end_s": 8,
                }
            ],
            "text_elements": [title, *captions],
            "transition_policy": {"type": "none", "duration_s": 0},
            "typography": {"style_id": "guided_story_v2", "font": "Inter"},
            "narration": narration.model_dump(mode="json"),
        }
    )
    return plan, (binding_1, binding_2), (visual_1, visual_2), narration_bed()


def test_narration_plan_compiles_a_hard_replace_audio_track_and_stills():
    plan, bindings, visuals, bed = narration_fixture()
    recipe = compile_phone_guided_plan(plan, bindings, visuals, narration=bed)

    assert recipe.duration == pytest.approx(plan.resolved_duration_s)
    audio_tracks = [track for track in recipe.tracks if track.kind == "audio"]
    assert len(audio_tracks) == 1
    narration_track_clips = audio_tracks[0].clips
    assert len(narration_track_clips) == 1
    voice_clip = narration_track_clips[0]
    assert (voice_clip.source_start, voice_clip.source_duration, voice_clip.volume) == (
        0,
        pytest.approx(8),
        1,
    )
    voice_asset_id = f"voiceover-{NARRATION_PLAN_ITEM_ID}"
    assert voice_clip.source_asset_id == voice_asset_id
    # Hard replace: no ducking, no gain, no matched original-audio bed.
    assert recipe.audio.original_volume == 0
    assert recipe.audio.narration_asset_id == voice_asset_id
    assert {"narrationAudio", "audioMix"} <= recipe.required_capabilities
    asset = next(a for a in recipe.asset_manifest.assets if a.id == voice_asset_id)
    assert asset.kind == "voiceover"
    assert asset.plan_item_id == NARRATION_PLAN_ITEM_ID
    assert (asset.fingerprint.sha256, asset.fingerprint.byte_count) == ("d" * 64, 999_000)
    # Stills still compile exactly as the KRI-121 photo lane always has.
    video_clips = recipe.tracks[0].clips
    assert len(video_clips) == 4
    assert [clip.still_layout for clip in video_clips] == [None, None, None, None]
    assert "stillImages" in recipe.required_capabilities
    # Title + all six caption groups made it through, in chronological order.
    assert recipe.text_layers[0].id == "text-0"
    caption_layers = recipe.text_layers[1:]
    assert len(caption_layers) == len(plan.text_elements) - 1
    starts = [layer.start for layer in caption_layers]
    assert starts == sorted(starts)
    for layer in recipe.text_layers:
        assert layer.end <= recipe.duration
    # Capability-gate verification (phone_render_verified_features) is
    # exercised separately below.
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_narration_recipe_passes_the_pilot_gate_once_capabilities_are_verified(monkeypatch):
    plan, bindings, visuals, bed = narration_fixture()
    recipe = compile_phone_guided_plan(plan, bindings, visuals, narration=bed)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)  # must not raise


def test_narration_plan_without_a_bed_fails_closed():
    plan, bindings, visuals, _bed = narration_fixture()
    with pytest.raises(UnsupportedPhonePlan, match="narration binding") as excinfo:
        compile_phone_guided_plan(plan, bindings, visuals)
    assert excinfo.value.capability == "narrationAudio"


def test_narration_bed_generation_mismatch_fails_closed():
    plan, bindings, visuals, _bed = narration_fixture()
    stale = narration_bed(generation="stale-generation")
    with pytest.raises(UnsupportedPhonePlan, match="replaced since approval") as excinfo:
        compile_phone_guided_plan(plan, bindings, visuals, narration=stale)
    assert excinfo.value.capability == "narrationAudio"


def test_narration_bed_duration_mismatch_fails_closed():
    plan, bindings, visuals, _bed = narration_fixture()
    drifted = narration_bed(duration_s=8.5)  # > 0.05s tolerance
    with pytest.raises(UnsupportedPhonePlan, match="replaced since approval") as excinfo:
        compile_phone_guided_plan(plan, bindings, visuals, narration=drifted)
    assert excinfo.value.capability == "narrationAudio"


def test_narration_bed_within_tolerance_still_compiles():
    plan, bindings, visuals, _bed = narration_fixture()
    close_enough = narration_bed(duration_s=8.04)  # inside the 0.05s tolerance
    compile_phone_guided_plan(plan, bindings, visuals, narration=close_enough)  # must not raise


def test_bed_given_without_plan_narration_fails_closed():
    plan, bindings = fixture()  # the plain fixture -- plan.narration is None
    with pytest.raises(UnsupportedPhonePlan, match="no narration") as excinfo:
        compile_phone_guided_plan(plan, bindings, narration=narration_bed())
    assert excinfo.value.capability == "narrationAudio"
