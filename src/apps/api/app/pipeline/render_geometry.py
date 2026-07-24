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
from collections.abc import Sequence
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

    def intersection_area(self, other: NormalizedBox) -> float:
        width = max(0.0, min(self.right, other.right) - max(self.left, other.left))
        height = max(0.0, min(self.bottom, other.bottom) - max(self.top, other.top))
        return width * height

    def coverage_by(self, other: NormalizedBox) -> float:
        """Fraction of THIS box's area that ``other`` covers (0 when this is empty).

        The caption-placement overlap metric (plan 011 Feature C, finding OV-6):
        unlike IoU, the denominator is fixed at the caption box's own area, so a
        larger face band can only INCREASE the reported overlap — it can never
        inflate the tolerance and certify a caption "clear" while it's on the face.
        """

        area = self.area
        return self.intersection_area(other) / area if area > 0 else 0.0

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


# Line-layout penalties (plan 011, Feature B). All are inert unless the caller
# passes keep_together pairs or penalize_widows — a bare measure_caption() is
# byte-identical to the pre-feature scoring. OVERFLOW dominates the others so a
# split that FITS always beats one that overflows at a given size (the shrink
# loop's iteration count is unchanged); BREAK/WIDOW only reorder among fitting
# splits. Pixel-scale base widths are < ~1100, so these magnitudes are decisive.
_OVERFLOW_PENALTY = 1_000_000.0
_BREAK_PENALTY = 1_000.0
_WIDOW_PENALTY = 500.0
_WIDOW_MAX_CHARS = 3


def _valid_keep_together(
    pairs: Sequence[tuple[int, int]] | None, word_count: int
) -> list[tuple[int, int]]:
    """Drop degenerate / out-of-range pairs (stale after a user cue-text edit)."""

    if not pairs:
        return []
    valid: list[tuple[int, int]] = []
    for pair in pairs:
        try:
            i, j = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 <= i < j <= word_count - 1:
            valid.append((i, j))
    return valid


def _split_breaks_pair(split: int, pairs: list[tuple[int, int]]) -> bool:
    """A break between word[split-1] and word[split] falls inside a kept pair."""

    return any(i < split <= j for i, j in pairs)


def _is_widow(lines: tuple[str, ...]) -> bool:
    """A line is a lone short word while the cue has >= 3 words total."""

    total = sum(len(line.split()) for line in lines)
    if total < 3:
        return False
    return any(len(line.split()) == 1 and len(line.strip()) <= _WIDOW_MAX_CHARS for line in lines)


def measure_caption(
    text: str,
    *,
    font_family: str,
    font_size_px: int,
    width_frac: float,
    y_frac: float,
    max_lines: int,
    canvas: Canvas = PORTRAIT,
    keep_together: Sequence[tuple[int, int]] | None = None,
    penalize_widows: bool = False,
) -> TextMeasurement:
    """Wrap with the production Skia typeface and return its normalized box.

    ``keep_together`` pairs (cue-relative, inclusive word indexes) must not be
    split across the two lines; ``penalize_widows`` discourages a lone short word
    on its own line. Both are soft, fit-first preferences: a split that fits the
    width always wins over one that overflows, so honoring them never forces an
    extra shrink. When neither is supplied the scoring is byte-identical to the
    pre-feature behavior (the ``SMART_CAPTION_LAYOUT_BALANCE_ENABLED`` /
    emphasis kill switches).
    """

    words = [word for word in text.split() if word]
    if not words:
        box = NormalizedBox(0.5, y_frac, 0.5, y_frac)
        return TextMeasurement(lines=(), font_size_px=font_size_px, box=box)
    max_width_px = canvas.width * width_frac
    pairs = _valid_keep_together(keep_together, len(words))
    apply_penalties = bool(pairs) or penalize_widows
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
                score = max(widths) + abs(widths[0] - widths[1]) * 0.08
                if apply_penalties:
                    if max(widths) > max_width_px:
                        score += _OVERFLOW_PENALTY
                    if pairs and _split_breaks_pair(split, pairs):
                        score += _BREAK_PENALTY
                    if penalize_widows and _is_widow(lines):
                        score += _WIDOW_PENALTY
                candidates.append((score, lines))
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


