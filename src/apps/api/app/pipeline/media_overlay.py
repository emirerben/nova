"""Media-overlay pipeline module (slice 1).

Composites timed, positioned image/video "cards" on top of a finished variant
video. The pass runs AFTER the initial render (including text burn), so cards
appear above captions — z-order is automatic.

FFmpeg approach:
- Generalizes `talking_head_assembler.build_talking_head_command` (video-over-video)
  and `text_overlay_skia._ffmpeg_burn_pngs` (PNG-over-video) by adding:
  * `scale={px}:-1` so the card is a floating scaled tile, not full-frame.
  * non-zero `overlay=x:y` for center-positioned compositing.
  * `tpad=stop_mode=clone` for video cards shorter than their window (freeze last
    frame, not loop — a looping screenshot looks broken).
  * `-loop 1` for image cards (static PNG/JPEG, infinite frame supply).

Audio: card audio is always dropped (`0:a?` maps base audio only). The base
variant already carries the authored audio bed (song / original / voiceover);
random card audio mixed in would be a defect.

Encoder policy: `_encoding_args(out, preset="veryfast")` — intentionally one step
faster than the `"fast"` rule for primary renders. The base video is already CRF 18
H.264 (not raw HDR footage), so mb-tree and psy-rd are still engaged (veryfast keeps
both; only trellis is dropped). The smooth-gradient macroblock risk described in
CLAUDE.md is specific to raw/HDR input; re-encoding already-compressed content has a
quality floor set by the input, not this pass. Using "fast" here caused ~10-minute
render times on shared Fly CPUs; "veryfast" cuts that to ~3-5 min. If banding
complaints arise, switch back to "fast" and live with the latency.

CLAUDE.md anti-pattern guard: subprocess FFmpeg only, never MoviePy.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import structlog

from app import storage
from app.agents._schemas.media_overlay import (
    MediaOverlay,
    validate_overlay_gcs_path,
)
from app.pipeline.image_clip import is_image_file, normalize_to_jpeg
from app.pipeline.reframe import _encoding_args

log = structlog.get_logger()

# Canvas dimensions — must match text_overlay_skia.CANVAS_{W,H}.
_CANVAS_W = 1080
_CANVAS_H = 1920


class MediaOverlayError(Exception):
    """Raised when the overlay apply-pass fails unrecoverably."""


def build_media_overlay_command(
    base_video: str,
    cards: list[MediaOverlay],
    card_local_paths: list[str],
    card_widths_px: list[int],
    output_path: str,
) -> list[str]:
    """Build the ffmpeg command to composite media-overlay cards on top of a video.

    Pure function — no I/O. Inputs are resolved local paths and pre-probed dims.
    `card_local_paths[i]` is the local file for `cards[i]`.
    `card_widths_px[i]` is the rendered card width in pixels (already scaled).

    The filter chain:
    1. One input node per card (after the base video at input 0).
    2. Per card: scale to `card_width_px:-1` (preserve aspect), tpad clone for
       video cards so short clips don't flicker at window end, PTS shift so the
       card plays from t=0 during its window, overlay at the computed top-left.
    3. Chain in z-order (ascending z, then list order).
    4. Map final composite video + base audio only (0:a?).
    5. Encode with `preset="veryfast"` (see module docstring for rationale).

    The `_encoding_args` call here is intentionally direct (not via a helper) so
    the encoder-policy AST gate can attribute it to this module.
    """
    assert len(cards) == len(card_local_paths) == len(card_widths_px)

    # Sort by z ascending (then stable list order via enumerate).
    ordered = sorted(enumerate(cards), key=lambda t: (t[1].z, t[0]))

    inputs: list[str] = ["-i", base_video]
    for _, card in ordered:
        idx = cards.index(card)
        local = card_local_paths[idx]
        if is_image_file(local) or local.endswith((".jpg", ".jpeg", ".png")):
            # Static image: supply infinite frames with -loop 1.
            inputs += ["-loop", "1", "-i", local]
        else:
            # Video card: no -loop; tpad clone handles the freeze-last-frame.
            inputs += ["-i", local]

    filter_parts: list[str] = []
    prev = "[0:v]"

    for filter_idx, (orig_idx, card) in enumerate(ordered):
        # ffmpeg input index: 0 = base, 1..N = cards in z-order
        in_idx = filter_idx + 1
        cw = card_widths_px[orig_idx]
        cx, cy = card.canvas_center_px()
        # Top-left of the card (overlay filter uses top-left origin).
        ox = cx - cw // 2
        # Height of the scaled card is not statically known (scale={cw}:-1
        # preserves aspect, so height = cw * original_h / original_w).
        # Express y using FFmpeg's overlay variable `overlay_h` so the filter
        # computes it at runtime: y = cy_px - overlay_h/2.
        oy_expr = f"({round(cy)}-overlay_h/2)"

        shifted = f"c{filter_idx}"
        out_label = f"ov{filter_idx}"
        local = card_local_paths[orig_idx]

        # Build the per-card filter chain.
        card_filter_parts: list[str] = []
        is_video_card = not is_image_file(local) and not local.endswith(
            (".jpg", ".jpeg", ".png", ".gif")
        )

        # For video cards: apply clip-level trim before scale so we only process
        # the user-selected segment. trim+setpts resets PTS to 0 within the segment.
        if is_video_card and (card.clip_trim_start_s or card.clip_trim_end_s):
            ts = card.clip_trim_start_s or 0.0
            if card.clip_trim_end_s is not None:
                card_filter_parts.append(
                    f"[{in_idx}:v]trim=start={ts:.3f}:end={card.clip_trim_end_s:.3f}"
                    f",setpts=PTS-STARTPTS"
                )
            else:
                card_filter_parts.append(f"[{in_idx}:v]trim=start={ts:.3f},setpts=PTS-STARTPTS")
        else:
            # No trim — enter the scale step directly.
            card_filter_parts.append(f"[{in_idx}:v]null")

        # Scale to card width; -2 rounds height to nearest even number so
        # yuv420p chroma subsampling doesn't misinterpret odd dimensions.
        # format=yuv420p normalises pixel format before the overlay compositor
        # to prevent implicit BT.601/709 colour-space conversions.
        card_filter_parts.append(f"scale={cw}:-2,format=yuv420p")

        # For video cards: freeze last frame to fill any gap between trim duration
        # and the overlay window. Use a bounded `stop_duration` instead of `stop=-1`
        # so FFmpeg only generates frames for the window, not the entire output video.
        # With stop=-1 a 5-second overlay on a 60-second output forces FFmpeg to
        # produce 60s of cloned card frames — 12x wasted work and the root cause of
        # ~10-minute overlay renders on shared Fly CPUs.
        if is_video_card:
            trim_s = card.clip_trim_start_s or 0.0
            trim_e = (
                card.clip_trim_end_s if card.clip_trim_end_s is not None else card.clip_duration_s
            )
            if trim_e is not None:
                trim_dur = trim_e - trim_s
                window_dur = card.end_s - card.start_s
                extra_pad = max(0.0, window_dur - trim_dur)
                if extra_pad > 0:
                    card_filter_parts.append(f"tpad=stop_mode=clone:stop_duration={extra_pad:.3f}")
                # extra_pad == 0: trim exactly fills the window — no tpad needed.
            else:
                # clip_duration_s unknown: safe fallback (slow but correct).
                card_filter_parts.append("tpad=stop_mode=clone:stop=-1")

        # PTS shift so the card plays from its own start during the window.
        card_filter_parts.append(f"setpts=PTS-STARTPTS+{card.start_s:.3f}/TB,settb=AVTB[{shifted}]")

        filter_parts.append(",".join(card_filter_parts))

        # Overlay with time-gate.
        filter_parts.append(
            f"{prev}[{shifted}]overlay={ox}:{oy_expr}:"
            f"enable='between(t,{card.start_s:.3f},{card.end_s:.3f})':eof_action=pass[{out_label}]"
        )
        prev = f"[{out_label}]"

    cmd = [
        "ffmpeg",
        *inputs,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        prev,
        # Base audio only — card audio is intentionally dropped.
        "-map",
        "0:a?",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        # veryfast: re-encoding already-CRF18 content; mb-tree+psy-rd still active.
        # See module docstring for the quality/speed tradeoff rationale.
        *_encoding_args(output_path, preset="veryfast", include_audio=False),
    ]
    return cmd


def apply_media_overlays(
    base_gcs_path: str,
    cards: list[MediaOverlay],
    output_gcs_path: str,
    job_id: str | None = None,
) -> str:
    """Download base variant, composite cards on top, upload result.

    Returns the new signed output URL.

    Best-effort per card: a card with a missing/unreadable asset is dropped with
    a logged warning rather than failing the variant (matches the talking-head
    best-effort pattern).

    Args:
        base_gcs_path: GCS object key for the variant to composite onto.
        cards: validated MediaOverlay list in display order.
        output_gcs_path: GCS key for the composited output (typically the same
            as base, overwriting it, or a new key for the carded variant).
        job_id: for structured log context.
    """
    if not cards:
        raise MediaOverlayError("apply_media_overlays called with empty card list")

    with tempfile.TemporaryDirectory(prefix="nova_media_overlay_") as tmpdir:
        base_local = os.path.join(tmpdir, "base.mp4")
        storage.download_to_file(base_gcs_path, base_local)

        # Download and normalise each card asset.
        ready_cards: list[MediaOverlay] = []
        local_paths: list[str] = []
        widths_px: list[int] = []

        for i, card in enumerate(cards):
            try:
                validate_overlay_gcs_path(card.src_gcs_path)
            except ValueError as exc:
                log.warning(
                    "media_overlay_invalid_path",
                    job_id=job_id,
                    card_id=card.id,
                    error=str(exc),
                )
                continue

            raw_ext = os.path.splitext(card.src_gcs_path)[-1].lower() or ".bin"
            raw_local = os.path.join(tmpdir, f"card_{i}_raw{raw_ext}")
            try:
                storage.download_to_file(card.src_gcs_path, raw_local)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "media_overlay_download_failed",
                    job_id=job_id,
                    card_id=card.id,
                    src=card.src_gcs_path,
                    error=str(exc),
                )
                continue

            if is_image_file(raw_local) or card.kind == "image":
                # Normalise to JPEG (handles HEIC/WEBP).
                norm_local = os.path.join(tmpdir, f"card_{i}.jpg")
                try:
                    normalize_to_jpeg(raw_local, norm_local)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "media_overlay_normalise_failed",
                        job_id=job_id,
                        card_id=card.id,
                        error=str(exc),
                    )
                    continue
                local_paths.append(norm_local)
            else:
                local_paths.append(raw_local)

            ready_cards.append(card)
            widths_px.append(card.card_width_px())

        if not ready_cards:
            log.warning("media_overlay_all_cards_failed", job_id=job_id)
            raise MediaOverlayError("All card assets failed to load — skipping apply-pass")

        # Validate timing: discard cards with end_s <= start_s (shouldn't happen
        # post-schema-validation, but defensive here too).
        valid: list[tuple[MediaOverlay, str, int]] = []
        for card, lp, wp in zip(ready_cards, local_paths, widths_px):
            if card.end_s <= card.start_s:
                log.warning("media_overlay_bad_timing", card_id=card.id, job_id=job_id)
                continue
            valid.append((card, lp, wp))

        if not valid:
            raise MediaOverlayError("No cards have valid timing — skipping apply-pass")

        final_cards, final_paths, final_widths = zip(*valid)  # type: ignore[misc]

        output_local = os.path.join(tmpdir, "output.mp4")
        cmd = build_media_overlay_command(
            base_local,
            list(final_cards),
            list(final_paths),
            list(final_widths),
            output_local,
        )
        log.info(
            "media_overlay_applying",
            job_id=job_id,
            card_count=len(final_cards),
            cmd_len=len(cmd),
        )
        result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-800:]
            raise MediaOverlayError(
                f"ffmpeg media-overlay composite failed (rc={result.returncode}): {stderr_tail}"
            )

        signed_url = storage.upload_public_read(
            output_local, output_gcs_path, content_type="video/mp4"
        )
        log.info("media_overlay_applied", job_id=job_id, gcs_path=output_gcs_path)
        return signed_url
