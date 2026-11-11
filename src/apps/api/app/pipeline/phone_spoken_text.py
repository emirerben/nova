"""The creator's text on the word they say, on a phone Talking edit (KRI-707).

"“Atakan” dediğimde ekrana büyük “ATAKAN” yazısı gelsin": the brief holds the text and the
spoken word it waits for (`app.kria.spoken_text_ask`). The worker finds the first time the
take says that word -- in the take's own word timings on the cut timeline, the same words the
captions were built from, never the caption cues (caption correction can rewrite a cue and
drop its word timings) -- and adds one ordinary, editable ``TextElement`` row from the word's
start. It rides the KRI-467 text lane next to the title, so the server, the phone render and
the iOS editor preview all read the same row.

Look: the title's (Playfair Display, 120 px, centred), fading in on the word and held for the
length the creator asked for ("for about a second", `SpokenTextAsk.hold_s`, KRI-972), else
`HOLD_S`. On a letterboxed picture the row stays inside the picture, drawn light with an outline
(KRI-1018, `place_talking_title(picture_band=...)`). Placement: the title's face check
(`phone_subtitled_title.place_talking_title`) over the row's own window, also kept clear of the
caption lines and the title shown at the same time; when no spot clears both, the face wins
(text never covers a face).

Matching is the reaction-beat trigger match (`phone_reaction_grounding`, KRI-178): folded
tokens, so "Atakan'ı" says "Atakan". Never guessed: a word the take doesn't say places nothing
and the receipt says so (``not_heard``). Words carry no speaker, so "the first time SHE says
Merope" is placed on the first "Merope" anyone says (KRI-791; the receipt says so) --
unless the creator picked which time before approval (KRI-724, ``SpokenTextAsk.pin``): then
exactly that occurrence (`opening_cut_ask.select_occurrence`), or nothing and the receipt
says the picked one wasn't found (``pinned_occurrence_missing``) or was cut out of the take
(``pinned_occurrence_cut``). Never the first match instead.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import structlog

from app.kria.spoken_text_ask import SPOKEN_TEXT_ELEMENT_PREFIX, SpokenTextAsk
from app.pipeline.narrated_title import narrated_title_placement
from app.pipeline.render_geometry import NormalizedBox

log = structlog.get_logger(__name__)

SPOKEN_TEXT_SOURCE = "spoken_text"
# The variant's receipt of every spoken-text ask: placed (where and when) or why not.
SPOKEN_TEXT_FIELD = "spoken_text_placement"

# How long the text stays from the word's start when the creator named no length (KRI-972:
# `SpokenTextAsk.hold_s` is theirs), and the shortest worth drawing (a word said right at the
# end of the take).
HOLD_S = 2.0
_MIN_S = 0.3
_MAX_CHARS = 80

# KRI-1018: on a letterboxed picture the text sits on the footage, whatever it shows (bright sky
# or a dark room), so it is drawn light with a dark outline: the outline carries the contrast
# the unknown background cannot. The same outline the list labels draw (no new phone capability).
PICTURE_TEXT_COLOR = "#FFFFFF"
PICTURE_STROKE_COLOR = "#000000"
PICTURE_STROKE_WIDTH = 8.0
# WCAG's minimum contrast for large text, applied to fill against outline (not to the footage).
_MIN_CONTRAST = 3.0

Placer = Callable[[dict, Sequence[NormalizedBox]], tuple[dict, dict[str, Any]]]


class _Canvas(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


def _word_dicts(words: Sequence[Any]) -> list[dict]:
    rows: list[dict] = []
    for word in words or ():
        if isinstance(word, Mapping):
            rows.append(dict(word))
        else:
            rows.append(
                {
                    "text": getattr(word, "text", None),
                    "start_s": getattr(word, "start_s", None),
                    "end_s": getattr(word, "end_s", None),
                }
            )
    return rows


def spoken_word_occurrences(
    words: Sequence[Any], triggers: Sequence[str]
) -> list[tuple[str, float, float]]:
    """Every time the take says any of ``triggers``, in time order: (trigger, start_s, end_s).

    The same matcher `find_spoken_word` uses, so the question asked before approval
    (`choice_questions._speech_occurrence`) counts what the render hears."""
    from app.services.phone_reaction_grounding import (  # noqa: PLC0415
        _match_trigger,
        _token_stream,
    )

    stream = _token_stream(_word_dicts(words))
    heard: dict[float, tuple[str, float, float]] = {}
    for trigger in triggers:
        for start, end in _match_trigger(stream, trigger, None, "every", joined=True).windows:
            heard.setdefault(round(float(start), 3), (trigger, float(start), float(end)))
    return [heard[key] for key in sorted(heard)]


def find_spoken_word(
    words: Sequence[Any], triggers: Sequence[str]
) -> tuple[str, float, float] | None:
    """The first time the take says any of ``triggers``: (trigger, start_s, end_s)."""
    # Keep the original Talking matcher byte-for-byte in behavior: its
    # first-only query has no repeated-occurrence cap or selection policy.
    from app.services.phone_reaction_grounding import (  # noqa: PLC0415
        _match_trigger,
        _token_stream,
    )

    stream = _token_stream(_word_dicts(words))
    heard: tuple[str, float, float] | None = None
    for trigger in triggers:
        windows = _match_trigger(stream, trigger, None, "first", joined=True).windows
        if windows and (heard is None or windows[0][0] < heard[1]):
            heard = (trigger, float(windows[0][0]), float(windows[0][1]))
    return heard


def find_pinned_word(
    words: Sequence[Any],
    ask: SpokenTextAsk,
    *,
    to_timeline: Callable[[float], float | None] | None = None,
) -> tuple[tuple[str, float, float] | None, str | None]:
    """KRI-724: the occurrence of ``ask``'s word the creator picked, on ``words``' timeline.

    ``to_timeline`` maps the pin's source time onto that timeline (None inside a cut; the
    word playing where that cut closes can still be the pick, `_pinned_across_cut`). Returns
    ``(hit, None)``, or ``(None, reason)``: ``pinned_occurrence_cut`` when the picked moment
    was cut out, ``pinned_occurrence_missing`` when the take's words don't hold it."""
    from app.kria.opening_cut_ask import select_occurrence  # noqa: PLC0415

    pin = ask.pin
    assert pin is not None  # noqa: S101 - only called for a pinned ask
    hits = spoken_word_occurrences(words, ask.triggers)
    approx = pin.approx_s
    if approx is not None and to_timeline is not None:
        mapped = to_timeline(approx)
        if mapped is None:
            return _pinned_across_cut(hits, pin, _cut_point(approx, to_timeline))
        approx = mapped
    # The ordinal decides first; the picked time (when it survived the cut) is the fallback.
    picked = select_occurrence(
        [(start, end) for _t, start, end in hits], dataclasses.replace(pin, approx_s=approx)
    )
    if picked is None:
        return None, "pinned_occurrence_missing"
    return next(hit for hit in hits if (hit[1], hit[2]) == picked), None


# How far past a cut-out picked time `_cut_point` looks for where the take resumes.
_CUT_PROBE_STEP_S = 0.02
_CUT_PROBE_MAX_S = 120.0


def _cut_point(source_s: float, to_timeline: Callable[[float], float | None]) -> float | None:
    """Where the take resumes on the timeline after the cut holding ``source_s``."""
    for step in range(1, int(_CUT_PROBE_MAX_S / _CUT_PROBE_STEP_S) + 1):
        mapped = to_timeline(source_s + step * _CUT_PROBE_STEP_S)
        if mapped is not None:
            return mapped
    return None


def _pinned_across_cut(
    hits: Sequence[tuple[str, float, float]], pin: Any, cut_point: float | None
) -> tuple[tuple[str, float, float] | None, str | None]:
    """The picked word when its (sentence-estimated) time fell inside a cut.

    The estimate spreads a sentence's words by character share, so it can land in a pause
    speech cleanup removed while the word itself plays right where the cut closes. The word
    nearest that point is the pick only when it sits within ``_MAX_PIN_DRIFT_S`` of it and,
    when the render hears the count the question offered, the ordinal names that same word.
    Otherwise the picked moment really was cut: never another occurrence in its place."""
    from app.kria.opening_cut_ask import _MAX_PIN_DRIFT_S  # noqa: PLC0415

    ordered = sorted(hits, key=lambda hit: hit[1])
    if cut_point is None or not ordered or pin.ordinal is None:
        return None, "pinned_occurrence_cut"
    index = min(range(len(ordered)), key=lambda i: abs(ordered[i][1] - cut_point))
    gaps = [
        abs(ordered[j][1] - ordered[index][1]) / 2.0
        for j in (index - 1, index + 1)
        if 0 <= j < len(ordered)
    ]
    if abs(ordered[index][1] - cut_point) > min([_MAX_PIN_DRIFT_S, *gaps]):
        return None, "pinned_occurrence_cut"
    if len(ordered) == pin.of and index != pin.ordinal - 1:
        return None, "pinned_occurrence_cut"
    return ordered[index], None


# English contractions with exactly one long form, so a cue written "you're" finds a
# recording transcribed "you are" and the reverse (PR #1758's N2 rule two). "'s" and "'d"
# are left alone: each has two long forms ("it is" / "it has"), so neither side is rewritten.
_CONTRACTIONS = {
    "i'm": "i am",
    "you're": "you are",
    "we're": "we are",
    "they're": "they are",
    "i've": "i have",
    "you've": "you have",
    "we've": "we have",
    "they've": "they have",
    "i'll": "i will",
    "you'll": "you will",
    "we'll": "we will",
    "they'll": "they will",
    "don't": "do not",
    "doesn't": "does not",
    "didn't": "did not",
    "isn't": "is not",
    "aren't": "are not",
    "wasn't": "was not",
    "weren't": "were not",
    "won't": "will not",
    "can't": "can not",
    "cannot": "can not",
}
_EDGE_PUNCTUATION = '.,!?;:"“”()[]'


def _long_form(text: object) -> str:
    """``text`` with every contraction above in its long form; other words unchanged."""
    out: list[str] = []
    for token in str(text or "").split():
        key = token.replace("’", "'").strip(_EDGE_PUNCTUATION).casefold()
        out.append(_CONTRACTIONS.get(key, token))
    return " ".join(out)


def spoken_cue_length(trigger: str) -> int:
    """How many spoken words ``trigger`` names once folded ("leave before 11" is 3)."""
    from app.services.phone_reaction_grounding import fold_tokens  # noqa: PLC0415

    return len(fold_tokens(_long_form(trigger)))


def find_spoken_word_matches(
    words: Sequence[Any], triggers: Sequence[str]
) -> list[tuple[str, float, float]]:
    """Every spoken match for the creator's triggers, in timeline order.

    The Talking path still uses :func:`find_spoken_word` and therefore keeps its
    shipped first-hit behavior.  Voiceover callers can inspect repetition before
    choosing a time, rather than silently treating an ambiguous repeated phrase
    as its first occurrence.  Both the recording and the triggers are compared in
    their long form (`_CONTRACTIONS`); a match keeps the word's own timing.
    """
    from app.services.phone_reaction_grounding import (  # noqa: PLC0415
        _match_trigger,
        _token_stream,
    )

    stream = _token_stream(
        [{**word, "text": _long_form(word.get("text"))} for word in _word_dicts(words)]
    )
    heard: list[tuple[str, float, float]] = []
    for trigger in triggers:
        windows = _match_trigger(stream, _long_form(trigger), None, "every", joined=True).windows
        heard.extend((trigger, float(start_s), float(end_s)) for start_s, end_s in windows)
    # Different trigger spellings may resolve to the same spoken interval.  It
    # is one moment, not an ambiguous pair of moments.
    unique: dict[tuple[float, float], tuple[str, float, float]] = {}
    for match in heard:
        # Preserve the first trigger the creator supplied, which is how the
        # original first-hit implementation resolved equivalent spellings.
        unique.setdefault((match[1], match[2]), match)
    return sorted(unique.values(), key=lambda match: match[1])


def spoken_text_element(
    ask: SpokenTextAsk, *, start_s: float, timeline_duration_s: float, canvas: _Canvas
) -> dict | None:
    """The text as one editable TextElement row from ``start_s``, or ``None`` when the
    take has no time left to show it."""
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

    text = " ".join(ask.text.split())[:_MAX_CHARS]
    start_s = round(max(0.0, float(start_s)), 3)
    end_s = round(min(start_s + (ask.hold_s or HOLD_S), float(timeline_duration_s)), 3)
    if not text or end_s - start_s < _MIN_S:
        return None
    element = TextElement(
        id=f"{SPOKEN_TEXT_ELEMENT_PREFIX}{ask.requirement_id}",
        text=text,
        start_s=start_s,
        end_s=end_s,
        role="generative_intro",
        **narrated_title_placement(text, canvas=canvas, explicit=True),
        effect="fade-in",
        source_params={"source": SPOKEN_TEXT_SOURCE, "requirement_id": ask.requirement_id},
    )
    return element.model_dump(mode="json", exclude_none=True)


def picture_look(placement: Mapping[str, Any]) -> dict[str, Any]:
    """The look the receipt promises for a row placed in a letterboxed picture, and whether
    the placement check found it inside the picture. ``outline_contrast`` is the fill against
    its outline: a fixed property of the two colours, NOT a measurement of the footage behind
    the text (nothing here samples the frame). It only guards the look against being edited
    into something with no outline contrast."""
    from app.services.phone_text_styling import _contrast  # noqa: PLC0415

    return {
        "text_color": PICTURE_TEXT_COLOR,
        "stroke_color": PICTURE_STROKE_COLOR,
        "stroke_width": PICTURE_STROKE_WIDTH,
        "outline_contrast": round(_contrast(PICTURE_TEXT_COLOR, PICTURE_STROKE_COLOR), 2),
        "min_outline_contrast": _MIN_CONTRAST,
        "inside_picture": placement.get("inside_picture") is True,
    }


def _overlaps(row: Mapping[str, Any], window: tuple[float, float]) -> bool:
    return float(row["start_s"]) < window[1] and float(row["end_s"]) > window[0]


def caption_boxes(
    cues: Sequence[dict],
    *,
    window: tuple[float, float],
    caption_style: str,
    canvas: _Canvas,
    look: Any = None,
) -> list[NormalizedBox]:
    """The ink of every caption line on screen during ``window``, on the canvas.
    ``look`` is the `PhoneCaptionLook` the recipe draws (``None``: the default)."""
    from app.pipeline import text_overlay_skia as cloud  # noqa: PLC0415
    from app.pipeline.phone_captions import (  # noqa: PLC0415
        CAPTION_FONT_FAMILY,
        caption_ink_box,
        compile_caption_layers,
    )

    shown = [dict(cue) for cue in cues if isinstance(cue, Mapping) and _overlaps(cue, window)]
    if not shown:
        return []
    layers = compile_caption_layers(
        shown,
        canvas_width=canvas.width,
        canvas_height=canvas.height,
        style=caption_style,
        look=look,
    )
    typeface = cloud._resolve_typeface_for_overlay({"font_family": CAPTION_FONT_FAMILY}).typeface
    boxes: list[NormalizedBox] = []
    for layer in layers:
        if layer.start >= window[1] or layer.end <= window[0]:
            continue
        inks = [box for run in layer.runs if (box := caption_ink_box(run, typeface)) is not None]
        if inks:
            boxes.append(
                NormalizedBox(
                    min(box[0] for box in inks) / canvas.width,
                    min(box[1] for box in inks) / canvas.height,
                    max(box[2] for box in inks) / canvas.width,
                    max(box[3] for box in inks) / canvas.height,
                )
            )
    return boxes


def _text_boxes(
    rows: Sequence[dict], *, window: tuple[float, float], canvas: _Canvas
) -> list[NormalizedBox]:
    from app.pipeline.phone_subtitled_title import _measure_title_box  # noqa: PLC0415

    return [_measure_title_box(row, canvas=canvas) for row in rows if _overlaps(row, window)]


def spoken_text_avoid(
    row: Mapping[str, Any],
    others: Sequence[dict],
    *,
    cues: Sequence[dict],
    caption_style: str,
    canvas: _Canvas,
    caption_look: Any = None,
) -> list[NormalizedBox]:
    """What ``row`` keeps clear of: the other text rows (``others``: the title, the rows placed
    so far) and the caption lines on screen during its window. The captions are a nicety: when
    they cannot be measured the row still keeps off the other text (and the placer, the face)."""
    window = (float(row["start_s"]), float(row["end_s"]))
    others = [other for other in others if other.get("id") != row.get("id")]
    avoid = [*_text_boxes(others, window=window, canvas=canvas)]
    try:
        avoid += caption_boxes(
            cues, window=window, caption_style=caption_style, canvas=canvas, look=caption_look
        )
    except Exception as exc:  # noqa: BLE001 - the captions are a nicety; the face is not
        log.warning("phone_spoken_text.caption_boxes_failed", error=str(exc)[:200])
    return avoid


def _list_asks(asks: Sequence[SpokenTextAsk]) -> set[str]:
    """KRI-1284: the requirement ids of the asks that are a numbered list ("1. Spec-kit",
    "2. Impeccable", ...): two or more texts that each start with a list marker. One numbered
    text alone, or plain words ("ATAKAN"), are not a list and keep today's look."""
    from app.pipeline.phone_list_labels import marker_number  # noqa: PLC0415

    numbered = {ask.requirement_id for ask in asks if marker_number(ask.text) is not None}
    return numbered if len(numbered) >= 2 else set()


