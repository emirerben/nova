"""Audio mixer for templates with a fixed-intro voiceover plus body music.

Used by the `single_video` template family (e.g. "How do you enjoy
your life?"). Mixes a voiceover that plays during a black-screen intro window
with a music track that is ducked under the voiceover and rises to full volume
when the body video starts.

Filter graph:

    Music ─split─┬─ atrim 0..INTRO_END  ─ volume=-12dB ─┐
                 │                                      │ acrossfade
                 └─ atrim INTRO_END-0.2..DUR ── volume=0dB ─┘     (200ms ramp,
                                                                no audible click)
                                       │
                                       └─── amix ─── loudnorm ── afade out
    Voiceover ── apad to DUR ──────────┘

CRITICAL invariants:
- Music file MUST be at least DUR seconds long. Looping is the caller's job
  (asset-prep step generates a pre-looped m4a). The mixer does not loop.
- Voiceover plays from t=0 and is silently padded (apad) to DUR; truncated
  at intro_duration_s by the natural end of the audio file (or earlier if VO
  is shorter than intro_duration_s — that's fine, music carries the rest).
- Probe failures are non-fatal: caller still gets *some* video, with warnings
  logged. Catastrophic failure (both probes fail) → input video copied unchanged.

CLAUDE.md anti-pattern guard: this module uses subprocess FFmpeg, never
MoviePy/VideoFileClip.
"""

import shutil
import subprocess

import structlog

log = structlog.get_logger()


class IntroVoiceoverMixError(Exception):
    pass


