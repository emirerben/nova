"""Masonry-collage montage compositor for plan-item generative renders.

Builds a white-canvas collage of rounded video tiles directly with FFmpeg. The
source videos are never buffered in Python; Pillow is only used to create small
alpha-mask PNGs for rounded corners.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from itertools import cycle, islice

import structlog

from app.config import settings

log = structlog.get_logger()

MASONRY_MAX_DURATION_S = 15.0
MASONRY_MAX_TILES = 18
MASONRY_TIMEOUT_S = 900
MASONRY_TILE_RADIUS_PX = 34
_PLACEMENT_SAMPLE_COUNT = 7
_PLACEMENT_MARGIN_PX = 42


@dataclass(frozen=True)
class MasonryTile:
    """One rounded video tile on the scrolling board."""

    input_index: int
    clip_id: str
    local_path: str
    x: int
    y: int
    width: int
    height: int
    mask_path: str
    is_image: bool = False


_MASONRY_LAYOUT: tuple[tuple[int, int, int, int], ...] = (
    (34, 46, 270, 480),
    (334, 28, 420, 250),
    (784, 64, 285, 500),
    (1098, 24, 440, 264),
    (1568, 74, 265, 472),
    (26, 568, 420, 244),
    (474, 330, 280, 498),
    (784, 600, 410, 250),
    (1224, 330, 292, 520),
    (1548, 600, 430, 248),
    (48, 850, 270, 480),
    (348, 872, 430, 260),
    (808, 892, 270, 480),
    (1110, 904, 420, 250),
    (1560, 886, 285, 506),
    (28, 1372, 430, 254),
    (488, 1412, 284, 474),
    (804, 1414, 424, 254),
)


def clamp_masonry_duration(duration_s: float) -> float:
    """Clamp masonry renders to the short reference-style window."""
    try:
        duration = float(duration_s)
    except (TypeError, ValueError, OverflowError):
        duration = MASONRY_MAX_DURATION_S
    if duration <= 0:
        duration = MASONRY_MAX_DURATION_S
    return round(max(0.1, min(duration, MASONRY_MAX_DURATION_S)), 3)


def masonry_text_placement_candidates(
    *,
    duration_s: float,
    reveal_window_s: float = 4.0,
    max_candidates: int = 3,
) -> list[dict]:
    """Find stable whitespace regions over the masonry reveal window.

    Samples the scrolling tile board and scores a small set of editor-friendly text
    boxes by how little they overlap visible tiles. This is intentionally lightweight:
    it runs in render planning, not inside FFmpeg, and returns normalized candidates
    the editor can apply directly.
    """
    output_w = int(settings.output_width)
    output_h = int(settings.output_height)
    board_width = max((x + w for x, _y, w, _h in _MASONRY_LAYOUT), default=output_w) + 34
    pan_px = max(0, board_width - output_w)
    duration = clamp_masonry_duration(duration_s)
    window = max(0.1, min(float(reveal_window_s), duration))
    sample_count = max(2, _PLACEMENT_SAMPLE_COUNT)
    sample_times = [window * i / (sample_count - 1) for i in range(sample_count)]

    rects_by_sample: list[list[tuple[float, float, float, float]]] = []
    for t in sample_times:
        progress = min(1.0, max(0.0, t / duration))
        scroll = pan_px * progress
        visible: list[tuple[float, float, float, float]] = []
        for x, y, w, h in _MASONRY_LAYOUT:
            left = x - scroll - _PLACEMENT_MARGIN_PX
            top = y - _PLACEMENT_MARGIN_PX
            right = x - scroll + w + _PLACEMENT_MARGIN_PX
            bottom = y + h + _PLACEMENT_MARGIN_PX
            if right <= 0 or left >= output_w or bottom <= 0 or top >= output_h:
                continue
            visible.append(
                (
                    max(0.0, left),
                    max(0.0, top),
                    min(float(output_w), right),
                    min(float(output_h), bottom),
                )
            )
        rects_by_sample.append(visible)

    probe_boxes = [
        (0.50, 0.50, 0.58, 0.18),
        (0.50, 0.33, 0.62, 0.16),
        (0.50, 0.68, 0.62, 0.16),
        (0.30, 0.50, 0.42, 0.18),
        (0.70, 0.50, 0.42, 0.18),
        (0.50, 0.82, 0.52, 0.13),
    ]

    scored: list[tuple[float, dict]] = []
    for x_frac, y_frac, w_frac, h_frac in probe_boxes:
        bw = w_frac * output_w
        bh = h_frac * output_h
        left = x_frac * output_w - bw / 2.0
        top = y_frac * output_h - bh / 2.0
        box = (left, top, left + bw, top + bh)
        worst_overlap = 0.0
        clear_samples = 0
        for rects in rects_by_sample:
            overlap = 0.0
            for rect in rects:
                ox = max(0.0, min(box[2], rect[2]) - max(box[0], rect[0]))
                oy = max(0.0, min(box[3], rect[3]) - max(box[1], rect[1]))
                overlap += ox * oy
            ratio = overlap / max(1.0, bw * bh)
            worst_overlap = max(worst_overlap, ratio)
            if ratio <= 0.02:
                clear_samples += 1
        stability = clear_samples / len(rects_by_sample)
        score = stability * 1.8 + w_frac - worst_overlap * 2.5
        scored.append(
            (
                score,
                {
                    "source": "masonry_whitespace",
                    "x_frac": round(x_frac, 4),
                    "y_frac": round(y_frac, 4),
                    "max_width_frac": round(w_frac, 4),
                    "confidence": round(max(0.2, min(0.98, score / 2.6)), 3),
                },
            )
        )

    scored.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _score, candidate in scored[: max(1, max_candidates)]]


def _write_mask(path: str, width: int, height: int, radius: int = MASONRY_TILE_RADIUS_PX) -> None:
    """Create a grayscale rounded-rectangle alpha mask."""
    if os.path.exists(path):
        return
    from PIL import Image, ImageDraw  # noqa: PLC0415

    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    image.save(path)


def _normalize_image_for_ffmpeg(input_path: str, output_path: str) -> str:
    """Decode a still image with Pillow and write an FFmpeg-friendly PNG."""
    if os.path.exists(output_path):
        return output_path
    try:
        import pillow_heif  # type: ignore[import]  # noqa: PLC0415

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    from PIL import Image, ImageOps  # noqa: PLC0415

    try:
        with Image.open(input_path) as image:
            normalized = ImageOps.exif_transpose(image)
            if normalized.mode not in {"RGB", "RGBA"}:
                normalized = normalized.convert("RGB")
            normalized.save(output_path, format="PNG", optimize=False)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"masonry image normalization failed for {os.path.basename(input_path)}: {exc}"
        ) from exc
    return output_path


def build_masonry_tiles(
    *,
    steps: list,
    clip_id_to_local: dict[str, str],
    mask_dir: str,
    max_tiles: int = MASONRY_MAX_TILES,
    normalize_images: bool = False,
) -> list[MasonryTile]:
    """Return deterministic tile specs, cycling uploaded clips when needed."""
    ordered_clip_ids = [str(getattr(step, "clip_id", "") or "") for step in steps]
    ordered_clip_ids = [cid for cid in ordered_clip_ids if cid in clip_id_to_local]
    if not ordered_clip_ids:
        ordered_clip_ids = [cid for cid in clip_id_to_local if clip_id_to_local.get(cid)]
    if not ordered_clip_ids:
        raise ValueError("masonry montage requires at least one local clip")

    os.makedirs(mask_dir, exist_ok=True)
    image_dir = os.path.join(os.path.dirname(mask_dir), "masonry_images")
    if normalize_images:
        os.makedirs(image_dir, exist_ok=True)
    count = min(max_tiles, len(_MASONRY_LAYOUT))
    cycled_ids = list(islice(cycle(ordered_clip_ids), count))
    tiles: list[MasonryTile] = []
    from app.pipeline.image_clip import is_image_file  # noqa: PLC0415

    for idx, (clip_id, (x, y, width, height)) in enumerate(zip(cycled_ids, _MASONRY_LAYOUT)):
        local_path = clip_id_to_local[clip_id]
        is_image = is_image_file(local_path)
        if normalize_images and is_image:
            local_path = _normalize_image_for_ffmpeg(
                local_path,
                os.path.join(image_dir, f"tile_{idx}.png"),
            )
        mask_path = os.path.join(mask_dir, f"mask_{width}x{height}.png")
        _write_mask(mask_path, width, height)
        tiles.append(
            MasonryTile(
                input_index=idx + 1,
                clip_id=clip_id,
                local_path=local_path,
                x=x,
                y=y,
                width=width,
                height=height,
                mask_path=mask_path,
                is_image=is_image,
            )
        )
    return tiles


def build_masonry_command(
    *,
    tiles: list[MasonryTile],
    output_path: str,
    duration_s: float,
    board_width: int,
    audio_source_path: str | None = None,
) -> list[str]:
    """Build the FFmpeg command for the masonry collage final encode."""
    if not tiles:
        raise ValueError("masonry montage requires at least one tile")
    from app.pipeline.reframe import _encoding_args  # noqa: PLC0415

    duration = clamp_masonry_duration(duration_s)
    output_w = int(settings.output_width)
    output_h = int(settings.output_height)
    output_fps = int(settings.output_fps)
    pan_px = max(0, board_width - output_w)

    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-t",
        f"{duration:.3f}",
        "-i",
        f"color=c=white:s={output_w}x{output_h}:r={output_fps}",
    ]
    for tile in tiles:
        if tile.is_image:
            cmd.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", tile.local_path])
        else:
            cmd.extend(["-stream_loop", "-1", "-t", f"{duration:.3f}", "-i", tile.local_path])
    for tile in tiles:
        cmd.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", tile.mask_path])

    audio_input_index = 1 + len(tiles) * 2
    if audio_source_path:
        cmd.extend(["-t", f"{duration:.3f}", "-i", audio_source_path])
    else:
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-t",
                f"{duration:.3f}",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
        )

    filters: list[str] = []
    for idx, tile in enumerate(tiles):
        mask_index = 1 + len(tiles) + idx
        filters.append(
            f"[{tile.input_index}:v]"
            "format=rgba,"
            f"scale={tile.width}:{tile.height}:force_original_aspect_ratio=increase,"
            f"crop={tile.width}:{tile.height},fps={output_fps},"
            "setpts=PTS-STARTPTS,setsar=1,format=rgba"
            f"[tile{idx}raw]"
        )
        filters.append(f"[{mask_index}:v]format=gray[mask{idx}]")
        filters.append(f"[tile{idx}raw][mask{idx}]alphamerge[tile{idx}]")

    previous = "[0:v]"
    for idx, tile in enumerate(tiles):
        out = "[outv]" if idx == len(tiles) - 1 else f"[base{idx}]"
        # Escape the comma in min(t\,N); otherwise FFmpeg parses it as the next
        # filter option inside the overlay expression.
        x_expr = f"{tile.x}-min(t\\,{duration:.3f})/{duration:.3f}*{pan_px}"
        filters.append(
            f"{previous}[tile{idx}]overlay=x={x_expr}:y={tile.y}:eval=frame:shortest=1{out}"
        )
        previous = out

    cmd.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-map",
            f"{audio_input_index}:a:0?",
            "-shortest",
            *_encoding_args(output_path, preset="fast"),
        ]
    )
    return cmd


def assemble_masonry_montage(
    *,
    steps: list,
    clip_id_to_local: dict[str, str],
    output_path: str,
    tmpdir: str,
    duration_s: float,
    audio_source_path: str | None = None,
    job_id: str | None = None,
) -> None:
    """Render the masonry collage to ``output_path``."""
    mask_dir = os.path.join(tmpdir, "masonry_masks")
    tiles = build_masonry_tiles(
        steps=steps,
        clip_id_to_local=clip_id_to_local,
        mask_dir=mask_dir,
        normalize_images=True,
    )
    board_width = max(tile.x + tile.width for tile in tiles) + 34
    cmd = build_masonry_command(
        tiles=tiles,
        output_path=output_path,
        duration_s=duration_s,
        board_width=board_width,
        audio_source_path=audio_source_path,
    )
    result = subprocess.run(cmd, capture_output=True, timeout=MASONRY_TIMEOUT_S, check=False)
    if result.returncode != 0:
        stderr_tail = result.stderr[-2000:].decode("utf-8", "replace")
        raise RuntimeError(f"ffmpeg masonry montage failed (rc={result.returncode}): {stderr_tail}")
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("ffmpeg masonry montage produced empty output")
    log.info(
        "masonry_montage_rendered",
        job_id=job_id,
        tiles=len(tiles),
        duration_s=clamp_masonry_duration(duration_s),
    )
