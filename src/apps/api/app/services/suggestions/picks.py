"""From what the suggestion agent picked to what a phone is served.

**The agent picks, the server builds.** An agent answer is ``{kind, target_ids, why, copy, rank}``
per card (``SuggestionAgentOutput``). This module is the only place a pick becomes a card:

1. ``issue_kinds`` decides which registry kinds exist for this context at all (the engine's
   proven candidates plus the registry kinds whose own availability says yes). The agent's
   schema is closed over that list.
2. ``build_suggestions`` takes a pick (or, with no agent, the deterministic order), resolves its
   target handles to targets the server owns, builds the ``Action`` through the kind's
   ``build_action`` and proves it with the kind's ``witness``. A pick whose action cannot be
   built or proven is dropped silently; it never reaches a client. Nothing the model wrote is
   ever an op, a message or a label.

So for ARBITRARY agent output the served cards are a subset of registry candidates whose
actions pass their witnesses (G10, ``tests/evals/test_suggestion_agent_evals.py``).

Pure and I/O free. Nothing here may import ``suggestion_chips``; the engine hands in the pool and
the copy callbacks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import ValidationError

from app.services.suggestion_chip_copy import (
    MAX_CARD_WHY_CHARS,
    footage_vocabulary,
    shares_run,
    validate_copy,
    validate_why,
)
from app.services.suggestions import registry
from app.services.suggestions.context import (
    NO_TARGET,
    SuggestionContext,
    Target,
    live_clips,
)
from app.services.suggestions.models import (
    MAX_SUGGESTIONS,
    WHOLE_VIDEO_HANDLE,
    Action,
    ChipAnchorOut,
    CopyDecisionAction,
    Suggestion,
    SuggestionKind,
    suggestion_id,
)

log = structlog.get_logger()

# Kinds whose card carries exact on-screen text the agent writes (validated before a builder
# ever sees it). One per set, like the KRI-756 copy-bearing rule.
COPY_KINDS = frozenset({"hook", "pinned_text"})

# Deterministic order for kinds the KRI-756 engine never scored. Impact first: bad audio and
# filler come before polish, a generated image is the last thing to offer.
NEW_KIND_SCORE: dict[str, int] = {
    "mute_clip": 95,
    "filler_words": 90,
    "duplicates": 75,
    "generated_overlay": 20,
}
_DEFAULT_NEW_SCORE = 10

# What a copy callback returns: the card title, the chat prompt a tap sends, evidence lines.
CardCopy = tuple[str, str, list[str]]
CopyFor = Callable[[str, Sequence[Target], "str | None"], "CardCopy | None"]


@dataclass(frozen=True)
class PoolRow:
    """A KRI-756 candidate the engine already proved (its target, what it touches, its score)."""

    kind: str
    target: Target
    touched: frozenset[str]
    score: int
    gap_tags: tuple[str, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Issued:
    """A kind the agent may pick for this context."""

    kind: SuggestionKind
    score: int
    # The engine's proven target for a KRI-756 kind; None for a registry-only kind.
    target: Target | None = None
    gap_tags: tuple[str, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Pick:
    """One card to build, from the agent or from the deterministic order."""

    kind: str
    target_ids: tuple[str, ...] = ()
    why: str = ""
    copy: str | None = None


def clip_handles(ctx: SuggestionContext) -> dict[str, Target]:
    """``c<ordinal>`` -> the clip's target. The only target names the agent can type."""
    out: dict[str, Target] = {}
    for clip in live_clips(ctx):
        ordinal, media_id = clip.get("ordinal"), clip.get("media_id")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool) and isinstance(media_id, str):
            out[f"c{ordinal}"] = Target(
                key=f"clip:{media_id}", media_id=media_id, clip_ordinal=ordinal
            )
    return out


def issue_kinds(
    ctx: SuggestionContext, state: str, pool: Iterable[PoolRow] = ()
) -> tuple[Issued, ...]:
    """The kinds that may exist for ``ctx`` right now, best deterministic rank first.

    KRI-756 kinds come from the engine's proven pool; every other registry kind is issued only
    when it is switched on for the phone (its own flag, and the signals flag when it reads any),
    belongs to this state, fits the client protocol, and its own ``availability`` says yes and
    ``already_present`` says no. A registered stub never offers, so it is never issued.
    """
    issued: list[Issued] = []
    seen: set[str] = set()
    for row in pool:
        kind = registry.get(row.kind)
        if kind is None or kind.kind in seen:
            continue
        seen.add(kind.kind)
        issued.append(Issued(kind, row.score, row.target, row.gap_tags, row.facts))
    for kind in registry.kinds_for(state, "phone"):
        if kind.kind in seen or kind.legacy is not None:
            continue
        if (
            kind.min_protocol is not None
            and ctx.client_protocol is not None
            and ctx.client_protocol < kind.min_protocol
        ):
            continue
        try:
            if not kind.availability(ctx) or kind.already_present(ctx):
                continue
        except Exception:  # noqa: BLE001 - one kind failing never takes the set down
            log.warning("suggestion_blocks.kind_error", kind=kind.kind, exc_info=True)
            continue
        seen.add(kind.kind)
        issued.append(Issued(kind, NEW_KIND_SCORE.get(kind.kind, _DEFAULT_NEW_SCORE)))
    issued.sort(key=lambda row: -row.score)
    return tuple(issued)


