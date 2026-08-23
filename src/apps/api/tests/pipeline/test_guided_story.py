from __future__ import annotations

import copy
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents._schemas.text_element import TextElement
from app.pipeline.generative_overlays import build_overlays_from_text_elements
from app.pipeline.guided_story import (
    GuidedStoryError,
    _compile_execution_plan_version,
    _download_selected,
    _mix_pinned_music,
    _render_moments,
    _render_video_moment,
    _upload_verified_outputs,
    _verify_receipt,
    compile_execution_plan,
    compile_guided_runtime_plan,
    validate_execution_plan,
    validate_proposal_timing,
    validate_ready_result,
    verify_guided_text_reburn,
)
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)
from app.schemas.guided_edit_revision import guided_editor_revision_from_approval


def _guided_snapshot(*, direction: str = "guided_story", catalog_extra: bool = False) -> dict:
    media = [
        MediaRef(
            lane="clip",
            media_id="coast-video",
            gcs_path="users/u/coast.mp4",
            generation="11",
            kind="video",
            duration_s=12,
            analysis={
                "subject": "coast",
                "description": "turquoise sea and a small boat",
                "best_moments": [{"start_s": 2, "end_s": 10, "description": "boat"}],
            },
        ),
        MediaRef(
            lane="asset",
            media_id="food-photo",
            gcs_path="users/u/food.jpg",
            generation="12",
            kind="image",
            analysis={"subject": "food", "description": "ice cream"},
        ),
        MediaRef(
            lane="asset",
            media_id="town-photo",
            gcs_path="users/u/town.jpg",
            generation="13",
            kind="image",
            analysis={"subject": "architecture", "description": "old town street"},
        ),
    ]
    if catalog_extra:
        media.append(
            MediaRef(
                lane="asset",
                media_id="unused-photo",
                gcs_path="users/u/unused.jpg",
                generation="14",
                kind="image",
            )
        )
    snapshot = EditProposalSnapshot(
        direction=direction,
        goal="Explain what stood out in Corfu",
        pace="balanced",
        duration_s=18,
        title="What Corfu felt like",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id="food",
                topic="Food",
                thought="Small treats made the hot afternoons better.",
                media_ids=["food-photo"],
                layout="supporting_card",
                duration_s=4,
            ),
            StoryBeat(
                beat_id="town",
                topic="Architecture",
                thought="The old streets reward slow wandering.",
                media_ids=["town-photo"],
                duration_s=4,
            ),
            StoryBeat(
                beat_id="coast",
                topic="Coast",
                thought="The water changes the pace of the whole day.",
                media_ids=["coast-video"],
                duration_s=4,
            ),
        ],
    )
    return {
        "proposal_version": 7,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                "lane": ref.lane,
                "media_id": ref.media_id,
                "gcs_path": ref.gcs_path,
                "generation": ref.generation,
                "kind": ref.kind,
            }
            for ref in media
        ],
    }


def test_proposal_timing_validator_rejects_unrenderable_revision() -> None:
    raw = _guided_snapshot(direction="text_explainer")
    snapshot = EditProposalSnapshot.model_validate(raw["approved_proposal"])
    impossible = snapshot.model_copy(
        update={
            "duration_s": 10,
            "story_beats": [
                StoryBeat(
                    beat_id="first",
                    topic="First",
                    media_ids=["coast-video", "food-photo", "town-photo"],
                    duration_s=4,
                ),
                StoryBeat(
                    beat_id="second",
                    topic="Second",
                    media_ids=["coast-video", "food-photo", "town-photo"],
                    duration_s=4,
                ),
            ],
        }
    )
    with pytest.raises(GuidedStoryError, match="too short to show all approved media"):
        validate_proposal_timing(impossible)


def test_proposal_timing_validator_ignores_malformed_best_moments() -> None:
    raw = _guided_snapshot()
    snapshot = EditProposalSnapshot.model_validate(raw["approved_proposal"])
    snapshot.media[0].analysis["best_moments"] = ["not an object", None]

    validate_proposal_timing(snapshot)


def test_compiler_uses_only_beat_selected_media_and_hits_target_duration() -> None:
    raw = _guided_snapshot(catalog_extra=True)
    plan = compile_execution_plan(raw, track=None)

    assert plan["selected_media_ids"] == ["food-photo", "town-photo", "coast-video"]
    assert "unused-photo" not in {row["media_id"] for row in plan["story_timeline"]}
    assert plan["resolved_duration_s"] == 18
    assert plan["compiler_version"] == 4
    assert plan["proposal_version"] == 7
    assert [row["beat_id"] for row in plan["beat_windows"]] == ["food", "town", "coast"]
    assert {row["layout"] for row in plan["story_timeline"]} == {
        "fullscreen",
        "supporting_card",
    }
    # Title and first thought occupy different vertical positions, so both can
    # remain readable for the full first beat.  Delaying the thought until the
    # title ended reduced short montage labels to a single frame.
    first_thought = next(row for row in plan["text_elements"] if row["id"] == "guided-thought-food")
    assert first_thought["start_s"] == 0.0
    assert first_thought["end_s"] == plan["beat_windows"][0]["end_s"]


def test_fast_montage_compiles_exact_source_windows_and_optional_beats() -> None:
    raw = _guided_snapshot(direction="fast_montage")
    proposal = EditProposalSnapshot.model_validate(raw["approved_proposal"])
    proposal.duration_s = 3
    proposal.fast_cuts = [
        FastMontageCut(
            cut_id="cut-1",
            media_id="coast-video",
            source_start_s=2.0,
            source_end_s=2.8,
            output_duration_s=0.8,
            role="hook",
            beat_align=True,
        ),
        FastMontageCut(
            cut_id="cut-2",
            media_id="food-photo",
            source_start_s=0.0,
            source_end_s=0.8,
            output_duration_s=0.8,
            role="build",
        ),
        FastMontageCut(
            cut_id="cut-3",
            media_id="town-photo",
            source_start_s=0.0,
            source_end_s=0.8,
            output_duration_s=0.8,
            role="build",
            beat_align=True,
        ),
        FastMontageCut(
            cut_id="cut-4",
            media_id="coast-video",
            source_start_s=6.0,
            source_end_s=6.6,
            output_duration_s=0.6,
            role="payoff",
        ),
    ]
    raw["approved_proposal"] = proposal.model_dump(mode="json")
    raw["media_digest"] = canonical_media_digest(proposal.media)

    plan = compile_execution_plan(
        raw,
        track={
            "track_id": "track-1",
            "title": "Corfu beat",
            "audio_gcs_path": "music/corfu.mp3",
            "generation": "1",
            "start_s": 0.0,
            "beat_timestamps_s": [0.79, 2.41],
        },
    )

    moments = plan["story_timeline"]
    assert plan["selected_media_ids"] == ["coast-video", "food-photo", "town-photo"]
    assert [(row["source_start_s"], row["source_end_s"]) for row in moments] == [
        (2.0, 2.79),
        (0.0, 0.8),
        (0.0, 0.8),
        (6.0, 6.59),
    ]
    assert moments[0]["output_end_s"] == pytest.approx(0.79, abs=0.001)
    assert moments[0]["beat_align"] is True
    assert moments[0]["beat_time_s"] == pytest.approx(0.79, abs=0.001)
    assert moments[1]["beat_align"] is False
    assert moments[1].get("beat_time_s") is None
    assert moments[2]["beat_time_s"] == pytest.approx(2.41, abs=0.001)
    assert all(row["duration_s"] >= 0.4 for row in moments)
    assert plan["resolved_duration_s"] == pytest.approx(3, abs=0.001)
    assert plan["transition_policy"] == {"type": "none", "duration_s": 0.0}
    assert len(plan["text_elements"]) == 1
    assert validate_execution_plan(plan, raw) == plan


