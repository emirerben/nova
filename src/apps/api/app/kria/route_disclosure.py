"""Say before approval what the route that will render a draft does not read (prompt fidelity).

The planner returns a strategy and the brief holds the creator's asks. Every render route reads
only part of both. What it does not read used to reach the creator as silence ("Balanced
pacing" on the card, a fast montage on the phone) or, for an ask no checker owns, as a neutral
"Have a look at these in the video" after the render. Each route's table
(`app.kria.route_capabilities`, one module per route) says what it consumes; this module turns
each gap into a typed, creator-readable disclosure the draft shows BEFORE approval:

* ``strategy_notices``: strategy fields the route ignores (``NotApplied`` / ``RecordedOnly``
  in its ``FIELD_HANDLING``), named on the card ("I can't use the story structure ... on this
  video"). When a route starts reading a field, its entry becomes ``Applied`` and the line
  disappears.
* ``ask_declines``: brief asks the route cannot do (its ``ASK_DECLINES``: animated text where no
  entrance plays, captions where none are drawn). They become a typed ``not_possible`` receipt
  (`with_route_declines`) whose reason is a *route decline*: a format limit that never asks
  "simpler version?" and that approval readiness treats as approvable (disposition
  ``declined``).
* ``research_notice``: the unified montage's text step may search the web after approval.
* ``planned_card_length``: the length shown on the draft is the length the plan renders.

``draft_route`` picks the table. With ``ROUTE_REGISTRY_ENABLED`` off only the unified montage
has one, the frozen pre-registry copy (`route_capabilities._legacy_unified`), and its lines need
approval readiness, byte-identical to before the registry.

Owner rule: nothing here refuses a render, raises to the creator or blocks approval. Every
function is pure and fail-open; the switch is ``settings.kria_route_disclosure_enabled``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from types import ModuleType
from typing import Any

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.contracts import RequirementReceipt
from app.kria.reply_language import say
from app.kria.route_capabilities import (
    NotApplied,
    RecordedOnly,
    RouteId,
    WithAsks,
    registry_enabled,
    resolve_route_id,
    table_for,
)
from app.kria.route_capabilities import _legacy_unified as legacy_unified
from app.kria.route_capabilities import _lines as lines
from app.kria.route_capabilities._types import (
    NOT_APPLIED_ON_ROUTE,
    RECORDED_ONLY_ON_ROUTE,
    AskDecline,
    GivenAsks,
)

NOT_APPLIED = NOT_APPLIED_ON_ROUTE
RECORDED_ONLY = RECORDED_ONLY_ON_ROUTE
RESEARCH_AFTER_APPROVAL = "research_after_approval"
UNIFIED_MONTAGE = str(RouteId.UNIFIED_MONTAGE)


def enabled() -> bool:
    return bool(settings.kria_route_disclosure_enabled)


@dataclass(frozen=True)
class Disclosure:
    """One honest line for the draft/approval card, with a typed reason for clients and logs."""

    code: str  # NOT_APPLIED | RECORDED_ONLY (the table entry's own code) | RESEARCH_AFTER_APPROVAL
    subject: str  # strategy field name, or an ask class ("text_animation")
    text: str
    requirement_id: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


# --- strategy fields a route does not read ---------------------------------------------------
#
# Source of truth: each route's ``FIELD_HANDLING`` (`app.kria.route_capabilities`).


def _table(route: str | None) -> ModuleType | None:
    """The route's table; with the registry off, only the unified montage has one: the frozen
    pre-registry copy, so nothing a lane adds to `unified_montage` reaches a switch-off card."""
    if route is None:
        return None
    if not registry_enabled():
        return legacy_unified if route == UNIFIED_MONTAGE else None
    return table_for(route)


def draft_route(
    *,
    creator_id: object,
    edit_format: object,
    strategy: Mapping[str, Any] | None,
    clip_paths: Iterable[object] = (),
    phone_montage: bool,
) -> str | None:
    """The route whose table the draft card reads, or None for no lines.

    ``phone_montage`` is the caller's `defers_to_unified_montage` verdict. With the registry
    off that is the only route (byte-identical to before); on, every route resolves the way
    the worker's dispatch will (`route_capabilities.resolve_route_id`)."""
    if not enabled():
        return None
    if not registry_enabled():
        return UNIFIED_MONTAGE if phone_montage else None
    route = resolve_route_id(
        creator_id=creator_id, edit_format=edit_format, strategy=strategy, clip_paths=clip_paths
    )
    return str(route) if route is not None else None


