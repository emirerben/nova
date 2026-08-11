"""Monotonic lifecycle transitions shared by TikTok routes and workers."""

from __future__ import annotations

_TERMINAL_VISIBILITIES = {"private", "public", "removed"}


def visibility_after_draft_inbox(
    current_visibility: str,
    current_processing_status: str,
) -> str:
    """Record inbox delivery without downgrading a later lifecycle event."""
    if current_visibility in _TERMINAL_VISIBILITIES:
        return current_visibility
    if current_visibility == "unknown" and current_processing_status == "complete":
        # A complete upload with unknown audience means the creator already
        # posted inside TikTok; an older inbox event must not turn it into a draft.
        return current_visibility
    return "draft"


def visibility_after_draft_post(current_visibility: str) -> str:
    """Record in-app posting while preserving known terminal visibility."""
    if current_visibility in _TERMINAL_VISIBILITIES:
        return current_visibility
    return "unknown"
