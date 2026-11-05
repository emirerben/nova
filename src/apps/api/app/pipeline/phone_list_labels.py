"""Numbered list labels on a phone Talking edit (Qendresa item 19).

"Add a label when each one starts: "1. Spec-kit", "2. Impeccable", ...": the brief holds each
label as a ``text`` requirement with no spoken trigger (`app.kria.brief.BriefRequirement`). Her
screen recordings take turns behind her (``background_media.sequence``, laid out by
`phone_background_sequence.lay_out`), so a label belongs where its item's recording is
introduced -- NOT at the first time the word is ever said ("agents" inside "trading agents").

This module is pure (no I/O, no DB, no settings): it pairs each label with a recording by what
it says (`match_labels`), takes the recording's start (`label_start`) or, with no recording to
follow, grounds the item on the take's own words (`ground_on_words`), and builds one ordinary
editable ``TextElement`` row per placed label plus a receipt (`list_label_rows`). It mirrors
`app.pipeline.phone_spoken_text` (rows + receipt, a ``place`` callback, never raises for one
ask) and rides the same KRI-467 text lane, so it needs no new phone capability.

No ``re`` and no word tests on lowered text (KRI-667): list markers are read with ``str``
methods and names are compared as `phone_reaction_grounding.fold_tokens` tokens.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.pipeline.narrated_title import narrated_title_placement
from app.pipeline.render_geometry import NormalizedBox

log = structlog.get_logger(__name__)

LIST_LABEL_SOURCE = "list_label"
LIST_LABEL_ELEMENT_PREFIX = "list-label-"
# The variant's receipt of every list-label ask: placed (where and when) or why not.
LIST_LABEL_FIELD = "list_label_placement"

# Her labels stay 2.5-3 s; the shortest worth drawing; her spot (bottom, centred).
HOLD_S = 3.0
MIN_S = 0.3
LABEL_Y_FRAC = 0.86
_MAX_CHARS = 80
_EPS = 1e-6
_DIGITS = "0123456789"
_MARKER_PUNCTUATION = ".:)"
_MIN_CONTAINED_LETTERS = 4
_MIN_OVERLAP_TOKENS = 2
_OVERLAP_SHARE = 2.0 / 3.0

# Match tiers, best first.
_T_LOOSE, _T_CONTAINED, _T_OVERLAP = 3, 2, 1


@dataclass(frozen=True)
class ListLabelAsk:
    requirement_id: str
    text: str  # the literal as shown, whitespace-collapsed
    item: str  # the literal without its leading list marker


@dataclass(frozen=True)
class LabelAnchor:
    index: int  # background sequence entry index
    spoken_phrase: str
    anchor_s: float  # where the entry is said (lay_out's anchor), row timeline
    window_start_s: float | None  # start of the entry's first window ending after anchor_s


def _collapse(text: object) -> str:
    return " ".join(str(text or "").split())


def _read_marker(text: str) -> tuple[int, bool]:
    """(length of the leading list marker in ``text``, whether it is explicit).

    A marker is an optional "#", 1-2 digits, then an optional single ".", ":" or ")". It is
    *explicit* when "#" or that punctuation is there; a bare "5 PROJECTS" drops its number
    too (``label_item``) but is not by itself a sign that the text is a list label."""
    i = 1 if text.startswith("#") else 0
    j = i
    while j < len(text) and j - i < 2 and text[j] in _DIGITS:
        j += 1
    if j == i:
        return 0, False
    if j < len(text) and text[j] in _DIGITS:  # three digits: a year or a count, not a marker
        return 0, False
    explicit = i == 1
    if j < len(text) and text[j] in _MARKER_PUNCTUATION:
        j += 1
        explicit = True
    elif j < len(text) and not text[j].isspace():  # "1st", "3D": part of the word
        return 0, False
    return j, explicit


def label_item(literal: str) -> str:
    """``literal`` without its ONE leading list marker ("1. Spec-kit" -> "Spec-kit")."""
    text = _collapse(literal)
    length, _explicit = _read_marker(text)
    return text[length:].strip() if length else text


def _has_explicit_marker(text: str) -> bool:
    return _read_marker(_collapse(text))[1]


def list_label_asks(requirements: Iterable[Any]) -> tuple[ListLabelAsk, ...]:
    """Every live global ``text`` requirement that no spoken trigger owns, in brief order.

    Being listed does NOT make it a label: only a content match (`match_labels`) or, for a text
    with a list marker, the take's words (`ground_on_words`) does. The title and the closing text
    are listed too and come out of `list_label_rows` as ``not_a_list_item``."""
    from app.kria.spoken_text_ask import requirement_spoken_text  # noqa: PLC0415

    asks: list[ListLabelAsk] = []
    for req in requirements:
        if not req.live or req.kind != "text" or req.scope != "global":
            continue
        text = _collapse(req.literal)
        if not text:
            continue
        if requirement_spoken_text(req) is not None:
            continue  # KRI-707's: never drawn twice
        item = label_item(text)
        if not any(char.isalnum() for char in item):
            continue
        asks.append(ListLabelAsk(requirement_id=req.id, text=text, item=item))
    return tuple(asks)


# ---------------------------------------------------------------------------------------------
# Pairing a label with a recording
# ---------------------------------------------------------------------------------------------


def _split_camel(text: str) -> str:
    out: list[str] = []
    previous = ""
    for char in text:
        if previous and previous.islower() and char.isupper():
            out.append(" ")
        out.append(char)
        previous = char
    return "".join(out)


def _tokens(text: str) -> list[str]:
    """Folded tokens of a name: "TradingAgents" -> [trading, agents]; "-" is a space."""
    from app.services.phone_reaction_grounding import fold_tokens  # noqa: PLC0415

    return fold_tokens(_split_camel(text.replace("-", " ")))


def _same(a: str, b: str) -> bool:
    from app.services.phone_background_sequence import _strip_plural  # noqa: PLC0415

    return a == b or _strip_plural(a) == _strip_plural(b)


def _lcs(a: Sequence[str], b: Sequence[str]) -> int:
    table = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i, left in enumerate(a):
        for j, right in enumerate(b):
            table[i + 1][j + 1] = (
                table[i][j] + 1 if _same(left, right) else max(table[i][j + 1], table[i + 1][j])
            )
    return table[len(a)][len(b)]


def _contained(small: Sequence[str], big: Sequence[str]) -> bool:
    if not small or len(small) >= len(big):
        return False
    if sum(len(token) for token in small) < _MIN_CONTAINED_LETTERS:
        return False
    return any(
        all(_same(big[i + k], token) for k, token in enumerate(small))
        for i in range(len(big) - len(small) + 1)
    )


def _tier(label: Sequence[str], phrase: Sequence[str]) -> int:
    """How well a label's tokens name a recording's phrase: 3 loose-equal, 2 contained,
    1 overlapping, 0 not at all."""
    from app.services.phone_background_sequence import _align_loose  # noqa: PLC0415

    a, b = list(label), list(phrase)
    if not a or not b:
        return 0
    if (
        _align_loose(a, b, 0, 0) == len(b)
        or _align_loose(b, a, 0, 0) == len(a)
        or (len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True)))
    ):
        return _T_LOOSE
    if _contained(a, b) or _contained(b, a):
        return _T_CONTAINED
    longer = max(len(a), len(b))
    common = _lcs(a, b)
    if common >= _MIN_OVERLAP_TOKENS and common >= _OVERLAP_SHARE * longer - _EPS:
        return _T_OVERLAP
    return 0


def match_labels(
    asks: Sequence[ListLabelAsk], entries: Sequence[tuple[int, str]]
) -> dict[str, int]:
    """requirement_id -> the background entry index its label belongs to.

    ``entries`` are (sequence entry index, spoken_phrase) of the phrased entries. Greedy in brief
    order: a label takes, among entries nobody took, the best tier (loose-equal, contained,
    overlapping); a tie goes to the first entry after the previous label's entry, else the lowest
    index. An entry is never given to two labels; a label no entry names is absent."""
    folded = [(index, _tokens(phrase)) for index, phrase in entries if phrase]
    taken: set[int] = set()
    matched: dict[str, int] = {}
    previous: int | None = None
    for ask in asks:
        label = _tokens(ask.item)
        # A text with no list marker ("comment SPEC KIT", a closing line) only pairs when it IS
        # the recording's name: containing or overlapping it is not enough to become a label.
        lowest = _T_OVERLAP if _has_explicit_marker(ask.text) else _T_LOOSE
        scored = [
            (tier, index)
            for index, phrase in folded
            if index not in taken
            if (tier := _tier(label, phrase)) >= lowest
        ]
        if not scored:
            continue
        best = max(tier for tier, _index in scored)
        candidates = sorted(index for tier, index in scored if tier == best)
        after = [index for index in candidates if previous is not None and index > previous]
        chosen = after[0] if after else candidates[0]
        matched[ask.requirement_id] = chosen
        taken.add(chosen)
        previous = chosen
    return matched


# ---------------------------------------------------------------------------------------------
# No recording to follow: ground the item on the take's own words
# ---------------------------------------------------------------------------------------------


def _hearings(stream: Sequence[Any], timed: list[dict], item: str) -> list[tuple[float, float]]:
    from app.services import phone_background_sequence as sequence  # noqa: PLC0415
    from app.services import phone_reaction_grounding as grounding  # noqa: PLC0415

    phrase = _split_camel(item.replace("-", " "))
    exact = [
        (float(a), float(b)) for a, b, _heard in grounding._phrase_windows(stream, timed, phrase)
    ]  # noqa: SLF001
    return (
        exact
        or sequence._loose_hearings(stream, phrase)  # noqa: SLF001
        or sequence._plural_toggled_hearings(stream, timed, phrase)  # noqa: SLF001
    )


def ground_on_words(
    asks: Sequence[ListLabelAsk],
    words: Sequence[dict],
    *,
    after_s: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """requirement_id -> start_s of the item's hearing on ``words``' timeline.

    In label order, each item's first hearing at/after the previous label's chosen time (and its
    own ``after_s`` floor), found exactly / by KRI-961, else loosely, else with the plural
    toggled. A hearing strictly inside a longer hearing of another label ("agents" inside
    "trading agents") belongs to that label and is dropped. Not heard: absent."""
    from app.services import phone_reaction_grounding as grounding  # noqa: PLC0415

    timed = [dict(word) for word in words if isinstance(word, Mapping)]
    stream = grounding._token_stream(timed)  # noqa: SLF001
    folded = {ask.requirement_id: tuple(_tokens(ask.item)) for ask in asks}
    heard = {ask.requirement_id: _hearings(stream, timed, ask.item) for ask in asks}
    chosen: dict[str, float] = {}
    previous = 0.0
    for ask in asks:
        rid = ask.requirement_id
        floor = max(previous, float((after_s or {}).get(rid, 0.0)))
        free = [
            (start, end)
            for start, end in heard[rid]
            if not any(
                other != rid
                and len(folded[other]) > len(folded[rid])
                and o_start <= start + _EPS
                and end <= o_end + _EPS
                and o_end - o_start > end - start + _EPS
                for other, spans in heard.items()
                for o_start, o_end in spans
            )
        ]
        pick = next((start for start, _end in free if start >= floor - _EPS), None)
        if pick is not None:
            chosen[rid] = pick
            previous = pick
    return chosen


# ---------------------------------------------------------------------------------------------
# Timing and rows
# ---------------------------------------------------------------------------------------------


def label_start(anchor: LabelAnchor) -> float:
    """When the label appears: where the entry is said, or -- when its window opens LATER (two
    names said back to back share one mention) -- the window's start. Never the lead-in window's
    0 s start: the talk opens on its first visual before she names it."""
    window = anchor.window_start_s
    if window is not None and window > anchor.anchor_s + _EPS:
        return float(window)
    return float(anchor.anchor_s)


def _label_element(ask: ListLabelAsk, *, start_s: float, end_s: float, canvas: Any) -> dict:
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

    text = ask.text[:_MAX_CHARS]
    fields = {**narrated_title_placement(text, canvas=canvas, explicit=True)}
    fields["y_frac"] = LABEL_Y_FRAC  # her spot; the fitted size_px keeps a long label in width
    element = TextElement(
        id=f"{LIST_LABEL_ELEMENT_PREFIX}{ask.requirement_id}",
        text=text,
        start_s=start_s,
        end_s=end_s,
        role="generative_intro",
        **fields,
        effect="fade-in",
        source_params={"source": LIST_LABEL_SOURCE, "requirement_id": ask.requirement_id},
    )
    return element.model_dump(mode="json", exclude_none=True)


def list_label_rows(
    asks: Sequence[ListLabelAsk],
    *,
    anchors: Sequence[LabelAnchor],
    words: Sequence[dict],
    timeline_duration_s: float,
    canvas: Any,
    place: Callable[[dict, list[NormalizedBox]], tuple[dict, dict]],
    avoid_for: Callable[[tuple[float, float], list[dict]], list[NormalizedBox]],
) -> tuple[list[dict], dict[str, Any]]:
    """One placed row per label that has its moment, plus the receipt of every ask.

    Receipt statuses: placed, not_heard (a list label nobody introduced), no_time (a sliver or
    past the take end), same_moment (another label starts at the same instant), not_a_list_item
    (a text no recording or list marker makes a label: the title, the closing text) or error.
    ``place(row, avoid)`` moves a row off her face; ``avoid_for(window, rows_so_far)`` returns
    the caption lines, other text and cards on screen in that window. Never raises for one ask."""
    by_index = {anchor.index: anchor for anchor in anchors}
    matched = match_labels(asks, [(a.index, a.spoken_phrase) for a in anchors if a.spoken_phrase])
    starts: dict[str, float] = {}
    basis: dict[str, dict[str, Any]] = {}
    for ask in asks:
        entry = matched.get(ask.requirement_id)
        if entry is not None and entry in by_index:
            starts[ask.requirement_id] = label_start(by_index[entry])
            basis[ask.requirement_id] = {"basis": "background", "entry_index": entry}

    # Marked labels no recording names: the item's own words, after the labels before it.
    fallback = [
        ask for ask in asks if ask.requirement_id not in starts and _has_explicit_marker(ask.text)
    ]
    if fallback:
        floors: dict[str, float] = {}
        for ask in fallback:
            earlier = [
                starts[other.requirement_id]
                for other in asks[: asks.index(ask)]
                if other.requirement_id in starts
            ]
            if earlier:
                floors[ask.requirement_id] = max(earlier)
        try:
            for rid, at in ground_on_words(fallback, words, after_s=floors).items():
                starts[rid] = at
                basis[rid] = {"basis": "words"}
        except Exception as exc:  # noqa: BLE001 - the take's words are a fallback, not a must
            log.warning("phone_list_labels.ground_failed", error=str(exc)[:200])

    status: dict[str, str] = {}
    order = {ask.requirement_id: position for position, ask in enumerate(asks)}
    timed = sorted(starts, key=lambda rid: (round(starts[rid], 3), order[rid]))
    kept: list[str] = []
    for rid in timed:
        if kept and abs(starts[rid] - starts[kept[-1]]) < 1e-3:
            status[rid] = "same_moment"
        else:
            kept.append(rid)
    duration = float(timeline_duration_s)
    windows: dict[str, tuple[float, float]] = {}
    next_start: float | None = None
    for rid in reversed(kept):
        start = round(max(0.0, starts[rid]), 3)
        end = min(start + HOLD_S, duration)
        if next_start is not None:
            end = min(end, next_start)
        end = round(end, 3)
        if end - start < MIN_S - _EPS:
            status[rid] = "no_time"
            continue
        windows[rid] = (start, end)
        next_start = start

    by_id = {ask.requirement_id: ask for ask in asks}
    rows: list[dict] = []
    receipt: dict[str, dict[str, Any]] = {}
    for rid in kept:
        if rid not in windows:
            continue
        ask = by_id[rid]
        start, end = windows[rid]
        try:
            row = _label_element(ask, start_s=start, end_s=end, canvas=canvas)
            avoid = avoid_for((start, end), list(rows))
            placed, placement = place(row, list(avoid))
        except Exception as exc:  # noqa: BLE001 - one ask never fails the render
            log.warning("phone_list_labels.failed", error=str(exc)[:200])
            status[rid] = "error"
            receipt[rid] = {"error": str(exc)[:200]}
            continue
        rows.append(placed)
        status[rid] = "placed"
        receipt[rid] = {
            **basis[rid],
            "start_s": placed["start_s"],
            "end_s": placed["end_s"],
            "element_id": placed["id"],
            "placement": placement,
            # A placement that could not look ("error") is never called clear of the face.
            "face_collision": placement.get("face_collision") is True
            or placement.get("status") == "error",
        }

    receipt_rows: list[dict[str, Any]] = []
    for ask in asks:
        rid = ask.requirement_id
        if rid in status:
            outcome = status[rid]
        elif _has_explicit_marker(ask.text):
            outcome = "not_heard"
        else:
            outcome = "not_a_list_item"
        receipt_rows.append(
            {
                "requirement_id": rid,
                "text": ask.text,
                "item": ask.item,
                "status": outcome,
                **receipt.get(rid, {}),
            }
        )
    rows.sort(key=lambda row: float(row["start_s"]))
    return rows, {"version": 1, "rows": receipt_rows}


__all__ = [
    "HOLD_S",
    "LABEL_Y_FRAC",
    "LIST_LABEL_ELEMENT_PREFIX",
    "LIST_LABEL_FIELD",
    "LIST_LABEL_SOURCE",
    "MIN_S",
    "LabelAnchor",
    "ListLabelAsk",
    "ground_on_words",
    "label_item",
    "label_start",
    "list_label_asks",
    "list_label_rows",
    "match_labels",
]
