"""Shared text wrapping helpers.

Karaoke renderers need to preserve per-word timing payloads, so they cannot
use generic paragraph wrappers that return only strings. This module keeps the
layout decision pure: callers provide a text measurement function for their
renderer, and receive original word indices grouped into balanced lines.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence


def balanced_word_wrap_indices(
    words: Sequence[str],
    measure_text: Callable[[str], float],
    max_width: float,
) -> list[list[int]]:
    """Wrap word indices with the minimum feasible line count, then balance.

    Greedy wrapping fills the first line until it hits ``max_width`` and can
    leave ugly tails like ``8 + 1`` words. This helper first finds the minimum
    number of lines needed to fit the words, then chooses the partition with
    better word-count and width balance. A single word is always feasible even
    when it exceeds ``max_width``; shrink-to-fit remains the caller's job.
    """
    n = len(words)
    if n == 0:
        return []
    if max_width <= 0:
        return [[i] for i in range(n)]

    width_cache: dict[tuple[int, int], float] = {}

    def width(start: int, end: int) -> float:
        key = (start, end)
        cached = width_cache.get(key)
        if cached is not None:
            return cached
        measured = float(measure_text(" ".join(words[start:end])))
        width_cache[key] = measured
        return measured

    def feasible(start: int, end: int) -> bool:
        return end == start + 1 or width(start, end) <= max_width

    # Minimum line count, independent of balance scoring.
    inf = n + 1
    min_lines_to: list[int] = [inf] * (n + 1)
    min_lines_to[0] = 0
    for end in range(1, n + 1):
        for start in range(end):
            if min_lines_to[start] != inf and feasible(start, end):
                min_lines_to[end] = min(min_lines_to[end], min_lines_to[start] + 1)

    line_count = min_lines_to[n]
    if line_count == inf:
        return [[i] for i in range(n)]
    if line_count == 1:
        return [list(range(n))]

    ideal_count = n / line_count

    def segment_cost(start: int, end: int) -> float:
        count = end - start
        segment_width = width(start, end)
        slack_ratio = max(0.0, max_width - segment_width) / max_width
        count_ratio = (count - ideal_count) / max(1.0, ideal_count)
        orphan_penalty = 1000.0 if n > 3 and count == 1 else 0.0
        return orphan_penalty + (8.0 * count_ratio * count_ratio) + (
            slack_ratio * slack_ratio
        )

    # dp[(end, lines)] = (cost, partition)
    dp: dict[tuple[int, int], tuple[float, list[list[int]]]] = {(0, 0): (0.0, [])}
    for end in range(1, n + 1):
        for lines_used in range(1, min(line_count, end) + 1):
            best: tuple[float, list[list[int]]] | None = None
            for start in range(lines_used - 1, end):
                prev = dp.get((start, lines_used - 1))
                if prev is None or not feasible(start, end):
                    continue
                prev_cost, prev_partition = prev
                candidate_partition = [*prev_partition, list(range(start, end))]
                candidate = (
                    prev_cost + segment_cost(start, end),
                    candidate_partition,
                )
                if best is None or candidate[0] < best[0]:
                    best = candidate
            if best is not None:
                dp[(end, lines_used)] = best

    result = dp.get((n, line_count))
    if result is None:
        return [[i] for i in range(n)]
    return result[1]
