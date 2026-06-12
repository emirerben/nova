"""Pre-PR text-overlay render-verify: prove the burned Skia text matches the recipe.

Why this exists
---------------
Agentic + music jobs burn text via the Skia renderer (`text_overlay_skia.py`).
The admin preview renders via Pillow, so a Skia-only regression ("looks right
locally, clips in prod" — the #296 class, e.g. prod job ff0d2e1c rendering
``text_anchor="left"`` as "s not just luck" instead of "It's not just luck")
never shows up until the burned video ships. CLAUDE.md states the rule in prose:
"an agentic/music overlay change is NOT verified by the admin preview — verify
the actual burned (Skia) video." This module is that rule as code.

The source of truth is the RECIPE: for each overlay we render through the real
Skia path and assert the declared text appears un-clipped, at the declared anchor.
This isolates the renderer — it does NOT check whether the agent extracted the
right text (that is a separate problem, see the text-overlay-strategy design doc).

Two render sources, one set of detectors
----------------------------------------
- ``RENDER_DRAW_FRAME`` (default, fast): calls ``_draw_frame`` — the exact Skia
  canvas prod uses — and inspects the transparent PNG. The production ffmpeg
  step is ``overlay=0:0`` (full-canvas composite at the origin); it physically
  cannot introduce horizontal text clipping that is not already in the PNG, so
  the draw-frame output is faithful for the clipping/anchor class and needs no
  ffmpeg. This is what the existing parity tests in
  ``tests/pipeline/test_text_overlay_skia.py`` already trust.
- ``RENDER_BURN`` (integration): runs the real ``burn_text_overlays_skia`` onto
  a synthetic solid-color clip and extracts the overlay's mid-time frame. Slower
  (ffmpeg per overlay) but exercises the image2 demuxer + overlay/setpts compositing
  and the timing window. Use it to confirm an overlay actually appears in a burned mp4.

Run ``_draw_frame`` rendering inside the prod Docker image (``make verify-overlays``)
so fonts (fonts-dejavu-core + bundled Playfair) match prod. OCR runs on the host
(no tesseract in the prod image) — see ``app/cli/verify_overlays.py``.

Detector verdicts (per overlay)
--------------------------------
- clipping: FAIL if the rendered text touches a horizontal frame edge (the #296
  signature). Robust, font-independent — this is the primary signal.
- content: OCR vs spec text via normalized similarity. Advisory: stylized fonts
  (Playfair italic, font-cycle) trip OCR, so a clean bbox + noisy OCR is a WARN,
  not a FAIL. A truncated read (OCR is a strict prefix/suffix of the spec — the
  "s not just luck" signature) IS a FAIL.

         recipe overlay
              │
       ┌──────┴───────┐
       ▼              ▼
  _draw_frame    burn_text_overlays_skia → ffmpeg extract frame
   (RGBA PNG)        (solid-bg frame)
       │              │
       └──────┬───────┘
              ▼
   text_bbox()  ── clipping verdict (edge proximity)
              ▼
   pytesseract ── content verdict (similarity vs spec.text)
              ▼
        OverlayResult → VerifyReport (overall = worst verdict)
"""

from __future__ import annotations

import difflib
import io
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from PIL import Image, ImageChops

from app.pipeline import text_overlay_skia as tos
from app.pipeline.font_identification import extract_frame_png

# -- Tunables ----------------------------------------------------------------

# Horizontal clipping margin. Legitimate text stays clear of the edges: the
# renderer wraps at _MAX_LINE_W_FRAC=0.9 (≥5%≈54px side margins) and left-anchored
# overlays sit at position_x_frac≈0.05 (≈54px). A clipped line sits at x≈0. 6px
# cleanly separates "clipped" from "legitimately near the edge" while tolerating
# anti-aliasing. Vertical clipping is not the documented failure mode, so we only
# flag literal top/bottom cut-off (margin 0).
EDGE_MARGIN_PX = 6

