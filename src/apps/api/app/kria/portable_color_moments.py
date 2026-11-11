"""Colour moments on the output timeline (KRI-1350).

A colour moment is a window of the finished video drawn in another tone (black-and-white,
warm, faded), entered and left with a cut, a white flash, a warm light-leak flash or a dip
to black. The end fade ramps the edit's last seconds to black. Both are drawn on device by
``KriaMediaEngine.ColorMomentPainter`` on the whole composed picture below the text.

Mirrors ``ColorMoments.swift``; the shared wire cases live in
``tests/fixtures/colour-moments/parity-v1.json``. Edge timing contract: an edge at ``t``
lasting ``d`` is drawn over ``[t - d/2, t + d/2]``; the flash peaks / the dip bottoms out on
the frame at ``t``, and the tone switches there, under it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

ColorMomentTone = Literal["mono", "warm", "faded"]
ColorMomentEdgeStyle = Literal["cut", "flash_white", "light_leak", "dip_black"]

MAX_COLOR_MOMENTS = 6
# Sound rows a colour moment owns carry this id prefix plus "<moment id>-<enter|exit>"; they sit
# exactly on that edge and are replaced, moved and removed together with the moments.
SOUND_ID_PREFIX = "cmsfx-"
MIN_COLOR_MOMENT_S = 0.5
DEFAULT_EDGE_S = 1 / 3
EDGE_RANGE_S = (0.1, 0.6)
END_FADE_RANGE_S = (0.3, 4.0)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ColorMomentEdge(_Model):
    style: ColorMomentEdgeStyle
    duration: float = Field(default=DEFAULT_EDGE_S, ge=EDGE_RANGE_S[0], le=EDGE_RANGE_S[1])

    @model_serializer(mode="wrap")
    def _omit_cut_duration(self, handler):
        payload = handler(self)
        if self.style == "cut":
            payload.pop("duration", None)
        return payload


class ColorMoment(_Model):
    id: str = Field(min_length=1, max_length=160)
    start: float = Field(ge=0, le=1800)
    end: float = Field(gt=0, le=1800)
    tone: ColorMomentTone
    enter: ColorMomentEdge | None = None
    exit: ColorMomentEdge | None = None

    @model_validator(mode="after")
    def _valid_window(self):
        if self.end - self.start < MIN_COLOR_MOMENT_S - 1e-9:
            raise ValueError(f"a colour moment lasts at least {MIN_COLOR_MOMENT_S} s")
        # A cut edge is the default and is never written (the phone omits it too).
        if self.enter is not None and self.enter.style == "cut":
            self.enter = None
        if self.exit is not None and self.exit.style == "cut":
            self.exit = None
        return self

    @model_serializer(mode="wrap")
    def _omit_cut_edges(self, handler):
        payload = handler(self)
        for key in ("enter", "exit"):
            if payload.get(key) is None:
                payload.pop(key, None)
        return payload


class EndFade(_Model):
    duration: float = Field(ge=END_FADE_RANGE_S[0], le=END_FADE_RANGE_S[1])


def validate_color_moments(moments: list[ColorMoment], duration: float) -> None:
    """Sorted, non-overlapping, unique and inside the timeline (failure mode 7)."""
    if len(moments) > MAX_COLOR_MOMENTS:
        raise ValueError(f"at most {MAX_COLOR_MOMENTS} colour moments")
    if len({moment.id for moment in moments}) != len(moments):
        raise ValueError("colour moment ids must be unique")
    for moment in moments:
        if moment.end > duration + 1e-6:
            raise ValueError("a colour moment exceeds the timeline")
    for left, right in zip(moments, moments[1:]):
        if right.start < left.end - 1e-6:
            raise ValueError("colour moments must be sorted and must not overlap")