def test_fast_montage_none_policy_resolves_to_hard_cut_boundaries() -> None:
    from app.pipeline import guided_story

    plan = {
        "transition_policy": {"type": "none", "duration_s": 0.0},
        "story_timeline": [{}, {}, {}],
    }

    assert guided_story._resolved_transition_boundaries(plan) == ["cut", "cut"]


def _orientation_snapshot(aspects: list[float], durations: list[float] | None = None) -> dict:
    durations = durations or [2.0] * len(aspects)
    media = [
        MediaRef(
            lane="clip",
            media_id=f"summer-{index}",
            gcs_path=f"users/u/summer-{index}.mp4",
            generation=str(index),
            kind="video",
            duration_s=20,
            aspect=aspect,
            analysis={"width": round(aspect * 1080), "height": 1080},
        )
        for index, aspect in enumerate(aspects, start=1)
    ]
    snapshot = EditProposalSnapshot(
        duration_s=max(10, round(sum(durations))),
        title="Summer 26",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id=f"place-{index}",
                topic=f"Place {index}",
                thought=f"Place {index}",
                media_ids=[ref.media_id],
                duration_s=duration,
            )
            for index, (ref, duration) in enumerate(zip(media, durations, strict=True), start=1)
        ],
    )
    return {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                "lane": ref.lane,
                "media_id": ref.media_id,
                "gcs_path": ref.gcs_path,
                "generation": ref.generation,
                "kind": ref.kind,
            }
            for ref in media
        ],
    }


def test_summer_26_five_landscape_sources_auto_select_landscape() -> None:
    plan = compile_execution_plan(_orientation_snapshot([1.7778] * 5), track=None)

    assert plan["output_orientation"] == "landscape"
    assert "10.0s landscape, 0.0s portrait" in plan["output_orientation_reason"]


def test_all_portrait_sources_auto_select_portrait() -> None:
    plan = compile_execution_plan(_orientation_snapshot([0.5625] * 5), track=None)

    assert plan["output_orientation"] == "portrait"
    assert "0.0s landscape, 10.0s portrait" in plan["output_orientation_reason"]


def test_mixed_media_uses_duration_weight_and_first_source_tie_break() -> None:
    dominant = compile_execution_plan(
        _orientation_snapshot([1.7778, 0.5625, 0.5625], [6, 2, 2]), track=None
    )
    tied = compile_execution_plan(_orientation_snapshot([0.5625, 1.7778], [5, 5]), track=None)

    assert dominant["output_orientation"] == "landscape"
    assert tied["output_orientation"] == "portrait"


def test_legacy_execution_plan_without_orientation_remains_valid_portrait() -> None:
    raw = _guided_snapshot()
    legacy = _compile_execution_plan_version(raw, track=None, compiler_version=2)
    assert legacy["typography"] == {"style_id": "guided_story_v1", "font": "Inter-Bold"}
    assert legacy["text_elements"][0]["stroke_width"] == 5
    assert legacy["text_elements"][1]["stroke_width"] == 4
    legacy.pop("output_orientation")
    legacy.pop("output_orientation_reason")

    validated = validate_execution_plan(legacy, raw)

    assert validated["output_orientation"] == "portrait"
    assert "Legacy guided stories" in validated["output_orientation_reason"]


def test_compiler_persists_editorial_text_defaults_without_strokes() -> None:
    plan = compile_execution_plan(_guided_snapshot(), track=None)

    assert plan["typography"] == {"style_id": "guided_story_v2", "font": "Fraunces"}
    title, *thoughts = plan["text_elements"]
    assert {
        "font_family": title["font_family"],
        "size_px": title["size_px"],
        "color": title["color"],
        "highlight_color": title["highlight_color"],
        "stroke_width": title["stroke_width"],
        "shadow_enabled": title["shadow_enabled"],
        "shadow_style": title["shadow_style"],
        "position": title["position"],
        "x_frac": title["x_frac"],
        "y_frac": title["y_frac"],
        "max_width_frac": title["max_width_frac"],
    } == {
        "font_family": "Fraunces",
        "size_px": 104.0,
        "color": "#FFF8F0",
        "highlight_color": "#D9FF70",
        "stroke_width": 0.0,
        "shadow_enabled": True,
        "shadow_style": "standard",
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": 0.16,
        "max_width_frac": 0.8,
    }
    assert thoughts
    assert all(element["font_family"] == "DM Sans" for element in thoughts)
    assert all(element["size_px"] == 60.0 for element in thoughts)
    assert all(element["stroke_width"] == 0.0 for element in thoughts)
    assert all(element["shadow_style"] == "standard" for element in thoughts)
    assert all(element["y_frac"] == 0.8 for element in thoughts)


def test_guided_story_text_defaults_reach_burn_dict_without_strokes() -> None:
    from app.pipeline import text_overlay_skia

    plan = compile_execution_plan(_guided_snapshot(), track=None)
    elements = [TextElement.model_validate(row) for row in plan["text_elements"]]

    overlays = build_overlays_from_text_elements(
        elements,
        video_duration_s=plan["resolved_duration_s"],
        independent_box_alignment=True,
    )

    assert len(overlays) == len(elements)
    assert [overlay["font_family"] for overlay in overlays] == [
        "Fraunces",
        "DM Sans",
        "DM Sans",
        "DM Sans",
    ]
    assert all(overlay["stroke_width"] == 0 for overlay in overlays)
    assert all(overlay["text_color"] == "#FFF8F0" for overlay in overlays)
    assert all(overlay["highlight_color"] == "#D9FF70" for overlay in overlays)
    assert all(overlay["shadow_enabled"] is True for overlay in overlays)
    assert all(overlay["shadow_style"] == "standard" for overlay in overlays)
    assert [overlay["position_y_frac"] for overlay in overlays] == [0.16, 0.8, 0.8, 0.8]

    title_resolution = text_overlay_skia.resolved_typeface_for_overlay(overlays[0])
    thought_resolution = text_overlay_skia.resolved_typeface_for_overlay(overlays[1])
    assert (title_resolution["name"], title_resolution["file"]) == (
        "Fraunces",
        "Fraunces-Bold.ttf",
    )
    assert (thought_resolution["name"], thought_resolution["file"]) == (
        "DM Sans",
        "DMSans-Bold.ttf",
    )
    assert title_resolution["source"] == thought_resolution["source"] == "font_family"
    assert title_resolution["fallback"] is thought_resolution["fallback"] is False


def test_guided_story_long_thought_wraps_without_shrinking_to_caption_size() -> None:
    from app.pipeline import text_overlay_skia

    plan = compile_execution_plan(_guided_snapshot(), track=None)
    thought = TextElement.model_validate(plan["text_elements"][1]).model_copy(
        update={
            "text": (
                "The streets looked different at every turn, especially in the quiet hour "
                "before dinner."
            )
        }
    )
    [overlay] = build_overlays_from_text_elements(
        [thought],
        video_duration_s=plan["resolved_duration_s"],
        independent_box_alignment=True,
    )
    resolution = text_overlay_skia._resolve_typeface_for_overlay(overlay)
    max_width = 1080 * overlay["max_width_frac"]
    font, size, lines = text_overlay_skia._shrink_to_fit(
        overlay["text"],
        resolution.typeface,
        overlay["text_size_px"],
        max_width,
    )

    assert size == 60
    assert 2 <= len(lines) <= 4
    assert max(font.measureText(line) for line in lines) <= max_width