# Content similarity (difflib ratio on normalized text; stdlib, no extra dep).
SIM_FAIL = 0.70  # below this → FAIL (text is wrong / mostly missing)
SIM_WARN = 0.88  # between FAIL and WARN → WARN (likely OCR noise, eyeball it)

# Synthetic-clip frame rate for the burn path. Matches the Skia sequence FPS.
_BURN_FPS = 30

Verdict = Literal["PASS", "FAIL", "WARN", "SKIPPED"]
RenderMode = Literal["draw_frame", "burn"]

RENDER_DRAW_FRAME: RenderMode = "draw_frame"
RENDER_BURN: RenderMode = "burn"

_WORST = {"SKIPPED": 0, "PASS": 1, "WARN": 2, "FAIL": 3}


def _worst(*verdicts: str) -> Verdict:
    return max(verdicts, key=lambda v: _WORST.get(v, 0))  # type: ignore[return-value]


def frame_name(slot_index: int, overlay_index: int) -> str:
    """Deterministic per-overlay frame filename. Both the OCR stage and the
    montage resolve frames by this name under ``<out>/frames`` rather than the
    stored absolute path, so a report rendered inside the prod Docker container
    (``/app/...``) is still readable from the host OCR stage."""
    return f"slot{slot_index:02d}_ov{overlay_index:02d}.png"


# -- Result types ------------------------------------------------------------


@dataclass
class OverlayResult:
    slot_index: int
    overlay_index: int
    text: str
    effect: str
    text_anchor: str
    text_color: str
    verdict: Verdict
    clipping: Verdict
    content: Verdict
    reasons: list[str] = field(default_factory=list)
    ocr_text: str | None = None
    similarity: float | None = None
    bbox: tuple[int, int, int, int] | None = None
    frame_path: str | None = None
    resolved_typeface: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerifyReport:
    overlays: list[OverlayResult] = field(default_factory=list)
    render_mode: str = RENDER_DRAW_FRAME
    ocr_available: bool = False

    @property
    def overall(self) -> Verdict:
        if not self.overlays:
            return "SKIPPED"
        return _worst(*(o.verdict for o in self.overlays))

    @property
    def counts(self) -> dict[str, int]:
        c = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIPPED": 0}
        for o in self.overlays:
            c[o.verdict] += 1
        return c

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "render_mode": self.render_mode,
            "ocr_available": self.ocr_available,
            "counts": self.counts,
            "overlays": [o.to_dict() for o in self.overlays],
        }


# -- Text normalization + similarity -----------------------------------------


