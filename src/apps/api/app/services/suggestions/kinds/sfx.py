"""``sfx``: "Add sound effects"."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
)
from app.services.suggestions.models import ChipKind, ChipKindSpec


def _available(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return cap_ok(ctx, "sound_effects")
    sfx = (ctx.snapshot or {}).get("sfx")
    return (
        "sfx" in families(ctx)
        and isinstance(sfx, Mapping)
        and bool(sfx.get("catalog"))  # empty catalog: add_sfx always fails closed
    )


def _present(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return False
    sfx = (ctx.snapshot or {}).get("sfx")
    return bool(isinstance(sfx, Mapping) and sfx.get("placements"))


def _op(ctx: SuggestionContext, target: Target) -> dict[str, Any] | None:
    catalog = ((ctx.snapshot or {}).get("sfx") or {}).get("catalog") or []
    first = next((r for r in catalog if isinstance(r, Mapping) and r.get("id")), None)
    if first is None:
        return None
    total = (ctx.snapshot or {}).get("total_duration_s")
    at_s = 0.5 if not isinstance(total, (int, float)) or total <= 1.0 else min(0.5, total / 2)
    return {"op": "add_sfx", "effect_id": first["id"], "at_s": round(float(at_s), 2)}


SPEC = ChipKindSpec(
    kind=ChipKind.SFX,
    states=frozenset({"first_prompt", "ready"}),
    capability={"first_prompt": "sound_effects", "ready": "family:sfx"},
    available=_available,
    already_present=_present,
    ground=ground_none,
    dry_run={"first_prompt": "strategy", "ready": "editor"},
    expects="sound-effect placements on the timeline",
    op=_op,
    claimed_op="add_sfx",
    witness_op="add_sfx",
    edit_tag="sfx",
)

KIND = legacy_kind(
    SPEC,
    surfaces=PHONE_AND_CLOUD,
    build_action=editor_op_action(SPEC),
    witness=editor_ops_witness,
)
