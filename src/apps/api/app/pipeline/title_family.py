"""KRI-1633: which phone title family a row id belongs to. Skia-free, so the editor diff and the
suggestion eval can read it without loading the renderer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
