"""The shapes every route table uses (route capability registry, KRI-1097 wave 2)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.kria.brief import BriefRequirement


def registry_enabled() -> bool:
    """Every route reads its own table (``ROUTE_REGISTRY_ENABLED``). False: only the unified
    montage discloses, only with approval readiness, exactly as before the registry."""
    return bool(settings.route_registry_enabled)


# ``(value, strategy) -> creator sentence``, or None when the value is the route's own
# behaviour or empty (nothing was asked, nothing to disclose).
Builder = Callable[[Any, Mapping[str, Any]], str | None]
# ``strategy -> creator sentence`` for a route limit no single strategy field owns.
Notice = Callable[[Mapping[str, Any]], str | None]


@dataclass(frozen=True)
class Applied:
    """The route reads the field and the render follows it.

    ``via`` names the carrier when the route reads it somewhere the read scan of
    ``READERS`` cannot see (dispatch, the render contract). The consumer gate
    (`scripts/kria/score_route.py`) flags an ``Applied`` field with no reader and no ``via``.
    """

    via: str = ""


@dataclass(frozen=True)
class RecordedOnly:
    """The route stores the value (the plan records it) but the render does not follow it."""

    build: Builder


@dataclass(frozen=True)
class NotApplied:
    """The route ignores the value; ``build`` is the line the approval card shows instead."""

    build: Builder


Handling = Applied | RecordedOnly | NotApplied


@dataclass(frozen=True)
class AskDecline:
    """A typed brief ask the route cannot do, answered as a route decline before approval.

    ``applies`` reads typed facts only (KRI-667: never the creator's wording). ``text`` is
    the receipt reason; every reason is listed in ``route_disclosure.ROUTE_DECLINE_PREFIXES``
    so approval readiness treats it as a format limit, never a "simpler version?" question.
    """

    subject: str
    applies: Callable[[BriefRequirement, Mapping[str, Any]], bool]
    text: Callable[[], str]


@dataclass(frozen=True)
class RouteReaders:
    """Where the route's code lives, for the consumer gate's read scan.

    ``files`` are paths under ``app/``; ``functions`` are names in
    ``app/tasks/generative_build.py`` whose bodies belong to the route.
    """

    files: tuple[str, ...]
    functions: tuple[str, ...] = ()
