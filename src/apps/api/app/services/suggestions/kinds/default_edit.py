"""``default_edit``: "Let Kria cut the first version" (first_prompt only, always chip 1)."""

from __future__ import annotations

from app.services.suggestions.builders import PHONE_AND_CLOUD, legacy_kind
from app.services.suggestions.context import can_draft, ground_none
from app.services.suggestions.models import ChipKind, ChipKindSpec

SPEC = ChipKindSpec(
    kind=ChipKind.DEFAULT_EDIT,
    states=frozenset({"first_prompt"}),
    capability={"first_prompt": "dispatch_render"},
    available=can_draft,
    already_present=lambda ctx: False,
    ground=ground_none,
    dry_run={"first_prompt": "none"},
    expects="a first draft built by the no-prompt Main Creator path",
)

KIND = legacy_kind(SPEC, surfaces=PHONE_AND_CLOUD)
