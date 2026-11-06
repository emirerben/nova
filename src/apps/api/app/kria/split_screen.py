"""Timeline-owned simultaneous sections and their portable render geometry."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SplitLayout(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=160)
    panel_weights: list[float] = Field(default_factory=lambda: [1, 1], min_length=2, max_length=3)
    gutter: float = Field(default=0, ge=0, le=0.1)
    background_color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def valid_weights(self):
        if any(weight <= 0 for weight in self.panel_weights) or not math.isfinite(
            sum(self.panel_weights)
        ):
            raise ValueError("panel weights must be positive")
        self.background_color = self.background_color.upper()
        return self


class SplitScreenSection(SplitLayout):
    duration_s: float = Field(ge=0.1, le=1800)
    panel_count: Literal[2, 3]

    @model_validator(mode="before")
    @classmethod
    def default_weights(cls, value):
        if isinstance(value, dict) and "panel_weights" not in value:
            value = {**value, "panel_weights": [1] * (3 if value.get("panel_count") == 3 else 2)}
        return value

    @model_validator(mode="after")
    def matching_panels(self):
        if len(self.panel_weights) != self.panel_count:
            raise ValueError("panel weights must match panel count")
        return self


class RecipeSplitScreenSection(SplitLayout):
    start: float = Field(ge=0)
    end: float = Field(gt=0, le=1800)

    @model_validator(mode="after")
    def positive_window(self):
        if self.end <= self.start:
            raise ValueError("split section must have a positive window")
        return self


class SplitScreenMembership(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(min_length=1, max_length=160)
    panel_index: int = Field(ge=0, le=2)


def transition_overlap(left: dict[str, Any], right: dict[str, Any]) -> float:
    if left.get("transition_after", "cut") == "cut":
        return 0.0
    overlap = min(
        float(left.get("transition_duration_s") or 0.3),
        0.3,
        float(left["duration_s"]) * 0.3,
        float(right["duration_s"]) * 0.3,
    )
    return overlap if overlap >= 0.1 else 0.0


def layout_split_timeline(
    rows: list[dict[str, Any]], sections: list[SplitScreenSection | dict[str, Any]]
) -> tuple[list[float], list[RecipeSplitScreenSection], float]:
    """Validate ownership and return each active occurrence's output start."""
    parsed = [SplitScreenSection.model_validate(section) for section in sections]
    by_id = {section.id: section for section in parsed}
    if len(by_id) != len(parsed):
        raise ValueError("split section ids must be unique")
    placed: dict[str, RecipeSplitScreenSection] = {}
    coverage: dict[tuple[str, int], list[tuple[float, float, dict]]] = {}
    previous_row = None
    starts = []
    cursor = 0.0
    previous_group = None
    for row in rows:
        if row.get("removed"):
            continue
        duration = float(row.get("duration_s") or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("clip duration must be finite and positive")
        values = [
            row.get(key) for key in ("split_section_id", "split_panel_index", "split_offset_s")
        ]
        section_id, panel, offset = values
        if any(value is not None for value in values):
            if any(value is None for value in values):
                raise ValueError("split membership requires section, panel and offset")
            section = by_id.get(section_id)
            if section is None:
                raise ValueError("clip references an unknown split section")
            if (
                isinstance(panel, bool)
                or not isinstance(panel, int)
                or not 0 <= panel < section.panel_count
            ):
                raise ValueError("split panel index is outside its section")
            if (
                isinstance(offset, bool)
                or not isinstance(offset, (float, int))
                or not math.isfinite(offset)
                or offset < 0
                or offset + duration > section.duration_s + 1e-6
            ):
                raise ValueError("split clip must fit within its section")
            if section_id in placed and previous_group != section_id:
                raise ValueError("split section members must be contiguous")
            if section_id not in placed:
                placed[section_id] = RecipeSplitScreenSection(
                    **section.model_dump(exclude={"duration_s", "panel_count"}),
                    start=cursor,
                    end=cursor + section.duration_s,
                )
                cursor += section.duration_s
            windows = coverage.setdefault((section_id, panel), [])
            windows.append((offset, offset + duration, row))
            starts.append(placed[section_id].start + offset)
        else:
            if previous_row is not None and previous_group is None:
                cursor -= transition_overlap(previous_row, row)
            starts.append(cursor)
            cursor += duration
        previous_group = section_id
        previous_row = row
    for windows in coverage.values():
        ordered = sorted(windows, key=lambda window: window[0])
        for index, (start, end, row) in enumerate(ordered):
            for prior_index, (prior_start, prior_end, prior) in enumerate(ordered[:index]):
                if start >= prior_end - 1e-6:
                    continue
                overlap = transition_overlap(prior, row)
                if (
                    prior_index != index - 1
                    or start <= prior_start
                    or overlap == 0
                    or prior_end - start > overlap + 1e-6
                ):
                    raise ValueError("split panel overlap must match its preceding transition")
    if set(placed) != set(by_id):
        raise ValueError("every split section needs a timeline member")
    return starts, list(placed.values()), cursor
