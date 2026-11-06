"""``title_animation``: "Animate the title" (an entrance animation on the existing title)."""

from __future__ import annotations

from collections.abc import Mapping

from app.services.suggestions.brief_asks import TITLE_ANIMATION, spoke
from app.services.suggestions.builders import PHONE_AND_CLOUD, legacy_kind
from app.services.suggestions.context import (
    SuggestionContext,
    Target,
    editable_title_bar,
    families,
)
from app.services.suggestions.models import ChipKind, ChipKindSpec

_ARCHETYPES = frozenset({"guided_story", "unified_montage"})


def _available(ctx: SuggestionContext) -> bool:
    return (
        ctx.state == "ready"
        and not spoke(ctx, TITLE_ANIMATION)  # KRI-129
        and ctx.resolved_archetype in _ARCHETYPES
        and "text" in families(ctx)  # the op is patch_text; the parser refuses it without it
        and editable_title_bar(ctx) is not None
    )


def _present(ctx: SuggestionContext) -> bool:
    found = editable_title_bar(ctx)
    if found is None:
        return True
    phases = found[1].get("animation_phases")
    entrance = phases.get("entrance") if isinstance(phases, Mapping) else None
    return entrance not in (None, "", "none")


def _ground(ctx: SuggestionContext) -> list[Target]:
    found = editable_title_bar(ctx)
    if found is None:
        return []
    index, bar = found
    return [Target(key=f"bar:{bar.get('id') or index}", text_bar_index=index)]


SPEC = ChipKindSpec(
    kind=ChipKind.TITLE_ANIMATION,
    states=frozenset({"ready"}),
    capability={"ready": "family:text"},
    available=_available,
    already_present=_present,
    ground=_ground,
    # The compiler cannot prove an entrance animation, so this kind is allow-listed:
    # guided_story / unified_montage only, a title bar present, text family allowed.
    dry_run={"ready": "allowlist"},
    expects="an entrance animation on the existing title",
    claimed_op="patch_text",
    edit_tag="title_animation",
)

KIND = legacy_kind(SPEC, surfaces=PHONE_AND_CLOUD)