def _is_list_row(row: Mapping[str, Any]) -> bool:
    from app.pipeline.phone_list_labels import LIST_LABEL_LOOK_PARAM  # noqa: PLC0415

    return bool((row.get("source_params") or {}).get(LIST_LABEL_LOOK_PARAM))


def _clean_spot(placed: Mapping[str, Any], placement: Mapping[str, Any], y_frac: float) -> bool:
    """The placer kept ``y_frac`` and found nothing there: no face, object or text under it."""
    return (
        abs(float(placed.get("y_frac") or -1.0) - y_frac) <= 1e-4
        and placement.get("status") != "error"
        and placement.get("face_collision") is not True
        and placement.get("object_collision") is not True
        and placement.get("text_overlap") is not True
    )


def share_list_spot(
    rows: list[dict],
    receipt: dict[str, Any],
    *,
    place: Placer,
    cues: Sequence[dict],
    caption_style: str,
    canvas: _Canvas,
    shown_rows: Sequence[dict] = (),
    caption_look: Any = None,
) -> tuple[list[dict], dict[str, Any]]:
    """KRI-1284: every label of one numbered list at ONE spot for the whole list.

    Each row was placed on its own, so one label could land somewhere else (run 2: four at the
    top, "5. Claude code game studio" under the face). Tried in order: the list's preset spot,
    then the spots the rows took (the most shared first). A spot counts only when the placer
    keeps it for EVERY row and finds no face, marked object or other text under any of them; the
    first such spot is taken. None: the rows keep their own spots and the receipt says ``split``
    (each is still off the face). ``place`` is the same placer the rows were placed with (call it
    again with the speaker frame once the lanes know it, F4). Never raises."""
    ids = [row["id"] for row in rows if _is_list_row(row)]
    if len(ids) < 2:
        return rows, receipt
    # One size for the list too: the smallest any label needed to fit the label band (a smaller
    # block centred on the same spot only gets clearer of the face).
    sizes = [float(row["size_px"]) for row in rows if row["id"] in ids and row.get("size_px")]
    if sizes:
        rows = [{**row, "size_px": min(sizes)} if row["id"] in ids else row for row in rows]
    by_id = {row["id"]: row for row in rows}
    entries = {
        entry.get("element_id"): entry
        for entry in receipt.get("rows") or []
        if entry.get("status") == "placed" and entry.get("element_id") in by_id
    }
    presets = [
        round(float(value), 4)
        for row_id in ids
        if (value := (entries.get(row_id) or {}).get("placement", {}).get("default_y_frac"))
        is not None
    ]
    taken = [round(float(by_id[row_id]["y_frac"]), 4) for row_id in ids]
    by_share = sorted(dict.fromkeys(taken), key=lambda y: (-taken.count(y), taken.index(y)))
    found: tuple[float, dict[str, tuple[dict, dict[str, Any]]]] | None = None
    try:
        for y_frac in dict.fromkeys([*presets, *by_share]):
            trial: dict[str, tuple[dict, dict[str, Any]]] = {}
            for row_id in ids:
                row = {**by_id[row_id], "y_frac": y_frac}
                avoid = spoken_text_avoid(
                    row,
                    [*shown_rows, *rows],
                    cues=cues,
                    caption_style=caption_style,
                    canvas=canvas,
                    caption_look=caption_look,
                )
                placed, placement = place(row, avoid)
                if not _clean_spot(placed, placement, y_frac):
                    break
                trial[row_id] = (placed, placement)
            else:
                found = (y_frac, trial)
                break
    except Exception as exc:  # noqa: BLE001 - the rows keep their own (face-clear) spots
        log.warning("phone_spoken_text.list_spot_failed", error=str(exc)[:200])
    out_rows = list(rows)
    out_entries = []
    for entry in receipt.get("rows") or []:
        row_id = entry.get("element_id")
        if row_id not in ids or entry.get("status") != "placed":
            out_entries.append(entry)
            continue
        if found is None:
            out_entries.append({**entry, "list_spot": {"status": "split", "rows": len(ids)}})
            continue
        y_frac, trial = found
        placed, placement = trial[row_id]
        out_rows[out_rows.index(by_id[row_id])] = placed
        default = (entry.get("placement") or {}).get("default_y_frac")
        out_entries.append(
            {
                **entry,
                "placement": {
                    **placement,
                    **({"default_y_frac": default} if default is not None else {}),
                },
                "face_collision": False,
                "list_spot": {"status": "shared", "y_frac": y_frac, "rows": len(ids)},
            }
        )
    return out_rows, {**receipt, "rows": out_entries}


