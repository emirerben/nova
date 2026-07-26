"""Media-overlay pipeline module (slice 1).

Composites timed, positioned image/video "cards" on top of a finished variant
video. The pass runs AFTER the initial render (including text burn), so cards
appear above captions — z-order is automatic.

Alpha transparency is supported for IMAGE pip cards only. Video cards (including
VP9/HEVC alpha sources) composite opaque, and fullscreen cards always flatten.

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

Encoder policy (mode-dependent, plan 009 E3):
- PIP-ONLY passes: `_encoding_args(out, preset="veryfast")` — intentionally one
  step faster than the `"fast"` rule for primary renders. The base video is
  already CRF 18 H.264 (not raw HDR footage), so mb-tree and psy-rd are still
  engaged (veryfast keeps both; only trellis is dropped). Using "fast" here
  caused ~10-minute render times on shared Fly CPUs (commit 2c560363 removed it
  after 603s renders vs the then-600s subprocess timeout).
- Passes containing ANY FULLSCREEN card: `preset="fast"`. A fullscreen card is
  raw user footage filling the whole frame — the smooth-gradient macroblock
  risk the encoder policy exists for. The speed cost is paired with a raised
  subprocess timeout (_TIMEOUT_FULLSCREEN_S) and a ONE-SHOT retry at veryfast
  on TimeoutExpired in `apply_media_overlays`, so no render ever dies on the
  preset choice; worst case quality degrades to today's veryfast.

CLAUDE.md anti-pattern guard: subprocess FFmpeg only, never MoviePy.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from math import ceil

import structlog

from app import storage
from app.agents._schemas.media_overlay import (
    MediaOverlay,
    coerce_media_overlays,
    validate_overlay_gcs_path,
)
from app.config import settings
from app.pipeline.canvas import PORTRAIT, Canvas
from app.pipeline.dissolve_effect import (
    DISSOLVE_OUT,
    MEDIA_OVERLAY_DISSOLVE_PARAMS,
    read_skia_png,
    render_dissolve_svg_displacement_image,
    write_skia_png,
)
from app.pipeline.image_clip import (
    image_has_alpha,
    is_image_file,
    normalize_to_jpeg,
    normalize_to_png,
)
from app.pipeline.reframe import _encoding_args
from app.pipeline.render_geometry import (
    NormalizedBox,
    PreparedMediaAsset,
    arbitrate_media_overlays,
    media_dimensions,
    opaque_alpha_box,
)

log = structlog.get_logger()

# Canvas dimensions — must match text_overlay_skia.CANVAS_{W,H}.
_CANVAS_W = 1080
_CANVAS_H = 1920

# Subprocess timeouts (plan 009 E3). Fullscreen passes run preset=fast, whose
# documented worst case (~10 min) exceeded the old 600s ceiling.
# Review C6/C25: the fast attempt AND the one-shot veryfast retry must share a
# SINGLE wall-clock budget — two independent 1200s ceilings summed to 2400s,
# past the render task's soft_time_limit=1740s / visibility_timeout=1900s, which
# with task_acks_late would SIGKILL → redeliver → loop (the documented Celery
# time-limit incident class). The budget below bounds attempt+retry total.
_TIMEOUT_PIP_S = 600
_TIMEOUT_FULLSCREEN_S = 900
# Hard ceiling on attempt+retry combined, kept under soft_time_limit(1740) minus
# the download/normalise/upload overhead around the ffmpeg call.
_FULLSCREEN_TOTAL_BUDGET_S = 1500
# R4-2: entering the pass with less wall-clock than this cannot fit even the
# veryfast retry — fail fast with a clear error instead of tripping the Celery
# hard limit mid-encode. Only consulted when a caller threads a deadline.
_DEADLINE_FLOOR_S = 120
_DISSOLVE_FPS = 30


class MediaOverlayError(Exception):
    """Raised when the overlay apply-pass fails unrecoverably."""


def _resolve_pass_budget(
    fullscreen_pass: bool, deadline_monotonic: float | None
) -> tuple[int, float]:
    """(per-attempt subprocess timeout_s, total attempt+retry budget_s).

    Default (deadline_monotonic=None) is byte-identical to the standalone-task
    numbers — the plans/009 preset/timeout pins stay untouched. Caption tasks
    enter this pass MID-task (after a burn that may have consumed minutes), so
    _FULLSCREEN_TOTAL_BUDGET_S sized for a task that STARTS with the pass can
    deterministically exceed the 1740s soft ceiling; the deadline clamps both
    the shared budget and the per-attempt timeout to the wall clock actually
    left, and raises when less than _DEADLINE_FLOOR_S remains.
    """
    timeout_s = _TIMEOUT_FULLSCREEN_S if fullscreen_pass else _TIMEOUT_PIP_S
    budget_s = float(_FULLSCREEN_TOTAL_BUDGET_S)
    if deadline_monotonic is not None:
        remaining = deadline_monotonic - time.monotonic()
        if remaining < _DEADLINE_FLOOR_S:
            raise MediaOverlayError(
                f"media-overlay pass skipped: {remaining:.0f}s left before the task "
                f"deadline (needs at least {_DEADLINE_FLOOR_S}s) — retry the edit"
            )
        budget_s = min(budget_s, remaining)
        timeout_s = min(timeout_s, int(budget_s))
    return timeout_s, budget_s


def _has_fullscreen(cards: list[MediaOverlay]) -> bool:
    """True when any card is a full-frame takeover (drives preset + timeout)."""
    return any(c.display_mode == "fullscreen" for c in cards)


def _is_dissolve_frame_sequence(path: str) -> bool:
    return "%04d" in path and "dissolve_card_" in path


def _run_ffmpeg(cmd: list[str], *, label: str, timeout_s: int = 180) -> None:
    result = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)
    if result.returncode != 0:
        stderr_tail = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise MediaOverlayError(f"{label} failed (rc={result.returncode}): {stderr_tail}")


def _render_dissolve_component_source_frames(
    *,
    card: MediaOverlay,
    local_path: str,
    card_width_px: int,
    has_alpha: bool,
    source_pattern: str,
    frame_count: int,
    duration_s: float,
    canvas: Canvas,
) -> None:
    """Rasterize the selected media card into full-frame transparent hold frames."""
    inputs: list[str]
    if is_image_file(local_path) or local_path.endswith((".jpg", ".jpeg", ".png")):
        inputs = [
            "-loop",
            "1",
            "-t",
            f"{duration_s:.3f}",
            "-i",
            local_path,
        ]
    else:
        inputs = ["-i", local_path]

    cx, cy = card.canvas_center_px()
    ox = cx - card_width_px // 2
    oy_expr = f"({round(cy)}-overlay_h/2)"
    is_fullscreen = card.display_mode == "fullscreen"
    is_video_card = not is_image_file(local_path) and not local_path.endswith(
        (".jpg", ".jpeg", ".png", ".gif")
    )

    card_parts: list[str] = []
    if is_video_card and (card.clip_trim_start_s or card.clip_trim_end_s):
        ts = card.clip_trim_start_s or 0.0
        if card.clip_trim_end_s is not None:
            card_parts.append(
                f"[0:v]trim=start={ts:.3f}:end={card.clip_trim_end_s:.3f},setpts=PTS-STARTPTS"
            )
        else:
            card_parts.append(f"[0:v]trim=start={ts:.3f},setpts=PTS-STARTPTS")
    else:
        card_parts.append("[0:v]null")

    if is_fullscreen:
        card_parts.append(
            f"scale={canvas.width}:{canvas.height}:force_original_aspect_ratio=increase,"
            f"crop={canvas.width}:{canvas.height},setsar=1,format=rgba"
        )
    else:
        pix_fmt = "rgba" if has_alpha else "yuv420p"
        if card.entrance_token == "pop_in":
            card_parts.append(
                f"scale=w='{card_width_px}*(0.82+0.18*min(t/0.18,1))':"
                f"h=-2:eval=frame,format={pix_fmt}"
            )
        else:
            card_parts.append(f"scale={card_width_px}:-2,format={pix_fmt}")

    if is_video_card:
        trim_s = card.clip_trim_start_s or 0.0
        trim_e = card.clip_trim_end_s if card.clip_trim_end_s is not None else card.clip_duration_s
        if trim_e is not None:
            trim_dur = trim_e - trim_s
            extra_pad = max(0.0, duration_s - trim_dur)
            if extra_pad > 0:
                card_parts.append(f"tpad=stop_mode=clone:stop_duration={extra_pad:.3f}")
        else:
            card_parts.append("tpad=stop_mode=clone:stop=-1")

    card_parts.append("setpts=PTS-STARTPTS,settb=AVTB[card]")
    layer_xy = "0:0" if is_fullscreen else f"{ox}:{oy_expr}"
    filter_complex = ";".join(
        [
            ",".join(card_parts),
            (
                f"color=c=black@0:s={canvas.width}x{canvas.height}:d={duration_s:.3f}:"
                f"r={_DISSOLVE_FPS},format=rgba,colorchannelmixer=aa=0[blank]"
            ),
            f"[blank][card]overlay={layer_xy}:eof_action=pass,format=rgba[out]",
        ]
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        *inputs,
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-frames:v",
        str(frame_count),
        "-start_number",
        "0",
        "-y",
        source_pattern,
    ]
    _run_ffmpeg(cmd, label="media-overlay dissolve source raster")


def _render_dissolve_card_sequence(
    *,
    card: MediaOverlay,
    local_path: str,
    card_width_px: int,
    has_alpha: bool,
    tmpdir: str,
    index: int,
    canvas: Canvas,
    job_id: str | None,
) -> str:
    """Render one media overlay's exit dissolve as full-frame transparent PNGs."""
    duration_s = max(0.001, float(card.end_s) - float(card.start_s))
    frame_count = max(1, int(ceil(duration_s * _DISSOLVE_FPS)))
    source_dir = os.path.join(tmpdir, f"dissolve_card_{index}_source")
    output_dir = os.path.join(tmpdir, f"dissolve_card_{index}_frames")
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    source_pattern = os.path.join(source_dir, "frame_%04d.png")
    output_pattern = os.path.join(output_dir, "frame_%04d.png")

    _render_dissolve_component_source_frames(
        card=card,
        local_path=local_path,
        card_width_px=card_width_px,
        has_alpha=has_alpha,
        source_pattern=source_pattern,
        frame_count=frame_count,
        duration_s=duration_s,
        canvas=canvas,
    )

    seed = 211 + index * 53
    for frame in range(frame_count):
        src = os.path.join(source_dir, f"frame_{frame:04d}.png")
        dst = os.path.join(output_dir, f"frame_{frame:04d}.png")
        source_img = read_skia_png(src)
        dissolved = render_dissolve_svg_displacement_image(
            source_img,
            frame / _DISSOLVE_FPS,
            duration_s,
            seed=seed,
            params=MEDIA_OVERLAY_DISSOLVE_PARAMS,
            cap_to_webkit=True,
        )
        write_skia_png(dissolved, dst)

    log.info(
        "media_overlay_dissolve_sequence_rendered",
        job_id=job_id,
        card_id=card.id,
        frame_count=frame_count,
        renderer="skia",
    )
    return output_pattern


