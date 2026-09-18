import typing

import pytest

from app.agents._schemas.text_element import TextElement
from app.kria.device_render import recipe_digest
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.recipes import MediaCapability
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import VisualRenderAsset
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
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
        ("layout", "supporting_card"),
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


@pytest.mark.parametrize("lane,kind", [("asset", "video"), ("clip", "image")])
def test_only_asset_lane_photos_take_the_still_path(lane, kind):
    # Pool videos and clip-lane images still need a bound device original.
    plan, bindings, visuals = photo_fixture(lane=lane, kind=kind)
    with pytest.raises(ValueError, match="immutable phone source"):
        compile_phone_guided_plan(plan, bindings, visuals)


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
