"""Body-slot audio layout constants.

Stream-copy concat in `template_orchestrate._concat_demuxer` is only safe when
every input file has identical audio layout (codec, sample rate, channel
count, bitrate). Drift in any of those produces a silent truncation of the
final output. The constants below are the contract: every file destined for
that concat must encode audio with these exact parameters.

Touched-by callers (keep in sync — drift here resurrects the very bug this
fixes):
  - reframe.py: `_encoding_args`, `_SILENT_AUDIO_INPUT`
  - interstitials.py: `render_color_hold`, `apply_curtain_close_tail`
"""

from __future__ import annotations

# AAC stereo at 44.1 kHz, 192 kbps. Source clips arrive at varying rates
# (44.1k iPhone, 48k Android, 22k web exports) and channel counts (mono voice
# memo, stereo, 5.1 phone audio). All get resampled to this layout at reframe
# time so downstream concat can stream-copy.
BODY_SLOT_AUDIO_CODEC = "aac"
BODY_SLOT_AUDIO_BITRATE = "192k"
BODY_SLOT_AUDIO_SAMPLE_RATE = "44100"
BODY_SLOT_AUDIO_CHANNELS = "2"

# Output args for re-encoding to body-slot layout. Used everywhere a video
# output needs concat-copy-compatible audio.
BODY_SLOT_AUDIO_OUT_ARGS: list[str] = [
    "-c:a", BODY_SLOT_AUDIO_CODEC,
    "-b:a", BODY_SLOT_AUDIO_BITRATE,
    "-ar", BODY_SLOT_AUDIO_SAMPLE_RATE,
    "-ac", BODY_SLOT_AUDIO_CHANNELS,
]

# lavfi anullsrc input matching the body-slot layout. Use as additional
# `-i ...` args when the source has no audio track but the output must.
SILENT_AUDIO_INPUT_ARGS: list[str] = [
    "-f", "lavfi",
    "-i", (
        f"anullsrc=channel_layout=stereo"
        f":sample_rate={BODY_SLOT_AUDIO_SAMPLE_RATE}"
    ),
]