def build_media_overlay_command(
    base_video: str,
    cards: list[MediaOverlay],
    card_local_paths: list[str],
    card_widths_px: list[int],
    output_path: str,
    card_has_alpha: list[bool] | None = None,
    force_veryfast: bool = False,
    canvas: Canvas = PORTRAIT,
) -> list[str]:
    """Build the ffmpeg command to composite media-overlay cards on top of a video.

    Pure function — no I/O. Inputs are resolved local paths and pre-probed dims.
    `card_local_paths[i]` is the local file for `cards[i]`.
    `card_widths_px[i]` is the rendered card width in pixels (already scaled).

    The filter chain:
    1. One input node per card (after the base video at input 0).
    2. Per card: pip → scale to `card_width_px:-2` (preserve aspect) + centered
       overlay; fullscreen → cover-crop to the full 1080x1920 canvas + overlay
       at 0:0. tpad clone for video cards so short clips don't flicker at
       window end, PTS shift so the card plays from t=0 during its window.
    3. Chain in z-order (ascending z, then list order).
    4. Map final composite video + base audio only (0:a?).
    5. Encode veryfast for pip-only passes, fast when any fullscreen card is
       present (see module docstring). `force_veryfast=True` is the
       timeout-retry escape hatch — it wins over the fullscreen escalation.

    The `_encoding_args` calls here are intentionally direct (not via a helper)
    with LITERAL preset constants so the encoder-policy AST gate can attribute
    both branches to this module.
    """
    if card_has_alpha is None:
        card_has_alpha = [False] * len(cards)
    assert len(cards) == len(card_local_paths) == len(card_widths_px) == len(card_has_alpha)

    # Sort by z ascending (then stable list order via enumerate).
    ordered = sorted(enumerate(cards), key=lambda t: (t[1].z, t[0]))

    inputs: list[str] = ["-i", base_video]
    for orig_idx, card in ordered:
        # orig_idx (not cards.index) — two EQUAL cards (same asset + geometry,
        # a real compiler output when one pool image backs two time windows)
        # would both resolve cards.index to the FIRST card's paths.
        local = card_local_paths[orig_idx]
        if _is_dissolve_frame_sequence(local):
            inputs += [
                "-framerate",
                str(_DISSOLVE_FPS),
                "-start_number",
                "0",
                "-i",
                local,
            ]
        elif is_image_file(local) or local.endswith((".jpg", ".jpeg", ".png")):
            # Static image: -loop 1 supplies frames; -t bounds the looped
            # source to the card's visible window. WITHOUT the bound the loop
            # is infinite and ffmpeg decodes+rescales the full-resolution image
            # for EVERY base frame — 8 multi-megapixel pool images on a 90s
            # clip crossed the 600s PIP timeout on shared Fly CPUs (prod job
            # 4010a394, 2026-07-21) and the whole visuals pass failed open.
            # Measured 4.6x faster with the bound; output byte-identical.
            inputs += ["-loop", "1", "-t", f"{max(0.1, float(card.end_s)):.3f}", "-i", local]
        else:
            # Video card: no -loop; tpad clone handles the freeze-last-frame.
            inputs += ["-i", local]

    filter_parts: list[str] = []
    prev = "[0:v]"
    alpha_pips: list[bool] = []

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
        is_fullscreen = card.display_mode == "fullscreen"
        is_dissolve_sequence = _is_dissolve_frame_sequence(local)
        dissolve_out = card.exit_token == DISSOLVE_OUT
        alpha_pip = bool(card_has_alpha[orig_idx]) and not is_fullscreen
        alpha_pips.append(alpha_pip)

        if is_dissolve_sequence:
            filter_parts.append(
                f"[{in_idx}:v]format=rgba,setpts=PTS-STARTPTS+{card.start_s:.3f}/TB,"
                f"settb=AVTB[{shifted}]"
            )
            filter_parts.append(
                f"{prev}[{shifted}]overlay=0:0:"
                f"enable='between(t,{card.start_s:.3f},{card.end_s:.3f})':"
                f"eof_action=pass[{out_label}]"
            )
            prev = f"[{out_label}]"
            continue

        if dissolve_out:
            raise ValueError(
                "dissolve-out media overlays must be prepared as Skia frame sequences "
                "before build_media_overlay_command"
            )

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

        if is_fullscreen:
            # Full-frame takeover (plan 009): cover-crop to exactly the canvas.
            # force_original_aspect_ratio=increase scales the SHORT side to fit,
            # crop center-cuts the overflow; setsar=1 guards odd input SARs.
            # Static dims — no runtime overlay_h expression needed.
            card_filter_parts.append(
                f"scale={canvas.width}:{canvas.height}:force_original_aspect_ratio=increase"
                f",crop={canvas.width}:{canvas.height},setsar=1,format=yuv420p"
            )
        else:
            # Scale to card width; -2 rounds height to nearest even number so
            # yuv420p chroma subsampling doesn't misinterpret odd dimensions.
            # Opaque cards keep the legacy yuv420p pre-format. Alpha cards stay
            # rgba and let overlay perform its internal conversion, matching the
            # legacy JPEG path's colorimetry.
            pix_fmt = "rgba" if alpha_pip else "yuv420p"
            if card.entrance_token == "pop_in":
                # 180ms scale-up authored in the overlay's local timeline. The
                # overlay x expression below uses overlay_w so the card remains
                # centered while its width changes.
                card_filter_parts.append(
                    f"scale=w='{cw}*(0.82+0.18*min(t/0.18,1))':h=-2:eval=frame,format={pix_fmt}"
                )
            else:
                card_filter_parts.append(f"scale={cw}:-2,format={pix_fmt}")

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
        card_filter_parts.append(
            f"setpts=PTS-STARTPTS+{card.start_s:.3f}/TB,settb=AVTB[{shifted}]"
        )
        filter_parts.append(",".join(card_filter_parts))

        # Overlay with time-gate. Fullscreen composites at the origin (static
        # dims); pip centers via the runtime overlay_h expression.
        if is_fullscreen:
            overlay_xy = "0:0"
        elif card.entrance_token == "pop_in":
            overlay_xy = f"({round(cx)}-overlay_w/2):{oy_expr}"
        else:
            overlay_xy = f"{ox}:{oy_expr}"
        filter_parts.append(
            f"{prev}[{shifted}]overlay={overlay_xy}:"
            f"enable='between(t,{card.start_s:.3f},{card.end_s:.3f})':eof_action=pass[{out_label}]"
        )
        prev = f"[{out_label}]"

    if any(alpha_pips):
        filter_parts.append(f"{prev}format=yuv420p[finalv]")
        prev = "[finalv]"

    # Mode-dependent encoder policy (module docstring). Two LITERAL call sites
    # on purpose — the encoder-policy AST gate reads constant presets only.
    if _has_fullscreen(cards) and not force_veryfast:
        # Full-frame user footage: final-output quality tier.
        encoding = _encoding_args(output_path, preset="fast", include_audio=False, canvas=canvas)
    else:
        # Pip-only (or fullscreen timeout-retry): re-encoding already-CRF18
        # content; mb-tree+psy-rd still active at veryfast.
        encoding = _encoding_args(
            output_path, preset="veryfast", include_audio=False, canvas=canvas
        )

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
        *encoding,
    ]
    return cmd


