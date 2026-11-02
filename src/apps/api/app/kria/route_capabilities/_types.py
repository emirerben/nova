"""The shapes every route table uses (route capability registry, KRI-1097 wave 2)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from app.config import settings
from app.kria.brief import BriefRequirement

# The typed code of every card line a table builds (`route_disclosure.Disclosure.code`): the
# code says the value is not followed, so no wording can turn a line into a claim that it is.
NOT_APPLIED_ON_ROUTE = "not_applied_on_route"
RECORDED_ONLY_ON_ROUTE = "recorded_only_on_route"


def registry_enabled() -> bool:
    """Every route reads its own table (``ROUTE_REGISTRY_ENABLED``). False: only the unified
    montage discloses, from the frozen pre-registry table (`_legacy_unified`), only with
    approval readiness, exactly as before the registry."""
    return bool(settings.route_registry_enabled)


# ``(value, strategy) -> creator sentence``, or None when the value is the route's own
# behaviour or empty (nothing was asked, nothing to disclose).
Builder = Callable[[Any, Mapping[str, Any]], str | None]
# ``strategy -> creator sentence`` for a route limit no single strategy field owns.
Notice = Callable[[Mapping[str, Any]], str | None]


@dataclass(frozen=True)
class WithAsks:
    """A builder that also reads the brief's live asks, typed facts only (KRI-667):
    ``(value, strategy, requirements) -> creator sentence | None``."""

    build: Callable[[Any, Mapping[str, Any], Sequence[BriefRequirement]], str | None]


@dataclass(frozen=True)
class Applied:
    """The route reads the field and the render follows it.

    ``via`` names the carrier when the route reads it outside the read scan of ``READERS``
    (dispatch, the render contract), as ``"<path under app/>:<symbol>"``. The consumer gate
    (`scripts/kria/score_route.py`) checks that the symbol is defined in that file and reads
    the field, and flags an ``Applied`` field with neither a reader nor such a carrier.
    """

    via: str = ""


@dataclass(frozen=True)
class RecordedOnly:
    """The route stores the value (the plan records it) but the render does not follow it."""

    build: Builder | WithAsks
    code: ClassVar[str] = RECORDED_ONLY_ON_ROUTE


@dataclass(frozen=True)
class NotApplied:
    """The route ignores the value; ``build`` is the line the approval card shows instead."""

    build: Builder | WithAsks
    code: ClassVar[str] = NOT_APPLIED_ON_ROUTE


Handling = Applied | RecordedOnly | NotApplied


@dataclass(frozen=True)
class AskDecline:
    """A typed brief ask the route cannot do, answered as a route decline before approval.

    ``applies`` reads typed facts only (KRI-667: never the creator's wording). ``sentence``
    is the receipt reason; every reason is listed in ``route_disclosure.ROUTE_DECLINE_PREFIXES``
    so approval readiness treats it as a format limit, never a "simpler version?" question.
    """

    subject: str
    applies: Callable[[BriefRequirement, Mapping[str, Any]], bool]
    sentence: Callable[[], str]


@dataclass(frozen=True)
class RenderEntry:
    """The one function the route's job runner builds its render recipe with.

    ``entry`` is ``"<path under app/>:<function>"`` inside the route's ``READERS``. ``recipe``
    maps a replay key (``title_animation``, ``pace``, ``length_s``, ``caption_mode``) to where
    its value sits in what ``entry`` returns (`scripts/kria/score_route.py` reads it there:
    ``a.b[]`` every item, ``[id^=text-]`` the items whose id starts with it, ``**`` any depth).
    """

    entry: str
    recipe: Mapping[str, str]


@dataclass(frozen=True)
class RouteReaders:
    """Where the route's code lives, for the consumer gate's read scan.

    ``files`` are paths under ``app/``; ``functions`` are names in
    ``app/tasks/generative_build.py`` whose bodies belong to the route.
    """

    files: tuple[str, ...]
    functions: tuple[str, ...] = ()
