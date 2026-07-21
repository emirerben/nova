"""Shared, deterministic geometry for Smart Captions v2 renderers.

The compiler, ASS writer, Skia title renderer, and media compositor must agree
about the boxes they protect.  This module owns normalized boxes, renderer-font
text measurement, bounded face sampling, alpha bounds, and the conservative
reposition/shrink/omit policy for decorative media.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
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
class ProtectedRegion:
    start_s: float
    end_s: float
    box: NormalizedBox
    kind: str = "protected"

    def overlaps(self, start_s: float, end_s: float) -> bool:
        return start_s < self.end_s and self.start_s < end_s

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_s": round(self.start_s, 3),
            "end_s": round(self.end_s, 3),
            "kind": self.kind,
            "box": self.box.as_dict(),
        }

    @classmethod
    def from_value(cls, value: ProtectedRegion | NormalizedBox | dict[str, Any]) -> ProtectedRegion:
        if isinstance(value, cls):
            return value
        if isinstance(value, NormalizedBox):
            return cls(0.0, float("inf"), value)
        raw_box = value.get("box") if isinstance(value.get("box"), dict) else value
        return cls(
            start_s=max(0.0, float(value.get("start_s") or 0.0)),
            end_s=max(float(value.get("end_s") or float("inf")), 0.0),
            box=NormalizedBox(
                left=float(raw_box["left"]),
                top=float(raw_box["top"]),
                right=float(raw_box["right"]),
                bottom=float(raw_box["bottom"]),
            ),
            kind=str(value.get("kind") or "protected"),
        )


@dataclass(frozen=True, slots=True)
class MediaFootprint:
    aspect_ratio: float = 1.0
    opaque_bounds: NormalizedBox = NormalizedBox(0.0, 0.0, 1.0, 1.0)


@dataclass(frozen=True, slots=True)
class PreparedMediaAsset:
    src_gcs_path: str
    local_path: str
    has_alpha: bool
    opaque_bounds: NormalizedBox
    width_px: int
    height_px: int

    @property
    def footprint(self) -> MediaFootprint:
        aspect = self.width_px / self.height_px if self.width_px > 0 and self.height_px > 0 else 1.0
        return MediaFootprint(aspect_ratio=aspect, opaque_bounds=self.opaque_bounds)


@lru_cache(maxsize=32)
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
    height_frac = height / canvas.height
    box = NormalizedBox(
        max(0.0, 0.5 - half_w),
        max(0.0, y_frac - height_frac),
        min(1.0, 0.5 + half_w),
        min(1.0, y_frac),
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


def sample_face_regions(
    video_path: str,
    anchor_times_s: list[float],
    *,
    max_samples: int = 12,
    timeout_s: float = 2.0,
) -> tuple[list[ProtectedRegion], dict[str, Any]]:
    """Sample faces in a killable subprocess so the wall-clock budget is real."""

    started = time.monotonic()
    anchors = sorted({max(0.0, float(value)) for value in anchor_times_s})[:max_samples]
    command = [
        sys.executable,
        "-m",
        "app.pipeline.face_sampler_worker",
        video_path,
        json.dumps(anchors, separators=(",", ":")),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(0.01, timeout_s),
            check=False,
        )
    except subprocess.TimeoutExpired:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        return [], {
            "attempted": len(anchors),
            "detected": 0,
            "elapsed_ms": elapsed_ms,
            "timed_out": True,
        }
    regions: list[ProtectedRegion] = []
    attempted = len(anchors)
    worker_error: str | None = None
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
            attempted = int(payload.get("attempted") or attempted)
            for sample in payload.get("samples") or []:
                at_s = max(0.0, float(sample["at_s"]))
                box = NormalizedBox(**sample["box"]).padded(0.08)
                regions.append(
                    ProtectedRegion(
                        start_s=max(0.0, at_s - 0.5),
                        end_s=at_s + 0.5,
                        box=box,
                        kind="face",
                    )
                )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            regions = []
            worker_error = f"bad_payload:{type(exc).__name__}"
    else:
        # A structurally broken sampler (cv2 missing, unimportable module) must
        # be distinguishable from a real zero-face clip in the receipts, or the
        # protection silently never runs and nobody notices.
        worker_error = f"rc_{result.returncode}:{(result.stderr or '').strip()[:120]}"
    elapsed_ms = round((time.monotonic() - started) * 1000)
    receipt: dict[str, Any] = {
        "attempted": attempted,
        "detected": len(regions),
        "elapsed_ms": elapsed_ms,
        "timed_out": False,
    }
    if worker_error is not None:
        receipt["worker_error"] = worker_error
    return regions, receipt


def media_dimensions(path: str) -> tuple[int, int]:
    """Return real pixel dimensions for image or video assets, square on failure."""

    try:
        with Image.open(path) as image:
            return max(1, image.width), max(1, image.height)
    except Exception:
        pass
    try:
        output = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                path,
            ],
            text=True,
            timeout=10,
            stderr=subprocess.DEVNULL,
        )
        stream = (json.loads(output).get("streams") or [])[0]
        return max(1, int(stream["width"])), max(1, int(stream["height"]))
    except Exception:
        return 1, 1


def _box_for_overlay(
    overlay: dict[str, Any],
    *,
    footprint: MediaFootprint | None = None,
    canvas: Canvas = PORTRAIT,
) -> NormalizedBox:
    width = min(1.0, max(0.05, float(overlay.get("scale") or 0.3)))
    footprint = footprint or MediaFootprint()
    aspect = max(0.01, footprint.aspect_ratio)
    height = width * canvas.width / canvas.height / aspect
    x, y = _center_for_overlay(overlay)
    left = x - width / 2
    top = y - height / 2
    opaque = footprint.opaque_bounds
    return NormalizedBox(
        max(0.0, left + opaque.left * width),
        max(0.0, top + opaque.top * height),
        min(1.0, left + opaque.right * width),
        min(1.0, top + opaque.bottom * height),
    )


def _center_for_overlay(overlay: dict[str, Any]) -> tuple[float, float]:
    position = overlay.get("position")
    raw_x = overlay.get("x_frac")
    raw_y = overlay.get("y_frac")
    x = float(raw_x if raw_x is not None else 0.5) if position == "custom" else 0.5
    y = (
        {"top": 0.18, "center": 0.5, "bottom": 0.82}.get(str(position), 0.5)
        if position != "custom"
        else float(raw_y if raw_y is not None else 0.5)
    )
    return x, y


def arbitrate_media_overlays(
    overlays: list[dict[str, Any]],
    *,
    protected_boxes: list[ProtectedRegion | NormalizedBox | dict[str, Any]],
    footprints_by_id: dict[str, MediaFootprint] | None = None,
    max_iou: float = 0.02,
    canvas: Canvas = PORTRAIT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reposition, shrink, then omit decorative PiP overlays on collisions."""

    resolved: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    protected_regions = [ProtectedRegion.from_value(value) for value in protected_boxes]
    footprints_by_id = footprints_by_id or {}
    occupied: list[tuple[float, float, NormalizedBox]] = []
    candidates = (
        (0.2, 0.14),
        (0.8, 0.14),
        (0.2, 0.42),
        (0.8, 0.42),
        (0.5, 0.2),
        # Lower-band fallbacks: on talk-to-camera footage the face and the
        # chapter heading own the whole top half, so a top-only grid omitted
        # every chapter visual as no_safe_candidate (2026-07-21 player-photo
        # report). Captions are protected regions, so the IoU gate still
        # rejects any lower spot the caption band actually occupies.
        (0.2, 0.68),
        (0.8, 0.68),
        (0.19, 0.81),
        (0.81, 0.81),
        (0.5, 0.74),
    )
    for source in overlays:
        overlay = dict(source)
        if overlay.get("display_mode") == "fullscreen":
            resolved.append(overlay)
            receipts.append({"id": overlay.get("id"), "decision": "fullscreen"})
            continue
        start_s = float(overlay.get("start_s") or 0.0)
        end_s = float(overlay.get("end_s") or float("inf"))
        footprint = footprints_by_id.get(str(overlay.get("id")), MediaFootprint())
        collision_boxes = [
            *[region.box for region in protected_regions if region.overlaps(start_s, end_s)],
            *[
                box
                for occupied_start, occupied_end, box in occupied
                if start_s < occupied_end and occupied_start < end_s
            ],
        ]
        accepted: dict[str, Any] | None = None
        original_x, original_y = _center_for_overlay(overlay)
        for shrink in (1.0, 0.85, 0.70, 0.55):
            for x_frac, y_frac in (
                (original_x, original_y),
                *candidates,
            ):
                trial = {
                    **overlay,
                    "position": "custom",
                    "x_frac": x_frac,
                    "y_frac": y_frac,
                    "scale": float(overlay.get("scale") or 0.3) * shrink,
                }
                box = _box_for_overlay(trial, footprint=footprint, canvas=canvas)
                if all(box.iou(protected) <= max_iou for protected in collision_boxes):
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
        elif not (
            abs(float(accepted["x_frac"]) - original_x) < 1e-9
            and abs(float(accepted["y_frac"]) - original_y) < 1e-9
        ):
            decision = "moved"
        accepted_box = _box_for_overlay(accepted, footprint=footprint, canvas=canvas)
        accepted["smart_layout_box"] = accepted_box.as_dict()
        resolved.append(accepted)
        occupied.append((start_s, end_s, accepted_box))
        receipts.append(
            {
                "id": overlay.get("id"),
                "decision": decision,
                "box": accepted["smart_layout_box"],
                "max_protected_iou": round(
                    max(
                        (
                            accepted_box.iou(region.box)
                            for region in protected_regions
                            if region.overlaps(start_s, end_s)
                        ),
                        default=0.0,
                    ),
                    5,
                ),
            }
        )
    return resolved, receipts