@pytest.mark.parametrize(
    ("direction", "minimum", "transition"),
    [
        ("guided_story", 1.4, "crossfade"),
        ("fast_montage", 0.8, "none"),
        ("text_explainer", 1.8, "crossfade"),
    ],
)
def test_direction_policy_is_persisted(direction: str, minimum: float, transition: str) -> None:
    raw = _guided_snapshot(direction=direction)
    plan = compile_execution_plan(raw, track=None)
    assert min(row["duration_s"] for row in plan["story_timeline"]) >= minimum
    assert plan["transition_policy"]["type"] == transition
    if direction == "text_explainer":
        assert max(row["size_px"] for row in plan["text_elements"][1:]) == 64


def test_execution_plan_is_fenced_to_approval_version_and_digest() -> None:
    raw = _guided_snapshot()
    plan = compile_execution_plan(
        raw,
        track={
            "track_id": "track-1",
            "title": "Dreamy",
            "audio_gcs_path": "music/dreamy.m4a",
            "generation": "123",
            "start_s": 12.5,
        },
    )
    assert validate_execution_plan(plan, raw) == plan

    changed = copy.deepcopy(raw)
    changed["proposal_version"] = 8
    with pytest.raises(GuidedStoryError, match="no longer matches approval") as exc:
        validate_execution_plan(plan, changed)
    assert exc.value.code == "guided_story_snapshot_invalid"


def test_execution_plan_accepts_v1_timing_across_compiler_upgrade() -> None:
    raw = _guided_snapshot()
    raw["approved_proposal"]["duration_s"] = 17
    raw["approved_proposal"]["story_beats"][0]["media_ids"] = [
        "food-photo",
        "town-photo",
    ]
    plan = _compile_execution_plan_version(raw, track=None, compiler_version=1)

    first_beat = [row for row in plan["story_timeline"] if row["beat_id"] == "food"]
    assert [row["duration_s"] for row in first_beat] == [2.953, 2.954]
    assert validate_execution_plan(plan, raw) == plan


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan["story_timeline"][0].update(gcs_path="users/other.jpg"),
        lambda plan: plan["story_timeline"][0].update(moment_id="changed-moment"),
        lambda plan: plan["story_timeline"][0].update(
            source_start_s=1.0,
            source_end_s=2.0,
            output_start_s=1.0,
            output_end_s=2.0,
            duration_s=1.0,
        ),
    ],
)
def test_execution_plan_v1_compatibility_still_rejects_semantic_drift(mutate) -> None:
    raw = _guided_snapshot()
    plan = _compile_execution_plan_version(raw, track=None, compiler_version=1)
    mutate(plan)

    with pytest.raises(GuidedStoryError) as exc:
        validate_execution_plan(plan, raw)

    assert exc.value.code == "guided_story_snapshot_invalid"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan["beat_windows"].pop(),
        lambda plan: plan["story_timeline"][0].update(gcs_path="users/other.jpg"),
        lambda plan: plan["text_elements"][0].update(text="Rewritten after approval"),
    ],
)
def test_execution_plan_rejects_any_semantic_drift_from_approval(mutate) -> None:
    raw = _guided_snapshot()
    plan = compile_execution_plan(raw, track=None)
    mutate(plan)

    with pytest.raises(GuidedStoryError) as exc:
        validate_execution_plan(plan, raw)

    assert exc.value.code == "guided_story_snapshot_invalid"


def test_compiler_fails_instead_of_dropping_media_when_duration_is_too_short() -> None:
    # Pinned to v3: v4's cross-beat water-fill (see test_undershooting_beat_
    # weights_never_inflate_past_clip_capacity) can legitimately satisfy a
    # severely underweighted beat's floor by borrowing headroom from another
    # beat -- a different, intentional capability, not a regression of this
    # naive per-beat-ratio scenario.
    raw = _guided_snapshot(direction="text_explainer")
    proposal = raw["approved_proposal"]
    proposal["story_beats"][0]["media_ids"] = ["food-photo", "town-photo", "coast-video"]
    proposal["story_beats"][0]["duration_s"] = 1
    proposal["story_beats"][1]["duration_s"] = 12
    proposal["story_beats"] = proposal["story_beats"][:2]
    proposal["duration_s"] = 10

    with pytest.raises(GuidedStoryError, match="too short") as exc:
        _compile_execution_plan_version(raw, track=None, compiler_version=3)
    assert exc.value.code == "guided_story_duration_impossible"


def test_runtime_compiles_approved_unused_image_and_video_sources() -> None:
    guided = _guided_snapshot(catalog_extra=True)
    snapshot = EditProposalSnapshot.model_validate(guided["approved_proposal"])
    unused_video = MediaRef(
        lane="clip",
        media_id="unused-video",
        gcs_path="users/u/unused.mp4",
        generation="15",
        kind="video",
        duration_s=6.0,
    )
    snapshot = snapshot.model_copy(update={"media": [*snapshot.media, unused_video]})
    guided["approved_proposal"] = snapshot.model_dump(mode="json")
    guided["media_digest"] = canonical_media_digest(snapshot.media)
    guided["media_identities"] = [
        {
            "lane": ref.lane,
            "media_id": ref.media_id,
            "gcs_path": ref.gcs_path,
            "generation": ref.generation,
            "kind": ref.kind,
        }
        for ref in snapshot.media
    ]
    canonical = compile_execution_plan(guided, track=None)
    revision = guided_editor_revision_from_approval(
        proposal_version=guided["proposal_version"],
        media_digest=guided["media_digest"],
        snapshot=guided["approved_proposal"],
        execution_plan=canonical,
    )
    revision["segments"].extend(
        [
            {
                "segment_id": "added-image",
                "media_id": "unused-photo",
                "source_start_s": 0.0,
                "source_end_s": 3.0,
                "duration_s": 3.0,
            },
            {
                "segment_id": "added-video",
                "media_id": "unused-video",
                "source_start_s": 0.0,
                "source_end_s": 3.0,
                "duration_s": 3.0,
            },
        ]
    )
    revision["state_hash"] = ""

    runtime = compile_guided_runtime_plan(canonical, guided, revision)
    by_id = {moment["moment_id"]: moment for moment in runtime["story_timeline"]}

    assert by_id["added-image"]["layout"] == "fullscreen"
    assert by_id["added-image"]["image_motion"] == "subtle_zoom_in"
    assert by_id["added-video"]["layout"] == "fullscreen"
    assert by_id["added-video"]["image_motion"] is None


