"""Tappable answers for a length question (KRI-738).

When the edit copilot asks "how short should they be?" it records the 2-3 lengths it offers as
structured ``length_options`` (a short label plus the seconds it stands for), next to the
question text. This module keeps only options the creator can see offered: the model's own
reply is the one shown, and the option's seconds appear in it. Nothing here reads meaning from
the wording of a message (KRI-667): the model states the fact, the server checks it.

The tap sends the option's label as an ordinary message; the copilot's ANSWER rule resolves it
against the options in its previous reply.
"""

from __future__ import annotations

from typing import Any

MAX_OPTIONS = 4
_MIN_SECONDS = 0.1  # editor_ops_v2.timeline.MIN_CLIP_S
_MAX_LABEL_CHARS = 48
_MAX_SECONDS = 120.0


def _stated(seconds: float, reply: str) -> bool:
    number = f"{seconds:g}"
    return number in reply or number.replace(".", ",") in reply


def length_options(raw: Any, reply: str) -> list[dict[str, Any]]:
    """The ``[{"label", "seconds"}]`` options the ``reply`` itself offers, or ``[]``.

    Fewer than two is not a choice, so it yields none.
    """
    if not isinstance(raw, list):
        return []
    kept: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get("label") or "").split())
        seconds = item.get("seconds")
        if (
            not label
            or len(label) > _MAX_LABEL_CHARS
            or isinstance(seconds, bool)
            or not isinstance(seconds, int | float)
            or not _MIN_SECONDS <= seconds <= _MAX_SECONDS
            or not _stated(float(seconds), reply)
        ):
            continue
        if label.casefold() in {option["label"].casefold() for option in kept}:
            continue
        kept.append({"label": label, "seconds": float(seconds)})
    return kept[:MAX_OPTIONS] if len(kept) >= 2 else []
