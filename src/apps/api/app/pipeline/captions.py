"""Generate ASS subtitle file from Whisper word timestamps.

ASS (Advanced SubStation Alpha) supports word-level highlight styling
which produces the "karaoke" caption effect common on TikTok/Reels.
"""

from app.pipeline.ass_utils import format_ass_time, sanitize_ass_text
from app.pipeline.transcribe import Transcript, Word


def generate_ass(
    transcript: Transcript,
    start_s: float,
    end_s: float,
    output_path: str,
) -> None:
    """Write an ASS subtitle file for the clip window [start_s, end_s].

    Words are shifted so clip-start = time 0.
    Highlight color: yellow (#FFFF00). Base color: white.
    """
    words = [w for w in transcript.words if start_s <= w.start_s < end_s]
    if not words:
        _write_empty_ass(output_path)
        return

    lines = _build_dialogue_lines(words, offset_s=start_s)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(_ASS_HEADER)
        f.write("\n[Events]\n")
        f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        for line in lines:
            f.write(line + "\n")


def _build_dialogue_lines(words: list[Word], offset_s: float) -> list[str]:
    """Group words into ~5-word chunks; within each chunk highlight word-by-word."""
    CHUNK_SIZE = 5
    dialogue_lines = []

    for i in range(0, len(words), CHUNK_SIZE):
        chunk = words[i : i + CHUNK_SIZE]
        chunk_start = chunk[0].start_s - offset_s
        chunk_end = chunk[-1].end_s - offset_s

        # Build ASS karaoke tags: {\k<centiseconds>}word
        text_parts = []
        for j, word in enumerate(chunk):
            dur_cs = int((word.end_s - word.start_s) * 100)
            clean = sanitize_ass_text(word.text.strip())
            if j == 0:
                text_parts.append(f"{{\\k{dur_cs}}}{clean}")
            else:
                text_parts.append(f" {{\\k{dur_cs}}}{clean}")

        text = "".join(text_parts)
        start_str = format_ass_time(max(0.0, chunk_start))
        end_str = format_ass_time(max(0.0, chunk_end))

        dialogue_lines.append(
            f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{text}"
        )

    return dialogue_lines


def _write_empty_ass(output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(_ASS_HEADER)
        f.write("\n[Events]\n")
        f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


_ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding  # noqa: E501
Style: Default,Arial,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,50,50,120,1  # noqa: E501
"""