def test_runtime_revision_preserves_looks_transition_order_and_music_window() -> None:
    guided = _guided_snapshot(catalog_extra=True)
    canonical = compile_execution_plan(guided, track=None)
    revision = guided_editor_revision_from_approval(
        proposal_version=guided["proposal_version"],
        media_digest=guided["media_digest"],
        snapshot=guided["approved_proposal"],
        execution_plan=canonical,
    )

    revision["segments"][0].update(
        {
            "look_preset": "olive_film",
            "look_adjustments": {
                "intensity": 0.7,
                "warmth": 0.3,
                "contrast": -0.2,
                "grain": 0.1,
                "vignette": 0.15,
            },
            "transition_after": "crossfade",
            "transition_duration_s": 0.2,
        }
    )
    revision["segments"][1].update(
        {
            # Move the second segment to the overlap boundary.  The compiler
            # must preserve this order and frame-quantized overlap.
            "output_start_s": 3.8,
            "look_preset": "smoky_split_tone",
            "look_adjustments": {
                "intensity": 0.6,
                "warmth": -0.2,
                "contrast": 0.25,
                "grain": 0.2,
                "vignette": 0.3,
            },
            "transition_after": "cut",
            "transition_duration_s": 0.0,
        }
    )
    revision["audio"] = {
        "mode": "track",
        "track_id": "replacement-track",
        "title": "Replacement track",
        "audio_gcs_path": "music/replacement.m4a",
        "generation": "track-generation-9",
        "start_s": 1.25,
        "end_s": 99.0,
        "level": 0.35,
    }
    revision["state_hash"] = ""

    runtime = compile_guided_runtime_plan(canonical, guided, revision)
    moments = runtime["story_timeline"]

    assert [moment["moment_id"] for moment in moments] == [
        segment["segment_id"] for segment in revision["segments"]
    ]
    assert moments[0]["look_preset"] == "olive_film"
    assert moments[0]["look_adjustments"]["warmth"] == pytest.approx(0.3)
    assert moments[1]["look_preset"] == "smoky_split_tone"
    assert moments[1]["transition_after"] == "cut"
    assert moments[0]["transition_after"] == "crossfade"
    assert moments[0]["output_end_s"] - moments[1]["output_start_s"] == pytest.approx(0.2)
    assert runtime["music"]["track_id"] == "replacement-track"
    assert runtime["music"]["audio_gcs_path"] == "music/replacement.m4a"
    assert runtime["music"]["generation"] == "track-generation-9"
    assert runtime["music"]["start_s"] == pytest.approx(1.266667)
    assert runtime["music"]["end_s"] == pytest.approx(
        runtime["music"]["start_s"] + runtime["resolved_duration_s"], abs=1e-3
    )
    assert runtime["music"]["level"] == pytest.approx(0.35)
    assert [row["generation"] for row in runtime["editor_source_pool"]] == [
        row["generation"] for row in guided["approved_proposal"]["media"]
    ]
    assert runtime["editor_revision_number"] == 1
    assert len(runtime["editor_revision_hash"]) == 64


def test_runtime_revision_removes_music_and_receipt_records_v2_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.pipeline import guided_story

    guided = _guided_snapshot(catalog_extra=True)
    canonical = compile_execution_plan(guided, track=None)
    revision = guided_editor_revision_from_approval(
        proposal_version=guided["proposal_version"],
        media_digest=guided["media_digest"],
        snapshot=guided["approved_proposal"],
        execution_plan=canonical,
    )
    revision["audio"] = {"mode": "none", "removed": True}
    revision["state_hash"] = ""
    runtime = compile_guided_runtime_plan(canonical, guided, revision)

    final = tmp_path / "guided-final.mp4"
    final.write_bytes(b"deterministic-v2-output")
    monkeypatch.setattr(
        guided_story,
        "probe_video",
        lambda _path: SimpleNamespace(
            duration_s=runtime["resolved_duration_s"], width=1080, height=1920, codec="h264"
        ),
    )
    monkeypatch.setattr(
        guided_story,
        "_story_canvas",
        lambda _orientation: SimpleNamespace(width=1080, height=1920),
    )
    monkeypatch.setattr(guided_story, "_audio_codec", lambda _path: "aac")
    monkeypatch.setattr(guided_story, "_sha256", lambda _path: "b" * 64)

    moment_receipts = [
        {
            "moment_id": moment["moment_id"],
            "beat_id": moment["beat_id"],
            "media_id": moment["media_id"],
            "generation": moment["generation"],
            "kind": moment["kind"],
            "layout": moment["layout"],
            "image_motion": moment.get("image_motion"),
        }
        for moment in runtime["story_timeline"]
    ]
    media_receipts = [
        {
            "media_id": media_id,
            "gcs_path": next(
                moment["gcs_path"]
                for moment in runtime["story_timeline"]
                if moment["media_id"] == media_id
            ),
            "generation": next(
                moment["generation"]
                for moment in runtime["story_timeline"]
                if moment["media_id"] == media_id
            ),
            "kind": next(
                moment["kind"]
                for moment in runtime["story_timeline"]
                if moment["media_id"] == media_id
            ),
        }
        for media_id in runtime["selected_media_ids"]
    ]
    text_receipts = [
        {"element_id": element["id"], "visible": True} for element in runtime["text_elements"]
    ]

    receipt = _verify_receipt(
        runtime,
        media_receipts,
        moment_receipts,
        text_receipts,
        str(final),
        music_applied=False,
    )

    assert receipt["schema_version"] == 2
    assert receipt["revision_number"] == runtime["editor_revision_number"]
    assert receipt["revision_hash"] == runtime["editor_revision_hash"]
    assert receipt["source_pool"] == runtime["editor_source_pool"]
    assert receipt["segment_order"] == [moment["moment_id"] for moment in moment_receipts]
    assert receipt["music_removed"] is True
    assert receipt["music"] is None


def test_render_moments_forwards_segment_looks_to_image_and_video_renderers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guided = _guided_snapshot()
    canonical = compile_execution_plan(guided, track=None)
    revision = guided_editor_revision_from_approval(
        proposal_version=guided["proposal_version"],
        media_digest=guided["media_digest"],
        snapshot=guided["approved_proposal"],
        execution_plan=canonical,
    )
    revision["segments"][0]["look_preset"] = "olive_film"
    revision["segments"][0]["look_adjustments"] = {
        "intensity": 0.8,
        "warmth": 0.2,
        "contrast": 0.1,
        "grain": 0.1,
        "vignette": 0.2,
    }
    revision["segments"][2]["look_preset"] = "smoky_split_tone"
    revision["segments"][2]["look_adjustments"] = {
        "intensity": 0.5,
        "warmth": -0.1,
        "contrast": 0.2,
        "grain": 0.2,
        "vignette": 0.3,
    }
    revision["state_hash"] = ""
    runtime = compile_guided_runtime_plan(canonical, guided, revision)
    image_moment = next(moment for moment in runtime["story_timeline"] if moment["kind"] == "image")
    video_moment = next(moment for moment in runtime["story_timeline"] if moment["kind"] == "video")
    runtime["story_timeline"] = [image_moment, video_moment]

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "app.pipeline.guided_story._render_image_moment",
        lambda *_args, **kwargs: calls.append(("image", kwargs)),
    )
    monkeypatch.setattr(
        "app.pipeline.guided_story._render_video_moment",
        lambda *_args, **kwargs: calls.append(("video", kwargs)),
    )
    probe_index = 0

    def probe_rendered(_path):
        nonlocal probe_index
        duration = runtime["story_timeline"][probe_index]["duration_s"]
        probe_index += 1
        return SimpleNamespace(duration_s=duration, width=1080, height=1920, codec="h264")

    monkeypatch.setattr("app.pipeline.guided_story.probe_video", probe_rendered)
    monkeypatch.setattr("app.pipeline.guided_story._sha256", lambda _path: "c" * 64)

    outputs, _receipts = _render_moments(
        runtime,
        {
            moment["media_id"]: f"{moment['media_id']}.source"
            for moment in runtime["story_timeline"]
        },
        str(tmp_path),
    )

    assert [Path(path).name for path in outputs] == ["moment_00.mp4", "moment_01.mp4"]
    assert calls[0][0] == "image"
    assert calls[0][1]["duration_s"] == image_moment["duration_s"]
    assert calls[0][1]["layout"] == image_moment["layout"]
    assert calls[0][1]["look_preset"] == "olive_film"
    assert calls[1][0] == "video"
    assert calls[1][1]["look_preset"] == "smoky_split_tone"
    assert calls[1][1]["look_adjustments"] == video_moment["look_adjustments"]


