"""Unit tests for the per-platform slide-post limits (no DB required)."""

from __future__ import annotations

from app.pipeline.slide_post.profiles import (
    DEFAULT_PLATFORM_PROFILE,
    PLATFORM_PROFILES,
    SlideInput,
    coerce_platform_profile,
    validate,
)


def _images(n: int) -> list[SlideInput]:
    return [SlideInput(slide_id=f"s{i}", kind="image") for i in range(n)]


def test_default_platform_profile_is_tiktok_photo() -> None:
    assert DEFAULT_PLATFORM_PROFILE == "tiktok_photo"


def test_coerce_platform_profile_unknown_falls_back_to_default() -> None:
    assert coerce_platform_profile("not-a-real-profile") == DEFAULT_PLATFORM_PROFILE
    assert coerce_platform_profile(None) == DEFAULT_PLATFORM_PROFILE
    assert coerce_platform_profile("Instagram-Carousel") == "instagram_carousel"


def test_tiktok_photo_rejects_video_slides() -> None:
    slides = [SlideInput(slide_id="a", kind="image"), SlideInput(slide_id="b", kind="video")]
    result = validate("tiktok_photo", slides)
    assert not result.ok
    assert any(e.code == "unsupported_media_kind" and e.slide_id == "b" for e in result.errors)


def test_tiktok_photo_allows_1_to_35_images() -> None:
    assert validate("tiktok_photo", _images(1)).ok
    assert validate("tiktok_photo", _images(35)).ok
    assert not validate("tiktok_photo", _images(0)).ok
    assert not validate("tiktok_photo", _images(36)).ok


def test_instagram_carousel_requires_at_least_two_slides() -> None:
    result = validate("instagram_carousel", _images(1))
    assert not result.ok
    assert any(e.code == "too_few_slides" for e in result.errors)


def test_instagram_carousel_allows_mixed_media_up_to_20() -> None:
    slides = [
        SlideInput(slide_id="a", kind="image"),
        SlideInput(slide_id="b", kind="video", duration_s=30),
    ]
    assert validate("instagram_carousel", slides).ok
    assert validate("instagram_carousel", _images(20)).ok
    assert not validate("instagram_carousel", _images(21)).ok


def test_instagram_carousel_caps_video_duration() -> None:
    slides = [
        SlideInput(slide_id="a", kind="image"),
        SlideInput(slide_id="b", kind="video", duration_s=91),
    ]
    result = validate("instagram_carousel", slides)
    assert not result.ok
    assert any(e.code == "video_too_long" and e.slide_id == "b" for e in result.errors)


def test_every_profile_declares_a_canvas_and_positive_limits() -> None:
    for profile, spec in PLATFORM_PROFILES.items():
        assert spec.id == profile
        assert spec.canvas[0] > 0 and spec.canvas[1] > 0
        assert 0 < spec.min_slides <= spec.max_slides
        assert spec.allowed_kinds  # never empty
