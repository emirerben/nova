"""``captions``: "Add captions" (speech without captions)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services.suggestions.brief_asks import CAPTIONS, spoke
from app.services.suggestions.builders import (
    PHONE_AND_CLOUD,
    editor_op_action,
    editor_ops_witness,
    legacy_kind,
)
from app.services.suggestions.context import (
    NO_TARGET,
    SuggestionContext,
    Target,
    any_speech,
    families,
)
from app.services.suggestions.models import ChipKind, ChipKindSpec


# Formats that always caption what is said (strategy_policy): a captions chip would be a no-op.
def _always_captioned(ctx: SuggestionContext) -> bool:
    fmt = str(ctx.edit_format or "")
    return fmt == "subtitled" or fmt.startswith("narrated")


def _available(ctx: SuggestionContext) -> bool:
    # KRI-129: a creator who already said anything about captions is never second-guessed.
    return not spoke(ctx, CAPTIONS) and _offerable(ctx)


def _offerable(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        # Gated on the footage (speech / talking to camera) and the edit format. The manifest
        # `caption_style` capability only mirrors main_creator_agent_execution_enabled and
        # nothing enforces it, so it is deliberately NOT consulted here.
        return any_speech(ctx)
    snap = ctx.snapshot or {}
    captions = snap.get("captions")
    return "caption" in families(ctx) and isinstance(captions, Mapping)


def _present(ctx: SuggestionContext) -> bool:
    if ctx.state == "first_prompt":
        return _always_captioned(ctx)
    captions = (ctx.snapshot or {}).get("captions")
    if not isinstance(captions, Mapping):
        return True
    meta = captions.get("meta") if isinstance(captions.get("meta"), Mapping) else {}
    # Only a switched-off caption lane with cues to show is something Kria can turn on.
    return meta.get("enabled") is not False or not captions.get("cues")


def _ground(ctx: SuggestionContext) -> list[Target]:
    if ctx.state == "first_prompt":
        return [NO_TARGET] if any_speech(ctx) else []
    return [NO_TARGET]


def _op(ctx: SuggestionContext, target: Target) -> dict[str, Any] | None:
    return {"op": "set_caption_meta", "patch": {"enabled": True}}


SPEC = ChipKindSpec(
    kind=ChipKind.CAPTIONS,
    states=frozenset({"first_prompt", "ready"}),
    capability={"first_prompt": "footage:speech", "ready": "family:caption"},
    available=_available,
    already_present=_present,
    ground=_ground,
    dry_run={"first_prompt": "strategy", "ready": "editor"},
    expects="captions burned on the speech",
    op=_op,
    claimed_op="set_caption_meta",
    witness_op="set_caption_meta",
    edit_tag="captions",
)

KIND = legacy_kind(
    SPEC,
    surfaces=PHONE_AND_CLOUD,
    build_action=editor_op_action(SPEC),
    witness=editor_ops_witness,
)
