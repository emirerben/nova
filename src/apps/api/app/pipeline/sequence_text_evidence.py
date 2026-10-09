"""Evidence helpers for text split into compiler-owned sequence fragments.

Sequence fragments are trusted only when their immutable ids prove one source
and consecutive ordinals.  This module deliberately does not join arbitrary
nearby text layers.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from app.pipeline.cloud_render_evidence import normalize_text

_SEQUENCE_ID = re.compile(r"^(?P<source>.+)::sequence-(?P<ordinal>[1-9][0-9]*)$")


def sequence_lineage(row: Mapping[str, Any]) -> tuple[str, int] | None:
    """Return the source id and ordinal encoded by a sequence child id."""

    row_id = row.get("element_id", row.get("id"))
    if not isinstance(row_id, str):
        return None
    match = _SEQUENCE_ID.fullmatch(row_id)
    if match is None:
        return None
    source = match.group("source")
    # Nested sequences are not safely composable: fail closed.
    if "::sequence-" in source:
        return None
    params = row.get("source_params")
    declared = row.get("sequence_source_id")
    if not isinstance(declared, str) and isinstance(params, Mapping):
        declared = params.get("sequence_source_id")
    if declared is not None and declared != source:
        return None
    return source, int(match.group("ordinal"))


def sequence_role(row: Mapping[str, Any]) -> str:
    """Classify a sequence's source lane from compiler-owned lineage."""

    lineage = sequence_lineage(row)
    source = lineage[0] if lineage is not None else row.get("element_id", row.get("id", ""))
    if isinstance(source, str) and source.startswith("text-"):
        source = source[5:]
    if source == "guided-title":
        return "opening"
    if source == "guided-closing-title":
        return "closing"
    if isinstance(source, str) and source.startswith(("clip-label-", "montage-text-")):
        return "clip"
    return "any"


def text_matches(
    rows: Iterable[Mapping[str, Any]],
    wanted: str,
    *,
    role: str = "any",
    tolerance_s: float = 0.1,
    allow_missing_role: bool = True,
) -> list[dict[str, Any]]:
    """Find exact visible text, including a valid complete fragment sequence."""

    materialized = [dict(row) for row in rows]
    wanted_normalized = normalize_text(wanted)
    matches: list[dict[str, Any]] = []
    for row in materialized:
        if normalize_text(row.get("text", "")) != wanted_normalized:
            continue
        if (
            role != "any"
            and row.get("role") != role
            and not (allow_missing_role and row.get("role") is None)
        ):
            continue
        matches.append(row)

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in materialized:
        lineage = sequence_lineage(row)
        if lineage is not None:
            groups.setdefault(lineage[0], []).append(row)
    for source, members in groups.items():
        parsed = [(sequence_lineage(row), row) for row in members]
        if any(item[0] is None for item in parsed):
            continue
        parsed.sort(key=lambda item: item[0][1])  # type: ignore[index]
        ordinals = [item[0][1] for item in parsed]  # type: ignore[index]
        if ordinals != list(range(1, len(ordinals) + 1)):
            continue
        chronological = sorted(
            (row for _lineage, row in parsed),
            key=lambda row: float(row.get("start", row.get("start_s", 0))),
        )
        if [sequence_lineage(row)[1] for row in chronological] != ordinals:  # type: ignore[index]
            continue
        roles = {row.get("role") for row in chronological}
        if len(roles) > 1:
            continue
        media_ids = {row.get("media_id") for row in chronological}
        if len(media_ids) > 1:
            continue
        previous_end: float | None = None
        for row in chronological:
            text = normalize_text(row.get("text", ""))
            if not text:
                break
            start = float(row.get("start", row.get("start_s", 0)))
            end = float(row.get("end", row.get("end_s", 0)))
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or end <= start
                or (previous_end is not None and abs(start - previous_end) > tolerance_s)
            ):
                break
            previous_end = end
        else:
            composed = " ".join(normalize_text(row["text"]) for row in chronological)
            first = chronological[0]
            inherited_role = first.get("role") or sequence_role(first)
            if (
                role != "any"
                and inherited_role != role
                and not (allow_missing_role and inherited_role is None)
            ):
                continue
            if normalize_text(composed) == wanted_normalized:
                start_key = "start" if "start" in first else "start_s"
                end_key = "end" if "end" in first else "end_s"
                matches.append(
                    {
                        "text": composed,
                        start_key: float(first[start_key]),
                        end_key: float(chronological[-1][end_key]),
                        "role": inherited_role or "any",
                        "media_id": first.get("media_id"),
                        "sequence_source_id": source,
                        "sequence_ordinals": ordinals,
                    }
                )
    return matches
