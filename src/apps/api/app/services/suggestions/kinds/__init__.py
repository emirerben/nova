"""The explicit list of suggestion kinds. A new kind is one module here plus one line below.

Order is part of the contract: the first nine are the KRI-756 chip kinds in the exact order the
engine's ``CHIP_KIND_GATE`` always iterated them (ties in ranking follow it).
"""

from __future__ import annotations

from app.services.suggestions.kinds import (
    captions,
    default_edit,
    duplicates,
    filler_words,
    generated_overlay,
    hook,
    music_level,
    mute_clip,
    opener_clip,
    pinned_text,
    sfx,
    title_animation,
    transition,
)

KIND_MODULES = (
    default_edit,
    hook,
    opener_clip,
    captions,
    sfx,
    music_level,
    transition,
    title_animation,
    pinned_text,
    mute_clip,
    filler_words,
    duplicates,
    generated_overlay,
)
