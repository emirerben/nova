"""FFmpeg xfade transition joining for template-mode slot assembly.

Builds a filter_complex chain that applies crossfade/fade/wipe transitions
between pre-rendered slot video files. Outputs video-only (-an) because
template audio is mixed separately via _mix_template_audio().

CRITICAL: Never use shell=True. Always pass args as a list.
"""

import subprocess

import structlog

log = structlog.get_logger()

# Transition type → FFmpeg xfade `transition` parameter.
# Public because single_pass._build_xfade_chain consumes it from a sibling
# module. Renamed from _XFADE_MAP in the M3 review pass — the leading
# underscore was misleading once the cross-module consumer landed.
XFADE_MAP: dict[str, str] = {
    "crossfade": "fade",
    "fade_black": "fadeblack",
    "wipe_left": "wipeleft",
    "wipe_right": "wiperight",
}
# Backcompat alias for any downstream consumer that imported the
# underscore name. Safe to delete after one release.
_XFADE_MAP = XFADE_MAP

# Gemini vocabulary → internal transition type.
# Gemini uses video-editor-friendly names; this maps them to _XFADE_MAP keys
# or special values handled by the pipeline (e.g. "curtain-close" → interstitial).
#
# match-cut and speed-ramp render with NO xfade filter at the boundary itself
# — they're semantic transitions, not animated ones. match-cut is a hard cut on
# visual continuity (action match, eye-line match), so the on-screen result is
# identical to "hard-cut"; the distinction lives in the recipe metadata for
# downstream tooling and editorial QA. speed-ramp's mechanic is on the
# *destination slot* (its `speed_factor` is > 1) — the cut between clips is a
# hard cut. Keeping these separate enum values lets the agent and the rubric
# reason about editorial intent without an animated effect.
_GEMINI_TO_INTERNAL: dict[str, str] = {
    "hard-cut": "none",
    "match-cut": "none",  # identical to hard-cut visually; distinction is editorial
    "whip-pan": "wipe_left",
    "zoom-in": "crossfade",
    "dissolve": "crossfade",
    "curtain-close": "none",  # handled as interstitial, not xfade
    "speed-ramp": "none",  # mechanic lives on dest slot's speed_factor, not the cut
    "none": "none",
}

DEFAULT_TRANSITION_DURATION_S = 0.3


def translate_transition(gemini_type: str) -> str:
    """Translate a Gemini transition_in value to an internal transition type.

    Returns a key that exists in _XFADE_MAP or "none".
    """
    return _GEMINI_TO_INTERNAL.get(gemini_type, "none")


class TransitionError(Exception):
    pass


def join_with_transitions(
    slot_paths: list[str],
    transitions: list[str],
    slot_durations: list[float],
    output_path: str,
) -> None:
    """Join slot video files with xfade transitions. Video-only output (-an).

    Only handles real visual transitions. "none" must be filtered out by the
    caller — chaining many xfade=fade:duration=0.001 filters caused FFmpeg
    to drop frames and truncate long outputs to ~3-4s (verified against the
    17-slot Dimples Passport recipe). The orchestrator now groups runs of
    "none" transitions via the concat demuxer and only invokes this function
    for visual boundaries.

    Args:
        slot_paths: Paths to rendered slot .mp4 files.
        transitions: Transition type per boundary (len = len(slot_paths) - 1).
                     Must be one of: "crossfade", "fade_black", "wipe_left", "wipe_right".
                     "none" is rejected.
        slot_durations: Visual output duration per slot in seconds (post speed-ramp).
        output_path: Where to write the joined output.

    Raises:
        TransitionError: If FFmpeg fails.
        ValueError: If inputs are invalid or any transition is "none".
    """
    if len(slot_paths) < 2:
        raise ValueError("Need at least 2 slots for transitions")
    if len(transitions) != len(slot_paths) - 1:
        raise ValueError(
            f"Expected {len(slot_paths) - 1} transitions, got {len(transitions)}"
        )
    if len(slot_durations) != len(slot_paths):
        raise ValueError(
            f"Expected {len(slot_paths)} durations, got {len(slot_durations)}"
        )
    if any(t == "none" for t in transitions):
        raise ValueError(
            "join_with_transitions does not handle 'none' — caller must "
            "group consecutive same-clip slots with concat and only pass "
            "visual transitions here. See _join_or_concat for the grouping."
        )

    # Record the picked transition sequence so the admin debug view can
    # show "which xfade ran at each slot boundary?" without parsing logs.
    # No-op when no job context is bound.
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    record_pipeline_event(
        stage="transition",
        event="xfade_chain_picked",
        data={
            "slot_count": len(slot_paths),
            "transitions": list(transitions),
            "slot_durations_s": [round(d, 3) for d in slot_durations],
        },
    )

    cmd = ["ffmpeg"]
    for path in slot_paths:
        cmd.extend(["-i", path])

    filter_complex = _build_xfade_filter(transitions, slot_durations)

    # The final output label is [v{N-1}] where N = len(slot_paths)
    final_label = f"[v{len(slot_paths) - 1}]"

    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", final_label,
        "-an",  # video-only — template audio mixed separately
        "-c:v", "libx264",
        "-profile:v", "high",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",  # QuickTime/browser compatibility
        "-r", "30",
        "-movflags", "+faststart",
        "-y",
        output_path,
    ])

    log.info(
        "transition_join_start",
        slots=len(slot_paths),
        active_transitions=[t for t in transitions if t != "none"],
    )

    result = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[:500]
        raise TransitionError(f"xfade join failed (rc={result.returncode}): {stderr}")

    log.info("transition_join_done", output=output_path)


def _build_xfade_filter(
    transitions: list[str],
    slot_durations: list[float],
) -> str:
    """Build the filter_complex string for chained xfade transitions.

    Caller guarantees every transition is a real visual effect (no "none"),
    so every boundary uses a proper duration. xfade offset for boundary i:
      offset_i = (sum of slot durations 0..i+1) - (sum of transition durations 0..i) - trans_dur_i
    """
    parts: list[str] = []
    cumulative_dur = slot_durations[0]
    cumulative_trans = 0.0

    for i, trans_type in enumerate(transitions):
        # Clamp transition duration to 30% of the shorter adjacent slot.
        max_dur = min(slot_durations[i], slot_durations[i + 1]) * 0.3
        trans_dur = min(DEFAULT_TRANSITION_DURATION_S, max_dur)

        offset = max(0.0, cumulative_dur - cumulative_trans - trans_dur)

        in_label = "[0:v]" if i == 0 else f"[v{i}]"
        next_input = f"[{i + 1}:v]"
        out_label = f"[v{i + 1}]"

        xfade_type = _XFADE_MAP.get(trans_type, "fade")
        parts.append(
            f"{in_label}{next_input}xfade=transition={xfade_type}"
            f":duration={trans_dur:.3f}:offset={offset:.3f}{out_label}"
        )

        cumulative_dur += slot_durations[i + 1]
        cumulative_trans += trans_dur

    return ";".join(parts)
