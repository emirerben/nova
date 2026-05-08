"""Tests for the mixed-media (video + photo) orchestrator path.

Covers positional binding helpers and the matcher used when a template
has any slot with media_type=photo.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.agents.gemini_analyzer import ClipMeta, TemplateRecipe
from app.pipeline.template_matcher import TemplateMismatchError
from app.tasks.template_orchestrate import (
    _build_positional_clip_metas,
    _is_mixed_media,
    _match_positional,
    _slots_in_position_order,
)


def _slots(*media_types: str) -> list[dict]:
    return [
        {
            "position": i + 1,
            "target_duration_s": 3.0 + i,
            "media_type": mt,
            "slot_type": "hook" if i == 0 else "outro",
            "priority": 5,
        }
        for i, mt in enumerate(media_types)
    ]


def _recipe(*media_types: str) -> TemplateRecipe:
    return TemplateRecipe(
        shot_count=len(media_types),
        total_duration_s=sum(3.0 + i for i in range(len(media_types))),
        hook_duration_s=3.0,
        slots=_slots(*media_types),
        copy_tone="casual",
        caption_style="bold",
    )


def _probe(duration: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(
        duration_s=duration, fps=30.0, width=1080, height=1920,
        has_audio=False, codec="h264", aspect_ratio="9:16",
        file_size_bytes=1000,
    )


class TestIsMixedMedia:
    def test_all_video_is_not_mixed(self):
        assert _is_mixed_media(_slots("video", "video")) is False

    def test_one_photo_makes_it_mixed(self):
        assert _is_mixed_media(_slots("video", "photo")) is True

    def test_legacy_slots_without_media_type_default_to_video(self):
        slots = [{"position": 1, "target_duration_s": 5.0}]
        assert _is_mixed_media(slots) is False


class TestSlotsInPositionOrder:
    def test_sorts_by_position(self):
        unsorted = [
            {"position": 3, "media_type": "video"},
            {"position": 1, "media_type": "video"},
            {"position": 2, "media_type": "photo"},
        ]
        sorted_slots = _slots_in_position_order(unsorted)
        assert [s["position"] for s in sorted_slots] == [1, 2, 3]


class TestBuildPositionalClipMetas:
    def test_photo_slot_skips_gemini(self):
        slots = _slots("photo")
        local_paths = ["/tmp/photo_clip.mp4"]
        probe_map = {"/tmp/photo_clip.mp4": _probe(4.0)}

        with patch("app.tasks.template_orchestrate.gemini_upload_and_wait") as up, \
             patch("app.tasks.template_orchestrate.analyze_clip") as an:
            metas = _build_positional_clip_metas(local_paths, slots, probe_map)

        assert up.call_count == 0, "photo slots must not hit Gemini"
        assert an.call_count == 0
        assert len(metas) == 1
        assert metas[0].clip_id == "clip_0"
        assert metas[0].best_moments[0]["end_s"] == 4.0

    def test_video_slot_runs_gemini(self):
        slots = _slots("video")
        local_paths = ["/tmp/video.mp4"]
        probe_map = {"/tmp/video.mp4": _probe(15.0)}

        fake_meta = ClipMeta(
            clip_id="g/will-be-overridden",
            transcript="t",
            hook_text="h",
            hook_score=8.0,
            best_moments=[{"start_s": 0, "end_s": 5, "energy": 7, "description": "x"}],
        )
        with (
            patch("app.tasks.template_orchestrate.gemini_upload_and_wait", return_value=MagicMock()),
            patch("app.tasks.template_orchestrate.analyze_clip", return_value=fake_meta),
        ):
            metas = _build_positional_clip_metas(local_paths, slots, probe_map)

        assert len(metas) == 1
        # clip_id is overridden positionally so downstream maps work
        assert metas[0].clip_id == "clip_0"
        assert metas[0].clip_path == "/tmp/video.mp4"

    def test_mixed_video_and_photo_in_correct_order(self):
        slots = _slots("video", "photo")
        local_paths = ["/tmp/v.mp4", "/tmp/p.mp4"]
        probe_map = {
            "/tmp/v.mp4": _probe(10.0),
            "/tmp/p.mp4": _probe(4.0),
        }

        fake_meta = ClipMeta(
            clip_id="g/x", transcript="", hook_text="", hook_score=5.0,
            best_moments=[{"start_s": 0, "end_s": 5, "energy": 5, "description": ""}],
        )
        with (
            patch("app.tasks.template_orchestrate.gemini_upload_and_wait", return_value=MagicMock()),
            patch("app.tasks.template_orchestrate.analyze_clip", return_value=fake_meta),
        ):
            metas = _build_positional_clip_metas(local_paths, slots, probe_map)

        assert [m.clip_id for m in metas] == ["clip_0", "clip_1"]
        assert metas[0].clip_path == "/tmp/v.mp4"
        assert metas[1].clip_path == "/tmp/p.mp4"
        # Photo synthetic moment spans full clip
        assert metas[1].best_moments[0]["end_s"] == 4.0

    def test_clip_count_must_match_slot_count(self):
        with pytest.raises(ValueError, match="needs 2 clips"):
            _build_positional_clip_metas(
                ["/tmp/only_one.mp4"],
                _slots("video", "photo"),
                {},
            )


class TestMatchPositional:
    def test_binds_clips_to_slots_in_order(self):
        recipe = _recipe("video", "photo")
        metas = [
            ClipMeta(
                clip_id="clip_0", transcript="", hook_text="", hook_score=5.0,
                best_moments=[
                    {"start_s": 0, "end_s": 3.5, "energy": 5, "description": ""},
                    {"start_s": 5, "end_s": 8, "energy": 6, "description": ""},
                ],
            ),
            ClipMeta(
                clip_id="clip_1", transcript="", hook_text="", hook_score=5.0,
                best_moments=[{"start_s": 0, "end_s": 4.0, "energy": 5, "description": ""}],
            ),
        ]

        plan = _match_positional(recipe, metas)
        assert len(plan.steps) == 2
        # Slot 1 (target 3.0s) → clip_0; the closer moment (3.5s) wins over (3s)
        assert plan.steps[0].clip_id == "clip_0"
        assert plan.steps[0].slot["position"] == 1
        # Slot 2 (target 4.0s) → clip_1
        assert plan.steps[1].clip_id == "clip_1"
        assert plan.steps[1].slot["position"] == 2

    def test_mismatch_raises(self):
        recipe = _recipe("video", "photo")
        with pytest.raises(TemplateMismatchError):
            _match_positional(recipe, [])

    def test_picks_closest_duration_moment(self):
        recipe = _recipe("video")  # slot 1 wants 3.0s
        meta = ClipMeta(
            clip_id="clip_0", transcript="", hook_text="", hook_score=5.0,
            best_moments=[
                {"start_s": 0, "end_s": 10, "energy": 5, "description": "long"},
                {"start_s": 0, "end_s": 3.1, "energy": 5, "description": "perfect"},
                {"start_s": 0, "end_s": 1, "energy": 5, "description": "short"},
            ],
        )
        plan = _match_positional(recipe, [meta])
        chosen = plan.steps[0].moment
        assert chosen["description"] == "perfect"
