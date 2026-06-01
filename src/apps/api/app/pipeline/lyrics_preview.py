"""Lyric-only preview rendering.

Renders the lyric `.ass` produced by the production ASS-generation path over a
1080x1920 black video with the track's audio. If preview output disagrees with
full-render output for any reason other than compositing, the bug is in the
production renderer, not here. This module never re-implements ASS generation.

Window policy (2026-05-27): previews show a 20-second window anchored at
the first lyric line WITHIN the admin-selected best section
(`track_config.best_start_s` / `best_end_s` on `MusicTrack`). This is what
the admin music page's section-strip ("click to preview + select as best
section") implies — clicking section #2 should yield section #2 lyrics.

When the saved section contains no lyric lines (instrumental sections,
bridge sections with no vocals) OR `track_config` has no bounds, the
preview falls back to the prior 2026-05-25 policy: anchor at
`first_line_of_song - LEAD_IN_S`. The fallback exists so songs like
Billie Jean (30s instrumental intro) still render a non-silent preview
when the admin hasn't picked a vocal-bearing section. LEAD_IN_S preserves
~2s of pre-vocal audio so the fade-in reads as natural rather than chopped
at frame 0; the section-anchored path additionally clamps the anchor at
`best_start_s` so audio never bleeds from outside the section. Tracks
whose available tail after the anchor is shorter than `PREVIEW_WINDOW_S`
(or whose `best_end_s - anchor` is shorter) render the shorter window.

The window is enforced at TWO layers:
  1. `build_lyrics_preview_recipe` passes the anchored
     `[best_start_s, best_end_s]` into `inject_lyric_overlays`, which rebases
     line timings to section-relative coordinates so the recipe slot only
     contains lines that fall inside the window.
  2. `_build_preview_ffmpeg_cmd` passes `-ss {preview_start_s}` (input-seek on
     the audio) and `-t {preview_duration_s}` to FFmpeg so the final MP4 is
     hard-capped on the encoder side too. Relying only on `-shortest` is unsafe
     here because lavfi `color=...` is an infinite source — without `-t`, the
     output would run until the audio ends.

Encoder policy (2026-05-25): goes through `_encoding_args(preset="fast")` so
the bytes admins watch in the browser stay banding-free on dark gradients. The
preset is locked by `tests/test_encoder_policy.py`; the CRF literal is asserted
inline in `tests/pipeline/test_lyrics_preview.py`.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings
from app.pipeline._ffmpeg_filter_paths import escape_ffmpeg_filter_path
from app.pipeline.lyric_injector import _finalize_lyric_audible_window, inject_lyric_overlays
from app.pipeline.reframe import _encoding_args
from app.pipeline.text_overlay import FONTS_DIR, generate_animated_overlay_ass
from app.services.pipeline_trace import record_pipeline_event
from app.storage import download_to_file, upload_public_read

# Maximum preview duration. The window is anchored at the first lyric line and
# extended forward up to this many seconds; tracks whose remaining audio after
# the anchor is shorter than this render the available tail and stop.
PREVIEW_WINDOW_S: float = 20.0

# Seconds of pre-roll the preview shows before the first lyric line so the
# fade-in animation reads as natural rather than chopped at video frame 0.
# `preview_start_s = max(0, first_line.start_s - LEAD_IN_S)` — early-vocal
# tracks (first line < LEAD_IN_S) keep the original `start_s = 0` behavior.
LEAD_IN_S: float = 2.0

# CRF target for the final preview encode. Documented + tested inline so a
# future tweak forces a conscious choice (encoder policy locks preset class,
# not the CRF literal — see test_lyrics_preview.py for the assertion).
PREVIEW_CRF: str = "20"


class LyricsPreviewInputError(ValueError):
    """Raised when a track cannot produce a lyric preview."""


def _read_best_end_s(track_config: Any) -> float | None:
    """Read `best_end_s` from a track_config that may be a dict (JSONB load)
    or an object with attributes (Pydantic, SimpleNamespace, etc.).

    Returns the float value or None if absent/unparseable. Never raises.
    """
    if track_config is None:
        return None
    if isinstance(track_config, dict):
        raw = track_config.get("best_end_s")
    else:
        raw = getattr(track_config, "best_end_s", None)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _read_best_bounds(track_config: Any) -> tuple[float | None, float | None]:
    """Read `(best_start_s, best_end_s)` from a track_config that may be a
    dict (JSONB load) or an object with attributes.

    NaN / Inf values are rejected (returned as None) so the section-anchor
    path's `best_start <= line <= best_end` comparison can't be silently
    bypassed — all NaN comparisons return False, which would always fail
    the in-section check and fire the fallback even when the section
    bounds were merely corrupted. Matches the guard in `_first_line_start_s`.

    Returns `(None, None)` semantics: either axis missing/unparseable
    propagates as None on that axis only, so callers can treat "either
    missing" as "no section configured" without per-axis branching.
    Never raises.
    """
    if track_config is None:
        return (None, None)
    if isinstance(track_config, dict):
        raw_start = track_config.get("best_start_s")
        raw_end = track_config.get("best_end_s")
    else:
        raw_start = getattr(track_config, "best_start_s", None)
        raw_end = getattr(track_config, "best_end_s", None)

    def _coerce(raw: Any) -> float | None:
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    return (_coerce(raw_start), _coerce(raw_end))


def _resolve_track_duration_s(track: Any) -> float:
    """Resolve the source track's total duration in seconds.

    Resolution order:
      1. `track.duration_s` if positive
      2. `track.track_config.best_end_s` (supports dict OR object shapes)
      3. Raise `LyricsPreviewInputError` — we won't ship a preview without
         knowing the source length.
    """
    duration_s = float(getattr(track, "duration_s", None) or 0.0)
    if duration_s <= 0:
        fallback = _read_best_end_s(getattr(track, "track_config", None))
        if fallback is not None and fallback > 0:
            duration_s = fallback
    if duration_s <= 0:
        raise LyricsPreviewInputError("Music track duration is unknown.")
    return duration_s


def _first_line_start_s(lyrics_cached: Any) -> float | None:
    """Return the earliest `start_s` across cached lyric lines, or None if the
    cache has no lines / no parseable timings.

    `lyrics_cached["lines"]` is normally pre-sorted ascending by
    `app/agents/lyrics.py`, but we min() across the array anyway so a future
    backfill / manual edit that breaks ordering still picks the right anchor.
    """
    if not isinstance(lyrics_cached, dict):
        return None
    lines = lyrics_cached.get("lines") or []
    starts: list[float] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        raw = line.get("start_s")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        # Reject NaN/Inf: `float("nan")` and `float("inf")` succeed and would
        # propagate to FFmpeg `-ss nan` (FFmpeg error) and to the JSON status
        # response (where the frontend's `formatMSS` would render "NaN:NaN").
        # All NaN comparisons return False, so the `<=` clamp in
        # `_resolve_preview_window` would silently pass non-finite values
        # through if we didn't guard here.
        if not math.isfinite(value):
            continue
        starts.append(value)
    if not starts:
        return None
    return min(starts)


def _coerce_finite(raw: Any) -> float | None:
    """Coerce a raw value to a finite float, or None.

    Centralizes the NaN/Inf + TypeError/ValueError guards used in three
    places (`_read_best_bounds`, `_first_line_start_s`,
    `_first_lyric_in_section`) so the rules stay consistent if we tighten
    them later (e.g. add an upper bound).
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _first_lyric_in_section(
    lyrics_cached: Any,
    best_start_s: float,
    best_end_s: float,
) -> float | None:
    """Return the earliest `start_s` across cached lyric lines that OVERLAP
    the half-open interval `[best_start_s, best_end_s)`, or None when no
    line overlaps.

    Overlap semantics (2026-05-27 rev2):
      - When the line has a finite `end_s`: `line_start < best_end_s AND
        line_end > best_start_s`. A line that starts before the section
        but ends inside it (e.g. a chorus line that bleeds in from the
        pre-chorus) still counts — its earliest in-section content is
        what the admin wants to preview.
      - When `end_s` is missing or non-finite: fall back to start-only
        membership at `best_start_s <= line_start < best_end_s`. Half-open
        on the upper bound so a line starting exactly at `best_end_s`
        does NOT count (it would render a window with no overlapping
        lyric content).

    The return value is `line.start_s`, not the clamped overlap point.
    `_resolve_preview_window` clamps the eventual anchor to
    `max(0.0, best_start_s, line.start_s - LEAD_IN_S)` so callers that
    receive a `line.start_s < best_start_s` do not bleed audio from
    outside the section.
    """
    if not isinstance(lyrics_cached, dict):
        return None
    lines = lyrics_cached.get("lines") or []
    starts: list[float] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        line_start = _coerce_finite(line.get("start_s"))
        if line_start is None:
            continue
        line_end = _coerce_finite(line.get("end_s"))
        if line_end is not None:
            # Interval-overlap path. Half-open on the section upper bound:
            # a line ending at best_start_s does NOT overlap.
            in_section = line_start < best_end_s and line_end > best_start_s
        else:
            # Start-only path. Half-open: a line starting at best_end_s
            # does NOT count.
            in_section = best_start_s <= line_start < best_end_s
        if in_section:
            starts.append(line_start)
    if not starts:
        return None
    return min(starts)


