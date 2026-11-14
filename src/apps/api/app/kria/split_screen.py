"""Timeline-owned simultaneous sections and their portable render geometry."""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class SplitLayout(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=160)
    panel_weights: list[float] = Field(default_factory=lambda: [1, 1], min_length=2, max_length=3)
    gutter: float = Field(default=0, ge=0, le=0.1)
    background_color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    # Protocol 47: an ended panel gives its height to the panels still playing. Omitted
    # from the wire when false so every pre-collapse section keeps its exact bytes.
    collapse_empty_panels: bool = False

    @model_serializer(mode="wrap")
    def omit_default_collapse(self, handler):
        payload = handler(self)
        if not self.collapse_empty_panels:
            payload.pop("collapse_empty_panels", None)
        return payload

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


BLANK_KIND = "blank"
_BLANK_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")  # kri667: a hex colour field


def is_blank_row(row: dict[str, Any]) -> bool:
    return row.get("media_kind") == BLANK_KIND


class RecipeSplitBlank(BaseModel):
    """A flat-colour block drawn in one panel for one window (protocol 51, `splitBlankPanel`)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=160)
    panel_index: int = Field(ge=0, le=2, strict=True)
    start: float = Field(ge=0)
    end: float = Field(gt=0, le=1800)
    color: str = Field(pattern=_BLANK_COLOR.pattern)

    @model_validator(mode="after")
    def positive_window(self):
        if self.end <= self.start:
            raise ValueError("blank block must have a positive window")
        self.color = self.color.upper()
        return self


class RecipeSplitScreenSection(SplitLayout):
    start: float = Field(ge=0)
    end: float = Field(gt=0, le=1800)
    # Omitted from the wire when empty so every section without a blank keeps its exact bytes.
    blanks: list[RecipeSplitBlank] = Field(default_factory=list, max_length=300)

    @model_serializer(mode="wrap")
    def omit_default_collapse(self, handler):
        payload = handler(self)
        if not self.collapse_empty_panels:
            payload.pop("collapse_empty_panels", None)
        if not self.blanks:
            payload.pop("blanks", None)
        return payload

    @model_validator(mode="after")
    def positive_window(self):
        if self.end <= self.start:
            raise ValueError("split section must have a positive window")
        for blank in self.blanks:
            if (
                blank.panel_index >= len(self.panel_weights)
                or blank.start < self.start - 1e-6
                or blank.end > self.end + 1e-6
            ):
                raise ValueError("blank block must sit inside its section and panel")
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


def collapsed_section_durations(
    rows: list[dict[str, Any]], sections: list[SplitScreenSection]
) -> dict[str, float]:
    """A collapsing section lasts until its longest member ends."""
    collapsing = {section.id for section in sections if section.collapse_empty_panels}
    ends: dict[str, float] = {}
    for row in rows:
        section_id = row.get("split_section_id")
        if row.get("removed") or section_id not in collapsing:
            continue
        offset, duration = row.get("split_offset_s"), row.get("duration_s")
        if all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in (offset, duration)
        ):
            ends[section_id] = max(ends.get(section_id, 0.0), float(offset) + float(duration))
    return ends


def sync_collapsed_section_durations(
    rows: list[dict[str, Any]], sections: list[dict[str, Any]]
) -> None:
    """Rewrite stored `duration_s` of collapsing sections to the derived duration, in place."""
    parsed = [SplitScreenSection.model_validate(section) for section in sections]
    ends = collapsed_section_durations(rows, parsed)
    for section in sections:
        if section["id"] in ends:
            section["duration_s"] = max(0.1, ends[section["id"]])


def layout_split_timeline(
    rows: list[dict[str, Any]], sections: list[SplitScreenSection | dict[str, Any]]
) -> tuple[list[float], list[RecipeSplitScreenSection], float]:
    """Validate ownership and return each active occurrence's output start."""
    parsed = [SplitScreenSection.model_validate(section) for section in sections]
    by_id = {section.id: section for section in parsed}
    if len(by_id) != len(parsed):
        raise ValueError("split section ids must be unique")
    derived = collapsed_section_durations(rows, parsed)
    by_id = {
        key: section.model_copy(update={"duration_s": max(0.1, derived[key])})
        if key in derived
        else section
        for key, section in by_id.items()
    }
    placed: dict[str, RecipeSplitScreenSection] = {}
    with_media: set[str] = set()
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
        blank = is_blank_row(row)
        if blank:
            if all(value is None for value in values):
                raise ValueError("a blank block belongs in a split panel")
            if row.get("clip_index") is not None:
                raise ValueError("a blank block has no source clip")
            if not isinstance(row.get("color"), str) or not _BLANK_COLOR.match(row["color"]):
                raise ValueError("a blank block needs a #RRGGBB colour")
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
            if blank:
                placed[section_id].blanks.append(
                    RecipeSplitBlank(
                        id=str(row.get("slot_id") or f"blank-{len(placed[section_id].blanks)}"),
                        panel_index=panel,
                        start=placed[section_id].start + offset,
                        end=placed[section_id].start + offset + duration,
                        color=row["color"],
                    )
                )
            else:
                with_media.add(section_id)
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
                if is_blank_row(prior) or is_blank_row(row):
                    raise ValueError("a blank block cannot overlap another clip in its panel")
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
    if set(placed) != with_media:
        raise ValueError("every split section needs a clip, not only blank blocks")
    return starts, list(placed.values()), cursor


