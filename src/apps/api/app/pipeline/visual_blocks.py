"""Compose structured visual blocks and stackable media beneath authored text."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import structlog
from PIL import Image

from app import storage
from app.agents._schemas.visual_block import (
    AssetBackground,
    BlurPreviousBackground,
    GradientBackground,
    MediaBlock,
    MontageBlock,
    SolidBackground,
    TextCardBlock,
    VisualBlock,
    VisualShot,
)
from app.config import settings

log = structlog.get_logger()

_FADE_S = 0.15
_TIMEOUT_S = 900


class VisualBlockError(RuntimeError):
    pass


def _media_frame_window(start_s: float, end_s: float) -> tuple[float, float]:
    """Quantize authored media boundaries to output frames.

    Both sides of an adjacent cut pass through the same half-up quantizer, so
    the compositor's half-open windows meet on exactly one output frame with
    neither a gap nor a double-visible boundary.
    """
    fps = max(1, int(settings.output_fps))
    frame_s = 1.0 / fps
    start_frame = math.floor(max(0.0, start_s) * fps + 0.5)
    end_frame = max(start_frame + 1, math.floor(max(0.0, end_s) * fps + 0.5))
    return start_frame * frame_s, end_frame * frame_s


def _run(cmd: list[str], *, label: str) -> None:
    result = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT_S, check=False)
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", errors="replace")[-1200:]
        raise VisualBlockError(f"{label} failed (rc={result.returncode}): {tail}")


def _has_audio(path: str) -> bool:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _motion_filter(shot: VisualShot, duration_s: float, *, still: bool = True) -> str:
    width, height, fps = settings.output_width, settings.output_height, settings.output_fps
    frames = max(1, round(duration_s * fps))
    scale = max(1.0, shot.crop.scale)
    start_zoom, end_zoom = scale, scale
    if shot.motion == "zoom_in":
        end_zoom = scale * 1.08
    elif shot.motion == "zoom_out":
        start_zoom = scale * 1.08
    x_frac, y_frac = shot.crop.x_frac, shot.crop.y_frac
    pan_dx = 0.08 if shot.motion == "pan_right" else -0.08 if shot.motion == "pan_left" else 0.0
    zoom_expr = f"{start_zoom}+({end_zoom}-{start_zoom})*on/{max(1, frames - 1)}"
    x_expr = f"max(0,min(iw-iw/zoom,(iw-iw/zoom)*({x_frac}+{pan_dx}*on/{max(1, frames - 1)})))"
    y_expr = f"max(0,min(ih-ih/zoom,(ih-ih/zoom)*{y_frac}))"
    return (
        f"scale={width * 4}:{height * 4}:force_original_aspect_ratio=increase,"
        f"crop={width * 4}:{height * 4},setsar=1,"
        f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':"
        f"d={frames if still else 1}:fps={fps}:s={width}x{height},format=yuv420p"
    )


def _render_image_shot(source: str, shot: VisualShot, duration_s: float, output: str) -> None:
    _run(
        [
            "ffmpeg",
            "-loop",
            "1",
            "-framerate",
            str(settings.output_fps),
            "-i",
            source,
            "-t",
            f"{duration_s:.6f}",
            "-vf",
            _motion_filter(shot, duration_s),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(settings.output_fps),
            "-y",
            output,
        ],
        label="visual-block image shot",
    )


def _render_video_shot(source: str, shot: VisualShot, duration_s: float, output: str) -> None:
    width, height = settings.output_width, settings.output_height
    x = max(0.0, min(1.0, shot.crop.x_frac))
    y = max(0.0, min(1.0, shot.crop.y_frac))
    scale = max(1.0, shot.crop.scale)
    if shot.motion == "none":
        vf = (
            f"scale={round(width * scale)}:{round(height * scale)}:"
            "force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:"
            f"x='max(0,min(iw-ow,(iw-ow)*{x}))':"
            f"y='max(0,min(ih-oh,(ih-oh)*{y}))',"
            f"fps={settings.output_fps},setpts=PTS-STARTPTS,setsar=1"
        )
    else:
        # d=1 consumes each source frame exactly once; the output-frame counter
        # still advances globally, preserving subtle zoom/pan on video shots.
        vf = f"{_motion_filter(shot, duration_s, still=False)},setpts=PTS-STARTPTS"
    _run(
        [
            "ffmpeg",
            "-ss",
            f"{float(shot.trim_start_s or 0.0):.6f}",
            "-i",
            source,
            "-t",
            f"{duration_s:.6f}",
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-y",
            output,
        ],
        label="visual-block video shot",
    )


def _download_shot(shot: VisualShot, tmpdir: str, index: int) -> str:
    suffix = Path(shot.src_gcs_path).suffix or (".jpg" if shot.kind == "image" else ".mp4")
    local = os.path.join(tmpdir, f"asset_{index}{suffix}")
    storage.download_to_file(shot.src_gcs_path, local)
    return local


def _download_media_block(block: MediaBlock, tmpdir: str, index: int) -> str:
    """Download a media block without baking its transform into a temp movie.

    Media blocks are composed in the final filter graph so contain/cover/focal
    point and PiP geometry remain editable until the single visual-block pass.
    Montage shots intentionally keep their existing pre-rendered path.
    """
    suffix = Path(block.src_gcs_path).suffix or (".jpg" if block.media_kind == "image" else ".mp4")
    local = os.path.join(tmpdir, f"media_{index}{suffix}")
    storage.download_to_file(block.src_gcs_path, local)
    return local


def _media_video_trim_window(block: MediaBlock) -> tuple[float, float]:
    source_duration = max(float(block.source_duration_s or 0.0), 0.1)
    frame_s = 1.0 / max(1, settings.output_fps)
    trim_start = max(
        0.0,
        min(float(block.trim_start_s or 0.0), max(0.0, source_duration - frame_s)),
    )
    trim_end = min(
        source_duration,
        max(
            trim_start + frame_s,
            min(float(block.trim_end_s or source_duration), source_duration),
        ),
    )
    return trim_start, trim_end


def _render_shot(shot: VisualShot, duration_s: float, tmpdir: str, index: int, output: str) -> None:
    local = _download_shot(shot, tmpdir, index)
    if shot.kind == "image":
        _render_image_shot(local, shot, duration_s, output)
    else:
        _render_video_shot(local, shot, duration_s, output)


def _concat(parts: list[str], output: str, tmpdir: str) -> None:
    manifest = os.path.join(tmpdir, f"concat_{Path(output).stem}.txt")
    Path(manifest).write_text("".join(f"file '{path}'\n" for path in parts))
    _run(
        [
            "ffmpeg",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            manifest,
            "-c",
            "copy",
            "-y",
            output,
        ],
        label="visual-block montage concat",
    )


def _render_montage(block: MontageBlock, tmpdir: str, block_index: int) -> str | None:
    parts: list[str] = []
    for shot_index, shot in enumerate(block.shots):
        output = os.path.join(tmpdir, f"block_{block_index}_shot_{shot_index}.mp4")
        try:
            _render_shot(shot, shot.duration_s, tmpdir, block_index * 100 + shot_index, output)
            parts.append(output)
        except Exception as exc:  # noqa: BLE001 - missing asset drops per shot
            log.warning(
                "visual_block_shot_dropped",
                block_id=block.id,
                shot_id=shot.id,
                error=str(exc)[:240],
            )
    # A partial montage would expose the underlying base early and break the
    # concrete timing contract. Fail open at block granularity instead.
    if len(parts) != len(block.shots):
        return None
    output = os.path.join(tmpdir, f"block_{block_index}.mp4")
    _concat(parts, output, tmpdir)
    return output


def _render_color(color: str, duration_s: float, output: str) -> None:
    _run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={settings.output_width}x{settings.output_height}:r={settings.output_fps}",
            "-t",
            f"{duration_s:.6f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-y",
            output,
        ],
        label="visual-block color card",
    )


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _gradient_png(background: GradientBackground, output: str) -> None:
    width, height = settings.output_width, settings.output_height
    start = _hex_rgb(background.from_color)
    end = _hex_rgb(background.to)
    angle = math.radians(background.angle_deg)
    dx, dy = math.cos(angle), math.sin(angle)
    corners = [0.0, dx * width, dy * height, dx * width + dy * height]
    low, high = min(corners), max(corners)
    span = max(1.0, high - low)
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            t = max(0.0, min(1.0, (dx * x + dy * y - low) / span))
            pixels[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(start, end))
    image.save(output, format="PNG")


def _render_blurred_previous(
    base_local: str, block: TextCardBlock, background: BlurPreviousBackground, output: str
) -> None:
    seek = max(0.0, block.start_s - 0.05)
    _run(
        [
            "ffmpeg",
            "-ss",
            f"{seek:.6f}",
            "-i",
            base_local,
            "-frames:v",
            "1",
            "-vf",
            f"scale={settings.output_width}:{settings.output_height}:force_original_aspect_ratio=increase,"
            f"crop={settings.output_width}:{settings.output_height},boxblur={background.blur_px:g}:2",
            "-y",
            output,
        ],
        label="visual-block blur frame",
    )


def _render_text_card(
    block: TextCardBlock, base_local: str, tmpdir: str, block_index: int
) -> str | None:
    duration_s = block.end_s - block.start_s
    output = os.path.join(tmpdir, f"block_{block_index}.mp4")
    bg = block.background
    if isinstance(bg, SolidBackground):
        _render_color(bg.color, duration_s, output)
    elif isinstance(bg, GradientBackground):
        png = os.path.join(tmpdir, f"block_{block_index}_gradient.png")
        _gradient_png(bg, png)
        shot = VisualShot(
            asset_id="generated-gradient",
            src_gcs_path="generated-gradient",
            kind="image",
            start_offset_s=0.0,
            duration_s=duration_s,
        )
        _render_image_shot(png, shot, duration_s, output)
    elif isinstance(bg, BlurPreviousBackground):
        png = os.path.join(tmpdir, f"block_{block_index}_blur.png")
        _render_blurred_previous(base_local, block, bg, png)
        shot = VisualShot(
            asset_id="blurred-previous",
            src_gcs_path="blurred-previous",
            kind="image",
            start_offset_s=0.0,
            duration_s=duration_s,
        )
        _render_image_shot(png, shot, duration_s, output)
    elif isinstance(bg, AssetBackground):
        _render_shot(bg.shot, duration_s, tmpdir, block_index * 100, output)
    else:  # pragma: no cover - discriminated schema makes this unreachable
        return None
    return output


def build_visual_block_composite_command(
    base_local: str,
    blocks: list[VisualBlock],
    replacement_paths: list[str],
    output_local: str,
    *,
    base_has_audio: bool = True,
) -> list[str]:
    cmd = ["ffmpeg", "-i", base_local]
    has_media = any(isinstance(block, MediaBlock) for block in blocks)
    for index, path in enumerate(replacement_paths):
        block = blocks[index]
        if has_media and isinstance(block, MediaBlock) and block.media_kind == "image":
            # Keep the source live until the base ends; the per-card trim and
            # half-open enable window bound what reaches the overlay filter.
            cmd.extend(["-loop", "1", "-framerate", str(settings.output_fps), "-i", path])
        elif has_media and isinstance(block, MediaBlock) and block.media_kind == "video":
            trim_start, _ = _media_video_trim_window(block)
            cmd.extend(["-ss", f"{trim_start:.6f}", "-i", path])
        else:
            cmd.extend(["-i", path])

    filters: list[str] = ["[0:v]setpts=PTS-STARTPTS[base0]"]
    current = "base0"
    if has_media:
        # Legacy blocks have no z field and must retain their historical order;
        # media blocks are the explicitly stackable lane and sort by z only.
        ordered = [
            (index, block)
            for index, block in enumerate(blocks)
            if not isinstance(block, MediaBlock)
        ] + sorted(
            ((index, block) for index, block in enumerate(blocks) if isinstance(block, MediaBlock)),
            key=lambda item: (item[1].z, item[1].start_s, item[1].id),
        )
    else:
        ordered = list(enumerate(blocks))

    for graph_index, (source_index, block) in enumerate(ordered):
        if isinstance(block, MediaBlock):
            transform = block.transform
            start_s, end_s = _media_frame_window(block.start_s, block.end_s)
            duration_s = end_s - start_s
            input_index = source_index + 1
            media_parts: list[str] = []
            if block.media_kind == "video":
                trim_start, trim_end = _media_video_trim_window(block)
                media_parts.append(f"trim=duration={trim_end - trim_start:.6f},setpts=PTS-STARTPTS")
            else:
                media_parts.append(f"trim=duration={duration_s:.6f},setpts=PTS-STARTPTS")

            if block.display_mode == "overlay":
                width = max(2, round(settings.output_width * block.scale))
                media_parts.append(f"scale={width}:-2,setsar=1")
                overlay_x = f"({settings.output_width}*{block.x_frac:.6f}-overlay_w/2)"
                overlay_y = f"({settings.output_height}*{block.y_frac:.6f}-overlay_h/2)"
            elif transform.fit_mode == "cover":
                zoom = max(1.0, float(transform.zoom))
                target_w = max(settings.output_width, round(settings.output_width * zoom))
                target_h = max(settings.output_height, round(settings.output_height * zoom))
                media_parts.append(
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                    f"crop={settings.output_width}:{settings.output_height}:"
                    f"x='(iw-ow)*{transform.focal_x:.6f}':"
                    f"y='(ih-oh)*{transform.focal_y:.6f}',setsar=1"
                )
                overlay_x, overlay_y = "0", "0"
            else:
                # Contain intentionally emits only the fitted source. The base
                # remains visible in the unused area instead of getting black
                # bars from a padded replacement movie. Zoom is applied before
                # placement; focal coordinates choose where an oversized or
                # off-center contained image sits over the base.
                zoom = max(1.0, float(transform.zoom))
                target_w = max(settings.output_width, round(settings.output_width * zoom))
                target_h = max(settings.output_height, round(settings.output_height * zoom))
                media_parts.append(
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,setsar=1"
                )
                overlay_x = f"(main_w-overlay_w)*{transform.focal_x:.6f}"
                overlay_y = f"(main_h-overlay_h)*{transform.focal_y:.6f}"

            media_parts.append("format=rgba")
            if block.transition_in == "fade" and duration_s > _FADE_S * 2:
                media_parts.append(f"fade=t=in:st=0:d={_FADE_S}:alpha=1")
            if block.transition_out == "fade" and duration_s > _FADE_S * 2:
                media_parts.append(
                    f"fade=t=out:st={max(0.0, duration_s - _FADE_S):.6f}:d={_FADE_S}:alpha=1"
                )
            media_label = f"media{graph_index}"
            media_parts.append(f"setpts=PTS-STARTPTS+{start_s:.6f}/TB,settb=AVTB[{media_label}]")
            filters.append(f"[{input_index}:v]" + ",".join(media_parts))
            output_label = f"base{graph_index + 1}"
            filters.append(
                f"[{current}][{media_label}]overlay={overlay_x}:{overlay_y}:"
                f"eof_action=pass:enable='gte(t,{start_s:.6f})*lt(t,{end_s:.6f})'"
                f"[{output_label}]"
            )
            current = output_label
            continue

        # Legacy Montage/TextCard path. Keep these expressions and input
        # indices byte-identical when no MediaBlock is present.
        index = graph_index
        duration_s = block.end_s - block.start_s
        fade_parts: list[str] = []
        if block.transition_in == "fade" and duration_s > _FADE_S * 2:
            fade_parts.append(f"fade=t=in:st=0:d={_FADE_S}:alpha=1")
        if block.transition_out == "fade" and duration_s > _FADE_S * 2:
            fade_parts.append(
                f"fade=t=out:st={max(0.0, duration_s - _FADE_S):.6f}:d={_FADE_S}:alpha=1"
            )
        fade = "," + ",".join(fade_parts) if fade_parts else ""
        input_index = source_index + 1 if has_media else index + 1
        filters.append(
            f"[{input_index}:v]setpts=PTS-STARTPTS+{block.start_s:.6f}/TB,"
            f"format=rgba{fade}[vb{index}]"
        )
        filters.append(
            f"[{current}][vb{index}]overlay=0:0:eof_action=pass:"
            f"enable='between(t,{block.start_s:.6f},{block.end_s:.6f})'"
            f"[base{index + 1}]"
        )
        current = f"base{index + 1}"

    mute_filters = []
    for block in blocks:
        if block.audio_policy.base != "mute":
            continue
        if isinstance(block, MediaBlock):
            start_s, end_s = _media_frame_window(block.start_s, block.end_s)
            mute_filters.append(f"volume=0:enable='gte(t,{start_s:.6f})*lt(t,{end_s:.6f})'")
        else:
            # Preserve the legacy inclusive audio window exactly.
            mute_filters.append(
                f"volume=0:enable='between(t,{block.start_s:.6f},{block.end_s:.6f})'"
            )
    filters.append(f"[{current}]format=yuv420p[vout]")
    if mute_filters and base_has_audio:
        filters.append(f"[0:a]{','.join(mute_filters)}[aout]")
    cmd.extend(["-filter_complex", ";".join(filters), "-map", "[vout]"])
    if mute_filters and base_has_audio:
        cmd.extend(["-map", "[aout]"])
    else:
        cmd.extend(["-map", "0:a?"])
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-y",
            output_local,
        ]
    )
    return cmd


def apply_visual_blocks(
    *,
    base_gcs_path: str,
    blocks: list[VisualBlock],
    output_gcs_path: str,
    job_id: str | None = None,
) -> str:
    """Download a clean base, apply valid blocks, and upload a text-free cache."""
    if not blocks:
        raise VisualBlockError("apply_visual_blocks called with an empty block list")
    with tempfile.TemporaryDirectory(prefix="nova_visual_blocks_") as tmpdir:
        base_local = os.path.join(tmpdir, "base.mp4")
        storage.download_to_file(base_gcs_path, base_local)
        ready_blocks: list[VisualBlock] = []
        replacements: list[str] = []
        for index, block in enumerate(blocks):
            try:
                if isinstance(block, MontageBlock):
                    replacement = _render_montage(block, tmpdir, index)
                elif isinstance(block, TextCardBlock):
                    replacement = _render_text_card(block, base_local, tmpdir, index)
                else:
                    replacement = _download_media_block(block, tmpdir, index)
            except Exception as exc:  # noqa: BLE001 - per-block fail-open
                log.warning(
                    "visual_block_dropped",
                    job_id=job_id,
                    block_id=block.id,
                    error=str(exc)[:300],
                )
                continue
            if replacement:
                ready_blocks.append(block)
                replacements.append(replacement)
        if not ready_blocks:
            raise VisualBlockError("No visual blocks could be rendered")
        output_local = os.path.join(tmpdir, "visual_blocks.mp4")
        _run(
            build_visual_block_composite_command(
                base_local,
                ready_blocks,
                replacements,
                output_local,
                base_has_audio=_has_audio(base_local),
            ),
            label="visual-block composite",
        )
        if not os.path.exists(output_local) or os.path.getsize(output_local) == 0:
            raise VisualBlockError("visual-block compositor produced no output")
        signed = storage.upload_public_read(output_local, output_gcs_path, content_type="video/mp4")
        log.info(
            "visual_blocks_applied",
            job_id=job_id,
            block_count=len(ready_blocks),
            output_gcs_path=output_gcs_path,
        )
        return signed


def copy_without_visual_blocks(source: str, output: str) -> None:
    """Test/helper parity path: a real file copy, never a transcode."""
    shutil.copy2(source, output)
