"""ASS subtitle format utilities shared between captions and text overlays."""

import re


def format_ass_time(seconds: float) -> str:
    """Format seconds as ASS time: H:MM:SS.cc"""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def sanitize_ass_text(text: str) -> str:
    """Remove ASS override blocks and special chars that break rendering.

    Strips {...} override blocks, replaces literal backslashes and braces,
    converts newlines to ASS line breaks.
    """
    # Remove ASS override blocks like {\b1} or {\fad(500,0)}
    text = re.sub(r"\{[^}]*\}", "", text)
    # Strip stray braces
    text = text.replace("{", "").replace("}", "")
    # Convert newlines to ASS newline
    text = text.replace("\n", "\\N")
    return text.strip()
