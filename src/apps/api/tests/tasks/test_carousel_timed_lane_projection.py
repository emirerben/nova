from app.tasks import generative_build as gb
from app.tasks.generative_build import _project_carousel_timed_lanes


def test_carousel_projects_downstream_and_crossing_lanes_but_not_music():
    variant = {
        "carousel_moment": {"timing_model": "ripple_v1"},
        "carousel_insertion_base_s": 4.0,
        "carousel_inserted_duration_s": 3.0,
        "carousel_ripple_duration_s": 2.4,
        "text_elements": [
            {"id": "cross", "start_s": 3.0, "end_s": 5.0},
            {"id": "after", "start_s": 5.0, "end_s": 6.0},
        ],
        "caption_cues": [{"text": "after", "start_s": 4.0, "end_s": 4.5}],
        "visual_blocks": [{"id": "visual", "start_s": 2.0, "end_s": 5.0}],
        "media_overlays": [{"id": "overlay", "start_s": 5.0, "end_s": 6.0}],
        "camera_effects": [{"id": "camera", "start_s": 4.0, "end_s": 5.0}],
        "sound_effects": [{"id": "sfx", "at_s": 4.0, "end_s": 4.5}],
        "motion_scenes": [{"id": "motion", "start_frame": 120, "end_frame_exclusive": 150}],
        "music_start_s": 12.0,
    }

    projected = _project_carousel_timed_lanes(variant)

    assert projected["text_elements"] == [
        {"id": "cross", "start_s": 3.0, "end_s": 7.4},
        {"id": "after", "start_s": 7.4, "end_s": 8.4},
    ]
    assert projected["caption_cues"] == [{"text": "after", "start_s": 6.4, "end_s": 6.9}]
    assert projected["visual_blocks"] == [{"id": "visual", "start_s": 2.0, "end_s": 7.4}]
    assert projected["media_overlays"] == [{"id": "overlay", "start_s": 7.4, "end_s": 8.4}]
    assert projected["camera_effects"] == [{"id": "camera", "start_s": 6.4, "end_s": 7.4}]
    assert projected["sound_effects"][0]["at_s"] == 6.4
    assert projected["motion_scenes"][0]["start_frame"] == 192
    assert projected["music_start_s"] == 12.0
    # Projection is pure: stored/base timestamps are never repeatedly mutated.
    assert variant["text_elements"][1]["start_s"] == 5.0


def test_legacy_carousel_keeps_all_lane_timestamps_byte_for_byte():
    variant = {
        "carousel_moment": {"transition": "crossfade"},
        "carousel_insertion_base_s": 4.0,
        "carousel_inserted_duration_s": 3.0,
        "text_elements": [{"start_s": 5.0, "end_s": 6.0}],
    }
    assert _project_carousel_timed_lanes(variant) is variant


def test_creator_layer_reburn_ingress_projects_without_mutating_persisted_lanes(monkeypatch):
    persisted = {
        "carousel_moment": {"timing_model": "ripple_v1"},
        "carousel_insertion_base_s": 3.0,
        "carousel_ripple_duration_s": 4.0,
        "visual_blocks": [{"id": "v", "start_s": 5.0, "end_s": 6.0}],
        "motion_scenes": [{"id": "m", "start_frame": 150, "end_frame_exclusive": 180}],
    }
    seen: dict[str, dict] = {}

    def visual(**kwargs):
        seen["visual"] = kwargs["variant"]
        return kwargs["base_gcs_path"], None

    def motion(**kwargs):
        seen["motion"] = kwargs["variant"]
        return kwargs["base_gcs_path"], None

    monkeypatch.setattr(gb, "_ensure_visual_blocks_base", visual)
    monkeypatch.setattr(gb, "_ensure_motion_base", motion)

    gb._ensure_creator_layer_base(
        job_id="job",
        variant_id="variant",
        variant=persisted,
        base_gcs_path="base.mp4",
    )

    assert seen["visual"]["visual_blocks"][0]["start_s"] == 9.0
    assert seen["motion"]["motion_scenes"][0]["start_frame"] == 270
    assert persisted["visual_blocks"][0]["start_s"] == 5.0
    assert persisted["motion_scenes"][0]["start_frame"] == 150
