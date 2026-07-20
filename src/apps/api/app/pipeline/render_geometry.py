"""Shared, deterministic geometry for Smart Captions v2 renderers.

The compiler, ASS writer, Skia title renderer, and media compositor must agree
about the boxes they protect.  This module owns normalized boxes, renderer-font
text measurement, bounded face sampling, alpha bounds, and the conservative
reposition/shrink/omit policy for decorative media.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import skia
from PIL import Image

from app.pipeline.canvas import PORTRAIT, Canvas

_FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
_FONT_FILES = {
    "montserrat": "Montserrat-Regular.ttf",
    "montserrat bold": "Montserrat-Bold.ttf",
    "tiktok sans": "TikTokSans-Bold.ttf",
}


@dataclass(frozen=True, slots=True)
class NormalizedBox:
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return max(0.0, self.right - self.left)

    @property
    def height(self) -> float:
        return max(0.0, self.bottom - self.top)

    @property
    def area(self) -> float:
        return self.width * self.height

    def padded(self, amount: float) -> NormalizedBox:
        return NormalizedBox(
            max(0.0, self.left - amount),
            max(0.0, self.top - amount),
            min(1.0, self.right + amount),
            min(1.0, self.bottom + amount),
        )

    def iou(self, other: NormalizedBox) -> float:
        width = max(0.0, min(self.right, other.right) - max(self.left, other.left))
        height = max(0.0, min(self.bottom, other.bottom) - max(self.top, other.top))
        intersection = width * height
        union = self.area + other.area - intersection
        return intersection / union if union > 0 else 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "left": round(self.left, 5),
            "top": round(self.top, 5),
            "right": round(self.right, 5),
            "bottom": round(self.bottom, 5),
        }


@dataclass(frozen=True, slots=True)
class TextMeasurement:
    lines: tuple[str, ...]
    font_size_px: int
    box: NormalizedBox


@dataclass(frozen=True, slots=True)
class PreparedMediaAsset:
    src_gcs_path: str
    local_path: str
    has_alpha: bool
    opaque_bounds: NormalizedBox


def _typeface(font_family: str) -> skia.Typeface:
    key = font_family.strip().casefold()
    filename = _FONT_FILES.get(key, _FONT_FILES["tiktok sans"])
    path = _FONT_DIR / filename
    typeface = skia.Typeface.MakeFromFile(str(path))
    return typeface or skia.Typeface.MakeDefault()


def measure_caption(
    text: str,
    *,
    font_family: str,
    font_size_px: int,
    width_frac: float,
    y_frac: float,
    max_lines: int,
    canvas: Canvas = PORTRAIT,
) -> TextMeasurement:
    """Wrap with the production Skia typeface and return its normalized box."""

    words = [word for word in text.split() if word]
    if not words:
        box = NormalizedBox(0.5, y_frac, 0.5, y_frac)
        return TextMeasurement(lines=(), font_size_px=font_size_px, box=box)
    max_width_px = canvas.width * width_frac
    chosen_lines: tuple[str, ...] = (" ".join(words),)
    chosen_size = font_size_px
    for size in range(font_size_px, 35, -2):
        font = skia.Font(_typeface(font_family), float(size))
        candidates: list[tuple[float, tuple[str, ...]]] = []
        if max_lines <= 1 or len(words) == 1:
            candidates.append((font.measureText(" ".join(words)), (" ".join(words),)))
        else:
            for split in range(1, len(words)):
                lines = (" ".join(words[:split]), " ".join(words[split:]))
                widths = [font.measureText(line) for line in lines]
                candidates.append((max(widths) + abs(widths[0] - widths[1]) * 0.08, lines))
        _, lines = min(candidates, key=lambda candidate: candidate[0])
        widest = max(font.measureText(line) for line in lines)
        chosen_lines = lines
        chosen_size = size
        if widest <= max_width_px:
            break
    font = skia.Font(_typeface(font_family), float(chosen_size))
    widest = min(max_width_px, max(font.measureText(line) for line in chosen_lines))
    height = chosen_size * 1.18 * len(chosen_lines)
    half_w = widest / canvas.width / 2
    half_h = height / canvas.height / 2
    box = NormalizedBox(
        max(0.0, 0.5 - half_w),
        max(0.0, y_frac - half_h),
        min(1.0, 0.5 + half_w),
        min(1.0, y_frac + half_h),
    )
    return TextMeasurement(lines=chosen_lines, font_size_px=chosen_size, box=box)


def opaque_alpha_box(path: str) -> NormalizedBox:
    """Return the non-transparent image bounds; opaque/video assets use full bounds."""

    try:
        with Image.open(path) as image:
            if "A" not in image.getbands():
                return NormalizedBox(0.0, 0.0, 1.0, 1.0)
            alpha = image.getchannel("A")
            bounds = alpha.getbbox()
            if not bounds:
                return NormalizedBox(0.0, 0.0, 0.0, 0.0)
            left, top, right, bottom = bounds
            return NormalizedBox(
                left / image.width,
                top / image.height,
                right / image.width,
                bottom / image.height,
            )
    except Exception:
        return NormalizedBox(0.0, 0.0, 1.0, 1.0)


def sample_face_boxes(
    video_path: str,
    anchor_times_s: list[float],
    *,
    max_samples: int = 12,
    timeout_s: float = 2.0,
) -> tuple[list[NormalizedBox], dict[str, Any]]:
    """Sample low-resolution face boxes at semantic anchors with a hard budget."""

    started = time.monotonic()
    boxes: list[NormalizedBox] = []
    attempted = 0
    try:
        import cv2  # noqa: PLC0415

        capture = cv2.VideoCapture(video_path)
        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        cascade = cv2.CascadeClassifier(cascade_path)
        for at_s in sorted(set(anchor_times_s))[:max_samples]:
            if time.monotonic() - started >= timeout_s:
                break
            attempted += 1
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, at_s) * 1000)
            ok, frame = capture.read()
            if not ok:
                continue
            height, width = frame.shape[:2]
            scale = min(1.0, 480.0 / max(width, height))
            small = cv2.resize(frame, None, fx=scale, fy=scale)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4)
            if len(faces) == 0:
                continue
            x, y, face_w, face_h = max(faces, key=lambda face: face[2] * face[3])
            sw, sh = small.shape[1], small.shape[0]
            boxes.append(
                NormalizedBox(x / sw, y / sh, (x + face_w) / sw, (y + face_h) / sh).padded(0.08)
            )
        capture.release()
    except Exception:
        boxes = []
    elapsed_ms = round((time.monotonic() - started) * 1000)
    return boxes, {
        "attempted": attempted,
        "detected": len(boxes),
        "elapsed_ms": elapsed_ms,
        "timed_out": elapsed_ms >= round(timeout_s * 1000),
    }


def _box_for_overlay(overlay: dict[str, Any]) -> NormalizedBox:
    width = min(1.0, max(0.05, float(overlay.get("scale") or 0.3)))
    height = width
    position = overlay.get("position")
    x = float(overlay.get("x_frac") or 0.5) if position == "custom" else 0.5
    y = (
        {"top": 0.18, "center": 0.5, "bottom": 0.82}.get(str(position), 0.5)
        if position != "custom"
        else float(overlay.get("y_frac") or 0.5)
    )
    return NormalizedBox(x - width / 2, y - height / 2, x + width / 2, y + height / 2)


def arbitrate_media_overlays(
    overlays: list[dict[str, Any]],
    *,
    protected_boxes: list[NormalizedBox],
    max_iou: float = 0.02,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reposition, shrink, then omit decorative PiP overlays on collisions."""

    resolved: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    candidates = ((0.2, 0.14), (0.8, 0.14), (0.2, 0.42), (0.8, 0.42), (0.5, 0.2))
    for source in overlays:
        overlay = dict(source)
        if overlay.get("display_mode") == "fullscreen":
            resolved.append(overlay)
            receipts.append({"id": overlay.get("id"), "decision": "fullscreen"})
            continue
        accepted: dict[str, Any] | None = None
        original_box = _box_for_overlay(overlay)
        for shrink in (1.0, 0.85, 0.70):
            for x_frac, y_frac in (
                (
                    (original_box.left + original_box.right) / 2,
                    (original_box.top + original_box.bottom) / 2,
                ),
                *candidates,
            ):
                trial = {
                    **overlay,
                    "position": "custom",
                    "x_frac": x_frac,
                    "y_frac": y_frac,
                    "scale": float(overlay.get("scale") or 0.3) * shrink,
                }
                box = _box_for_overlay(trial)
                if all(box.iou(protected) <= max_iou for protected in protected_boxes):
                    accepted = trial
                    break
            if accepted is not None:
                break
        if accepted is None:
            receipts.append({"id": overlay.get("id"), "decision": "omitted_no_safe_candidate"})
            continue
        decision = "kept"
        if accepted["scale"] < float(overlay.get("scale") or 0.3):
            decision = "shrunk"
        elif (accepted["x_frac"], accepted["y_frac"]) != (
            overlay.get("x_frac"),
            overlay.get("y_frac"),
        ):
            decision = "moved"
        accepted["smart_layout_box"] = _box_for_overlay(accepted).as_dict()
        resolved.append(accepted)
        receipts.append(
            {
                "id": overlay.get("id"),
                "decision": decision,
                "box": accepted["smart_layout_box"],
                "max_protected_iou": round(
                    max(
                        (
                            _box_for_overlay(accepted).iou(protected)
                            for protected in protected_boxes
                        ),
                        default=0.0,
                    ),
                    5,
                ),
            }
        )
    return resolved, receipts