def decoupled_from_readiness() -> bool:
    """Card lines and route declines for every account, not only approval-readiness ones."""
    return enabled() and registry_enabled()


def strategy_notices(
    strategy: Mapping[str, Any] | None,
    *,
    route: str | None,
    requirements: Sequence[BriefRequirement] | None = None,
    media: Mapping[str, Any] | None = None,
) -> list[Disclosure]:
    """Typed "not applied" lines for strategy fields the route does not read."""
    table = _table(route)
    if not enabled() or table is None or not strategy:
        return []
    verdict_of = getattr(table, "story_verdict", None)
    verdict = verdict_of(strategy, requirements, media) if verdict_of is not None else None
    out: list[Disclosure] = []
    for field, handling in table.FIELD_HANDLING.items():
        if not isinstance(handling, NotApplied | RecordedOnly):
            continue
        if verdict is not None:
            direction, reason = verdict
            if reason is None and field in {"direction", "pacing"}:
                continue  # built as that story: both are read (`story_direction`)
            if field == "direction":
                # Cut as a fast montage instead: the one line says why (a reason that is only
                # the plan's own trouble leaves the generic line below).
                from app.pipeline.story_direction import (  # noqa: PLC0415
                    fallback_note,
                    says_fallback,
                )

                if says_fallback(reason, direction):
                    out.append(
                        Disclosure(
                            handling.code, field, fallback_note(reason, direction, future=True)
                        )
                    )
                    continue
            if field == "story_structure" and reason is None:
                lines_of = getattr(table, "story_lines_notice", None)
                text = lines_of(strategy) if lines_of is not None else None
                if text:
                    out.append(Disclosure(handling.code, field, text))
                continue
        try:
            build = handling.build
            text = (
                build.build(strategy.get(field), strategy, list(requirements or ()))
                if isinstance(build, WithAsks)
                else build(strategy.get(field), strategy)
            )
        except Exception:  # noqa: BLE001 - a disclosure must never fail a draft
            text = None
        if text:
            out.append(Disclosure(handling.code, field, text))
    for subject, notice in getattr(table, "NOTICES", {}).items():
        try:
            text = notice(strategy)
        except Exception:  # noqa: BLE001 - a disclosure must never fail a draft
            text = None
        if text:
            out.append(Disclosure(NOT_APPLIED, subject, text))
    return out


# --- brief asks the route does not read ------------------------------------------------------

# Receipt reasons that mean "this route cannot do it" (a limit of the route, not a simpler
# plan to choose). `is_route_decline_reason` matches them by prefix like the format limits.
ROUTE_DECLINE_PREFIXES: tuple[str, ...] = lines.DECLINE_PREFIXES


def is_route_decline_reason(reason: object) -> bool:
    return isinstance(reason, str) and any(reason.startswith(p) for p in ROUTE_DECLINE_PREFIXES)


def _declines(
    ask: AskDecline,
    req: BriefRequirement,
    strategy: Mapping[str, Any],
    live: Sequence[BriefRequirement],
) -> bool:
    """``ask`` declines ``req``; a `GivenAsks` test also reads the brief's live asks."""
    test = ask.applies
    return test.test(req, strategy, live) if isinstance(test, GivenAsks) else test(req, strategy)


def ask_declines(
    requirements: Iterable[BriefRequirement],
    strategy: Mapping[str, Any] | None,
    *,
    route: str | None,
) -> list[Disclosure]:
    """Asks on a live requirement that nothing on ``route`` reads, each with its typed reason."""
    table = _table(route)
    if not enabled() or table is None:
        return []
    asks = table.ASK_DECLINES
    strategy = strategy or {}
    live = [req for req in requirements if req.live]
    out: list[Disclosure] = []
    for req in live:
        decline = next((ask for ask in asks if _declines(ask, req, strategy, live)), None)
        if decline is not None:
            out.append(
                Disclosure(NOT_APPLIED, decline.subject, decline.sentence(), requirement_id=req.id)
            )
    return out


