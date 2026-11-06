"""The kind registry: discovery from the explicit list in ``kinds/__init__.py`` and queries.

``legacy_gate()`` is the KRI-756 engine's ``CHIP_KIND_GATE``, in its original order.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable
from typing import Any

from app.services.suggestions.models import SURFACES, ChipKind, ChipKindSpec, SuggestionKind


@functools.lru_cache(maxsize=1)
def all_kinds() -> tuple[SuggestionKind, ...]:
    from app.services.suggestions.kinds import KIND_MODULES  # noqa: PLC0415

    kinds = tuple(module.KIND for module in KIND_MODULES)
    names = [k.kind for k in kinds]
    if len(set(names)) != len(names):
        raise RuntimeError(f"duplicate suggestion kind registered: {names}")
    return kinds


def get(kind: str) -> SuggestionKind | None:
    return next((k for k in all_kinds() if k.kind == kind), None)


def legacy_gate() -> dict[ChipKind, ChipKindSpec]:
    return {ChipKind(k.kind): k.legacy for k in all_kinds() if k.legacy is not None}


def enabled(kind: SuggestionKind, *, settings: Any | None = None) -> bool:
    """Is the kind switched on for v2 sets? Its own flag, and the signals flag when it reads any."""
    if settings is None:
        from app.config import settings as live  # noqa: PLC0415

        settings = live
    if kind.flag is not None and not bool(getattr(settings, kind.flag, False)):
        return False
    return not kind.required_signals or bool(
        getattr(settings, "kria_suggestion_signals_enabled", False)
    )


def kinds_for(
    state: str, surface: str, *, settings: Any | None = None
) -> tuple[SuggestionKind, ...]:
    return tuple(
        k
        for k in all_kinds()
        if state in k.states and surface in k.surfaces and enabled(k, settings=settings)
    )


def problems(kinds: Iterable[SuggestionKind] | None = None) -> list[str]:
    """What is wrong with a registration. Empty means every kind can be proven and has a home.

    A kind without a witness could ship a card nothing checked; one without a declared surface
    could be offered where it cannot run. A guard test fails on any entry here.
    """
    from app.services.clip_signals import names as signal_names  # noqa: PLC0415

    known_signals = signal_names()
    out: list[str] = []
    for k in all_kinds() if kinds is None else kinds:
        if not callable(k.witness):
            out.append(f"{k.kind}: no witness")
        if not callable(k.build_action):
            out.append(f"{k.kind}: no build_action")
        if not k.surfaces:
            out.append(f"{k.kind}: no surface declared")
        elif not k.surfaces <= SURFACES:
            out.append(f"{k.kind}: unknown surface {sorted(k.surfaces - SURFACES)}")
        unknown = sorted(set(k.required_signals) - known_signals)
        if unknown:
            out.append(f"{k.kind}: required signal not registered {unknown}")
    return out