def test_compiler_gives_short_video_its_available_time_and_redistributes_beat() -> None:
    raw = _guided_snapshot()
    proposal = raw["approved_proposal"]
    coast = next(row for row in proposal["media"] if row["media_id"] == "coast-video")
    coast["duration_s"] = 1.966667
    coast["analysis"]["duration_s"] = 1.966667
    coast["analysis"]["best_moments"] = []
    proposal["duration_s"] = 12
    proposal["story_beats"] = [
        {
            "beat_id": "old-town",
            "topic": "Old Town",
            "thought": "The old streets reward slow wandering.",
            "media_ids": ["food-photo", "town-photo", "coast-video"],
            "layout": "fullscreen",
            "duration_s": 10,
        },
        {
            "beat_id": "closing",
            "topic": "Closing",
            "thought": "One last look before leaving.",
            "media_ids": ["food-photo"],
            "layout": "fullscreen",
            "duration_s": 2,
        },
    ]

    plan = compile_execution_plan(raw, track=None)

    moments = plan["story_timeline"]
    video = next(row for row in moments if row["media_id"] == "coast-video")
    assert moments.index(video) < len(moments) - 1
    assert video["source_end_s"] <= 1.967
    assert video["duration_s"] >= 1.4
    assert plan["beat_windows"][0]["resolved_duration_s"] == 10
    assert plan["resolved_duration_s"] == 12


@pytest.mark.parametrize(
    ("direction", "media_ids", "video_duration_s", "transition_type"),
    [
        ("guided_story", ["food-photo", "coast-video"], 1.4, "crossfade"),
        ("fast_montage", ["coast-video", "food-photo"], 0.8, "none"),
    ],
)
def test_compiler_accepts_video_at_minimum_without_transition_overlap(
    direction: str,
    media_ids: list[str],
    video_duration_s: float,
    transition_type: str,
) -> None:
    raw = _guided_snapshot(direction=direction)
    proposal = raw["approved_proposal"]
    coast = next(row for row in proposal["media"] if row["media_id"] == "coast-video")
    coast["duration_s"] = video_duration_s
    coast["analysis"]["duration_s"] = video_duration_s
    coast["analysis"]["best_moments"] = []
    proposal["duration_s"] = 10
    proposal["story_beats"] = [
        {
            "beat_id": "boundary",
            "topic": "Boundary",
            "thought": "Every approved source remains visible.",
            "media_ids": media_ids,
            "layout": "fullscreen",
            "duration_s": 10,
        }
    ]

    plan = compile_execution_plan(raw, track=None)

    video = next(row for row in plan["story_timeline"] if row["media_id"] == "coast-video")
    assert video["duration_s"] == pytest.approx(video_duration_s, abs=0.001)
    assert video["source_end_s"] <= video_duration_s + 0.001
    assert plan["transition_policy"]["type"] == transition_type
    assert plan["resolved_duration_s"] == 10


def test_compiler_rejects_selected_video_without_duration() -> None:
    # Pinned to v3: v4's beat-window pre-clamp reaches the generic per-beat
    # floor check in _allocate_beat_durations before its capacity loop, so the
    # specific "no usable duration" message is only guaranteed at this
    # explicit version. Both versions still raise guided_story_duration_
    # impossible -- only the message text differs.
    raw = _guided_snapshot()
    proposal = raw["approved_proposal"]
    coast = next(row for row in proposal["media"] if row["media_id"] == "coast-video")
    coast["duration_s"] = None

    with pytest.raises(GuidedStoryError, match="no usable duration") as exc:
        _compile_execution_plan_version(raw, track=None, compiler_version=3)

    assert exc.value.code == "guided_story_duration_impossible"


def test_compiler_rejects_video_too_short_after_transition_overlap() -> None:
    # Pinned to v3 for the same reason as test_compiler_rejects_selected_
    # video_without_duration above -- the specific message is a v3-scoped
    # guarantee; v4 still rejects this, just via the generic beat-floor
    # message.
    raw = _guided_snapshot()
    proposal = raw["approved_proposal"]
    coast = next(row for row in proposal["media"] if row["media_id"] == "coast-video")
    coast["duration_s"] = 1.45
    proposal["duration_s"] = 10
    proposal["story_beats"] = [
        {
            "beat_id": "short-video",
            "topic": "Short video",
            "thought": "A quick glimpse.",
            "media_ids": ["coast-video"],
            "layout": "fullscreen",
            "duration_s": 5,
        },
        {
            "beat_id": "closing",
            "topic": "Closing",
            "thought": "One last look.",
            "media_ids": ["food-photo"],
            "layout": "fullscreen",
            "duration_s": 5,
        },
    ]

    with pytest.raises(GuidedStoryError, match="too short to show clearly") as exc:
        _compile_execution_plan_version(raw, track=None, compiler_version=3)

    assert exc.value.code == "guided_story_duration_impossible"


def test_compiler_rejects_all_video_beat_without_enough_total_footage() -> None:
    raw = _guided_snapshot()
    proposal = raw["approved_proposal"]
    for row in proposal["media"]:
        row["kind"] = "video"
        row["duration_s"] = 2.0
    for row in raw["media_identities"]:
        row["kind"] = "video"
    media = [MediaRef.model_validate(row) for row in proposal["media"]]
    raw["media_digest"] = canonical_media_digest(media)
    proposal["duration_s"] = 10
    proposal["story_beats"] = [
        {
            "beat_id": "all-video",
            "topic": "All video",
            "thought": "Every approved clip should remain visible.",
            "media_ids": ["food-photo", "town-photo", "coast-video"],
            "layout": "fullscreen",
            "duration_s": 10,
        }
    ]

    with pytest.raises(GuidedStoryError, match="longer than its approved videos") as exc:
        compile_execution_plan(raw, track=None)

    assert exc.value.code == "guided_story_duration_impossible"


def _clamped_beat_snapshot() -> dict:
    """A beat-weight-undershoots-total snapshot (prod job 0be72363 shape).

    weight_total (6) < duration_s (12), so the naive ratio formula inflates
    every beat's resolved_beat_s by the same 2x factor. The video beat's
    clip sits right at its real capacity (3.9s) -- just under the 4.0s the
    naive ratio would demand -- so v3's per-beat allocator cannot fit it,
    while v4's beat-window water-fill clamps it to capacity and gives the
    saved time to the (uncapped) image beats instead.
    """
    raw = _guided_snapshot()
    proposal = raw["approved_proposal"]
    coast = next(row for row in proposal["media"] if row["media_id"] == "coast-video")
    coast["duration_s"] = 3.9
    coast["analysis"]["duration_s"] = 3.9
    coast["analysis"]["best_moments"] = []
    proposal["duration_s"] = 12
    proposal["story_beats"] = [
        {
            "beat_id": "food",
            "topic": "Food",
            "thought": "Small treats made the hot afternoons better.",
            "media_ids": ["food-photo"],
            "layout": "fullscreen",
            "duration_s": 2,
        },
        {
            "beat_id": "town",
            "topic": "Architecture",
            "thought": "The old streets reward slow wandering.",
            "media_ids": ["town-photo"],
            "layout": "fullscreen",
            "duration_s": 2,
        },
        {
            "beat_id": "coast",
            "topic": "Coast",
            "thought": "The water changes the pace of the whole day.",
            "media_ids": ["coast-video"],
            "layout": "fullscreen",
            "duration_s": 2,
        },
    ]
    return raw


