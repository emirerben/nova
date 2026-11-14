"""Shorten creator or model text for a question the creator reads, without cutting a word."""

from __future__ import annotations

ELLIPSIS = "…"


def trim_at_word(text: str, limit: int) -> str:
    """``text`` unchanged when it fits in ``limit``; else its whole words plus an ellipsis.

    The result is never longer than ``limit`` and never ends mid-word ("…restart the
    kilometer t", KRI-552). One word longer than ``limit`` is cut, since there is no
    word boundary to cut at.
    """
    if len(text) <= limit:
        return text
    room = max(limit - len(ELLIPSIS), 0)
    # The cut falls between words when the first dropped character is whitespace.
    cut = room
    while cut > 0 and not text[cut].isspace():
        cut -= 1
    kept = text[:cut].rstrip() if cut > 0 else text[:room]
    return kept.rstrip(",;:-") + ELLIPSIS


# A line is never shortened below this, so a reason keeps at least a few words.
_MIN_LINE_CHARS = 24


def _reason_start(line: str) -> int | None:
    """Where the trailing ``" (reason)"`` of a receipt line opens, or None."""
    if not line.endswith(")"):
        return None
    depth = 0
    for i in range(len(line) - 1, -1, -1):
        if line[i] == ")":
            depth += 1
        elif line[i] == "(":
            depth -= 1
            if depth == 0:
                return i if i > 1 and line[i - 1] == " " else None
    return None


def _shorten_line(line: str, cap: int) -> str:
    """``line`` within ``cap``: a trailing "(reason)" loses words first, the ask it names
    stays; anything else is cut at a word."""
    if len(line) <= cap:
        return line
    start = _reason_start(line)
    if start is not None:
        room = cap - start - 2  # "(" and ")"
        if room >= len(ELLIPSIS) + 1:
            return f"{line[: start + 1]}{trim_at_word(line[start + 1 : -1], room)})"
        if start - 1 <= cap:
            return line[: start - 1]
    return trim_at_word(line, cap)


def fit_lines(text: str, limit: int) -> str:
    """``text`` within ``limit``, the longest lines shortened first, never mid-word (KRI-1408).

    Every line longer than one shared cap is shortened to it (a receipt's "(reason)" before the
    ask it names), and the cap is the largest that fits, so short lines (labels, a question)
    keep every word while one long reason loses its tail. When even short lines cannot fit, the
    end is dropped at a whole word (`trim_at_word`).
    """
    if len(text) <= limit:
        return text
    lines = text.split("\n")

    def size(cap: int) -> int:
        return sum(len(_shorten_line(line, cap)) for line in lines) + len(lines) - 1

    low, high = _MIN_LINE_CHARS, max(len(line) for line in lines)
    if low > high or size(low) > limit:
        return trim_at_word(text, limit)
    while low < high:  # the largest cap that fits
        mid = (low + high + 1) // 2
        if size(mid) <= limit:
            low = mid
        else:
            high = mid - 1
    return "\n".join(_shorten_line(line, low) for line in lines)