def with_route_declines(
    receipts: Sequence[RequirementReceipt],
    requirements: Iterable[BriefRequirement],
    strategy: Mapping[str, Any] | None,
    *,
    route: str | None,
) -> list[RequirementReceipt]:
    """``receipts`` with a typed ``not_possible`` answer for every ask the route cannot read.

    The receipt is a checked answer ("Couldn't: ... (I can't animate ... yet)"), not an
    ``unchecked`` "have a look". Its reason is a route decline, so it never turns into the
    "simpler version?" question and readiness approves it with disclosure."""
    declines = {d.requirement_id: d for d in ask_declines(requirements, strategy, route=route)}
    if not declines:
        return list(receipts)
    out = []
    seen = set()
    for receipt in receipts:
        decline = declines.get(receipt.requirement_id)
        if decline is None:
            out.append(receipt)
            continue
        seen.add(receipt.requirement_id)
        if receipt.verification == "checked":
            out.append(receipt)  # a checker's own verdict is never replaced
            continue
        out.append(
            receipt.model_copy(
                update={
                    "status": "not_possible",
                    "verification": "checked",
                    "stage": "checked",
                    "reason": decline.text,
                }
            )
        )
    for req_id, decline in declines.items():
        if req_id not in seen:
            out.append(
                RequirementReceipt(
                    requirement_id=req_id,
                    status="not_possible",
                    verification="checked",
                    stage="checked",
                    reason=decline.text,
                )
            )
    return out


def requirements_with_route_declines(
    checked: Sequence[BriefRequirement],
    live: Iterable[BriefRequirement],
    strategy: Mapping[str, Any] | None,
    *,
    route: str | None,
) -> list[BriefRequirement]:
    """``checked`` plus each live ask the route cannot do that the draft check left out.

    A first draft leaves some asks to the render (`requirements_to_check_at_draft`: the
    unified montage's text, order and timing asks). An ask the route cannot do at all is not
    the render's to settle, so its decline reaches the card now, for every account. Nothing
    else is checked or receipted differently. Registry off: ``checked`` as it is."""
    if not decoupled_from_readiness():
        return list(checked)
    live = list(live)
    ids = {req.id for req in checked}
    declined = {d.requirement_id for d in ask_declines(live, strategy, route=route)}
    return [*checked, *(req for req in live if req.id in declined and req.id not in ids)]


# --- research after approval -----------------------------------------------------------------


def research_notice(
    requirements: Iterable[BriefRequirement],
    strategy: Mapping[str, Any] | None,
    *,
    route: str | None,
    previewed: bool = False,
) -> Disclosure | None:
    """The render's text step may search the web after approval: say so now.

    Shown on the unified route when the planner chose the explainer direction and no research
    was already shown on the card (``previewed``: the sourced preview lines say it).

    Limit: the render searches when the text composer returns a ``research_request`` (decided
    at render), which nothing at draft time predicts. The explainer direction is the nearest
    typed signal, so a plan with another direction that still researches gets no line."""
    if not enabled() or route != UNIFIED_MONTAGE or previewed:
        return None
    from app.kria.research_preview import RESEARCH_FACT  # noqa: PLC0415

    strategy = strategy or {}
    # The planner (a model) chose an explainer: its text is facts about the footage, which the
    # render's text step may look up. No wording is read here (KRI-667: models own meaning).
    if strategy.get("direction") != "text_explainer":
        return None
    for req in requirements:
        if req.live and req.kind == "text" and req.facts.get(RESEARCH_FACT) is not None:
            return None  # a typed research ask has its own preview/limit flow
    return Disclosure(
        RESEARCH_AFTER_APPROVAL,
        "research",
        say(
            en="To write that text I may research facts on the web after you approve",
            tr="Bu yazıyı hazırlarken onayından sonra web'de bilgi araştırabilirim",
        ),
    )


# --- one place for the card --------------------------------------------------------------------


