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
    # Keep the original Talking matcher byte-for-byte in behavior: its
    # first-only query has no repeated-occurrence cap or selection policy.
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
        windows = _match_trigger(stream, _long_form(trigger), None, "every").windows
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
    "find_spoken_word_matches",
    "spoken_cue_length",
    "spoken_text_element",
    "spoken_text_rows",
    "spoken_word_occurrences",
]
