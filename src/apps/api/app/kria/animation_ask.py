"""What a creator's "with animations" asks of the on-screen text, as one typed value.

Prompt fidelity (Assos): "With animations, explain why people should visit Assos" arrived as
prose on a ``text`` requirement with no ``facts.animation`` (the planner only typed an
animation the creator NAMED), so nothing downstream could draw, check or decline it. The
planner prompt now records the ask in ``facts.animation`` (an edge id, or ``"any"`` when the
creator named none); this is the one reader every layer shares: the brief view (what the route
draws), the creation text pass (apply and verify) and the receipt checker (judge).

Structured only, by design (KRI-667): the model that reads the message records the meaning, the
server reads the field. Nothing here guesses meaning from the creator's words, so a brief
without the fact (an older thread) is simply not an animation ask. Pure; it never raises.

An ask is NAMED (a registry edge: "typewriter", "fade") or UNNAMED. The creator's named id
always wins; an unnamed ask gets ``DEFAULT_TEXT_ENTRANCE``, a plain readable fade, never a
designed edge the creator did not choose. The device decides which form is emitted
(``text_entrance_form``): full ``animation_phases`` where ``authoredText`` is verified, else the
legacy ``effect`` that only needs ``animatedText``, else nothing (the receipt says so).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.agents._schemas.text_animation_phases import LEGACY_EFFECT_ENTRANCE
from app.schemas.text_style_intent import (
    TITLE_ANIMATION_SPEED,
    normalize_label_position,
    normalize_title_animation,
)
from app.text_fold import loose_text

# What the planner writes in ``facts.animation`` when the creator asked for animation without
# naming one. Any value that is not a registry edge reads as unnamed too.
UNNAMED_ANIMATION = "any"
# A plain fade: readable on every text length and on every device that animates text at all.
DEFAULT_TEXT_ENTRANCE = "fade"

EntranceForm = Literal["phases", "effect"]

_OFF_VALUES = frozenset({"none", "no", "off", "false", "static", "yok", "hayir"})


@dataclass(frozen=True)
class AnimationAsk:
    """One live text requirement's animation ask: ``edge`` None means the creator named none."""

    edge: str | None
    scope: str = "global"

    @property
    def named(self) -> bool:
        return self.edge is not None

    @property
    def value(self) -> str:
        return self.edge or UNNAMED_ANIMATION

    def resolved_edge(self) -> str:
        return self.edge or DEFAULT_TEXT_ENTRANCE


def _fact_ask(value: object) -> tuple[bool, str | None]:
    """``(asked, named edge)`` for a ``facts.animation`` value."""
    if value is True:
        return True, None
    if not isinstance(value, str) or not value.strip():
        return False, None
    if loose_text(value) in _OFF_VALUES:
        return False, None
    return True, normalize_title_animation(value)


def animates_text_kind(req: Any, *, styles: bool = True) -> bool:
    """``req`` is a kind whose ``facts.animation`` animates the on-screen text: a ``text`` ask,
    or (``styles``) a ``style`` ask ("animate the big text in"). The one rule the route card
    (`route_capabilities._lines.asks_text_animation`) and `animation_ask` share."""
    kind = getattr(req, "kind", None)
    return kind == "text" or (styles and kind == "style")


def animation_ask(req: Any, *, styles: bool = False) -> AnimationAsk | None:
    """The animation a live ``text`` requirement asks for, else None.

    Reads ``facts.animation`` only: a registry edge is NAMED; the sentinel, ``true`` or an id
    the registry does not know is UNNAMED (the creator wanted some animation); "none" /
    "static" is no ask. ``styles`` counts a ``style`` ask too (`animates_text_kind`): a route
    lane passes it behind its own switch; the unified montage keeps text asks only.
    """
    if not animates_text_kind(req, styles=styles) or not getattr(req, "live", True):
        return None
    facts: Mapping[str, Any] = getattr(req, "facts", None) or {}
    if "animation" not in facts:
        return None
    asked, edge = _fact_ask(facts["animation"])
    return AnimationAsk(edge, str(getattr(req, "scope", "") or "global")) if asked else None


