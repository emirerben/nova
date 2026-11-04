"""Small shared pieces for the kind modules: witnesses, action builders, the legacy adapter."""

from __future__ import annotations

from collections.abc import Sequence

from app.services.suggestions.context import SuggestionContext, Target, snapshot_digest
from app.services.suggestions.models import (
    Action,
    BuildAction,
    ChipKindSpec,
    EditorOpsAction,
    SuggestionKind,
    WitnessResult,
)

_MAX_OPS = 8


def unavailable(ctx: SuggestionContext) -> bool:
    """A registered stub: the kind exists so its lane edits one module, but it never offers."""
    return False


def no_action(ctx: SuggestionContext, targets: Sequence[Target], copy: str | None) -> Action | None:
    """No v2 action yet: a kind with this build step never reaches a client."""
    return None


def stub_witness(action: Action, ctx: SuggestionContext) -> WitnessResult:
    return WitnessResult(ok=False, tier="none", reason="not_implemented")


def editor_ops_witness(action: Action, ctx: SuggestionContext) -> WitnessResult:
    """Prove every op through the copilot parser and the draft compiler (``ctx.editor_witness``).

    The witness runs on the same editor snapshot the action was built from; a stale base is the
    tap path's concern, not this check's.
    """
    if not isinstance(action, EditorOpsAction):
        return WitnessResult(ok=False, tier="editor", reason="not_editor_ops")
    if ctx.editor_witness is None:
        return WitnessResult(ok=False, tier="editor", reason="no_witness")
    if action.base_digest != snapshot_digest(ctx.snapshot):
        return WitnessResult(ok=False, tier="editor", reason="stale_base")
    touched: set[str] = set()
    for op in action.ops:
        proven = ctx.editor_witness(dict(op))
        if proven is None:
            return WitnessResult(ok=False, tier="editor", reason="op_refused")
        touched |= proven
    return WitnessResult(ok=True, tier="editor", touched=frozenset(touched))


def editor_op_action(spec: ChipKindSpec) -> BuildAction:
    """Build ``EditorOps`` from the engine row's own ``op``: the exact op its dry run proved."""

    def build(ctx: SuggestionContext, targets: Sequence[Target], copy: str | None) -> Action | None:
        base = snapshot_digest(ctx.snapshot)
        if base is None or spec.op is None or not targets:
            return None
        ops = []
        for target in targets[:_MAX_OPS]:
            op = spec.op(ctx, target)
            if op is None:
                return None
            ops.append(op)
        return EditorOpsAction(ops=ops, base_digest=base)

    return build


def legacy_kind(
    spec: ChipKindSpec,
    *,
    surfaces: frozenset[str],
    build_action: BuildAction = no_action,
    witness=stub_witness,
    flag: str | None = None,
    required_signals: tuple[str, ...] = (),
) -> SuggestionKind:
    """Register a KRI-756 engine row as a registry kind, without changing what it does."""
    return SuggestionKind(
        kind=str(spec.kind),
        states=spec.states,
        required_signals=required_signals,
        availability=spec.available,
        already_present=spec.already_present,
        build_action=build_action,
        witness=witness,
        surfaces=surfaces,
        flag=flag,
        min_protocol=spec.min_protocol,
        legacy=spec,
    )


PHONE_AND_CLOUD = frozenset({"phone", "cloud"})
PHONE = frozenset({"phone"})
