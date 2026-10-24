"""The creator's text on the word they say, on a phone Talking edit (KRI-707).

"“Atakan” dediğimde ekrana büyük “ATAKAN” yazısı gelsin": the brief holds the text and the
spoken word it waits for (`app.kria.spoken_text_ask`). The worker finds the first time the
take says that word -- in the take's own word timings on the cut timeline, the same words the
captions were built from, never the caption cues (caption correction can rewrite a cue and
drop its word timings) -- and adds one ordinary, editable ``TextElement`` row from the word's
start. It rides the KRI-467 text lane next to the title, so the server, the phone render and
the iOS editor preview all read the same row.

Look: the title's (Playfair Display, 120 px, centred), fading in on the word and held for
`HOLD_S`. Placement: the title's face check (`phone_subtitled_title.place_talking_title`)
over the row's own window, also kept clear of the caption lines and the title shown at the
same time; when no spot clears both, the face wins (text never covers a face).

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

from app.kria.spoken_text_ask import SpokenTextAsk
from app.pipeline.narrated_title import narrated_title_placement
from app.pipeline.render_geometry import NormalizedBox

log = structlog.get_logger(__name__)

SPOKEN_TEXT_SOURCE = "spoken_text"
SPOKEN_TEXT_ELEMENT_PREFIX = "spoken-text-"
# The variant's receipt of every spoken-text ask: placed (where and when) or why not.
SPOKEN_TEXT_FIELD = "spoken_text_placement"

# How long the text stays from the word's start, and the shortest worth drawing (a word said
# right at the end of the take).
HOLD_S = 2.0
_MIN_S = 0.3
_MAX_CHARS = 80

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
        for start, end in _match_trigger(stream, trigger, None, "every").windows:
            heard.setdefault(round(float(start), 3), (trigger, float(start), float(end)))
    return [heard[key] for key in sorted(heard)]


def find_spoken_word(
    words: Sequence[Any], triggers: Sequence[str]
) -> tuple[str, float, float] | None:
    """The first time the take says any of ``triggers``: (trigger, start_s, end_s)."""
    from app.services.phone_reaction_grounding import (  # noqa: PLC0415
        _match_trigger,
        _token_stream,
    )

    stream = _token_stream(_word_dicts(words))
    heard: tuple[str, float, float] | None = None
    for trigger in triggers:
        windows = _match_trigger(stream, trigger, None, "first").windows
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

    ``to_timeline`` maps the pin's source time onto that timeline (None inside a cut). Returns
    ``(hit, None)``, or ``(None, reason)``: ``pinned_occurrence_cut`` when the picked moment
    was cut out, ``pinned_occurrence_missing`` when the take's words don't hold it."""
    from app.kria.opening_cut_ask import select_occurrence  # noqa: PLC0415

    pin = ask.pin
    assert pin is not None  # noqa: S101 - only called for a pinned ask
    approx = pin.approx_s
    if approx is not None and to_timeline is not None:
        approx = to_timeline(approx)
        if approx is None:
            # The picked moment was cut out: never fall back to the ordinal (another
            # occurrence the render hears would take its place).
            return None, "pinned_occurrence_cut"
    hits = spoken_word_occurrences(words, ask.triggers)
    # The ordinal decides first; the picked time (when it survived the cut) is the fallback.
    picked = select_occurrence(
        [(start, end) for _t, start, end in hits], dataclasses.replace(pin, approx_s=approx)
    )
    if picked is None:
        return None, "pinned_occurrence_missing"
    return next(hit for hit in hits if (hit[1], hit[2]) == picked), None


def spoken_text_element(
    ask: SpokenTextAsk, *, start_s: float, timeline_duration_s: float, canvas: _Canvas
) -> dict | None:
    """The text as one editable TextElement row from ``start_s``, or ``None`` when the
    take has no time left to show it."""
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

    text = " ".join(ask.text.split())[:_MAX_CHARS]
    start_s = round(max(0.0, float(start_s)), 3)
    end_s = round(min(start_s + HOLD_S, float(timeline_duration_s)), 3)
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
    rows: list[dict] = []
    receipt_rows: list[dict[str, Any]] = []
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
            window = (float(row["start_s"]), float(row["end_s"]))
            avoid = [*_text_boxes([*shown_rows, *rows], window=window, canvas=canvas)]
            try:
                avoid += caption_boxes(
                    cues,
                    window=window,
                    caption_style=caption_style,
                    canvas=canvas,
                    look=caption_look,
                )
            except Exception as exc:  # noqa: BLE001 - the captions are a nicety; the face is not
                log.warning("phone_spoken_text.caption_boxes_failed", error=str(exc)[:200])
            placed, placement = place(row, avoid)
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
    return rows, {"version": 1, "rows": receipt_rows}


__all__ = [
    "HOLD_S",
    "SPOKEN_TEXT_ELEMENT_PREFIX",
    "SPOKEN_TEXT_FIELD",
    "SPOKEN_TEXT_SOURCE",
    "caption_boxes",
    "find_pinned_word",
    "find_spoken_word",
    "spoken_text_element",
    "spoken_text_rows",
    "spoken_word_occurrences",
]