def _probe_duration(path: str) -> float:
    """Return media duration in seconds, or 0.0 sentinel on probe failure."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True, timeout=10, check=False,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


def render_intro_voiceover_mix(
    *,
    video_path: str,
    music_path: str,
    voiceover_path: str,
    output_path: str,
    intro_duration_s: float,
    music_duck_db: float = -12.0,
    music_fadeup_ms: int = 200,
    output_lufs: float = -14.0,
) -> None:
    """Mix voiceover + ducked-then-full music onto a silent video track.

    Args:
        video_path: Silent video (intro+body already concatenated, no audio
                    or audio-stripped).
        music_path: Music asset, MUST be ≥ video duration. Caller handles loop.
        voiceover_path: Voiceover asset, plays t=0..min(vo_dur, intro_duration_s).
        output_path: Where to write the muxed mp4.
        intro_duration_s: Length of the black-screen intro window (seconds).
                          Music is ducked from t=0 until ramp ends ~50ms past
                          intro_duration_s.
        music_duck_db: Attenuation applied to music during intro (negative dB).
        music_fadeup_ms: Length of the music duck→full ramp at the drop.
        output_lufs: Target loudness for final master.

    Raises:
        IntroVoiceoverMixError: only on truly unrecoverable conditions; most
            failure modes degrade gracefully (see module docstring).
    """
    video_dur = _probe_duration(video_path)
    music_dur = _probe_duration(music_path)
    vo_dur = _probe_duration(voiceover_path)

    log.info(
        "intro_vo_mix_start",
        video_dur=round(video_dur, 3),
        music_dur=round(music_dur, 3),
        vo_dur=round(vo_dur, 3),
        intro_duration_s=intro_duration_s,
    )

    # Catastrophic: video probe failed. Cannot trim correctly. Copy input.
    if video_dur <= 0:
        log.warning("intro_vo_mix_video_probe_failed", falling_back="copy_input")
        shutil.copy2(video_path, output_path)
        return

    # Music probe failed → keep VO if we have it, else silent body.
    # Voiceover probe failed → music-only (existing behavior shape).
    if music_dur <= 0 and vo_dur <= 0:
        log.warning("intro_vo_mix_both_probes_failed", falling_back="copy_input")
        shutil.copy2(video_path, output_path)
        return

    # Music length contract: must cover the full video. Caller's job.
    if music_dur > 0 and music_dur < video_dur - 0.05:
        log.warning(
            "intro_vo_mix_music_shorter_than_video",
            music_dur=round(music_dur, 3),
            video_dur=round(video_dur, 3),
            note="music will end early; loudnorm + amix will produce silence tail",
        )

    # Floor fadeup to 50ms (no audible click), cap so it can never exceed
    # the intro window or half the video duration. Without the cap, recipes
    # with intro_duration_s < fadeup_s clamp intro_end to fadeup_s and the
    # acrossfade ends up blending two streams that both start at music_t=0
    # — produces a double-volume artifact at start.
    fadeup_s = max(music_fadeup_ms / 1000.0, 0.05)
    fadeup_s = min(fadeup_s, max(intro_duration_s, 0.05), max(video_dur * 0.5, 0.05))
    fade_out_start = max(0.0, video_dur - 0.5)

    # Build filter graph based on which audio inputs are usable.
    # Inputs in ffmpeg cmd:
    #   0: video_path  (video stream only consumed)
    #   1: music_path  (only present if music_dur > 0)
    #   2: voiceover_path  (only present if vo_dur > 0; index shifts if music absent)
    inputs: list[str] = ["-i", video_path]
    music_idx: int | None = None
    vo_idx: int | None = None

    if music_dur > 0:
        music_idx = len(inputs) // 2  # next input index
        inputs += ["-i", music_path]
    if vo_dur > 0:
        vo_idx = len(inputs) // 2
        inputs += ["-i", voiceover_path]

    # Compose filter graph
    filter_parts: list[str] = []
    final_audio_label = "[out_a]"

    # All three branches end with `loudnorm,aresample=48000,afade`. The
    # aresample is critical — loudnorm internally upsamples to 192kHz and
    # without an explicit downsample the AAC encoder picks 96kHz, which
    # plays back as "weird" / wrong-pitch on some browsers and devices.
    # 48kHz is the modern TikTok/YouTube standard.
    _OUTPUT_SAMPLE_RATE = 48000
    _TAIL = (
        f"loudnorm=I={output_lufs}:TP=-1.5:LRA=11,"
        f"aresample={_OUTPUT_SAMPLE_RATE},"
        f"afade=t=out:st={fade_out_start:.3f}:d=0.5[out_a]"
    )

    if music_idx is not None and vo_idx is not None:
        # Both streams present — full graph (intro low → drop → body full + VO mixed in)
        intro_end = max(intro_duration_s, fadeup_s)
        body_in = max(0.0, intro_end - fadeup_s)
        filter_parts.append(
            f"[{music_idx}:a]asplit=2[m_intro][m_body];"
            f"[m_intro]atrim=0:{intro_end:.3f},asetpts=PTS-STARTPTS,"
            f"volume={music_duck_db:.2f}dB[m_low];"
            f"[m_body]atrim={body_in:.3f}:{video_dur:.3f},asetpts=PTS-STARTPTS,"
            f"volume=0dB[m_full];"
            f"[m_low][m_full]acrossfade=d={fadeup_s:.3f}:c1=tri:c2=tri[m_mixed];"
            f"[{vo_idx}:a]apad=pad_dur={video_dur:.3f},"
            f"atrim=0:{video_dur:.3f},asetpts=PTS-STARTPTS[vo_padded];"
            f"[m_mixed][vo_padded]amix=inputs=2:duration=longest:weights=1 1[mix];"
            f"[mix]{_TAIL}"
        )
    elif music_idx is not None:
        # Music only (VO probe failed) — same shape as old _mix_template_audio.
        filter_parts.append(
            f"[{music_idx}:a]atrim=0:{video_dur:.3f},asetpts=PTS-STARTPTS,"
            f"{_TAIL}"
        )
    else:
        # VO only (music probe failed). Pad VO with silence for body, no music.
        filter_parts.append(
            f"[{vo_idx}:a]apad=pad_dur={video_dur:.3f},"
            f"atrim=0:{video_dur:.3f},asetpts=PTS-STARTPTS,"
            f"{_TAIL}"
        )

    cmd: list[str] = (
        ["ffmpeg"]
        + inputs
        + [
            "-filter_complex", ";".join(filter_parts),
            "-map", "0:v",
            "-map", final_audio_label,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-t", f"{video_dur:.3f}",
            "-movflags", "+faststart",
            "-y",
            output_path,
        ]
    )

    result = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-800:]
        log.warning("intro_vo_mix_ffmpeg_failed", stderr=stderr)
        # Last-resort fallback: copy input video unchanged.
        shutil.copy2(video_path, output_path)
        return

    log.info("intro_vo_mix_done", output=output_path)