def _resolve_preview_window_with_policy(
    track: Any,
) -> tuple[float, float, str, str | None]:
    """Compute the preview window AND name which policy produced it.

    Returns `(start_s, duration_s, policy, fallback_reason)`:
      - `policy = "section"` — section-anchored path succeeded.
        `fallback_reason` is `None`.
      - `policy = "fallback"` — first-vocal-of-song fallback fired.
        `fallback_reason` names which gate the section path failed:
          * `"no_bounds"` — `track_config` missing one or both bounds.
          * `"invalid_bounds"` — `best_end_s <= best_start_s` (zero / negative span).
          * `"no_lyrics_in_section"` — section has no overlapping lyric lines.
          * `"no_positive_available_window"` — anchor + section_end gave a
            non-positive renderable window (e.g. `best_end_s` past
            `track_duration_s` AND anchor past either).

    Telemetry in `render_lyrics_preview` keys off `policy`, NOT a post-hoc
    "is anchor outside section" check. The post-hoc check missed cases
    where the fallback anchor happened to land inside the configured
    section (e.g. section [20, 30] with no lyrics inside, first vocal at
    30.8s → fallback anchor 28.8s sits inside [20, 30] even though the
    fallback fired). The policy field is authoritative.

    Two-tier behavior (2026-05-27):
      1. **Section-anchored (primary).** When `track_config.best_start_s` /
         `best_end_s` are both present, the span is positive, and the
         section contains at least one overlapping lyric line, anchor at
         `max(0.0, best_start_s, first_in_section - LEAD_IN_S)`. The
         `0.0` clamp is defense-in-depth against negative `best_start_s`
         (the API accepts finite negatives today). Duration is capped by
         `best_end_s - anchor` and `PREVIEW_WINDOW_S`.
      2. **First-vocal-of-song (fallback).** Anchor at
         `max(0.0, first_line_of_song - LEAD_IN_S)`. Preserves the
         2026-05-25 Billie Jean fix where 30s of instrumental intro would
         otherwise have rendered a silent preview.

    Both numeric values are rounded to 3 decimals so the FFmpeg `-ss` /
    `-t` literals match the recipe slot's `target_duration_s` precision.
    """
    track_duration_s = _resolve_track_duration_s(track)
    lyrics_cached = getattr(track, "lyrics_cached", None)
    section_start_s, section_end_s = _read_best_bounds(getattr(track, "track_config", None))

    # Section-anchored path. Track WHY the path falls through so the
    # telemetry can be specific instead of conflating four distinct cases.
    fallback_reason: str | None
    if section_start_s is None or section_end_s is None:
        fallback_reason = "no_bounds"
    elif section_end_s <= section_start_s:
        fallback_reason = "invalid_bounds"
    else:
        first_in_section = _first_lyric_in_section(lyrics_cached, section_start_s, section_end_s)
        if first_in_section is None:
            fallback_reason = "no_lyrics_in_section"
        else:
            # 0.0 clamp guards against negative `best_start_s` slipping in
            # (the API column is a free float). Without it, a section saved
            # at best_start_s=-5 with a lyric at 0.5s would anchor at -1.5
            # and pass `-ss -1.500` to FFmpeg, which silently treats it
            # as 0s — but the math downstream (`available`, `duration_s`)
            # would be off.
            anchor = max(0.0, section_start_s, first_in_section - LEAD_IN_S)
            available = min(track_duration_s, section_end_s) - anchor
            if available > 0:
                duration_s = min(PREVIEW_WINDOW_S, available)
                return (
                    round(anchor, 3),
                    round(duration_s, 3),
                    "section",
                    None,
                )
            fallback_reason = "no_positive_available_window"

    # Fallback: first vocal of the whole song.
    first_start = _first_line_start_s(lyrics_cached)
    if first_start is None or first_start <= LEAD_IN_S:
        start_s = 0.0
    else:
        start_s = max(0.0, first_start - LEAD_IN_S)
    available = track_duration_s - start_s
    if available <= 0:
        raise LyricsPreviewInputError(
            f"Lyric anchor at {start_s:.3f}s (first line at "
            f"{first_start if first_start is not None else 'n/a'}s) "
            f"exceeds track duration {track_duration_s:.3f}s."
        )
    duration_s = min(PREVIEW_WINDOW_S, available)
    return round(start_s, 3), round(duration_s, 3), "fallback", fallback_reason


