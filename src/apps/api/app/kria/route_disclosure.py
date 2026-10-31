"""Say before approval what the route that will render a draft does not read (prompt fidelity).

The planner returns a strategy and the brief holds the creator's asks. The phone unified
montage route (`pipeline/unified_montage.py`) reads only part of both. What it does not read
used to reach the creator as silence ("Balanced pacing" on the card, a fast montage on the
phone) or, for an ask no checker owns, as a neutral "Have a look at these in the video" after
the render. This module is the one registry of what that route does not consume, and turns
each gap into a typed, creator-readable disclosure the draft shows BEFORE approval:

* ``UNREAD_STRATEGY_FIELDS``: strategy fields the route ignores. A field that is set but not
  read is named ("I can't use the story structure ... on this video"). When a route unit
  starts reading a field, delete its entry here and the line disappears.
* ``ask_declines``: brief asks nothing on the route reads (today: animated text outside the
  title, when the phone plays no text entrance either). They become a typed ``not_possible``
  receipt (`with_route_declines`) whose reason is a *route decline*: a format limit that never
  asks "simpler version?" and that approval readiness treats as approvable (disposition
  ``declined``).
* ``research_notice``: the render's text step may search the web after approval.
* ``planned_card_length``: the length shown on the draft is the length the plan renders.

Owner rule: nothing here refuses a render, raises to the creator or blocks approval. Every
function is pure and fail-open; the switch is ``settings.kria_route_disclosure_enabled``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.contracts import RequirementReceipt
from app.kria.reply_language import say

NOT_APPLIED = "not_applied_on_route"
RESEARCH_AFTER_APPROVAL = "research_after_approval"
UNIFIED_MONTAGE = "unified_montage"

# What the unified montage route stamps on every plan it compiles
# (`unified_montage.py`: ``direction="fast_montage"``, ``pace="fast"``). A strategy that
# already says this loses nothing, so it needs no line.
_ROUTE_DIRECTION = "fast_montage"
_ROUTE_PACE = "fast"


def enabled() -> bool:
    return bool(settings.kria_route_disclosure_enabled)


@dataclass(frozen=True)
class Disclosure:
    """One honest line for the draft/approval card, with a typed reason for clients and logs."""

    code: str  # NOT_APPLIED | RESEARCH_AFTER_APPROVAL
    subject: str  # strategy field name, or an ask class ("text_animation")
    text: str
    requirement_id: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _clip(value: object, limit: int = 70) -> str:
    text = " ".join(str(value or "").split()).replace('"', "'")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _list(values: Iterable[object]) -> str:
    return ", ".join(_clip(v, 40) for v in values if str(v or "").strip())


# --- strategy fields the unified montage route does not read --------------------------------
#
# Source of truth for "unread": `scripts/kria/score_assos.py` check 10 scans the route files for
# reads of every CreativeStrategy field and reports drift from this table (report only).
# Each builder returns the creator sentence for a value the route does not apply, or None when
# the value is the route's own behaviour / empty (nothing was asked, nothing to disclose).


def _hook(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not str(value or "").strip():
        return None
    return say(
        en=(
            f'I can\'t use the opening hook idea ("{_clip(value)}") on this video yet, '
            "so it won't be shown as text"
        ),
        tr=(
            f'Açılış kanca fikrini ("{_clip(value)}") bu videoda henüz kullanamıyorum, '
            "o yüzden yazı olarak gösterilmeyecek"
        ),
    )


def _story(value: Any, _s: Mapping[str, Any]) -> str | None:
    items = [v for v in (value or []) if str(v or "").strip()]
    if not items:
        return None
    return say(
        en=(
            f"I can't follow the planned story structure ({_list(items)}) on this video yet, "
            "so it won't change the order of the clips"
        ),
        tr=(
            f"Planlanan hikâye yapısını ({_list(items)}) bu videoda henüz uygulayamıyorum, "
            "o yüzden kliplerin sırasını değiştirmeyecek"
        ),
    )


def _pacing(value: Any, strategy: Mapping[str, Any]) -> str | None:
    if not value or value == _ROUTE_PACE:
        return None
    from app.services.length_policy import effective_pace  # noqa: PLC0415

    if effective_pace(strategy) is not None:
        # The pace unit applies it: the route holds each clip as long as this pace says
        # (`PACE_DRIVES_CUTS_ENABLED`). Only a silent "balanced" default is still not applied.
        return None
    return say(
        en=(
            f"The {str(value).replace('_', ' ')} pacing isn't applied on this video yet: "
            "the cuts follow a fast pace"
        ),
        tr=(
            f"{str(value).replace('_', ' ')} tempo bu videoda henüz uygulanmıyor: "
            "kesimler hızlı tempoda"
        ),
    )


def _direction(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value or value == _ROUTE_DIRECTION:
        return None
    return say(
        en=(
            f"The {str(value).replace('_', ' ')} direction isn't applied on this video yet: "
            "it is cut as a fast montage"
        ),
        tr=(
            f"{str(value).replace('_', ' ')} yönü bu videoda henüz uygulanmıyor: "
            "hızlı bir montaj olarak kesiliyor"
        ),
    )


def _caption_style(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value or value in {"auto", "none"}:
        return None
    return say(
        en=f"I can't apply the {value} caption style on this video yet, so it won't be used",
        tr=f"{value} altyazı stilini bu videoda henüz uygulayamıyorum, kullanılmayacak",
    )


def _treatments(value: Any, _s: Mapping[str, Any]) -> str | None:
    items = [v for v in (value or []) if str(v or "").strip()]
    if not items:
        return None
    return say(
        en=(
            f"I can't add the treatments ({_list(items)}) on this video yet, "
            "so they won't be applied"
        ),
        tr=f"Şu efektleri ({_list(items)}) bu videoda henüz ekleyemiyorum, uygulanmayacak",
    )


def _archetype(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en=f"I can't use the {str(value).replace('_', ' ')} story archetype on this video yet",
        tr=f"{str(value).replace('_', ' ')} hikâye kalıbını bu videoda henüz kullanamıyorum",
    )


def _hero(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en="I can't feature one hero clip on this video yet, so every clip is cut the same way",
        tr=(
            "Bu videoda tek bir ana klibi öne çıkaramıyorum, "
            "bu yüzden bütün klipler aynı şekilde kesiliyor"
        ),
    )


def _overlay(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en="I can't use the planned overlay display on this video yet, so it won't be shown",
        tr="Planlanan katman gösterimini bu videoda henüz kullanamıyorum, gösterilmeyecek",
    )


def _sfx(value: Any, _s: Mapping[str, Any]) -> str | None:
    if not value:
        return None
    return say(
        en="I can't add sound effects on this video yet, so none will be played",
        tr="Bu videoya henüz ses efekti ekleyemiyorum, hiçbiri çalmayacak",
    )


UNREAD_STRATEGY_FIELDS: dict[str, Callable[[Any, Mapping[str, Any]], str | None]] = {
    "intro_hook": _hook,
    "story_structure": _story,
    "pacing": _pacing,
    "direction": _direction,
    "caption_style": _caption_style,
    "optional_treatments": _treatments,
    "archetype": _archetype,
    "hero_media_id": _hero,
    "overlay_display": _overlay,
    "licensed_sfx": _sfx,
}


def _story_verdict(
    strategy: Mapping[str, Any],
    requirements: Sequence[BriefRequirement] | None,
    media: Mapping[str, Any] | None,
) -> tuple[str, str | None] | None:
    """``(direction, reason)`` for a planner direction the route builds as a story, else None.

    ``reason`` None means the story will be built (the route reads ``direction``,
    ``story_structure`` and ``pacing``); a reason means the route keeps the fast montage and
    says why. The same pure rules the render applies (`pipeline.story_direction`), over what
    the draft knows before approval: the strategy, the live asks and the pinned length plan.
    Fail-open: any trouble returns None and the generic lines below stand.
    """
    from app.services.length_policy import story_direction  # noqa: PLC0415

    direction = story_direction(strategy)
    if direction is None:
        return None
    try:
        from app.kria.brief import CreativeBrief  # noqa: PLC0415
        from app.pipeline.story_direction import (  # noqa: PLC0415
            crossfade_blocker,
            story_blocker,
            story_lines,
            story_media_blocker,
        )
        from app.pipeline.unified_montage import brief_view  # noqa: PLC0415

        view = brief_view(CreativeBrief(version=1, requirements=list(requirements or [])))
        reason = (
            story_blocker(strategy, view)
            or (None if story_lines(strategy) else "no_story_lines")
            or story_media_blocker(strategy, media)
            or crossfade_blocker(strategy, direction)
        )
    except Exception:  # noqa: BLE001 - a disclosure must never fail a draft
        return None
    return direction, reason


def _story_lines_notice(strategy: Mapping[str, Any]) -> str | None:
    """The outline sets the chapters (clip grouping), but the clips keep their order."""
    items = [v for v in (strategy.get("story_structure") or []) if str(v or "").strip()]
    if not items:
        return None
    return say(
        en=(
            f"The planned story structure ({_list(items)}) sets the chapters "
            "(which clips are grouped together), but it won't reorder your clips: they stay in "
            "the order you gave"
        ),
        tr=(
            f"Planlanan hikâye yapısı ({_list(items)}) bölümleri (hangi kliplerin birlikte "
            "gruplanacağını) belirliyor ama kliplerinin sırasını değiştirmeyecek: verdiğin sırada "
            "kalacaklar"
        ),
    )


def strategy_notices(
    strategy: Mapping[str, Any] | None,
    *,
    route: str | None,
    requirements: Sequence[BriefRequirement] | None = None,
    media: Mapping[str, Any] | None = None,
) -> list[Disclosure]:
    """Typed "not applied" lines for strategy fields the route does not read."""
    if not enabled() or route != UNIFIED_MONTAGE or not strategy:
        return []
    verdict = _story_verdict(strategy, requirements, media)
    out: list[Disclosure] = []
    for field, build in UNREAD_STRATEGY_FIELDS.items():
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
                            NOT_APPLIED, field, fallback_note(reason, direction, future=True)
                        )
                    )
                    continue
            if field == "story_structure" and reason is None:
                text = _story_lines_notice(strategy)
                if text:
                    out.append(Disclosure(NOT_APPLIED, field, text))
                continue
        try:
            text = build(strategy.get(field), strategy)
        except Exception:  # noqa: BLE001 - a disclosure must never fail a draft
            text = None
        if text:
            out.append(Disclosure(NOT_APPLIED, field, text))
    return out


# --- brief asks the route does not read ------------------------------------------------------

ANIMATION_DECLINE_EN = (
    "I can't animate on-screen text on this video yet, so the text shows without motion"
)
ANIMATION_DECLINE_TR = (
    "Bu videoda ekrandaki yazıyı henüz canlandıramıyorum, o yüzden yazı hareketsiz görünecek"
)
# Receipt reasons that mean "this route cannot do it" (a limit of the route, not a simpler
# plan to choose). `is_route_decline_reason` matches them by prefix like the format limits.
ROUTE_DECLINE_PREFIXES: tuple[str, ...] = (ANIMATION_DECLINE_EN, ANIMATION_DECLINE_TR)


def is_route_decline_reason(reason: object) -> bool:
    return isinstance(reason, str) and any(reason.startswith(p) for p in ROUTE_DECLINE_PREFIXES)


def asks_text_animation(req: BriefRequirement) -> bool:
    """True when the ask is for moving text, from the brief's typed facts only: a typed
    ``animation`` (an edge id, or "any" for a generic "animated text" ask that named no style;
    the brief writer records it, `main_creator` prompt v106). No wording is read here
    (KRI-667), so an ask recorded without the fact is not disclosed."""
    if req.kind not in {"text", "style"}:
        return False
    return bool(req.facts.get("animation"))


def _text_animation_consumed(req: BriefRequirement) -> bool:
    """The creation text pass plays this ask (prompt fidelity, animation unit).

    Only a ``text`` ask the phone can play an entrance for: when the phone has not verified any
    text-entrance form (``text_entrance_form`` is None) nothing moves, so the decline below
    stays and says so. A ``style`` ask is not read by that pass."""
    from app.kria.animation_ask import animation_ask, text_entrance_form  # noqa: PLC0415

    return (
        bool(settings.text_animation_ask_enabled)
        and animation_ask(req) is not None
        and text_entrance_form() is not None
    )


def _title_animation_consumed(req: BriefRequirement, strategy: Mapping[str, Any]) -> bool:
    """The unified route animates exactly one text: the opening title, from a typed id
    (`unified_montage.brief_view` reads ``facts.animation`` for ``scope == "title"``; the
    strategy's ``title_animation`` is the other typed source)."""
    from app.schemas.text_style_intent import normalize_title_animation  # noqa: PLC0415

    if req.scope != "title":
        return False
    return bool(
        normalize_title_animation(req.facts.get("animation"))
        or normalize_title_animation(strategy.get("title_animation"))
    )


def ask_declines(
    requirements: Iterable[BriefRequirement],
    strategy: Mapping[str, Any] | None,
    *,
    route: str | None,
) -> list[Disclosure]:
    """Asks on a live requirement that nothing on ``route`` reads, each with its typed reason."""
    if not enabled() or route != UNIFIED_MONTAGE:
        return []
    strategy = strategy or {}
    out: list[Disclosure] = []
    for req in requirements:
        if not req.live:
            continue
        if (
            asks_text_animation(req)
            and not _title_animation_consumed(req, strategy)
            and not _text_animation_consumed(req)
        ):
            out.append(
                Disclosure(
                    NOT_APPLIED,
                    "text_animation",
                    say(en=ANIMATION_DECLINE_EN, tr=ANIMATION_DECLINE_TR),
                    requirement_id=req.id,
                )
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


def card_text(disclosures: Sequence[Disclosure], *, limit: int = 640) -> str:
    """The disclosures as card lines under a plain heads-up, within ``limit`` characters (whole
    lines only)."""
    lines: list[str] = []
    used = 0
    for item in disclosures:
        extra = len(item.text) + 3
        if lines and used + extra > limit:
            break
        lines.append(f"- {item.text}")
        used += extra
    if not lines:
        return ""
    heads_up = say(en="Heads-up for this video:", tr="Bu video için not:")
    return "\n".join([heads_up, *lines])


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