def _face_protection_box(raw: NormalizedBox) -> NormalizedBox:
    """Turn a raw Haar detection into the box the layout engine protects.

    Haar on low-res selfie frames merges background into giant detections (a
    "face" spanning 70% of the frame width) and a uniform 0.08 padding then
    blankets the empty band above the hairline — together they blocked every
    top corner and broke the four-corner flag composition (2026-07-21).
    Clamp implausible widths to a centered head-size box and keep the TOP
    padding thin; sides and chin stay generously padded.
    """

    if raw.width > 0.55:
        center_x = (raw.left + raw.right) / 2
        raw = NormalizedBox(
            max(0.0, center_x - 0.275), raw.top, min(1.0, center_x + 0.275), raw.bottom
        )
    return NormalizedBox(
        max(0.0, raw.left - 0.06),
        max(0.0, raw.top - 0.02),
        min(1.0, raw.right + 0.06),
        min(1.0, raw.bottom + 0.08),
    )


def sample_face_regions(
    video_path: str,
    anchor_times_s: list[float],
    *,
    max_samples: int = 12,
    timeout_s: float = 2.0,
    count_decoded: bool = False,
) -> tuple[list[ProtectedRegion], dict[str, Any]]:
    """Sample faces in a killable subprocess so the wall-clock budget is real.

    ``count_decoded`` (default off ⇒ every legacy caller's receipt is byte-
    identical) adds a ``decoded`` field — the number of anchors that produced a
    decodable frame — which the caption-placement chooser uses as its coverage
    denominator (plan 011 Feature C).
    """

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
    decoded: int | None = None
    worker_error: str | None = None
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
            attempted = int(payload.get("attempted") or attempted)
            raw_decoded = payload.get("decoded")
            if raw_decoded is not None:
                decoded = int(raw_decoded)
            for sample in payload.get("samples") or []:
                at_s = max(0.0, float(sample["at_s"]))
                box = _face_protection_box(NormalizedBox(**sample["box"]))
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
    if count_decoded:
        # Fall back to attempted when a legacy worker image doesn't report it
        # (anchors on the rendered base ⇒ attempted == decodable in that case).
        receipt["decoded"] = decoded if decoded is not None else attempted
    if worker_error is not None:
        receipt["worker_error"] = worker_error
    return regions, receipt


# ── Face-aware caption placement (plan 011 Feature C) ────────────────────────
# Platform UI chrome the caption band must clear (TikTok/Reels/Shorts own the top
# and bottom edges). A candidate whose translated caption box crosses either safe
# edge is rejected even if it clears every face/title box.
# NOTE the coupling: the bottom edge equals captions.CAPTION_Y_FRAC_MAX, and a
# candidate's box bottom IS its (already clamped) y_frac, so the bottom check is
# currently a tautology and only the TOP edge can reject. That is deliberate
# belt-and-braces — if either constant is ever changed independently the check
# starts biting, which is why it is written out rather than dropped.
_CAPTION_TOP_SAFE_FRAC = 0.10
_CAPTION_BOTTOM_SAFE_FRAC = 0.90
# A candidate is "clear" when the caption box's own area is ≤ 5% covered by the
# face band / a title box (coverage-fraction, NOT IoU — finding OV-6). 5% matches
# the design doc's face-overlap spike policy.
_FACE_OVERLAP_MAX_COVERAGE = 0.05
# The dominant face band only exists when the SAME face recurs on ≥ 60% of
# decodable frames; below that it flickers in and out and isn't worth moving
# captions for.
_DOMINANT_FACE_MIN_PRESENCE = 0.60
# Fewer than three decodable anchors is too little signal — keep the preset.
_MIN_USABLE_ANCHORS = 3
# Two detections belong to the same face when their boxes overlap this much.
# Haar occasionally fires once on background texture; a blind union of every
# detection would let that single spurious box inflate the band across half the
# frame and drag the caption off a face that never moved.
_FACE_CLUSTER_MIN_IOU = 0.30


def _union_box(boxes: list[NormalizedBox]) -> NormalizedBox:
    return NormalizedBox(
        min(box.left for box in boxes),
        min(box.top for box in boxes),
        max(box.right for box in boxes),
        max(box.bottom for box in boxes),
    )