def _resolve_preview_window(track: Any) -> tuple[float, float]:
    """Thin wrapper returning just `(start_s, duration_s)` for callers that
    do not need the policy / fallback_reason fields (e.g. recipe building,
    existing tests). Telemetry consumers in `render_lyrics_preview` call
    `_resolve_preview_window_with_policy` directly.
    """
    start_s, duration_s, _policy, _reason = _resolve_preview_window_with_policy(track)
    return start_s, duration_s


def build_lyrics_preview_recipe(track: Any, lyrics_config_effective: dict) -> dict:
    """Build a one-slot recipe anchored at the first lyric line and inject
    lyrics via production code.

    The chosen lyric style lives inside ``lyrics_config_effective["style"]``
    (one of "line", "karaoke", "per-word-pop"). The admin route always sets
    it explicitly. Callers that omit it (legacy tests, internal helpers
    written against the old implicit-Line behavior) get ``"line"`` as the
    backwards-compatible default — NOT the dispatcher's intrinsic default
    of ``"karaoke"``, because every caller of this module historically
    meant Line. Passing an unset style downstream would silently flip
    behavior; this default makes the migration safe.
    """
    lyrics_cached = getattr(track, "lyrics_cached", None)
    if not lyrics_cached:
        raise LyricsPreviewInputError("Music track has no cached lyrics to preview.")
    preview_start_s, preview_duration_s = _resolve_preview_window(track)

    recipe = {
        "slots": [
            {
                "position": 1,
                "target_duration_s": preview_duration_s,
                "text_overlays": [],
            }
        ]
    }
    cfg = {**lyrics_config_effective, "enabled": True}
    cfg.setdefault("style", "line")
    recipe = inject_lyric_overlays(
        recipe,
        lyrics_cached,
        best_start_s=preview_start_s,
        best_end_s=preview_start_s + preview_duration_s,
        lyrics_config=cfg,
    )
    if cfg.get("style") == "line":
        # Full music renders run this after slot collection. Preview has one
        # synthetic slot, so slot-relative time already equals absolute video
        # time; running it here keeps stale line-bound text out of the preview.
        slot = recipe["slots"][0]
        slot["text_overlays"] = _finalize_lyric_audible_window(
            slot.get("text_overlays") or [],
            audio_mix_song_start_s=preview_start_s,
            audio_mix_song_end_s=preview_start_s + preview_duration_s,
        )
    return recipe