def card_disclosures(
    requirements: Iterable[BriefRequirement],
    strategy: Mapping[str, Any] | None,
    *,
    route: str | None,
    research_previewed: bool = False,
    media: Mapping[str, Any] | None = None,
) -> list[Disclosure]:
    """Lines for the draft reply and approval card: research after approval, then unread strategy
    fields.
    (Asks nothing reads show as their own receipts, `with_route_declines`.)"""
    requirements = list(requirements)
    research = research_notice(requirements, strategy, route=route, previewed=research_previewed)
    # Research first: it is the one line about something that will happen, and the card keeps
    # whole lines within its budget, longest-lived (strategy detail) last.
    return ([research] if research is not None else []) + strategy_notices(
        strategy, route=route, requirements=requirements, media=media
    )


def card_layout(
    disclosures: Sequence[Disclosure], *, limit: int = 640
) -> tuple[list[Disclosure], list[Disclosure]]:
    """``(whole, rest)``: the disclosures whose whole sentence fits ``limit`` characters, in
    order, and the ones after the first that does not."""
    whole: list[Disclosure] = []
    used = 0
    for index, item in enumerate(disclosures):
        extra = len(item.text) + 3
        if whole and used + extra > limit:
            return whole, list(disclosures[index:])
        whole.append(item)
        used += extra
    return whole, []


def card_text(disclosures: Sequence[Disclosure], *, limit: int = 640) -> str:
    """The disclosures as card lines under a plain heads-up: whole sentences within ``limit``
    characters, then one compact line naming every disclosure that did not fit (never cut
    silently). A subject with no short name keeps its whole sentence. With the registry or the
    disclosure switch off the lines past the budget are dropped, as before the registry."""
    whole, rest = card_layout(disclosures, limit=limit)
    return _card_lines(whole, rest if decoupled_from_readiness() else [])


def card_text_within(disclosures: Sequence[Disclosure], room: int) -> str:
    """``card_text`` in at most ``room`` characters, for a reply with a hard length limit (a
    held draft): fewer whole sentences, the rest named on the compact line, so no line is cut
    mid-sentence. Registry or disclosure switch off: ``card_text`` as it is."""
    text = card_text(disclosures)
    if len(text) <= room or not decoupled_from_readiness():
        return text
    whole, _rest = card_layout(disclosures)
    for keep in range(len(whole) - 1, -1, -1):
        text = _card_lines(disclosures[:keep], disclosures[keep:])
        if len(text) <= room:
            break
    return text


def _card_lines(whole: Sequence[Disclosure], rest: Sequence[Disclosure]) -> str:
    out = [f"- {item.text}" for item in whole]
    named = [item.subject for item in rest if lines.subject_label(item.subject)]
    if named:
        out.append(f"- {lines.compact_line(named)}")
    out += [f"- {item.text}" for item in rest if not lines.subject_label(item.subject)]
    if not out:
        return ""
    heads_up = say(en="Heads-up for this video:", tr="Bu video için not:")
    return "\n".join([heads_up, *out])


# --- length -----------------------------------------------------------------------------------


def planned_card_length(
    strategy: Mapping[str, Any],
    media: Mapping[str, Any] | None,
    *,
    route: str | None,
) -> float | None:
    """The length the draft should show, or None to leave the strategy as it is.

    The unified montage plays the approved length plan (the song section or the footage), and
    ignores the planner's ``target_duration_s`` unless the creator stated a length
    (`unified_montage` ``creator_duration_proven``). So when no length was stated, the number on
    the card is the plan's, not the model's guess (Assos: card 20 s, recipe 18.005 s)."""
    if not enabled() or route != UNIFIED_MONTAGE or not media:
        return None
    if strategy.get("creator_duration_s") is not None or strategy.get("target_duration_requested"):
        return None
    from app.kria.approval_readiness import planned_length_s  # noqa: PLC0415

    try:
        planned = planned_length_s(dict(media))
    except ValueError:
        return None
    from app.schemas.edit_proposal import (  # noqa: PLC0415
        MAX_PROPOSAL_DURATION_S,
        MIN_PROPOSAL_DURATION_S,
    )

    if planned is None or not MIN_PROPOSAL_DURATION_S <= planned <= MAX_PROPOSAL_DURATION_S:
        return None
    current = strategy.get("target_duration_s")
    if isinstance(current, (int, float)) and abs(float(current) - planned) < 1e-6:
        return None
    return round(planned, 3)