def _dominant_face_cluster(boxes: list[NormalizedBox]) -> list[NormalizedBox]:
    """Largest group of mutually-overlapping detections — the recurring face.

    The design reads "union of padded face boxes PRESENT ON ≥ 60% of anchors":
    the band is built from the box that keeps showing up, not from every box the
    detector ever emitted. Greedy single-link grouping against each cluster's
    running union; ties resolve to the earliest cluster so the result is
    deterministic.
    """

    clusters: list[list[NormalizedBox]] = []
    for box in boxes:
        for cluster in clusters:
            if _union_box(cluster).iou(box) >= _FACE_CLUSTER_MIN_IOU:
                cluster.append(box)
                break
        else:
            clusters.append([box])
    return max(clusters, key=len) if clusters else []


def _translate_box_to_y(box: NormalizedBox, y_frac: float) -> NormalizedBox:
    """Move a caption box so its BOTTOM edge sits at ``y_frac``; size unchanged.

    Wrapping/shrinking is y-independent, so the probe box is measured ONCE and
    only arithmetically translated per candidate (plan 011 Feature C).
    """

    height = box.height
    return NormalizedBox(box.left, max(0.0, y_frac - height), box.right, min(1.0, y_frac))


def choose_caption_y_frac(
    face_regions: list[ProtectedRegion],
    face_receipt: dict[str, Any],
    caption_probe_boxes: Sequence[NormalizedBox],
    title_boxes: list[ProtectedRegion],
    candidates: tuple[float, ...],
) -> tuple[float, dict[str, Any]]:
    """Pick ONE static caption y_frac that keeps the band off the speaker's face.

    Pure and FAIL-OPEN — never raises. ``candidates[0]`` is the preset and is
    always tried first, so a well-framed video changes nothing. The overlap gate
    is COVERAGE-FRACTION (intersection ÷ caption-box area ≤ 5%), NOT IoU: IoU's
    denominator grows with the face-band size and would invert the incentive
    (finding OV-6). ``caption_probe_boxes`` carries EVERY distinct measured cue
    shape and a candidate must clear the gate for all of them — see the comment
    at the evaluation loop for why no single box is the worst case. Every receipt
    embeds the raw sampler receipt under
    ``face_sampler`` so a structurally broken cv2 worker is distinguishable from a
    genuinely well-framed clip in /admin/jobs (finding QUAL-2). On any of
    no-face / timeout / error / < 3 usable anchors the preset is returned with a
    ``reason`` in ``{no_face, sampler_timeout, sampler_error, insufficient_anchors}``;
    when a real band exists but no candidate is both clear and chrome-safe, the
    least-overlap candidate is returned with ``status == "best_effort"``.
    """

    from app.pipeline.captions import clamp_caption_y_frac  # noqa: PLC0415

    ladder = tuple(clamp_caption_y_frac(value) for value in candidates) or (0.705,)
    preset_y = ladder[0]

    def _preset(reason: str, **extra: Any) -> tuple[float, dict[str, Any]]:
        receipt: dict[str, Any] = {
            "status": "preset",
            "reason": reason,
            "chosen_y_frac": preset_y,
            "preset_y_frac": preset_y,
            "candidates": list(ladder),
            "face_sampler": dict(face_receipt),
        }
        receipt.update(extra)
        return preset_y, receipt

    if face_receipt.get("timed_out"):
        return _preset("sampler_timeout")
    if face_receipt.get("worker_error"):
        return _preset("sampler_error")
    raw_decoded = face_receipt.get("decoded")
    if raw_decoded is not None:
        decoded = int(raw_decoded)
    else:
        decoded = int(face_receipt.get("attempted") or 0)
    if decoded < _MIN_USABLE_ANCHORS:
        return _preset("insufficient_anchors", decoded=decoded)
    if not face_regions:
        return _preset("no_face", decoded=decoded)
    # Presence counts the RECURRING face, not every detection: a lone spurious
    # box must not certify a "dominant" band, and must not widen one either.
    cluster = _dominant_face_cluster([region.box for region in face_regions])
    presence = len(cluster) / decoded
    if presence < _DOMINANT_FACE_MIN_PRESENCE:
        return _preset(
            "no_face",
            decoded=decoded,
            face_presence=round(presence, 3),
            detections=len(face_regions),
        )

    band = _union_box(cluster)
    protected = [band, *[title.box for title in title_boxes]]

    # EVERY distinct cue shape is probed, not just the tallest. The gate divides by
    # the probe's OWN area, so no single cue is the universal worst case: against a
    # face band near the caption's bottom edge a SHORT one-line cue reports far more
    # coverage than a tall two-line one, while a band higher up only collides with
    # the tall box — and the true maximum can fall at an intermediate height. A
    # candidate must therefore clear the gate for ALL shapes.
    probes = list(caption_probe_boxes) or [NormalizedBox(0.3, 0.58, 0.7, 0.705)]

    evaluated: list[dict[str, Any]] = []
    for index, y_frac in enumerate(ladder):
        translated = [_translate_box_to_y(probe, y_frac) for probe in probes]
        coverage = round(
            max(
                (box.coverage_by(area) for box in translated for area in protected),
                default=0.0,
            ),
            5,
        )
        clears_chrome = all(
            box.top >= _CAPTION_TOP_SAFE_FRAC and box.bottom <= _CAPTION_BOTTOM_SAFE_FRAC
            for box in translated
        )
        evaluated.append({"y_frac": y_frac, "coverage": coverage, "clears_chrome": clears_chrome})
        if coverage <= _FACE_OVERLAP_MAX_COVERAGE and clears_chrome:
            return y_frac, {
                "status": "well_framed" if index == 0 else "moved",
                "chosen_y_frac": y_frac,
                "preset_y_frac": preset_y,
                "candidate_index": index,
                "candidates": list(ladder),
                "coverage": coverage,
                "face_band": band.as_dict(),
                "face_presence": round(presence, 3),
                "decoded": decoded,
                "evaluated": evaluated,
                "face_sampler": dict(face_receipt),
            }

    # No candidate is both clear and chrome-safe → least-overlap fallback.
    # Chrome-safety is the PRIMARY key, not a tiebreak: a caption pushed off the
    # top edge or under the platform UI is unreadable, which is strictly worse
    # than one that merely overlaps the face. Ranking coverage first would let a
    # candidate the chrome gate just rejected win on a low face-overlap score —
    # incoherent, since the same function already deemed that position unsafe.
    # Only if NO candidate clears chrome does coverage alone decide.
    best = min(
        range(len(ladder)),
        key=lambda i: (
            0 if evaluated[i]["clears_chrome"] else 1,
            evaluated[i]["coverage"],
            i,
        ),
    )
    return ladder[best], {
        "status": "best_effort",
        "chosen_y_frac": ladder[best],
        "preset_y_frac": preset_y,
        "candidate_index": best,
        "candidates": list(ladder),
        "coverage": evaluated[best]["coverage"],
        "face_band": band.as_dict(),
        "face_presence": round(presence, 3),
        "decoded": decoded,
        "evaluated": evaluated,
        "face_sampler": dict(face_receipt),
    }


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
    placed_assets: list[tuple[float, float, str]] = []
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
        asset_key = str(overlay.get("src_gcs_path") or overlay.get("asset_id") or "")
        if asset_key and any(
            key == asset_key and start_s < placed_end and placed_start < end_s
            for placed_start, placed_end, key in placed_assets
        ):
            # The same asset visible twice at once reads as a glitch, not a
            # composition (2026-07-21 duplicate-Spain report) — whatever lane
            # produced the second card, only the first placement renders.
            receipts.append({"id": overlay.get("id"), "decision": "omitted_duplicate_asset"})
            continue
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

        # Fallback spots rank in three tiers — true corners, then edges, then
        # the center column as a last resort — and closest to the card's
        # assigned spot within a tier. The loop is SPOT-major: every shrink is
        # tried at a preferred spot before moving on, because a slightly
        # smaller flag in the free corner beats a full-size one parked under
        # the captions (2026-07-21 France-flag-in-the-middle report; the
        # padded face box only misses the corner gate at reduced scale).
        def _spot_tier(spot: tuple[float, float]) -> int:
            if abs(spot[0] - 0.5) < 0.1:
                return 2
            return 0 if spot[1] <= 0.2 or spot[1] >= 0.75 else 1

        ranked_candidates = sorted(
            candidates,
            key=lambda spot: (
                _spot_tier(spot),
                (spot[0] - original_x) ** 2 + (spot[1] - original_y) ** 2,
            ),
        )
        for x_frac, y_frac in (
            (original_x, original_y),
            *ranked_candidates,
        ):
            for shrink in (1.0, 0.85, 0.70, 0.55):
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
        if asset_key:
            placed_assets.append((start_s, end_s, asset_key))
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
