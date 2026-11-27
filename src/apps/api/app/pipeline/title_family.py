"""KRI-1633: which phone title family a row id belongs to. Skia-free, so the editor diff and the
suggestion eval can read it without loading the renderer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Talking, recorded-voiceover and guided/montage story titles. A stack's other lines carry
# ``<family>-line-N``. The first id must equal ``phone_subtitled_title.TALKING_TITLE_ELEMENT_ID``
# (asserted where that module is imported, in ``phone_title_look``).
TITLE_FAMILY_IDS = ("opening-title", "narrated-title", "guided-title")
TITLE_LOOK_PARAM = "title_look"
_MIN_LINES = 2


def title_family_of(row_id: object) -> str | None:
    """The family a title row id belongs to (``opening-title``, ``narrated-title``,
    ``guided-title``, each optionally followed by ``-line-N``), else ``None``."""
    text = str(row_id or "")
    for family in TITLE_FAMILY_IDS:
        if text == family or (
            text.startswith(f"{family}-line-") and text[len(family) + 6 :].isdigit()
        ):
            return family
    return None


def title_look_line_ids(bar_ids: Sequence[str], family: str | None = None) -> list[str]:
    """The ids a stacked title's lines carry (`opening-title`, `opening-title-line-N`, and the
    narrated / guided families), in the order given. A whole-title edit addresses every one of
    them: all of ONE family (``family``, else the first one present), never lines of two."""
    families = [title_family_of(i) for i in bar_ids]
    present = family or next((f for f in TITLE_FAMILY_IDS if f in families), None)
    return [i for i, family in zip(bar_ids, families, strict=True) if present and family == present]


def _look_params(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    params = row.get("source_params")
    look = params.get(TITLE_LOOK_PARAM) if isinstance(params, Mapping) else None
    return look if isinstance(look, Mapping) else None


def is_title_look_row(row: Mapping[str, Any]) -> bool:
    return _look_params(row) is not None


# The shortest a title line is shown (`phone_subtitled_title._TITLE_MIN_S / 5`).
MIN_LINE_S = 0.1


def title_window(rows: Sequence[Mapping[str, Any]]) -> tuple[float, float]:
    """When one title is on screen: its first line's entrance to its last line's exit (KRI-1618:
    an accent line may enter after the others)."""
    return (
        min(float(row["start_s"]) for row in rows),
        max(float(row["end_s"]) for row in rows),
    )


def _declared_delay(row: Mapping[str, Any]) -> float:
    from app.pipeline.intro_scene import STAGGERS  # noqa: PLC0415

    look = _look_params(row) or {}
    if look.get("style") != "intro_accent":
        return 0.0
    return STAGGERS.get(str(look.get("stagger")), 0.0)


def retime_title_rows(
    rows: Sequence[Mapping[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    """One title's lines shown over ``[start, end]``: an intro accent line waits the stagger it
    declares (`intro_scene.STAGGERS`), every other line enters at ``start``, and all leave at
    ``end``. A window too short for the wait shows every line together. The times come from the
    declaration, never from the rows' current offsets, so a cut that bent them is undone and
    retiming twice gives the same rows."""
    delays = [_declared_delay(row) for row in rows]
    if end - start - max(delays, default=0.0) < MIN_LINE_S:
        delays = [0.0] * len(delays)
    return [
        {**row, "start_s": start + delay, "end_s": end}
        for row, delay in zip(rows, delays, strict=True)
    ]


def retime_title_stacks(rows: Sequence[Any]) -> list[Any]:
    """``rows`` with each stacked title that declares a stagger retimed over its own window
    (`retime_title_rows`). For a lane the system moved row by row (a Range cut re-anchors each
    text row through source time on its own), so a late accent line is back at its delay. Rows
    of a stack that declares nothing, and every other row, are returned as they were."""
    out = list(rows)
    for family in TITLE_FAMILY_IDS:
        lines = [
            index
            for index, row in enumerate(out)
            if isinstance(row, Mapping)
            and not row.get("removed")
            and title_family_of(row.get("id")) == family
            and is_title_look_row(row)
        ]
        stack = [out[index] for index in lines]
        if len(stack) < _MIN_LINES or not any(_declared_delay(row) for row in stack):
            continue
        for index, row in zip(lines, retime_title_rows(stack, *title_window(stack)), strict=True):
            out[index] = row
    return out


# KRI-1613: a sound-effect lane row an intro scene owns carries this prefix plus the id of the
# line it sits on; it is moved with that line and dropped with its declaration.
INTRO_SOUND_ID_PREFIX = "introsfx-"


@dataclass(frozen=True)
class IntroSoundCue:
    id: str  # the lane row's id: ``introsfx-<line id>``
    sound: str  # a slug of the seeded creator sound library
    at_s: float  # the line's entrance (its ``start_s``)


def intro_sound_cues(rows: Sequence[Any]) -> list[IntroSoundCue]:
    """The sounds the intro scene lines in ``rows`` declare (`intro_scene.SOUND_CUES`), in time
    order: one per entrance instant of the lines the cue sounds on, on the first of those lines
    in line order. Lines that enter on the same frame are one entrance and share one sound."""
    from app.pipeline.intro_scene import SILENT, SOUND_CUES, SOUNDS  # noqa: PLC0415

    cues: dict[str, IntroSoundCue] = {}
    for family in TITLE_FAMILY_IDS:
        lines = sorted(
            (
                row
                for row in rows
                if isinstance(row, Mapping)
                and not row.get("removed")
                and title_family_of(row.get("id")) == family
                and isinstance(row.get("start_s"), int | float)
            ),
            key=lambda row: int((_look_params(row) or {}).get("line") or 0),
        )
        instants: set[float] = set()
        for row in lines:
            look = _look_params(row) or {}
            sound, cue = look.get("sound"), look.get("sound_cue")
            if sound == SILENT or sound not in SOUNDS or cue not in SOUND_CUES:
                continue
            at = round(float(row["start_s"]), 3)
            if look.get("style") in SOUND_CUES[cue] and at not in instants:
                instants.add(at)
                cue_id = f"{INTRO_SOUND_ID_PREFIX}{row['id']}"
                cues[cue_id] = IntroSoundCue(id=cue_id, sound=str(sound), at_s=at)
    return sorted(cues.values(), key=lambda cue: (cue.at_s, cue.id))


def issued_delay(delay: float, *, tolerance_s: float) -> bool:
    """Is ``delay`` (a line's entrance after the title's first) one the engine issues?"""
    from app.pipeline.intro_scene import STAGGERS  # noqa: PLC0415

    return any(abs(delay - issued) <= tolerance_s for issued in STAGGERS.values())


def title_stack_rows(live: Sequence[dict[str, Any]], row: dict[str, Any]) -> list[dict[str, Any]]:
    """KRI-1633: every line (in line order) of the stacked title ``row`` heads, else ``[row]``.
    Only a family's first-line id heads a stack, so an edit aimed at one ``-line-N`` stays a line
    edit; an edit aimed at the title itself (index op, selector id) means the whole stack, as the
    Review Plan shows it (`plan_review._title_ids`). ``live`` are the rows not removed."""
    family = title_family_of(row.get("id"))
    if family is None or row.get("id") != family or not is_title_look_row(row):
        return [row]
    stack = [r for r in live if title_family_of(r.get("id")) == family and is_title_look_row(r)]
    if len(stack) < _MIN_LINES or not any(r is row for r in stack):
        return [row]
    return sorted(stack, key=lambda r: int((_look_params(r) or {}).get("line") or 0))
