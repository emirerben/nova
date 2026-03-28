"""FFmpeg xfade transition joining for template-mode slot assembly.

Builds a filter_complex chain that applies crossfade/fade/wipe transitions
between pre-rendered slot video files. Outputs video-only (-an) because
template audio is mixed separately via _mix_template_audio().

CRITICAL: Never use shell=True. Always pass args as a list.
"""

import subprocess

import structlog

log = structlog.get_logger()

# Transition type → FFmpeg xfade `transition` parameter
_XFADE_MAP: dict[str, str] = {
    "crossfade": "fade",
    "fade_black": "fadeblack",
    "wipe_left": "wipeleft",
    "wipe_right": "wiperight",
}

# Gemini vocabulary → internal transition type.
# Gemini uses video-editor-friendly names; this maps them to _XFADE_MAP keys
# or special values handled by the pipeline (e.g. "curtain-close" → interstitial).
_GEMINI_TO_INTERNAL: dict[str, str] = {
    "hard-cut": "none",
    "whip-pan": "wipe_left",
    "zoom-in": "crossfade",
    "dissolve": "crossfade",
    "curtain-close": "none",  # handled as interstitial, not xfade
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

    Args:
        slot_paths: Paths to rendered slot .mp4 files.
        transitions: Transition type per boundary (len = len(slot_paths) - 1).
                     Values: "crossfade", "fade_black", "wipe_left", "wipe_right", "none".
        slot_durations: Visual output duration per slot in seconds (post speed-ramp).
        output_path: Where to write the joined output.

    Raises:
        TransitionError: If FFmpeg fails.
        ValueError: If inputs are invalid.
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
        "-crf", "23",
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

    xfade offset for boundary i:
      offset_i = (sum of slot durations 0..i+1) - (sum of transition durations 0..i) - trans_dur_i

    For "none" transitions, use a near-zero duration crossfade (0.001s) to keep
    the filter chain valid while producing a hard cut.
    """
    parts: list[str] = []
    cumulative_dur = slot_durations[0]
    cumulative_trans = 0.0

    for i, trans_type in enumerate(transitions):
        # Determine transition duration — clamp to 30% of either adjacent slot
        if trans_type == "none":
            trans_dur = 0.0
        else:
            max_dur = min(slot_durations[i], slot_durations[i + 1]) * 0.3
            trans_dur = min(DEFAULT_TRANSITION_DURATION_S, max_dur)

        # xfade offset = cumulative video so far minus overlaps minus this transition
        offset = cumulative_dur - cumulative_trans - trans_dur
        offset = max(0.0, offset)

        # Input/output labels
        in_label = "[0:v]" if i == 0 else f"[v{i}]"
        next_input = f"[{i + 1}:v]"
        out_label = f"[v{i + 1}]"

        xfade_type = _XFADE_MAP.get(trans_type, "fade")

        if trans_dur > 0:
            parts.append(
                f"{in_label}{next_input}xfade=transition={xfade_type}"
                f":duration={trans_dur:.3f}:offset={offset:.3f}{out_label}"
            )
        else:
            # Hard cut: near-zero duration crossfade
            parts.append(
                f"{in_label}{next_input}xfade=transition=fade"
                f":duration=0.001:offset={offset:.3f}{out_label}"
            )

        cumulative_dur += slot_durations[i + 1]
        cumulative_trans += trans_dur

    return ";".join(parts)
