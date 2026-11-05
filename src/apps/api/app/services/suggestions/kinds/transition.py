"""``transition``: "Add transitions between the clips"."""

from __future__ import annotations

from typing import Any

from app.services.suggestions.brief_asks import TRANSITION, spoke
from app.services.suggestions.builders import (
    PHONE_AND_CLOUD,
    editor_op_action,
    editor_ops_witness,
    legacy_kind,
)
from app.services.suggestions.context import (
    SuggestionContext,
    Target,
    cap_ok,
    families,
    ground_none,
    live_clips,
    slots,
)
from app.services.suggestions.models import ChipKind, ChipKindSpec


def _available(ctx: SuggestionContext) -> bool:
    # KRI-129: a creator who already said anything about transitions is never second-guessed.
    return not spoke(ctx, TRANSITION) and _offerable(ctx)


def _offerable(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return cap_ok(ctx, "transitions") and len(live_clips(ctx)) >= 2
    return "transition" in families(ctx) and len(slots(ctx)) >= 2


def _present(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return False
    return any(str(s.get("transition_after") or "cut") != "cut" for s in slots(ctx)[:-1])


def _op(ctx: SuggestionContext, target: Target) -> dict[str, Any] | None:
    return {"op": "set_transition", "boundary_index": 0, "transition": "crossfade"}


SPEC = ChipKindSpec(
    kind=ChipKind.TRANSITION,
    states=frozenset({"first_prompt", "ready"}),
    capability={"first_prompt": "transitions", "ready": "family:transition"},
    available=_available,
    already_present=_present,
    ground=ground_none,
    dry_run={"first_prompt": "strategy", "ready": "editor"},
    expects="transitions on the clip boundaries",
    op=_op,
    claimed_op="set_transition",
    witness_op="set_transition",
    edit_tag="transitions",
)

KIND = legacy_kind(
    SPEC,
    surfaces=PHONE_AND_CLOUD,
    build_action=editor_op_action(SPEC),
    witness=editor_ops_witness,
)