def _targets_for(
    issued: Issued, target_ids: Sequence[str], handles: Mapping[str, Target]
) -> list[Target]:
    if issued.target is not None:
        # An engine kind is grounded by the engine (one proven target); the agent's handles
        # cannot re-point it.
        return [issued.target]
    resolved = [handles[h] for h in target_ids if h != WHOLE_VIDEO_HANDLE and h in handles]
    return resolved or [NO_TARGET]


def _clean_why(
    why: str,
    *,
    kind: str,
    language: str,
    vocabulary: frozenset[str],
    transcripts: Sequence[str],
) -> str:
    if not why:
        return ""
    checked, _reason = validate_why(
        why, language=language, vocabulary=vocabulary, kind=kind, max_chars=MAX_CARD_WHY_CHARS
    )
    if checked is None or shares_run(checked, transcripts):
        return ""
    return checked


def _anchor(target: Target) -> ChipAnchorOut:
    return ChipAnchorOut(
        media_id=target.media_id,
        clip_ordinal=target.clip_ordinal,
        text_bar_index=target.text_bar_index,
    )


def build_suggestions(
    ctx: SuggestionContext,
    issued: Sequence[Issued],
    picks: Sequence[Pick],
    *,
    base_digest: str,
    copy_for: CopyFor,
    transcripts: Sequence[str] = (),
    footage_texts: Sequence[str] = (),
    cap: int = MAX_SUGGESTIONS,
) -> tuple[list[Suggestion], dict[str, int]]:
    """Build and prove one card per pick, in pick order. Returns ``(cards, drop_reasons)``.

    Never raises. A card appears only when its kind is issued and switched on, its action was
    built by the kind's own builder and its witness passed; at most one card per kind, no two
    cards touching the same edit target, at most one copy-bearing card.
    """
    drops: dict[str, int] = {}

    def drop(reason: str) -> None:
        drops[reason] = drops.get(reason, 0) + 1

    by_kind = {i.kind.kind: i for i in issued}
    handles = clip_handles(ctx)
    base_vocab = footage_vocabulary(live_clips(ctx))
    out: list[Suggestion] = []
    seen_kinds: set[str] = set()
    seen_touched: set[str] = set()
    copy_bearing = 0
    for pick in picks:
        if len(out) >= cap:
            drop("over_limit")
            continue
        row = by_kind.get(pick.kind)
        if row is None:
            drop("unknown_kind")
            continue
        kind = row.kind
        if kind.kind in seen_kinds:
            drop("duplicate_kind")
            continue
        if not registry.enabled(kind):
            drop("kind_disabled")
            continue
        if kind.kind in COPY_KINDS and copy_bearing:
            drop("copy_bearing")
            continue
        targets = _targets_for(row, pick.target_ids, handles)
        copy: str | None = None
        if kind.kind in COPY_KINDS and pick.copy:
            copy, reason = validate_copy(
                pick.copy,
                language=ctx.language,
                transcripts=transcripts,
                footage=footage_texts,
            )
            if copy is None:
                drop(f"copy_{reason}")
        try:
            action: Action | None = kind.build_action(ctx, targets, copy)
        except Exception:  # noqa: BLE001 - a builder that crashes proves nothing
            log.warning("suggestion_blocks.build_error", kind=kind.kind, exc_info=True)
            action = None
        if action is None:
            drop("no_action")
            continue
        try:
            proven = kind.witness(action, ctx)
        except Exception:  # noqa: BLE001
            log.warning("suggestion_blocks.witness_error", kind=kind.kind, exc_info=True)
            drop("witness_error")
            continue
        if not proven.ok:
            drop(f"witness_{proven.reason or 'refused'}")
            continue
        if proven.touched & seen_touched:
            drop("overlap")
            continue
        rendered = copy_for(kind.kind, targets, copy)
        if rendered is None:
            drop("no_copy")
            continue
        title, prompt, evidence = rendered
        vocabulary = base_vocab | footage_vocabulary([{"subject": line} for line in evidence])
        why = _clean_why(
            pick.why,
            kind=kind.kind,
            language=ctx.language,
            vocabulary=vocabulary,
            transcripts=transcripts,
        )
        # With no usable agent reason, the server's own measured line is the reason and the
        # evidence keeps the rest, so the card never says the same thing twice.
        if not why and evidence:
            why, evidence = evidence[0], evidence[1:]
        try:
            card = Suggestion(
                id=suggestion_id(kind.kind, [t.key for t in targets], base_digest),
                kind=kind.kind,
                title=title,
                why=why,
                evidence=evidence[:3],
                prompt=prompt,
                copy_text=action.text if isinstance(action, CopyDecisionAction) else None,
                rank=len(out) + 1,
                anchor=_anchor(targets[0]),
                requires_protocol=kind.min_protocol,
                action=action,
                witness_tier=proven.tier,
            )
        except ValidationError:
            drop("invalid_card")
            continue
        seen_kinds.add(kind.kind)
        seen_touched |= proven.touched
        copy_bearing += 1 if kind.kind in COPY_KINDS else 0
        out.append(card)
    return out, drops


def deterministic_picks(issued: Sequence[Issued]) -> list[Pick]:
    """The no-agent order: the issued kinds by score, whole-video targets, no wording."""
    return [Pick(kind=row.kind.kind) for row in issued]
