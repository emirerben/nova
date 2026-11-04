"""``hook``: an opening hook / title, routed into the creative-copy approval flow.

The KRI-756 chip carries no wording (its tap asks the planner for a hook). The v2 path builds a
``CopyDecisionAction`` with the exact text the creator approves by tapping; until that lands
(``build_action`` is a stub) this kind only ever offers the legacy chip.
"""

from __future__ import annotations

from typing import Any

from app.services.suggestions.builders import PHONE_AND_CLOUD, legacy_kind
from app.services.suggestions.context import (
    SuggestionContext,
    Target,
    can_draft,
    families,
    ground_none,
    title_bar,
)
from app.services.suggestions.models import ChipKind, ChipKindSpec


def _available(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return can_draft(ctx)
    return "title" in families(ctx)


def _present(ctx: SuggestionContext) -> bool:
    # first_prompt: nothing is in the edit yet. ready: any title bar already says something.
    return ctx.state == "ready" and title_bar(ctx) is not None


def _op(ctx: SuggestionContext, target: Target) -> dict[str, Any] | None:
    # A PROBE: proves a title CAN be set on this edit. The chip itself routes into the
    # creative-copy flow and carries no wording; set_title is in CHIP_OP_WITNESS_PROBES.
    return {"op": "set_title", "title": "Hook"}


SPEC = ChipKindSpec(
    kind=ChipKind.HOOK,
    states=frozenset({"first_prompt", "ready"}),
    capability={"first_prompt": "dispatch_render", "ready": "family:title"},
    available=_available,
    already_present=_present,
    ground=ground_none,
    dry_run={"first_prompt": "strategy", "ready": "editor"},
    expects="an opening hook, through the creative-copy approval flow",
    op=_op,
    witness_op="set_title",
    edit_tag="title",
)

KIND = legacy_kind(SPEC, surfaces=PHONE_AND_CLOUD, flag="kria_suggestion_hook_copy_enabled")