def build_lyrics_preview_ass_files(
    track: Any,
    lyrics_config_effective: dict,
    output_dir: str,
) -> list[str]:
    """Generate the same ASS files production would burn for lyric overlays."""
    os.makedirs(output_dir, exist_ok=True)
    recipe = build_lyrics_preview_recipe(track, lyrics_config_effective)
    slots = recipe.get("slots") or []
    if not slots:
        raise LyricsPreviewInputError("Lyric preview produced no renderable slots.")
    slot = slots[0]
    ass_files = generate_animated_overlay_ass(
        slot.get("text_overlays") or [],
        slot_duration_s=float(slot.get("target_duration_s") or 0.0),
        output_dir=output_dir,
        slot_index=0,
    )
    if not ass_files:
        raise LyricsPreviewInputError("Lyric preview produced no renderable lyric overlays.")
    return ass_files


_STYLE_PATH_TOKEN = {"line": "line", "karaoke": "karaoke", "per-word-pop": "popup"}


def _style_path_segment(lyrics_config_effective: dict) -> str:
    """Return a filesystem-safe segment for the configured lyric style.

    Falls back to "line" if the style is missing or unknown — matching the
    runtime default in ``inject_lyric_overlays`` (which itself falls back to
    "karaoke" if unset, but Line is what the historical preview path always
    rendered, so an empty-style preview should not silently change category).
    The "-" in "per-word-pop" is collapsed to "popup" so the path stays
    URL-friendly and human-readable.
    """
    style = lyrics_config_effective.get("style")
    return _STYLE_PATH_TOKEN.get(str(style) if style is not None else "", "line")