def test_undershooting_beat_weights_never_inflate_past_clip_capacity() -> None:
    raw = _clamped_beat_snapshot()

    plan = _compile_execution_plan_version(raw, track=None, compiler_version=4)

    assert plan["resolved_duration_s"] == raw["approved_proposal"]["duration_s"]
    by_media = {row["media_id"]: row for row in raw["approved_proposal"]["media"]}
    for row in plan["story_timeline"]:
        if row["kind"] != "video":
            continue
        ref_duration_s = float(by_media[row["media_id"]]["duration_s"])
        assert row["source_end_s"] <= ref_duration_s + 0.001


def test_v3_still_fails_where_v4_redistributes() -> None:
    raw = _clamped_beat_snapshot()

    with pytest.raises(GuidedStoryError, match="longer than its approved videos") as exc:
        _compile_execution_plan_version(raw, track=None, compiler_version=3)

    assert exc.value.code == "guided_story_duration_impossible"


def test_execution_plan_accepts_v3_timing_across_compiler_upgrade() -> None:
    raw = _guided_snapshot()
    plan = _compile_execution_plan_version(raw, track=None, compiler_version=3)

    assert plan["compiler_version"] == 3
    assert validate_execution_plan(plan, raw) == plan


def test_compiler_beat_windows_still_sum_to_the_approved_total_when_clamped() -> None:
    raw = _clamped_beat_snapshot()

    plan = _compile_execution_plan_version(raw, track=None, compiler_version=4)

    beat_windows = plan["beat_windows"]
    assert sum(row["resolved_duration_s"] for row in beat_windows) == pytest.approx(
        raw["approved_proposal"]["duration_s"], abs=0.001
    )
    for index in range(1, len(beat_windows)):
        assert beat_windows[index]["start_s"] == beat_windows[index - 1]["end_s"]


def test_compiler_still_rejects_a_total_no_beat_can_hold() -> None:
    raw = _guided_snapshot()
    proposal = raw["approved_proposal"]
    for row in proposal["media"]:
        row["kind"] = "video"
        row["duration_s"] = 2.0
    for row in raw["media_identities"]:
        row["kind"] = "video"
    media = [MediaRef.model_validate(row) for row in proposal["media"]]
    raw["media_digest"] = canonical_media_digest(media)
    proposal["duration_s"] = 10
    proposal["story_beats"] = [
        {
            "beat_id": "food",
            "topic": "Food",
            "thought": "Small treats made the hot afternoons better.",
            "media_ids": ["food-photo"],
            "layout": "fullscreen",
            "duration_s": 3,
        },
        {
            "beat_id": "town",
            "topic": "Architecture",
            "thought": "The old streets reward slow wandering.",
            "media_ids": ["town-photo"],
            "layout": "fullscreen",
            "duration_s": 3,
        },
        {
            "beat_id": "coast",
            "topic": "Coast",
            "thought": "The water changes the pace of the whole day.",
            "media_ids": ["coast-video"],
            "layout": "fullscreen",
            "duration_s": 4,
        },
    ]

    with pytest.raises(GuidedStoryError, match="longer than its approved videos") as exc:
        _compile_execution_plan_version(raw, track=None, compiler_version=4)

    assert exc.value.code == "guided_story_duration_impossible"


def test_missing_selected_source_has_stable_failure_code(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.storage.download_generation_to_file",
        lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError("generation missing")),
    )
    plan = {
        "selected_media_ids": ["food-photo"],
        "story_timeline": [
            {
                "media_id": "food-photo",
                "gcs_path": "users/u/food.jpg",
                "generation": "12",
                "kind": "image",
            }
        ],
    }

    with pytest.raises(GuidedStoryError) as exc:
        _download_selected(plan, str(tmp_path))

    assert exc.value.code == "guided_story_media_missing"


def test_selected_source_with_wrong_format_is_rejected(tmp_path, monkeypatch) -> None:
    def invalid_image(_path: str, local: str, *, generation: str) -> None:
        assert generation == "12"
        with open(local, "wb") as handle:
            handle.write(b"not an image")

    monkeypatch.setattr("app.storage.download_generation_to_file", invalid_image)
    plan = {
        "selected_media_ids": ["food-photo"],
        "story_timeline": [
            {
                "media_id": "food-photo",
                "gcs_path": "users/u/food.jpg",
                "generation": "12",
                "kind": "image",
            }
        ],
    }

    with pytest.raises(GuidedStoryError) as exc:
        _download_selected(plan, str(tmp_path))

    assert exc.value.code == "guided_story_media_replaced"


def test_selected_heic_is_normalized_without_changing_source_receipt(tmp_path, monkeypatch) -> None:
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    source = tmp_path / "uploaded.heic"
    try:
        Image.new("RGB", (80, 120), (24, 120, 180)).save(source, format="HEIF")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"local pillow-heif cannot encode HEIF: {exc}")
    source_bytes = source.read_bytes()

    def download(_path: str, local: str, *, generation: str) -> None:
        assert generation == "12"
        shutil.copy2(source, local)

    monkeypatch.setattr("app.storage.download_generation_to_file", download)
    plan = {
        "selected_media_ids": ["corfu-photo"],
        "story_timeline": [
            {
                "media_id": "corfu-photo",
                "gcs_path": "users/u/corfu.HEIC",
                "generation": "12",
                "kind": "image",
            }
        ],
    }

    local_by_id, receipts = _download_selected(plan, str(tmp_path))

    render_source = Path(local_by_id["corfu-photo"])
    assert render_source.suffix == ".jpg"
    with Image.open(render_source) as image:
        assert image.format == "JPEG"
        assert image.size == (80, 120)
    assert receipts[0]["bytes"] == len(source_bytes)
    assert receipts[0]["sha256"] == hashlib.sha256(source_bytes).hexdigest()


def test_selected_transparent_image_preserves_alpha_and_source_receipt(
    tmp_path, monkeypatch
) -> None:
    from PIL import Image

    source = tmp_path / "uploaded.webp"
    image = Image.new("RGBA", (80, 120), (24, 120, 180, 255))
    image.putpixel((0, 0), (24, 120, 180, 0))
    image.save(source, format="WEBP", lossless=True)
    source_bytes = source.read_bytes()

    def download(_path: str, local: str, *, generation: str) -> None:
        assert generation == "13"
        shutil.copy2(source, local)

    monkeypatch.setattr("app.storage.download_generation_to_file", download)
    plan = {
        "selected_media_ids": ["transparent-card"],
        "story_timeline": [
            {
                "media_id": "transparent-card",
                "gcs_path": "users/u/card.webp",
                "generation": "13",
                "kind": "image",
            }
        ],
    }

    local_by_id, receipts = _download_selected(plan, str(tmp_path))

    render_source = Path(local_by_id["transparent-card"])
    assert render_source.suffix == ".png"
    with Image.open(render_source) as normalized:
        assert normalized.format == "PNG"
        assert normalized.getchannel("A").getextrema() == (0, 255)
    assert receipts[0]["bytes"] == len(source_bytes)
    assert receipts[0]["sha256"] == hashlib.sha256(source_bytes).hexdigest()