def animation_targets(
    requirements: Iterable[Any], *, styles: bool = False
) -> list[tuple[str, AnimationAsk]]:
    """``(target, ask)`` per distinct target, most specific first (``styles``: `animation_ask`).

    Targets: ``clip:<id>`` (one label), ``labels`` (per-clip), ``title``, ``all`` (global).
    When two asks hit one target the named one wins; the first named (or first) is kept.
    """
    found: dict[str, AnimationAsk] = {}
    for req in requirements:
        ask = animation_ask(req, styles=styles)
        if ask is None:
            continue
        scope = ask.scope
        if scope.startswith("clip:"):
            target = scope
        elif scope == "per_clip":
            target = "labels"
        elif scope == "title":
            target = "title"
        else:
            target = "all"
        if target not in found or (ask.named and not found[target].named):
            found[target] = ask

    def rank(item: tuple[str, AnimationAsk]) -> int:
        return 0 if item[0].startswith("clip:") else {"labels": 1, "title": 1, "all": 2}[item[0]]

    return sorted(found.items(), key=rank)


def requested_text_position(requirements: Iterable[Any]) -> str | None:
    """The corner a live GLOBAL, non-literal text ask names (``facts.position``), else None.

    A literal ask ("put 'Assos 2026' bottom right") pins ITS OWN line; it never moves the
    per-clip labels, which is a different route's behaviour.
    """
    for req in requirements:
        if getattr(req, "kind", None) != "text" or not getattr(req, "live", True):
            continue
        if getattr(req, "literal", None):
            continue
        if str(getattr(req, "scope", "") or "") != "global":
            continue
        spot = normalize_label_position((getattr(req, "facts", None) or {}).get("position"))
        if spot is not None:
            return spot
    return None


def text_entrance_form() -> EntranceForm | None:
    """How the phone can play a text entrance now, else None (the text stays static).

    ``animation_phases`` need ``authoredText`` verified (a layer carrying them is refused
    without it); the legacy ``effect`` only needs ``animatedText``. Emitting a form the device
    has not verified would fail the render, so a closed gate emits nothing and says so.
    """
    from app.config import settings  # noqa: PLC0415

    verified = set(settings.phone_render_verified_features)
    if "authoredText" in verified:
        return "phases"
    if "animatedText" in verified:
        return "effect"
    return None


_LEGACY_EFFECT_FOR_EDGE = {entrance: effect for effect, entrance in LEGACY_EFFECT_ENTRANCE.items()}
# A designed edge has no legacy effect: the closest plain one is the default fade.
_FALLBACK_EFFECT = _LEGACY_EFFECT_FOR_EDGE[DEFAULT_TEXT_ENTRANCE]


def entrance_patch(edge: str, form: EntranceForm) -> dict[str, Any]:
    """The text-row patch that plays ``edge`` on the phone in ``form``."""
    if form == "phases":
        return {
            "animation_phases": {
                "entrance": edge,
                "exit": "none",
                "loop": "none",
                "speed": TITLE_ANIMATION_SPEED.get(edge, 1.0),
            }
        }
    return {"effect": _LEGACY_EFFECT_FOR_EDGE.get(edge, _FALLBACK_EFFECT)}


def text_look_for(requirements: Iterable[Any]) -> dict[str, Any] | None:
    """The ``TextCompositionProgram.text_look`` for the live asks, else None.

    None when nothing asks for animation or the device cannot play any form; callers report
    the second case (`text_entrance_form`) and never fail on it.
    """
    targets = animation_targets(requirements)
    form = text_entrance_form()
    if not targets or form is None:
        return None
    return {
        "form": form,
        "entries": [
            {"target": target, "entrance": ask.resolved_edge(), "named": ask.named}
            for target, ask in targets
        ],
    }