def render_lyrics_preview(
    track: Any,
    lyrics_config_effective: dict,
    job_id: str,
) -> tuple[str, dict]:
    """Render a browser-playable MP4 preview and upload it.

    ``job_id`` is required and namespaces the GCS object so concurrent
    previews (or sequential previews after a config edit) do not overwrite
    one another. Without this, every preview for the same track wrote to
    `music-lyrics-previews/{track_id}/lyrics-preview.mp4` — admin-visible
    silent UX corruption (job A's status row pointed at a URL serving job
    B's render bytes).

    The chosen lyric style (line / karaoke / per-word-pop) is encoded in the
    GCS path between the track and job segments. This lets the dashboard
    show one preview per style without races between concurrent style runs
    against the same track. The 24h lifecycle rule in
    ``infra/gcs-lifecycle.json`` matches both layouts (the rule keys on the
    ``music-lyrics-previews/`` prefix), so per-style paths still get
    deleted on the same schedule as flat-path legacy objects.
    """
    audio_gcs_path = getattr(track, "audio_gcs_path", None)
    if not audio_gcs_path:
        raise LyricsPreviewInputError("Music track has no audio file.")
    track_id = str(getattr(track, "id", "unknown"))
    style_segment = _style_path_segment(lyrics_config_effective)

    # Resolve the window ONCE so the same (start_s, duration_s) reaches both
    # the ASS generation (via build_lyrics_preview_recipe → inject_lyric_overlays)
    # and the FFmpeg `-ss` / `-t` flags. Drift between those two would mean the
    # lyrics land outside the video frame or the audio plays a different
    # segment than the lyrics describe.
    (
        preview_start_s,
        preview_duration_s,
        policy,
        fallback_reason,
    ) = _resolve_preview_window_with_policy(track)

    # Defense-in-depth telemetry: emit a structured event whenever the user
    # has configured section bounds AND the resolver fell back to the
    # first-vocal-of-song policy. Gating on the explicit `policy` field
    # (not on whether the final anchor sits inside the section) is the
    # only correct signal: a fallback can still produce a preview start
    # that happens to land inside the configured section (e.g. section
    # [20, 30] with no lyrics inside + first vocal at 30.8s → fallback
    # anchor 28.8s sits inside [20, 30] even though the fallback fired).
    # The previous "outside section" inference missed that case.
    # `record_pipeline_event` no-ops when there is no active
    # `pipeline_trace_for` context, so this is safe in tests that bypass
    # the Celery wrapper.
    section_start_raw, section_end_raw = _read_best_bounds(getattr(track, "track_config", None))
    section_bounds_configured = section_start_raw is not None and section_end_raw is not None
    if policy == "fallback" and section_bounds_configured:
        record_pipeline_event(
            "preview",
            "anchor_outside_section",
            {
                "preview_start_s": preview_start_s,
                "best_start_s": section_start_raw,
                "best_end_s": section_end_raw,
                "reason": fallback_reason or "no_lyrics_in_section",
            },
        )

    with tempfile.TemporaryDirectory(prefix="nova_lyrics_preview_") as tmpdir:
        audio_ext = Path(str(audio_gcs_path)).suffix or ".m4a"
        local_audio = os.path.join(tmpdir, f"audio{audio_ext}")
        download_to_file(audio_gcs_path, local_audio)

        ass_files = build_lyrics_preview_ass_files(track, lyrics_config_effective, tmpdir)
        output_path = os.path.join(tmpdir, "lyrics_preview.mp4")
        cmd = _build_preview_ffmpeg_cmd(
            local_audio, ass_files, output_path, preview_start_s, preview_duration_s
        )

        result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"lyrics preview ffmpeg failed (rc={result.returncode}): {stderr}")
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("lyrics preview ffmpeg produced empty output")

        # Per-job AND per-style namespacing: track_id alone collides across
        # iterations (different config edits for the same track) and the
        # admin dashboard now renders three independent style previews. The
        # 24h delete rule in `infra/gcs-lifecycle.json` keys on the
        # `music-lyrics-previews/` prefix so blobs don't accumulate forever.
        object_path = (
            f"music-lyrics-previews/{track_id}/{style_segment}/{job_id}/lyrics-preview.mp4"
        )
        output_url = upload_public_read(output_path, object_path)
        return output_url, {
            "ass_count": len(ass_files),
            "ffmpeg_cmd": cmd,
            "output_gcs_path": object_path,
            "preview_start_s": preview_start_s,
            "preview_duration_s": preview_duration_s,
            "lyric_style": style_segment,
        }


