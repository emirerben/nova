"""``generated_overlay``: a generated-image overlay, produced only on tap.

Stub (last phase). Planned action: ``AsyncGenerateAction`` -> ``execute_generated_media`` -> a new
``add_overlay`` compile handler. Talking formats first, montage behind
``PHONE_RENDER_VERIFIED_FEATURES``. The card shows the cost before the creator consents.
"""

from __future__ import annotations

from app.services.suggestions.builders import PHONE, no_action, stub_witness, unavailable
from app.services.suggestions.models import SuggestionKind

KIND = SuggestionKind(
    kind="generated_overlay",
    states=frozenset({"ready"}),
    required_signals=(),
    availability=unavailable,
    already_present=unavailable,
    build_action=no_action,
    witness=stub_witness,
    surfaces=PHONE,
    flag="kria_suggestion_generated_overlay_enabled",
)
