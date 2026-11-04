"""``mute_clip``: mute a clip whose own sound is wind or crowd noise.

Stub: registered so the lane that owns it edits only this module. Planned action: a
``BriefUpdateAction`` setting ``clip_sound: off`` scoped ``clip:<media_id>``
(``kria/clip_sound_toggle_ask.py``); no new editor op and no protocol bump.
"""

from __future__ import annotations

from app.services.suggestions.builders import PHONE, no_action, stub_witness, unavailable
from app.services.suggestions.models import SuggestionKind

KIND = SuggestionKind(
    kind="mute_clip",
    states=frozenset({"ready"}),
    required_signals=("audio_quality",),
    availability=unavailable,
    already_present=unavailable,
    build_action=no_action,
    witness=stub_witness,
    surfaces=PHONE,
    flag="kria_suggestion_mute_clip_enabled",
)
