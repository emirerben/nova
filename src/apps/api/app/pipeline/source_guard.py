"""Heavy-source downscale guard — bound peak decode memory before any reframe.

Why (2026-07-21 OOM incident, prod job e8173a25, worker 6e826515c714e8): a
170MB / 134s high-bitrate upload OOM-killed the worker during
``reframe_and_export``. Every per-slot reframe (and every variant) decodes the
ORIGINAL source at native resolution with ffmpeg's auto thread count —
frame-threaded 4K HEVC decode holds a thread-multiplied set of ~25MB frame
buffers plus the DPB, and that peak repeats for each of the job's slots. The
fix mirrors ``_pretonemap_hdr_clips`` (generative_build.py): convert the heavy
source ONCE at ingest to a bounded intermediate, then feed every downstream
consumer the intermediate.

Scope rules (keep this list tight — the guard must be a no-op for normal
uploads):

  - SDR only. HDR/HLG sources are excluded because ``_pretonemap_hdr_clips``
    already downscales them inside its zscale chain — a naive 8-bit re-encode
    here would destroy the tonemap input.
  - Trigger: probed SHORT edge > ``settings.source_downscale_short_edge_max``
    (default 1920). Symmetric in width/height, so phone rotation metadata
    (which swaps display dims but not coded dims) cannot flip the decision.
  - Target: the 1080x1920 cover scale, computed INSIDE the filter from post-
    autorotate frame dims (``iw``/``ih``), capped at 1 so nothing is ever
    upscaled. Every ``_build_video_filter`` branch (crop / letterbox / 16:9)
    then sees a source it would not have scaled up either.
  - The guard's own ffmpeg pass caps decoder AND encoder threads
    (``settings.source_downscale_ffmpeg_threads``) — bounding peak memory with
    an unbounded-memory pre-pass would be self-defeating.
  - Best-effort: any failure keeps the original clip in place (slow but
    correct). The guard must never fail a job.

Encoding: crf 16 / preset fast, matching the ``_pretonemap_hdr_clips`` quality
budget — this is the one extra generation that carries the full gradient
budget into the reframe→burn chain, so it must not introduce banding the final
``preset=fast`` pass can't recover (see tests/test_encoder_policy.py history).
Audio is stream-copied so original-audio variants keep faithful source audio.

Kill switch: ``SOURCE_DOWNSCALE_GUARD_ENABLED=false`` + worker restart.
"""

from __future__ import annotations

import os
import subprocess

import structlog

from app.config import settings

log = structlog.get_logger()

# Keep in sync with reframe._HLG_TRANSFER / reframe._HDR10_TRANSFER (imported
# lazily where needed to avoid a config-time import cycle; values are stable
# ffprobe transfer-characteristic names).
_HDR_TRANSFERS = frozenset({"arib-std-b67", "smpte2084"})

# Single ffmpeg invocation budget for one downscale pass. A 134s 4K source at
# 2 threads re-encodes in well under this; the cap exists so a pathological
# input can't eat the orchestrator's soft_time_limit.
_GUARD_TIMEOUT_S = 900


def needs_downscale(probe: object) -> bool:
    """True when this SDR source's short edge exceeds the guard threshold."""
    if not settings.source_downscale_guard_enabled:
        return False
    if getattr(probe, "color_trc", "bt709") in _HDR_TRANSFERS:
        return False
    try:
        width = int(getattr(probe, "width", 0) or 0)
        height = int(getattr(probe, "height", 0) or 0)
    except (TypeError, ValueError):
        return False
    if width <= 0 or height <= 0:
        return False
    return min(width, height) > settings.source_downscale_short_edge_max