def has_timeline_transition_successor(rows: list[dict[str, Any]], row: dict[str, Any]) -> bool:
    active = [candidate for candidate in rows if not candidate.get("removed")]
    if row.get("split_section_id"):
        return any(
            candidate.get("split_section_id") == row["split_section_id"]
            and candidate.get("split_panel_index") == row["split_panel_index"]
            and float(candidate["split_offset_s"]) > float(row["split_offset_s"])
            for candidate in active
        )
    return bool(active) and active[-1] is not row


def set_timeline_transition(
    rows: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    row: dict[str, Any],
    transition: str,
    duration: float | None,
    *,
    validate: bool = True,
) -> None:
    """Apply an outgoing transition without changing source windows or peer clocks."""
    patch = {
        "transition_after": transition,
        "transition_duration_s": None if transition == "cut" else duration,
    }
    if not row.get("split_section_id"):
        row.update(patch)
        return
    later = sorted(
        [
            candidate
            for candidate in rows
            if not candidate.get("removed")
            and candidate.get("split_section_id") == row["split_section_id"]
            and candidate.get("split_panel_index") == row["split_panel_index"]
            and float(candidate["split_offset_s"]) > float(row["split_offset_s"])
        ],
        key=lambda candidate: float(candidate["split_offset_s"]),
    )
    shifted = {}
    if later:
        end = float(row["split_offset_s"]) + float(row["duration_s"])
        next_start = float(later[0]["split_offset_s"])
        # An authored gap stays a gap. At an actual join the suffix moves as
        # one unit so later transitions and deliberate gaps retain their lengths.
        if next_start <= end + 1e-6:
            old_overlap = max(0.0, end - next_start)
            new_overlap = transition_overlap({**row, **patch}, later[0])
            delta = old_overlap - new_overlap
            shifted = {
                id(candidate): float(candidate["split_offset_s"]) + delta for candidate in later
            }
    proposed = [
        {
            **candidate,
            **(patch if candidate is row else {}),
            **({"split_offset_s": shifted[id(candidate)]} if id(candidate) in shifted else {}),
        }
        for candidate in rows
    ]
    # Validate before mutating: removing an overlap must never trim a suffix
    # that no longer fits or silently extend the section.
    if validate:
        layout_split_timeline(proposed, sections)
    row.update(patch)
    for candidate in later:
        if id(candidate) in shifted:
            candidate["split_offset_s"] = shifted[id(candidate)]
