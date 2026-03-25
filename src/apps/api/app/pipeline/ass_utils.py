"""Shared ASS (Advanced SubStation Alpha) utilities.

Used by captions.py (speech captions) and text_overlay.py (editorial overlays).
Centralizes time formatting and text sanitization to stay DRY.
"""

import re


def sanitize_ass_text(text: str) -> str:
    """Escape ASS special chars to prevent override tag injection.

    Strips backslashes, {...} override blocks, orphan braces.
    Converts newlines to ASS line break (\\N).
    """
    # Strip backslashes first (prevents crafted escape sequences)
    text = text.replace("\\", "")
    # Remove override tag blocks like {\\b1} or {\\pos(320,50)}
    text = re.sub(r"\{[^}]*\}", "", text)
    # Remove orphan braces
    text = text.replace("{", "").replace("}", "")
    # Convert newlines to ASS line breaks
    text = text.replace("\n", "\\N")
    return text.strip()


def format_ass_time(seconds: float) -> str:
    """Format seconds as ASS time: H:MM:SS.cc (centiseconds)."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