def build_downscale_cmd(input_path: str, output_path: str) -> list[str]:
    """ffmpeg command for one bounded downscale pass (pure — unit-testable).

    The cover-scale expression uses ``iw``/``ih`` (post-autorotate) so rotated
    phone footage scales by its DISPLAY dims; ``min(1, …)`` forbids upscaling;
    ``trunc(…/2)*2`` keeps dims even for libx264+yuv420p. Commas inside the
    quoted expression are protected by the single quotes ffmpeg's filter
    parser honors (args are passed as a list — no shell).
    """
    ow = settings.output_width
    oh = settings.output_height
    cover = f"min(1,max({ow}/iw,{oh}/ih))"
    vf = f"scale=w='trunc(iw*{cover}/2)*2':h='trunc(ih*{cover}/2)*2':flags=lanczos:eval=init"
    threads = str(settings.source_downscale_ffmpeg_threads)
    return [
        "ffmpeg",
        "-y",
        # Decoder thread cap — MUST precede -i to apply to the input decoder.
        "-threads",
        threads,
        "-i",
        input_path,
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-crf",
        "16",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        # Encoder thread cap (output-side -threads).
        "-threads",
        threads,
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        output_path,
    ]


def downscale_oversized_sources(
    local_paths: list[str],
    probe_map: dict,
    tmpdir: str,
    *,
    job_id: str | None = None,
) -> int:
    """Downscale each oversized SDR clip once, in place. Returns count converted.

    Mutates ``local_paths`` (repoints converted entries at the intermediate)
    and ``probe_map`` (adds a probe for each intermediate). Runs strictly
    serially — one bounded-thread ffmpeg at a time is the memory contract.
    On success the ORIGINAL local file is deleted: /tmp is tmpfs (RAM-backed)
    on Fly, so a 170MB original left behind is 170MB of the same RAM budget
    this guard exists to protect.
    """
    from app.pipeline.image_clip import is_image_file  # noqa: PLC0415
    from app.pipeline.probe import probe_video  # noqa: PLC0415
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    converted = 0
    for idx, local_path in enumerate(local_paths):
        probe = probe_map.get(local_path)
        if probe is None or not needs_downscale(probe):
            continue
        # Still images ride clip_paths too (looped-image inputs) and carry a
        # synthetic probe with real pixel dims — a 12MP photo must NOT be run
        # through a libx264 video pass (image_clip owns image rendering).
        if is_image_file(local_path):
            continue
        out_path = os.path.join(tmpdir, f"guard_{idx}_{os.path.basename(local_path)}")
        cmd = build_downscale_cmd(local_path, out_path)
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=_GUARD_TIMEOUT_S)
            new_probe = probe_video(out_path)
        except Exception as exc:  # noqa: BLE001 — guard is best-effort by contract
            stderr = getattr(exc, "stderr", b"") or b""
            log.warning(
                "source_guard_downscale_failed",
                job_id=job_id,
                input=local_path,
                error=str(exc)[:200],
                stderr_tail=stderr[-500:].decode("utf-8", "replace") if stderr else "",
            )
            continue
        probe_map[out_path] = new_probe
        # Drop the ORIGINAL's probe entry — `_available_footage_s` sums
        # probe_map.values(), so a retained stale entry double-counts this
        # clip's duration and inflates the footage ceiling every variant is
        # sized against. (The pre-tonemap pass keeps its originals, but it
        # runs AFTER the footage sum; this guard runs inside _ingest_clips,
        # before it.)
        probe_map.pop(local_path, None)
        local_paths[idx] = out_path
        try:
            os.remove(local_path)
        except OSError:
            pass  # tmpdir cleanup will get it
        converted += 1
        log.info(
            "source_guard_downscaled",
            job_id=job_id,
            input=os.path.basename(local_path),
            src_res=f"{getattr(probe, 'width', '?')}x{getattr(probe, 'height', '?')}",
            dst_res=f"{new_probe.width}x{new_probe.height}",
        )
        record_pipeline_event(
            "reframe",
            "source_guard_downscaled",
            {
                "clip_index": idx,
                "src_res": f"{getattr(probe, 'width', 0)}x{getattr(probe, 'height', 0)}",
                "dst_res": f"{new_probe.width}x{new_probe.height}",
            },
        )
    return converted
