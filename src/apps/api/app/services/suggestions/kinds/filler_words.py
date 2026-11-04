"""``filler_words``: switch on filler-word cleanup for a talking edit.

Stub. Planned action: an ``ItemSettingAction`` (``speech_cleanup_enabled``) followed by a
re-render, only where ``capability_for_item`` allows cleanup. The count and seconds saved come
from ``pipeline/silence_cut.py`` and ``speech_cleanup_preflight``; there is no new detector.
"""

from __future__ import annotations

from app.services.suggestions.builders import PHONE, no_action, stub_witness, unavailable
from app.services.suggestions.models import SuggestionKind

KIND = SuggestionKind(
    kind="filler_words",
    states=frozenset({"ready"}),
    required_signals=(),
    availability=unavailable,
    already_present=unavailable,
    build_action=no_action,
    witness=stub_witness,
    surfaces=PHONE,
    flag="kria_suggestion_filler_words_enabled",
)