def test_video_window_beyond_downloaded_duration_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.guided_story.probe_video",
        lambda _path: SimpleNamespace(duration_s=1.0),
    )

    with pytest.raises(GuidedStoryError) as exc:
        _render_video_moment(
            "source.mp4",
            "output.mp4",
            start_s=0.0,
            end_s=2.0,
            layout="fullscreen",
        )

    assert exc.value.code == "guided_story_duration_impossible"


def test_snapshot_identity_set_must_match_approved_catalog() -> None:
    raw = _guided_snapshot()
    raw["media_identities"].pop()
    with pytest.raises(GuidedStoryError, match="identities") as exc:
        compile_execution_plan(raw, track=None)
    assert exc.value.code == "guided_story_snapshot_invalid"


def test_partial_output_upload_is_compensated(tmp_path, monkeypatch) -> None:
    from app import storage

    clean = tmp_path / "clean.mp4"
    final = tmp_path / "final.mp4"
    clean.write_bytes(b"clean")
    final.write_bytes(b"final")
    uploaded: list[str] = []
    deleted: list[str] = []

    def upload(_local: str, key: str) -> str:
        uploaded.append(key)
        if key.endswith("final.mp4"):
            raise RuntimeError("provider failed after create")
        return f"https://example.test/{key}"

    monkeypatch.setattr(storage, "upload_public_read", upload)
    monkeypatch.setattr(storage, "delete_object_best_effort", deleted.append)

    with pytest.raises(RuntimeError, match="provider failed"):
        _upload_verified_outputs(
            str(clean),
            str(final),
            base_key="jobs/base.mp4",
            output_key="jobs/final.mp4",
        )

    assert uploaded == ["jobs/base.mp4", "jobs/final.mp4"]
    assert deleted == ["jobs/base.mp4", "jobs/final.mp4"]


def test_deleted_pinned_music_generation_has_stable_failure_code(monkeypatch) -> None:
    from app.tasks import template_orchestrate

    monkeypatch.setattr(
        template_orchestrate,
        "_mix_template_audio",
        lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError("generation gone")),
    )
    music = {
        "track_id": "track-1",
        "title": "Corfu Drift",
        "audio_gcs_path": "music/corfu.m4a",
        "generation": "123",
        "start_s": 0.0,
    }
    track = SimpleNamespace(
        id="track-1",
        audio_gcs_path="music/corfu.m4a",
        generation="123",
    )

    with pytest.raises(GuidedStoryError) as exc:
        _mix_pinned_music("story.mp4", "base.mp4", "/tmp", music, track)

    assert exc.value.code == "guided_story_music_missing"


def test_legacy_pinned_music_without_end_uses_story_duration(monkeypatch) -> None:
    from app.tasks import template_orchestrate

    captured: dict = {}
    monkeypatch.setattr(
        template_orchestrate,
        "_mix_template_audio",
        lambda *_args, **kwargs: captured.update(kwargs),
    )
    music = {
        "track_id": "track-1",
        "title": "Corfu Drift",
        "audio_gcs_path": "music/corfu.m4a",
        "generation": "123",
        "start_s": 2.0,
    }
    track = SimpleNamespace(
        id="track-1",
        audio_gcs_path="music/corfu.m4a",
        generation="123",
    )

    _mix_pinned_music(
        "story.mp4",
        "base.mp4",
        "/tmp",
        music,
        track,
        output_duration_s=4.5,
    )

    assert captured["validated_window_duration_s"] == 4.5
    assert captured["audio_window_duration_s"] == 4.5


def test_pinned_music_swap_forwards_exact_window_and_level(monkeypatch) -> None:
    from app.tasks import template_orchestrate

    captured: dict = {}
    monkeypatch.setattr(
        template_orchestrate,
        "_mix_template_audio",
        lambda *_args, **kwargs: captured.update(kwargs),
    )
    music = {
        "track_id": "replacement-track",
        "title": "Replacement track",
        "audio_gcs_path": "music/replacement.m4a",
        "generation": "track-generation-9",
        "start_s": 1.25,
        "end_s": 4.75,
        "level": 0.35,
    }
    track = SimpleNamespace(
        id="replacement-track",
        audio_gcs_path="music/replacement.m4a",
        generation="track-generation-9",
    )

    _mix_pinned_music(
        "assembled.mp4",
        "clean-base.mp4",
        "/tmp",
        music,
        track,
        output_duration_s=3.5,
    )

    assert captured["audio_start_offset_s"] == pytest.approx(1.25)
    assert captured["validated_window_duration_s"] == pytest.approx(3.5)
    assert captured["audio_window_duration_s"] == pytest.approx(3.5)
    assert captured["audio_generation"] == "track-generation-9"
    assert captured["audio_gain"] == pytest.approx(0.35)


def _verified_receipt(plan: dict) -> dict:
    beat_ids = [row["beat_id"] for row in plan["beat_windows"]]
    moment_ids = [row["moment_id"] for row in plan["story_timeline"]]
    text_ids = [row["id"] for row in plan["text_elements"]]
    return {
        "schema_version": 1,
        "verified": True,
        "proposal_version": plan["proposal_version"],
        "media_digest": plan["media_digest"],
        "expected_beat_ids": beat_ids,
        "actual_beat_ids": beat_ids,
        "expected_moment_ids": moment_ids,
        "actual_moment_ids": moment_ids,
        "expected_media_ids": plan["selected_media_ids"],
        "actual_media_ids": plan["selected_media_ids"],
        "expected_text_ids": text_ids,
        "actual_text_ids": text_ids,
        "media_count": len(plan["selected_media_ids"]),
        "image_count": 2,
        "video_count": 1,
        "expected_duration_s": plan["resolved_duration_s"],
        "actual_duration_s": plan["resolved_duration_s"],
        "music_applied": False,
        "music": None,
        "output": {
            "width": 1080,
            "height": 1920,
            "video_codec": "h264",
            "audio_codec": "aac",
            "sha256": "a" * 64,
        },
        "base_storage": {
            "path": "generative-jobs/job-1/base_guided.mp4",
            "generation": "base-gen",
            "size": 100,
            "md5_hash": None,
        },
        "output_storage": {
            "path": "generative-jobs/job-1/final_guided.mp4",
            "generation": "output-gen",
            "size": 101,
            "md5_hash": None,
        },
        "media_stages": [
            {
                "media_id": media_id,
                "gcs_path": next(
                    row["gcs_path"] for row in plan["story_timeline"] if row["media_id"] == media_id
                ),
                "generation": next(
                    row["generation"]
                    for row in plan["story_timeline"]
                    if row["media_id"] == media_id
                ),
                "kind": next(
                    row["kind"] for row in plan["story_timeline"] if row["media_id"] == media_id
                ),
            }
            for media_id in plan["selected_media_ids"]
        ],
        "moment_stages": [
            {
                "moment_id": row["moment_id"],
                "beat_id": row["beat_id"],
                "media_id": row["media_id"],
                "generation": row["generation"],
                "kind": row["kind"],
                "layout": row["layout"],
                "image_motion": row["image_motion"],
            }
            for row in plan["story_timeline"]
        ],
        "text_stages": [{"element_id": element_id, "visible": True} for element_id in text_ids],
    }


def _ready_result(plan: dict) -> dict:
    return {
        "variant_id": "guided_story",
        "resolved_archetype": "guided_story",
        "render_status": "ready",
        "ok": True,
        "proposal_version": plan["proposal_version"],
        "media_digest": plan["media_digest"],
        "story_timeline": plan["story_timeline"],
        "text_elements": plan["text_elements"],
        "base_video_path": "generative-jobs/job-1/base_guided.mp4",
        "video_path": "generative-jobs/job-1/final_guided.mp4",
        "render_receipt": _verified_receipt(plan),
    }