def _build_preview_ffmpeg_cmd(
    local_audio: str,
    ass_files: list[str],
    output_path: str,
    preview_start_s: float,
    preview_duration_s: float,
) -> list[str]:
    """Assemble the FFmpeg invocation for a lyric-only preview.

    Encoder policy: routes through ``_encoding_args(preset="fast", crf="20")``
    so the final output stays in the banding-safe x264 territory. The call
    site is locked by ``tests/test_encoder_policy.py:FINAL_OUTPUT_REQUIRED``
    (preset class) and ``test_lyrics_preview.py`` (CRF literal).

    Window: emits ``-ss {preview_start_s}`` immediately before the audio
    ``-i`` (input-seek, fast and keyframe-safe; affects only the audio input,
    not the infinite lavfi color source) and ``-t {preview_duration_s}``
    before the encoding block. The lavfi color source is INFINITE, so
    ``-shortest`` alone would let the output run until the (track-tail) audio
    ends — the explicit ``-t`` is the layer that actually guarantees the
    output stays ≤ ``PREVIEW_WINDOW_S``.
    """
    filter_parts: list[str] = ["[0:v]null[base]"]
    prev = "base"
    fontsdir = escape_ffmpeg_filter_path(FONTS_DIR)
    for idx, ass_path in enumerate(ass_files):
        out = f"ass{idx}"
        escaped_ass = escape_ffmpeg_filter_path(ass_path)
        filter_parts.append(f"[{prev}]subtitles='{escaped_ass}':fontsdir='{fontsdir}'[{out}]")
        prev = out

    # ``-t`` and ``-shortest`` are both per-output flags and must appear
    # before the output encoding block (which ends in ``-y output_path``).
    # _encoding_args owns everything from ``-c:v`` onward, so they go just
    # before that. The ``-ss`` is an *input* option for the audio input — it
    # must come right before its ``-i``, never after.
    return [
        "ffmpeg",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={settings.output_width}x{settings.output_height}:r={settings.output_fps}",
        "-ss",
        f"{preview_start_s:.3f}",
        "-i",
        local_audio,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        f"[{prev}]",
        "-map",
        "1:a",
        "-t",
        f"{preview_duration_s:.3f}",
        "-shortest",
        *_encoding_args(output_path, preset="fast", crf=PREVIEW_CRF),
    ]