def apply_media_overlays(
    base_gcs_path: str,
    cards: list[MediaOverlay],
    output_gcs_path: str,
    job_id: str | None = None,
    deadline_monotonic: float | None = None,
    protected_boxes: list[dict[str, object]] | None = None,
    layout_receipt_out: list[dict] | None = None,
    applied_cards_out: list[dict] | None = None,
    canvas: Canvas = PORTRAIT,
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
        deadline_monotonic: optional time.monotonic() wall-clock ceiling from a
            caller that entered this pass mid-task (R4-2) — clamps the encode
            budget via _resolve_pass_budget. None keeps behavior byte-identical.
        layout_receipt_out: optional sink for Smart collision decisions.
        applied_cards_out: optional sink for the resolved cards that actually
            reached the compositor, suitable for persistence and later reburns.
    """
    if not cards:
        raise MediaOverlayError("apply_media_overlays called with empty card list")
    # Geometry arbitration is opted into ONLY via protected_boxes (an empty
    # list still opts in — it enables mutual-overlap dedup with nothing else
    # protected). layout_receipt_out alone must NOT flip this on: callers pass
    # a receipt sink unconditionally, and v1/legacy renders must keep their
    # pre-arbitration layout byte-stable.
    needs_geometry = protected_boxes is not None

    with tempfile.TemporaryDirectory(prefix="nova_media_overlay_") as tmpdir:
        base_local = os.path.join(tmpdir, "base.mp4")
        storage.download_to_file(base_gcs_path, base_local)

        # Download and normalise each card asset.
        prepared_cards: list[tuple[MediaOverlay, PreparedMediaAsset]] = []
        raw_by_gcs: dict[str, str] = {}
        prepared_by_key: dict[tuple[str, bool], PreparedMediaAsset] = {}

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

            use_alpha_requested = (
                settings.media_overlay_alpha_enabled and card.display_mode != "fullscreen"
            )
            prepared_key = (card.src_gcs_path, use_alpha_requested)
            prepared = prepared_by_key.get(prepared_key)
            if prepared is None:
                raw_local = raw_by_gcs.get(card.src_gcs_path)
                if raw_local is None:
                    raw_ext = os.path.splitext(card.src_gcs_path)[-1].lower() or ".bin"
                    raw_local = os.path.join(tmpdir, f"asset_{len(raw_by_gcs)}_raw{raw_ext}")
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
                    raw_by_gcs[card.src_gcs_path] = raw_local

            if prepared is None and (is_image_file(raw_local) or card.kind == "image"):
                # Normalise to JPEG (handles HEIC/WEBP). Alpha-capable pip
                # images keep PNG only behind the dedicated kill switch.
                use_alpha = use_alpha_requested and image_has_alpha(raw_local)
                try:
                    if use_alpha:
                        norm_local, alpha_preserved = normalize_to_png(
                            raw_local,
                            os.path.join(tmpdir, f"asset_{len(prepared_by_key)}.png"),
                        )
                    else:
                        norm_local = os.path.join(tmpdir, f"asset_{len(prepared_by_key)}.jpg")
                        normalize_to_jpeg(raw_local, norm_local)
                        alpha_preserved = False
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "media_overlay_normalise_failed",
                        job_id=job_id,
                        card_id=card.id,
                        error=str(exc),
                    )
                    continue
                width_px, height_px = media_dimensions(norm_local) if needs_geometry else (1, 1)
                prepared = PreparedMediaAsset(
                    src_gcs_path=card.src_gcs_path,
                    local_path=norm_local,
                    has_alpha=use_alpha and alpha_preserved,
                    # opaque_bounds is only consumed by geometry arbitration —
                    # keep the two probes symmetric so legacy renders skip both.
                    opaque_bounds=(
                        opaque_alpha_box(norm_local)
                        if needs_geometry
                        else NormalizedBox(0.0, 0.0, 1.0, 1.0)
                    ),
                    width_px=width_px,
                    height_px=height_px,
                )
            elif prepared is None:
                width_px, height_px = media_dimensions(raw_local) if needs_geometry else (1, 1)
                prepared = PreparedMediaAsset(
                    src_gcs_path=card.src_gcs_path,
                    local_path=raw_local,
                    has_alpha=False,
                    opaque_bounds=NormalizedBox(0.0, 0.0, 1.0, 1.0),
                    width_px=width_px,
                    height_px=height_px,
                )
            prepared_by_key[prepared_key] = prepared
            prepared_cards.append((card, prepared))

        if not prepared_cards:
            log.warning("media_overlay_all_cards_failed", job_id=job_id)
            raise MediaOverlayError("All card assets failed to load — skipping apply-pass")

        if needs_geometry:
            footprints = {card.id: prepared.footprint for card, prepared in prepared_cards}
            resolved_payloads, layout_receipts = arbitrate_media_overlays(
                [card.model_dump(exclude_none=True) for card, _prepared in prepared_cards],
                protected_boxes=protected_boxes or [],
                footprints_by_id=footprints,
                canvas=canvas,
            )
            resolved_cards = coerce_media_overlays(resolved_payloads) or []
            prepared_by_id = {card.id: prepared for card, prepared in prepared_cards}
            prepared_cards = [
                (card, prepared_by_id[card.id])
                for card in resolved_cards
                if card.id in prepared_by_id
            ]
            if layout_receipt_out is not None:
                layout_receipt_out.extend(layout_receipts)
            if not prepared_cards:
                raise MediaOverlayError("No decorative media has a collision-free layout")

        # Validate timing: discard cards with end_s <= start_s (shouldn't happen
        # post-schema-validation, but defensive here too).
        valid: list[tuple[MediaOverlay, str, int, bool]] = []
        applied_card_payloads: list[dict] = []
        for render_idx, (card, prepared) in enumerate(prepared_cards):
            if card.end_s <= card.start_s:
                log.warning("media_overlay_bad_timing", card_id=card.id, job_id=job_id)
                continue
            card_width_px = round(card.scale * canvas.width)
            render_path = prepared.local_path
            render_has_alpha = prepared.has_alpha
            if card.exit_token == DISSOLVE_OUT:
                render_path = _render_dissolve_card_sequence(
                    card=card,
                    local_path=prepared.local_path,
                    card_width_px=card_width_px,
                    has_alpha=prepared.has_alpha,
                    tmpdir=tmpdir,
                    index=render_idx,
                    canvas=canvas,
                    job_id=job_id,
                )
                render_has_alpha = True
            valid.append(
                (
                    card,
                    render_path,
                    card_width_px,
                    render_has_alpha,
                )
            )
            applied_card_payloads.append(card.model_dump(exclude_none=True))

        if not valid:
            raise MediaOverlayError("No cards have valid timing — skipping apply-pass")
        if applied_cards_out is not None:
            applied_cards_out.extend(applied_card_payloads)

        final_cards, final_paths, final_widths, final_alpha_flags = zip(*valid)  # type: ignore[misc]

        output_local = os.path.join(tmpdir, "output.mp4")
        fullscreen_pass = _has_fullscreen(list(final_cards))
        # Shared wall-clock budget (review C6): fast attempt + veryfast retry must
        # together stay under the render task's soft_time_limit. The retry gets
        # only the time left, so worst-case ffmpeg wall time is the shared budget,
        # not 2× the per-attempt ceiling. R4-2: the budget shrinks further when a
        # mid-task caller threads its own deadline (and raises when too little is
        # left — the render fails fast with a clear error, not a hard-limit kill).
        timeout_s, budget_s = _resolve_pass_budget(fullscreen_pass, deadline_monotonic)
        deadline = time.monotonic() + budget_s
        cmd = build_media_overlay_command(
            base_local,
            list(final_cards),
            list(final_paths),
            list(final_widths),
            output_local,
            card_has_alpha=list(final_alpha_flags),
            canvas=canvas,
        )
        log.info(
            "media_overlay_applying",
            job_id=job_id,
            card_count=len(final_cards),
            fullscreen=fullscreen_pass,
            timeout_s=timeout_s,
            cmd_len=len(cmd),
        )
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            if not fullscreen_pass:
                raise
            # ONE-SHOT retry at veryfast (plan 009 E3): a fullscreen pass that
            # outruns the fast ceiling degrades to today's quality tier instead
            # of failing the variant. The retry only gets the remaining shared
            # budget (review C6) — if none is left, the timeout propagates as a
            # failed render rather than blowing the Celery task limit.
            retry_timeout = min(_TIMEOUT_PIP_S, int(deadline - time.monotonic()))
            if retry_timeout <= 0:
                raise
            log.warning(
                "media_overlay_fast_timeout_retry_veryfast",
                job_id=job_id,
                retry_timeout_s=retry_timeout,
            )
            cmd = build_media_overlay_command(
                base_local,
                list(final_cards),
                list(final_paths),
                list(final_widths),
                output_local,
                card_has_alpha=list(final_alpha_flags),
                force_veryfast=True,
                canvas=canvas,
            )
            result = subprocess.run(cmd, capture_output=True, timeout=retry_timeout, check=False)
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
