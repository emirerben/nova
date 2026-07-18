"""Output canvas selection for render pipelines."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Canvas:
    width: int
    height: int


PORTRAIT = Canvas(1080, 1920)
LANDSCAPE = Canvas(1920, 1080)


def canvas_for_orientation(orientation: str | None) -> Canvas:
    if orientation == "landscape":
        return LANDSCAPE
    return PORTRAIT
