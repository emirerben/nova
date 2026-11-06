"""``pinned_text``: dark. It needs the creator's own words, so it is never emitted."""

from __future__ import annotations

from app.services.suggestions.builders import PHONE_AND_CLOUD, legacy_kind, unavailable
from app.services.suggestions.models import ChipKind, ChipKindSpec

SPEC = ChipKindSpec(
    kind=ChipKind.PINNED_TEXT,
    states=frozenset(),  # dark: PINNED_TEXT_PROVEN is False
    capability={},
    available=unavailable,
    already_present=lambda ctx: False,
    ground=lambda ctx: [],
    dry_run={},
    expects="a corner line, through the creative-copy approval flow",
    edit_tag="pinned_text",
)

KIND = legacy_kind(SPEC, surfaces=PHONE_AND_CLOUD)