def spoken_text_rows(
    asks: Sequence[SpokenTextAsk],
    *,
    words: Sequence[Any],
    cues: Sequence[dict],
    caption_style: str,
    timeline_duration_s: float,
    canvas: _Canvas,
    place: Placer,
    shown_rows: Sequence[dict] = (),
    caption_look: Any = None,
    to_timeline: Callable[[float], float | None] | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """One placed row per ask the take says, plus the receipt of every ask.

    ``to_timeline`` maps a source time onto ``words``' timeline (None inside a cut), for the
    occurrence a creator picked (KRI-724, `find_pinned_word`).

    ``words`` and ``cues`` are on the timeline the recipe plays (after any cut).
    ``place(row, avoid)`` moves a row off the speaker's face, keeping it clear of
    ``avoid`` when it can. ``shown_rows`` (the title) are kept clear too. Never raises
    for one ask: a failure is that ask's receipt row (``error``)."""
    from app.pipeline.phone_list_labels import (  # noqa: PLC0415
        LIST_LABEL_LOOK_PARAM,
        LabelTooLong,
        label_look,
    )

    rows: list[dict] = []
    receipt_rows: list[dict[str, Any]] = []
    listed = _list_asks(asks)
    for ask in asks:
        entry: dict[str, Any] = {
            "requirement_id": ask.requirement_id,
            "text": ask.text,
            "triggers": list(ask.triggers),
        }
        if ask.speaker:
            # KRI-791: the ask named who says it; the take's words can't tell, so the first
            # time anyone says the word is the one shown (and the receipt says so).
            entry["speaker"] = ask.speaker
        if ask.pin is not None:
            # KRI-724: the creator picked which time; only that one is ever shown.
            entry["pin"] = {
                "ordinal": ask.pin.ordinal,
                "of": ask.pin.of,
                "approx_s": ask.pin.approx_s,
            }
        try:
            if ask.pin is not None:
                heard, missed = find_pinned_word(words, ask, to_timeline=to_timeline)
                if heard is None:
                    receipt_rows.append({**entry, "status": missed})
                    continue
            else:
                heard = find_spoken_word(words, ask.triggers)
            if heard is None:
                receipt_rows.append({**entry, "status": "not_heard"})
                continue
            trigger, start_s, _end_s = heard
            row = spoken_text_element(
                ask, start_s=start_s, timeline_duration_s=timeline_duration_s, canvas=canvas
            )
            if row is None:
                receipt_rows.append({**entry, "status": "no_time", "heard_at_s": start_s})
                continue
            if ask.requirement_id in listed:
                # KRI-1284: one label of a numbered list, in the list-label look (bold cream,
                # dark outline: readable on a white README and a dark terminal alike).
                try:
                    row = label_look(row, canvas=canvas)
                    row["source_params"] = {
                        **(row.get("source_params") or {}),
                        LIST_LABEL_LOOK_PARAM: True,
                    }
                except LabelTooLong:
                    entry["list_look"] = "too_long"  # never cut: drawn in the plain look
            avoid = spoken_text_avoid(
                row,
                [*shown_rows, *rows],
                cues=cues,
                caption_style=caption_style,
                canvas=canvas,
                caption_look=caption_look,
            )
            placed, placement = place(row, avoid)
            look = None
            if placement.get("picture_band") is not None:
                # KRI-1018: a letterboxed picture. The placer kept the row inside it; the look
                # is light with an outline, never left to a default or a restyle.
                placed = {
                    **placed,
                    "color": PICTURE_TEXT_COLOR,
                    "stroke_color": PICTURE_STROKE_COLOR,
                    "stroke_width": PICTURE_STROKE_WIDTH,
                }
                look = picture_look(placement)
        except Exception as exc:  # noqa: BLE001 - one ask never fails the render
            log.warning("phone_spoken_text.failed", error=str(exc)[:200])
            receipt_rows.append({**entry, "status": "error", "error": str(exc)[:200]})
            continue
        rows.append(placed)
        receipt_rows.append(
            {
                **entry,
                "status": "placed",
                "trigger": trigger,
                **({"pinned": True} if ask.pin is not None else {}),
                "element_id": placed["id"],
                "start_s": placed["start_s"],
                "end_s": placed["end_s"],
                "placement": placement,
                # KRI-972: the length the creator asked for, and whether the take ended before
                # the row could stay that long.
                **(
                    {
                        "hold_s": ask.hold_s,
                        "hold_clipped": float(placed["end_s"]) - float(placed["start_s"])
                        < ask.hold_s - 1e-6,
                    }
                    if ask.hold_s
                    else {}
                ),
                # KRI-747: a text over the row's character limit is drawn cut; say so.
                **(
                    {"shown_text": placed["text"], "truncated": True}
                    if placed["text"] != " ".join(ask.text.split())
                    else {}
                ),
                **({"picture_look": look} if look is not None else {}),
                # A placement that could not look ("error") is never called clear of the face.
                "face_collision": placement.get("face_collision") is True
                or placement.get("status") == "error",
                # KRI-827: only with a marked object (absent otherwise, so receipts of
                # renders that protect nothing extra are unchanged).
                **(
                    {"object_collision": placement["object_collision"] is True}
                    if "object_collision" in placement
                    else {}
                ),
            }
        )
    receipt: dict[str, Any] = {"version": 1, "rows": receipt_rows}
    if listed:
        rows, receipt = share_list_spot(
            rows,
            receipt,
            place=place,
            cues=cues,
            caption_style=caption_style,
            canvas=canvas,
            shown_rows=shown_rows,
            caption_look=caption_look,
        )
    return rows, receipt


__all__ = [
    "HOLD_S",
    "SPOKEN_TEXT_ELEMENT_PREFIX",
    "SPOKEN_TEXT_FIELD",
    "SPOKEN_TEXT_SOURCE",
    "caption_boxes",
    "find_pinned_word",
    "find_spoken_word",
    "find_spoken_word_matches",
    "share_list_spot",
    "spoken_cue_length",
    "spoken_text_element",
    "spoken_text_rows",
    "spoken_text_avoid",
    "spoken_word_occurrences",
]