def test_ready_result_requires_canonical_plan_and_complete_stage_receipts(monkeypatch) -> None:
    plan = compile_execution_plan(_guided_snapshot(), track=None)
    result = _ready_result(plan)

    assert validate_ready_result(plan, result, job_id="job-1", verify_storage=False) == result

    corrupt = copy.deepcopy(result)
    corrupt["render_receipt"]["actual_moment_ids"].pop()
    with pytest.raises(GuidedStoryError) as exc:
        validate_ready_result(plan, corrupt, job_id="job-1", verify_storage=False)
    assert exc.value.code == "guided_story_receipt_mismatch"

    for section, field, value in (
        (None, "expected_duration_s", 99.0),
        ("media_stages", "generation", "replaced-source"),
        ("moment_stages", "layout", "fullscreen"),
    ):
        corrupt = copy.deepcopy(result)
        target = (
            corrupt["render_receipt"] if section is None else corrupt["render_receipt"][section][0]
        )
        target[field] = value
        with pytest.raises(GuidedStoryError) as exc:
            validate_ready_result(plan, corrupt, job_id="job-1", verify_storage=False)
        assert exc.value.code == "guided_story_receipt_mismatch"

    from app import storage

    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda path: storage.ObjectMetadata(
            path=path,
            generation="base-gen" if "base_" in path else "output-gen",
            etag=None,
            size=100 if "base_" in path else 101,
            content_type="video/mp4",
            md5_hash=None,
        ),
    )
    assert validate_ready_result(plan, result, job_id="job-1", verify_storage=True) == result
    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda path: storage.ObjectMetadata(
            path=path,
            generation="replacement",
            etag=None,
            size=100,
            content_type="video/mp4",
            md5_hash=None,
        ),
    )
    with pytest.raises(GuidedStoryError) as exc:
        validate_ready_result(plan, result, job_id="job-1", verify_storage=True)
    assert exc.value.code == "guided_story_receipt_mismatch"

    corrupt = copy.deepcopy(result)
    corrupt["render_receipt"]["media_stages"].pop()
    with pytest.raises(GuidedStoryError) as exc:
        validate_ready_result(plan, corrupt, job_id="job-1", verify_storage=False)
    assert exc.value.code == "guided_story_receipt_mismatch"


def test_guided_text_reburn_rejects_text_outside_final_timeline(tmp_path) -> None:
    plan = compile_execution_plan(_guided_snapshot(), track=None)
    outside = [dict(plan["text_elements"][0], start_s=20.0, end_s=21.0)]

    with pytest.raises(GuidedStoryError) as exc:
        verify_guided_text_reburn(
            _verified_receipt(plan),
            outside,
            [{"element_id": outside[0]["id"], "visible": True}],
            str(tmp_path / "final.mp4"),
            str(tmp_path / "base.mp4"),
        )

    assert exc.value.code == "guided_story_text_missing"


def test_guided_text_reburn_rejects_invisible_required_element(tmp_path, monkeypatch) -> None:
    from app.pipeline import guided_story

    plan = compile_execution_plan(_guided_snapshot(), track=None)
    final = tmp_path / "final.mp4"
    base = tmp_path / "base.mp4"
    final.write_bytes(b"final")
    base.write_bytes(b"base")
    monkeypatch.setattr(
        guided_story,
        "probe_video",
        lambda _path: SimpleNamespace(
            duration_s=18.0,
            width=1080,
            height=1920,
            codec="h264",
        ),
    )
    monkeypatch.setattr(guided_story, "_audio_codec", lambda _path: "aac")

    with pytest.raises(GuidedStoryError) as exc:
        verify_guided_text_reburn(
            _verified_receipt(plan),
            [plan["text_elements"][0]],
            [{"element_id": "guided-title", "visible": False}],
            str(final),
            str(base),
        )

    assert exc.value.code == "guided_story_text_missing"


@pytest.mark.parametrize("drop", ["supporting_card", "media", "text"])
def test_receipt_fault_injection_never_publishes_a_missing_required_stage(
    drop: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.pipeline import guided_story

    plan = compile_execution_plan(_guided_snapshot(), track=None)
    media_receipts = [{"media_id": media_id} for media_id in plan["selected_media_ids"]]
    moment_receipts = [
        {
            "moment_id": row["moment_id"],
            "beat_id": row["beat_id"],
            "media_id": row["media_id"],
            "kind": row["kind"],
            "layout": row["layout"],
        }
        for row in plan["story_timeline"]
    ]
    text_receipts = [{"element_id": row["id"], "visible": True} for row in plan["text_elements"]]
    if drop == "supporting_card":
        moment_receipts = [row for row in moment_receipts if row["layout"] != "supporting_card"]
    elif drop == "media":
        media_receipts.pop()
    else:
        text_receipts.pop()

    final = tmp_path / "final.mp4"
    final.write_bytes(b"video")
    monkeypatch.setattr(
        guided_story,
        "probe_video",
        lambda _path: SimpleNamespace(
            duration_s=18,
            width=guided_story.settings.output_width,
            height=guided_story.settings.output_height,
            codec="h264",
            has_audio=True,
        ),
    )
    monkeypatch.setattr(guided_story, "_audio_codec", lambda _path: "aac")
    monkeypatch.setattr(guided_story, "_sha256", lambda _path: "hash")

    with pytest.raises(GuidedStoryError) as exc:
        _verify_receipt(
            plan,
            media_receipts,
            moment_receipts,
            text_receipts,
            str(final),
            music_applied=False,
        )
    assert exc.value.code in {"guided_story_text_missing", "guided_story_receipt_mismatch"}


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
def test_selected_rotated_video_is_normalized_without_changing_source_receipt(
    tmp_path, monkeypatch
) -> None:
    """Regression for prod jobs ca168a9f/4467f18a/d9e4833c (2026-08-19): phone
    clips with a -90° Display Matrix reached reframe unnormalized (the montage
    path runs Stage 0.5, guided stories did not), so probe misclassified them
    and the landscape render crashed. The download step must normalize the
    local file in place while the identity receipt keeps the UNTOUCHED
    download's bytes/sha."""
    import subprocess as sp

    from app.pipeline.orientation import detect_rotation_and_dims

    stored = tmp_path / "stored.mp4"
    source = tmp_path / "uploaded.mp4"
    sp.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x36:d=0.4:r=30",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(stored),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    sp.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-display_rotation",
            "-90",
            "-i",
            str(stored),
            "-c",
            "copy",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    source_bytes = source.read_bytes()

    def download(_path: str, local: str, *, generation: str) -> None:
        assert generation == "14"
        shutil.copy2(source, local)

    monkeypatch.setattr("app.storage.download_generation_to_file", download)
    plan = {
        "selected_media_ids": ["tortoise-video"],
        "story_timeline": [
            {
                "media_id": "tortoise-video",
                "gcs_path": "users/u/VID_20260813.mp4",
                "generation": "14",
                "kind": "video",
            }
        ],
    }

    local_by_id, receipts = _download_selected(plan, str(tmp_path))

    # Local render source: rotation flag stripped, pixels portrait.
    rotation, width, height = detect_rotation_and_dims(local_by_id["tortoise-video"])
    assert rotation == 0
    assert (width, height) == (36, 64)
    # Identity receipt: the untouched download, not the normalized bytes.
    assert receipts[0]["bytes"] == len(source_bytes)
    assert receipts[0]["sha256"] == hashlib.sha256(source_bytes).hexdigest()
