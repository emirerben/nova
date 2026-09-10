"""Platform profiles for mixed-media "slide posts" — the single source of
truth for every per-platform limit (slide count, allowed media kinds, video
duration, canvas shape).

Kept deliberately free of any import from `app.schemas.slide_post` or
`app.models` — this module operates on plain `SlideInput` tuples, not on the
persisted draft or the ORM row, so it can be unit-tested with no DB and
reused unchanged by both the route-level draft validator and the worker-side
render/export builder.

TikTok photo mode accepts images only (1-35 slides, no video items at all —
this is the actual behavior of TikTok's Content Posting API photo mode, not
an arbitrary restriction Kria adds). Instagram carousels accept a mix of
images and videos (2-20 items, each video capped at 90s). This is why "mixed
media" as the issue names it is really the Instagram shape, with TikTok photo
mode as the images-only degenerate case — see plans/024.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, get_args

PlatformProfile = Literal["tiktok_photo", "instagram_carousel"]

PLATFORM_PROFILE_IDS: tuple[str, ...] = get_args(PlatformProfile)

DEFAULT_PLATFORM_PROFILE: PlatformProfile = "tiktok_photo"

# Canonical output canvas per profile. Both are portrait; the exact ratio
# differs (TikTok photo mode renders 9:16, Instagram carousel posts render
# 4:5) so normalization must be profile-aware, not a single shared constant
# the way the video pipeline's 1080x1920 is.
_TIKTOK_PHOTO_CANVAS = (1080, 1920)
_INSTAGRAM_CAROUSEL_CANVAS = (1080, 1350)


@dataclass(frozen=True)
class PlatformProfileSpec:
    id: PlatformProfile
    label: str
    allowed_kinds: frozenset[str]
    min_slides: int
    max_slides: int
    # None = no per-item video duration cap (still bounded by MAX_SLIDES et al.).
    max_video_duration_s: float | None
    canvas: tuple[int, int]
    image_format: Literal["jpeg"] = "jpeg"


PLATFORM_PROFILES: dict[PlatformProfile, PlatformProfileSpec] = {
    "tiktok_photo": PlatformProfileSpec(
        id="tiktok_photo",
        label="TikTok photo post",
        allowed_kinds=frozenset({"image"}),
        min_slides=1,
        max_slides=35,
        max_video_duration_s=None,
        canvas=_TIKTOK_PHOTO_CANVAS,
    ),
    "instagram_carousel": PlatformProfileSpec(
        id="instagram_carousel",
        label="Instagram carousel",
        allowed_kinds=frozenset({"image", "video"}),
        min_slides=2,
        max_slides=20,
        max_video_duration_s=90.0,
        canvas=_INSTAGRAM_CAROUSEL_CANVAS,
    ),
}


def coerce_platform_profile(value: object) -> PlatformProfile:
    """Normalize arbitrary input to a known profile (mirrors coerce_montage_preset)."""

    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in PLATFORM_PROFILE_IDS:
            return normalized  # type: ignore[return-value]
    return DEFAULT_PLATFORM_PROFILE


@dataclass(frozen=True)
class SlideInput:
    """Plain, resolved slide info — kind + duration, nothing profile-specific."""

    slide_id: str
    kind: Literal["image", "video"]
    duration_s: float | None = None


@dataclass(frozen=True)
class SlideValidationIssue:
    code: str
    message: str
    slide_id: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[SlideValidationIssue] = field(default_factory=list)
    warnings: list[SlideValidationIssue] = field(default_factory=list)


def validate(profile: PlatformProfile, slides: list[SlideInput]) -> ValidationResult:
    """Validate an ordered slide list against one platform profile's limits.

    Pure and side-effect free — callers decide what to do with a non-ok
    result (block export, or just show a badge). A video slide rejected by
    `tiktok_photo` is reported as an error with a `code` the caller can map
    to the still-frame-fallback remedy in the UI, rather than silently
    dropping the slide here.
    """

    spec = PLATFORM_PROFILES[profile]
    errors: list[SlideValidationIssue] = []
    warnings: list[SlideValidationIssue] = []

    if len(slides) < spec.min_slides:
        errors.append(
            SlideValidationIssue(
                code="too_few_slides",
                message=f"{spec.label} needs at least {spec.min_slides} slide(s)",
            )
        )
    if len(slides) > spec.max_slides:
        errors.append(
            SlideValidationIssue(
                code="too_many_slides",
                message=f"{spec.label} allows at most {spec.max_slides} slides",
            )
        )

    for slide in slides:
        if slide.kind not in spec.allowed_kinds:
            errors.append(
                SlideValidationIssue(
                    slide_id=slide.slide_id,
                    code="unsupported_media_kind",
                    message=(
                        f"{spec.label} does not support video slides — use a still frame instead"
                    ),
                )
            )
            continue
        if (
            slide.kind == "video"
            and spec.max_video_duration_s is not None
            and (slide.duration_s or 0) > spec.max_video_duration_s
        ):
            errors.append(
                SlideValidationIssue(
                    slide_id=slide.slide_id,
                    code="video_too_long",
                    message=(
                        f"{spec.label} caps video slides at {int(spec.max_video_duration_s)}s"
                    ),
                )
            )

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)
