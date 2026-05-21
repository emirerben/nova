"""Escaping helpers for FFmpeg filter graph path values."""


def escape_ffmpeg_filter_path(p: str) -> str:
    """Escape a filesystem path for use inside an FFmpeg filter value."""
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")
