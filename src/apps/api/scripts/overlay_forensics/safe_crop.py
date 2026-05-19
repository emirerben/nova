"""16:9 -> 9:16 center-crop projection math.

When a horizontal recipe video is adapted to vertical output, a naive
center-crop discards the left and right margins. Text overlays positioned
near the horizontal edges of the recipe (x_frac near 0 or 1) fall outside
the resulting 9:16 frame.

This module projects bboxes from the recipe's coordinate system onto the
hypothetical 9:16 center-crop and reports whether they survive intact, or
if any portion would be clipped.
"""
from __future__ import annotations

from dataclasses import dataclass

from .masking import BBox, RelativeBBox

TARGET_VERTICAL_RATIO = 9.0 / 16.0  # width / height for 9:16 portrait

# Safe area within the 9:16 frame — text whose centroid falls outside is
# either off-frame or in the user's thumb zone.
SAFE_AREA_X_MIN = 0.05
SAFE_AREA_X_MAX = 0.95


@dataclass
class CropProjection:
    survives: bool
    projected_x_frac: float | None  # centroid x_frac in the 9:16 frame, or None if clipped
    projected_cy_frac: float | None
    note: str


def project_to_9x16(
    bbox: BBox,
    source_w: int,
    source_h: int,
    target_aspect_w_over_h: float = TARGET_VERTICAL_RATIO,
) -> CropProjection:
    """Project a bbox from a horizontal source onto a center-crop with the
    target aspect ratio (default 9:16 portrait).

    If the source is already at-or-narrower than the target, every bbox
    trivially survives — the crop is effectively a no-op.
    """
    source_aspect = source_w / max(source_h, 1)
    if source_aspect <= target_aspect_w_over_h + 1e-3:
        # Source is already vertical-or-square — no horizontal crop needed.
        cx = (bbox.x_min + bbox.x_max) / 2
        cy = (bbox.y_min + bbox.y_max) / 2
        return CropProjection(
            survives=True,
            projected_x_frac=cx / max(source_w, 1),
            projected_cy_frac=cy / max(source_h, 1),
            note="source already at or narrower than target aspect — no crop applied",
        )

    crop_w = source_h * target_aspect_w_over_h
    crop_x_start = (source_w - crop_w) / 2
    crop_x_end = crop_x_start + crop_w

    cx = (bbox.x_min + bbox.x_max) / 2
    cy = (bbox.y_min + bbox.y_max) / 2

    # Any portion of the bbox outside [crop_x_start, crop_x_end] is clipped.
    clipped_left = bbox.x_min < crop_x_start
    clipped_right = bbox.x_max > crop_x_end
    centroid_outside = cx < crop_x_start or cx > crop_x_end

    if centroid_outside:
        return CropProjection(
            survives=False,
            projected_x_frac=None,
            projected_cy_frac=cy / max(source_h, 1),
            note=(
                f"centroid x={cx:.0f}px outside center-crop band "
                f"[{crop_x_start:.0f}, {crop_x_end:.0f}] — bbox lost in 9:16 adaptation"
            ),
        )

    projected_cx_frac = (cx - crop_x_start) / crop_w
    projected_cy_frac = cy / source_h

    if clipped_left or clipped_right:
        side = "left" if clipped_left else "right"
        return CropProjection(
            survives=False,
            projected_x_frac=projected_cx_frac,
            projected_cy_frac=projected_cy_frac,
            note=f"bbox extends past the {side} edge of the 9:16 center-crop",
        )

    # Survives the crop, but check safe-area within the 9:16 frame too.
    if projected_cx_frac < SAFE_AREA_X_MIN or projected_cx_frac > SAFE_AREA_X_MAX:
        return CropProjection(
            survives=True,
            projected_x_frac=projected_cx_frac,
            projected_cy_frac=projected_cy_frac,
            note=(
                f"survives crop but lands at x_frac={projected_cx_frac:.2f} "
                f"in the 9:16 frame — outside safe area "
                f"[{SAFE_AREA_X_MIN:.2f}, {SAFE_AREA_X_MAX:.2f}]"
            ),
        )

    return CropProjection(
        survives=True,
        projected_x_frac=projected_cx_frac,
        projected_cy_frac=projected_cy_frac,
        note="survives 9:16 center-crop within safe area",
    )


def project_relative_to_9x16(
    rel: RelativeBBox,
    source_aspect_w_over_h: float,
    target_aspect_w_over_h: float = TARGET_VERTICAL_RATIO,
) -> CropProjection:
    """Same as project_to_9x16 but operating on RelativeBBox + aspect ratio.

    Convenient when you already have normalized coords.
    """
    # Convert to pixel coords in a 1000-tall virtual frame for the math.
    h = 1000
    w = int(h * source_aspect_w_over_h)
    bbox = BBox(
        x_min=int(rel.x_frac * w),
        y_min=int(rel.y_frac * h),
        x_max=int((rel.x_frac + rel.w_frac) * w),
        y_max=int((rel.y_frac + rel.h_frac) * h),
    )
    return project_to_9x16(bbox, w, h, target_aspect_w_over_h)
