"""``duplicates``: keep the better of two near-identical clips.

Stub. Planned action: ``EditorOpsAction`` (``remove_clip`` / ``remove_visual_media``) once there
is a render; before one the card is prompt-only. Pairs are computed when the set is served from
the ``near_dup_hash`` signal.
"""

from __future__ import annotations

from app.services.suggestions.builders import PHONE_AND_CLOUD, no_action, stub_witness, unavailable
from app.services.suggestions.models import SuggestionKind

KIND = SuggestionKind(
    kind="duplicates",
    states=frozenset({"first_prompt", "ready"}),
    required_signals=("near_dup_hash",),
    availability=unavailable,
    already_present=unavailable,
    build_action=no_action,
    witness=stub_witness,
    surfaces=PHONE_AND_CLOUD,
    flag="kria_suggestion_duplicates_enabled",
)
