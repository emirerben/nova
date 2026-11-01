"""The creation formats a creator can pick in the chat's "What are we making?" picker.

One row per picker card: the id the client sends (``select_format``), the render
``edit_format`` it stores on the thread, and what the card changes about the user-song
mode. A card never adds a render path. ``lipsync`` is a montage whose only effect is the
DEFAULT ``song_sync`` of an attached song, and the creator's words still win over it
(KRI-129), so every montage path keeps working for it unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SongSyncDefault = Literal["lipsync"]


@dataclass(frozen=True)
class CreationFormat:
    edit_format: str
    # What an attached song does when the creator's words do not say. ``None`` = the card
    # changes nothing (a plain montage keeps the song as background music).
    song_sync_default: SongSyncDefault | None = None
    # Offered only where an uploaded song can be used (``phone_user_song_supported``).
    needs_song_lane: bool = False


CREATION_FORMATS: dict[str, CreationFormat] = {
    "montage": CreationFormat("montage"),
    "lipsync": CreationFormat("montage", song_sync_default="lipsync", needs_song_lane=True),
    "narrated": CreationFormat("narrated_planned"),
    "talking_to_camera": CreationFormat("subtitled"),
    "slides": CreationFormat("slides"),
}

# Picker ids that carry a song default: the manifest's `chosen_format` hint values.
SONG_SYNC_DEFAULT_BY_FORMAT: dict[str, SongSyncDefault] = {
    key: fmt.song_sync_default
    for key, fmt in CREATION_FORMATS.items()
    if fmt.song_sync_default is not None
}
