"""Shorten creator or model text for a question the creator reads, without cutting a word."""

from __future__ import annotations

from collections.abc import Callable, Sequence

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


# A reason or a line is never shortened below this, so it keeps its first few words.
_MIN_PART_CHARS = 24


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


def _shorten_line(line: str, reason_cap: int, line_cap: int | None) -> str:
    """``line`` with its trailing "(reason)" cut to ``reason_cap`` characters at a word (the ask
    a receipt row names stays whole); a line with no reason only when ``line_cap`` is set."""
    start = _reason_start(line)
    if start is None:
        return line if line_cap is None else trim_at_word(line, line_cap)
    reason = line[start + 1 : -1]
    return f"{line[: start + 1]}{trim_at_word(reason, reason_cap)})"


def _largest_cap(fits: Callable[[int], bool], high: int) -> int | None:
    """The largest cap in [_MIN_PART_CHARS, high] that ``fits``, or None."""
    low = _MIN_PART_CHARS
    if high < low:
        return high if fits(high) else None
    if not fits(low):
        return None
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    return low


def _without_repeated_rows(text: str) -> list[str]:
    """``text``'s lines, a list row ("- ...") that repeats an earlier one left out."""
    seen: set[str] = set()
    lines = []
    for line in text.split("\n"):
        if line.startswith("- "):
            if line in seen:
                continue
            seen.add(line)
        lines.append(line)
    return lines


def fit_lines(text: str, limit: int, *, keep: Sequence[str] = ()) -> str:
    """``text`` within ``limit``, the longest lines shortened first, never mid-word (KRI-1408).

    A list row that repeats an earlier one is dropped first. Then every receipt reason longer than
    one shared cap is shortened to it at a word, the ask its row names kept whole, and the cap is
    the largest that fits: short reasons keep every word while the longest lose their tails, and
    headers, notices and the question stay whole. Only when the reasons cannot make room are the
    other lines shortened the same way; when even that cannot fit, the end is dropped at a whole
    word (`trim_at_word`). Rows starting with a ``keep`` prefix (what failed: "- Couldn't: ")
    keep their reasons until everything else is down to its first words.
    """
    if len(text) <= limit:
        return text
    # A line said twice (the same receipt listed twice) goes before any reason loses a word.
    lines = _without_repeated_rows(text)
    if len("\n".join(lines)) <= limit:
        return "\n".join(lines)

    def kept(line: str) -> bool:
        return bool(keep) and line.startswith(tuple(keep))

    def fitted(reason_cap: int, line_cap: int | None, kept_cap: int) -> str:
        return "\n".join(
            _shorten_line(line, kept_cap if kept(line) else reason_cap, line_cap) for line in lines
        )

    def reason_size(line: str) -> int:
        start = _reason_start(line)
        return 0 if start is None else len(line) - start - 2

    longest_reason = max((reason_size(line) for line in lines), default=0)
    longest_kept = max((reason_size(line) for line in lines if kept(line)), default=0)
    floor = min(_MIN_PART_CHARS, longest_reason)
    full = longest_kept
    longest_line = max(len(line) for line in lines)
    # In order, each step only once the one before is at its first words: other reasons, then
    # lines with no reason (headers, notices), then what failed. Asks always stay whole.
    steps: list[Callable[[int], str]] = [
        lambda c: fitted(c, None, full),
        lambda c: fitted(floor, c, full),
        lambda c: fitted(floor, _MIN_PART_CHARS, c),
    ]
    for step, high in zip(steps, (longest_reason, longest_line, longest_kept), strict=True):
        cap = _largest_cap(lambda c, step=step: len(step(c)) <= limit, high)
        if cap is not None:
            return step(cap)
    return trim_at_word("\n".join(lines), limit)