def _normalize(s: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — so "It's not just
    luck!" and "its not just luck" compare equal. OCR drops/garbles punctuation
    constantly; punctuation is not the signal we gate on."""
    s = s.replace("\n", " ").replace("\\n", " ").replace("\\N", " ")
    s = re.sub(r"[^\w\s]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def check_content(ocr_text: str | None, spec_text: str) -> tuple[Verdict, float | None, str]:
    """Compare OCR'd text to the recipe's declared text.

    FAIL when the read is wrong/mostly-missing OR a clean truncation of the spec
    (the "s not just luck" signature: OCR is a strict substring of the spec that
    drops a prefix/suffix). WARN when similarity is in the murky middle (likely
    OCR noise on a stylized font — the bbox check is the real signal there).
    """
    spec_n = _normalize(spec_text)
    if not spec_n:
        return "SKIPPED", None, "spec text empty"
    if ocr_text is None:
        return "SKIPPED", None, "OCR unavailable"
    ocr_n = _normalize(ocr_text)
    if not ocr_n:
        return "FAIL", 0.0, "OCR found no text where the recipe declares text"

    ratio = difflib.SequenceMatcher(None, ocr_n, spec_n).ratio()

    # Truncation signature: OCR is a contiguous substring of the spec but missing
    # a non-trivial prefix or suffix. This is exactly the #296 left-clip bug.
    if ocr_n in spec_n and ocr_n != spec_n:
        dropped = len(spec_n) - len(ocr_n)
        if dropped >= 2:
            return (
                "FAIL",
                ratio,
                f"OCR text is a truncation of the recipe text "
                f"(read {ocr_n!r}, expected {spec_n!r}) — likely clipped/dropped",
            )

    if ratio >= SIM_WARN:
        return "PASS", ratio, f"OCR≈recipe (similarity {ratio:.2f})"
    if ratio >= SIM_FAIL:
        return (
            "WARN",
            ratio,
            f"OCR partially matches (similarity {ratio:.2f}) — likely stylized-font "
            f"OCR noise; eyeball the montage. read {ocr_n!r} vs {spec_n!r}",
        )
    return (
        "FAIL",
        ratio,
        f"OCR text does not match recipe (similarity {ratio:.2f}): "
        f"read {ocr_n!r}, expected {spec_n!r}",
    )


# -- Bounding box + clipping --------------------------------------------------


def text_bbox(frame: Image.Image, bg_hex: str | None = None) -> tuple[int, int, int, int] | None:
    """Bounding box of the rendered text in a frame.

    Transparent source (``_draw_frame`` output, ``bg_hex is None``): the opaque
    pixels ARE the text — use ``getbbox()`` on the alpha, like the parity tests.
    Solid-background source (burned-frame extraction, ``bg_hex`` set): the text is
    wherever the frame differs from the known background color.
    """
    if bg_hex is None:
        return frame.convert("RGBA").getbbox()
    bg = Image.new("RGB", frame.size, _hex_to_rgb(bg_hex))
    diff = ImageChops.difference(frame.convert("RGB"), bg).convert("L")
    # Threshold out JPEG/encode noise so only real glyph pixels survive.
    mask = diff.point(lambda p: 255 if p > 24 else 0)
    return mask.getbbox()


def check_clipping(
    bbox: tuple[int, int, int, int] | None, frame_w: int, frame_h: int
) -> tuple[Verdict, str]:
    """FAIL if the text touches a horizontal frame edge (the #296 signature).

    Horizontal is strict (EDGE_MARGIN_PX); vertical only flags literal cut-off,
    since near-top/near-bottom placement is legitimate and not the failure mode
    we are guarding."""
    if bbox is None:
        return "FAIL", "no text rendered (empty frame) where the recipe declares text"
    left, top, right, bottom = bbox
    if left <= EDGE_MARGIN_PX:
        return "FAIL", f"text clipped at LEFT edge (bbox left={left}px ≤ {EDGE_MARGIN_PX}px)"
    if right >= frame_w - EDGE_MARGIN_PX:
        edge = frame_w - EDGE_MARGIN_PX
        return "FAIL", f"text clipped at RIGHT edge (bbox right={right}px ≥ {edge}px)"
    if top <= 0:
        return "FAIL", f"text clipped at TOP edge (bbox top={top}px)"
    if bottom >= frame_h:
        return "FAIL", f"text clipped at BOTTOM edge (bbox bottom={bottom}px)"
    return "PASS", "text fully inside frame"


# -- Render sources -----------------------------------------------------------


def _settled_t(duration_s: float) -> float:
    """Pick a render time where reveal/pop/font-cycle animations have settled,
    so we inspect the final composed text rather than a mid-animation frame."""
    if duration_s <= 0:
        return 0.0
    return max(0.0, min(duration_s * 0.95, duration_s - 0.05))


def render_overlay_frame(overlay: dict) -> Image.Image:
    """Render one overlay via the real Skia ``_draw_frame`` (transparent RGBA)."""
    duration_s = max(0.0, float(overlay.get("end_s", 0.0)) - float(overlay.get("start_s", 0.0)))
    img = tos._draw_frame(overlay, _settled_t(duration_s), max(duration_s, 0.5))
    return Image.open(io.BytesIO(bytes(img.encodeToData()))).convert("RGBA")


def burn_overlay_to_frame(overlay: dict, bg_hex: str, tmpdir: str) -> Image.Image:
    """Run the REAL prod burn (``burn_text_overlays_skia``) on a synthetic
    solid-color clip and return the overlay's settled mid-time frame on that bg.

    Exercises the full prod compositing path (Skia PNG sequence → image2 demuxer
    → overlay/setpts), not just the canvas."""
    start_s = float(overlay.get("start_s", 0.0))
    end_s = float(overlay.get("end_s", 0.0))
    dur = max(end_s, start_s + 0.5) + 0.5  # clip must outlast the overlay
    clip = os.path.join(tmpdir, "bg.mp4")
    out = os.path.join(tmpdir, "burned.mp4")
    color = _hex_to_rgb(bg_hex)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x{bg_hex.lstrip('#')}:s={tos.CANVAS_W}x{tos.CANVAS_H}:r={_BURN_FPS}",
            "-t",
            f"{dur:.3f}",
            "-pix_fmt",
            "yuv420p",
            "-loglevel",
            "error",
            clip,
        ],
        capture_output=True,
        timeout=120,
        check=True,
    )
    tos.burn_text_overlays_skia(clip, [overlay], out, tmpdir)
    sample_t = start_s + _settled_t(end_s - start_s)
    frame_png = extract_frame_png(out, sample_t)
    _ = color  # bg color is encoded into the clip; kept for symmetry/readability
    return Image.open(io.BytesIO(frame_png)).convert("RGB")


# -- OCR (host side; lazy import so the prod image can run render-only) --------


def ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def ocr_text(frame: Image.Image, text_color_hex: str | None) -> str | None:
    """OCR a frame. Composite transparent frames onto a contrasting solid so the
    glyphs sit on a clean background (ideal OCR conditions we control)."""
    try:
        import pytesseract
    except Exception:
        return None
    if frame.mode == "RGBA":
        bg_hex = _contrast_bg(text_color_hex)
        canvas = Image.new("RGB", frame.size, _hex_to_rgb(bg_hex))
        canvas.paste(frame, (0, 0), frame)
        frame = canvas
    try:
        return pytesseract.image_to_string(frame)
    except Exception:
        return None


# -- Color helpers ------------------------------------------------------------


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _luminance(hex_str: str) -> float:
    r, g, b = _hex_to_rgb(hex_str)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _contrast_bg(text_color_hex: str | None) -> str:
    """Black behind light text, white behind dark text — maximize OCR contrast."""
    if not text_color_hex:
        return "#000000"
    try:
        return "#000000" if _luminance(text_color_hex) >= 0.5 else "#FFFFFF"
    except Exception:
        return "#000000"


# -- Verify one overlay / a whole recipe -------------------------------------


def verify_overlay(
    overlay: dict,
    *,
    slot_index: int = 0,
    overlay_index: int = 0,
    render_mode: RenderMode = RENDER_DRAW_FRAME,
    run_ocr: bool = True,
    frame_out_dir: str | None = None,
) -> OverlayResult:
    text = str(overlay.get("text", ""))
    effect = str(overlay.get("effect", "none"))
    anchor = str(overlay.get("text_anchor", "center"))
    text_color = overlay.get("text_color", "#FFFFFF")

    res = OverlayResult(
        slot_index=slot_index,
        overlay_index=overlay_index,
        text=text,
        effect=effect,
        text_anchor=anchor,
        text_color=str(text_color),
        verdict="SKIPPED",
        clipping="SKIPPED",
        content="SKIPPED",
        resolved_typeface=tos.resolved_typeface_for_overlay(overlay),
    )

    if effect == "player-card":
        res.reasons.append("player-card is Pillow-only; not rendered by Skia — skipped")
        return res
    if not text.strip() and not overlay.get("spans"):
        res.reasons.append("overlay has no text — skipped")
        return res

    # 1. Render.
    bg_hex: str | None = None
    try:
        if render_mode == RENDER_BURN:
            bg_hex = _contrast_bg(text_color)
            with tempfile.TemporaryDirectory(prefix="ov_verify_burn_") as td:
                frame = burn_overlay_to_frame(overlay, bg_hex, td)
        else:
            frame = render_overlay_frame(overlay)
    except NotImplementedError as e:
        res.reasons.append(f"renderer skipped: {e}")
        return res
    except Exception as e:  # noqa: BLE001 — surface render crashes as FAIL, never swallow
        res.verdict = res.clipping = "FAIL"
        res.reasons.append(f"render crashed: {type(e).__name__}: {e}")
        return res

    # 2. Clipping (primary, font-independent).
    bbox = text_bbox(frame, bg_hex)
    res.bbox = tuple(bbox) if bbox else None
    res.clipping, clip_reason = check_clipping(bbox, frame.width, frame.height)
    res.reasons.append(clip_reason)

    # 3. Content (advisory).
    if run_ocr:
        read = ocr_text(frame, text_color)
        res.ocr_text = read
        res.content, res.similarity, content_reason = check_content(read, text)
        res.reasons.append(content_reason)
    else:
        res.content = "SKIPPED"
        res.reasons.append("content OCR not run (render-only stage)")

    # 4. Optionally persist the frame for the montage / agent review.
    if frame_out_dir is not None:
        os.makedirs(frame_out_dir, exist_ok=True)
        fp = os.path.join(frame_out_dir, frame_name(slot_index, overlay_index))
        frame.save(fp, "PNG")
        res.frame_path = fp

    font_fallback = bool(res.resolved_typeface.get("fallback"))
    if font_fallback:
        res.clipping = _worst(res.clipping, "FAIL")
        requested = res.resolved_typeface.get("requested_font_family")
        resolved = res.resolved_typeface.get("name")
        res.reasons.append(f"requested font_family {requested!r} resolved to {resolved!r} fallback")

    res.verdict = _worst(res.clipping, res.content)
    return res


def iter_recipe_overlays(recipe: dict) -> list[tuple[int, int, dict]]:
    """Flatten a recipe into (slot_index, overlay_index, overlay) tuples.

    Accepts the recipe shape (``{"slots": [{"text_overlays": [...]}]}``) and the
    looser shapes used by fixtures: a bare overlay list, or
    ``{"overlays": [...]}``."""
    if isinstance(recipe, list):
        return [(0, i, ov) for i, ov in enumerate(recipe)]
    if "slots" in recipe:
        out: list[tuple[int, int, dict]] = []
        for si, slot in enumerate(recipe.get("slots") or []):
            for oi, ov in enumerate(slot.get("text_overlays") or []):
                out.append((si, oi, ov))
        return out
    if "overlays" in recipe:
        return [(0, i, ov) for i, ov in enumerate(recipe.get("overlays") or [])]
    raise ValueError("recipe must be a list, or have a 'slots' or 'overlays' key")


def ocr_result_from_frame(res: OverlayResult, frames_dir: str) -> OverlayResult:
    """Second-stage (host) OCR pass: read the overlay's frame from ``frames_dir``
    (by deterministic name, not the stored path — see ``frame_name``), fill the
    content verdict, and recompute the overall verdict. Used by the Makefile flow
    where rendering happens in the prod Docker image and OCR happens on the host."""
    fp = os.path.join(frames_dir, frame_name(res.slot_index, res.overlay_index))
    if not os.path.exists(fp):
        res.content = "SKIPPED"
        res.reasons.append("content OCR skipped: no rendered frame on disk")
        res.verdict = _worst(res.clipping, res.content)
        return res
    frame = Image.open(fp)
    read = ocr_text(frame, res.text_color)
    res.ocr_text = read
    res.content, res.similarity, reason = check_content(read, res.text)
    res.reasons.append(reason)
    res.verdict = _worst(res.clipping, res.content)
    return res


def verify_recipe(
    recipe: dict,
    *,
    render_mode: RenderMode = RENDER_DRAW_FRAME,
    run_ocr: bool = True,
    frame_out_dir: str | None = None,
) -> VerifyReport:
    report = VerifyReport(render_mode=render_mode, ocr_available=run_ocr and ocr_available())
    for slot_i, ov_i, overlay in iter_recipe_overlays(recipe):
        report.overlays.append(
            verify_overlay(
                overlay,
                slot_index=slot_i,
                overlay_index=ov_i,
                render_mode=render_mode,
                run_ocr=run_ocr and report.ocr_available,
                frame_out_dir=frame_out_dir,
            )
        )
    return report
