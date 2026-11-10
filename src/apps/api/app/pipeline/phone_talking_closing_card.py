"""The closing card of a phone Talking edit (KRI-1346): a full-screen colour with text, AFTER
the last word.

"End on a red screen with yellow text 'follow for more brand and marketing content'" (Çiğdem's
Yorumla end card; "kırmızı ekranla bitir, üstünde sarı 'takip et' yazsın" is the same ask in
Turkish). The brief records it as one ``text`` requirement with typed facts: ``closing`` (the
words), ``background_color`` and optionally ``text_color`` / ``duration_s``. No prose is read
here (KRI-667): colours are a hex or a named preset (`normalise_background_color`, EN + TR).

How it renders (no new recipe field, so every app build that decodes a held frame draws it):

* the last main-track clip holds its last frame for the card's seconds (``hold_duration``,
  capability ``freezeFrame``); a held tail is visual-only on the device, so the talk's audio
  ends exactly where it did and nothing spoken is cut or covered;
* a solid ``visual_fills`` entry (id ``closing-card``, the top order) covers that hold;
* the words are one ordinary text row (id ``closing-card-text``) centred on the card, fitted
  to the canvas width; the compiler times it to the card's window, so a speech cleanup, an
  inserted clip or an ending clip before it can never leave it on the talk.

`ClosingCardSpec` is what the worker pins on the variant (``phone_closing_card``) and the
editor's Save reads back from the recipe (`closing_card_from_recipe`), so a caption or text
Save keeps the card.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

CLOSING_CARD_SOURCE = "closing_card"
CLOSING_CARD_FILL_ID = "closing-card"
CLOSING_CARD_TEXT_ID = "closing-card-text"
CLOSING_CARD_FIELD = "phone_closing_card"
CLOSING_CARD_FILL_ORDER = 1000
CLOSING_CARD_DEFAULT_S = 3.0
CLOSING_CARD_MIN_S = 1.0
CLOSING_CARD_MAX_S = 10.0
# WCAG contrast below this reads as "can't read it" (yellow on yellow, white on light grey).
MIN_TEXT_CONTRAST = 2.0
_FONT = "TikTok Sans Bold"
_SIZE_FRAC = 0.055
_MIN_SIZE_FRAC = 0.028
_MAX_WIDTH_FRAC = 0.82
_MAX_HEIGHT_FRAC = 0.42
_SHRINK = 0.9
# A text row holds at most this many characters (`TextElement.text`); longer words are never cut
# short to fit: the card is refused instead, and the receipt says why.
CLOSING_CARD_TEXT_MAX_CHARS = 500
TOO_LONG = "text_too_long"
UNDRAWABLE = "undrawable_text"


@dataclass(frozen=True)
class ClosingCardSpec:
    """The card as asked: its words, its colours (``#RRGGBB``) and how long it holds."""

    text: str
    background_color: str
    text_color: str
    duration_s: float = CLOSING_CARD_DEFAULT_S
    requirement_id: str | None = None
    # The text colour was named by the creator (False: picked for contrast).
    text_color_asked: bool = False
    # The named text colour was unreadable on the background and was swapped.
    text_color_swapped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: object) -> ClosingCardSpec | None:
        if not isinstance(raw, Mapping):
            return None
        try:
            return cls(
                text=str(raw["text"]),
                background_color=str(raw["background_color"]),
                text_color=str(raw["text_color"]),
                duration_s=float(raw.get("duration_s") or CLOSING_CARD_DEFAULT_S),
                requirement_id=raw.get("requirement_id"),
                text_color_asked=bool(raw.get("text_color_asked")),
                text_color_swapped=bool(raw.get("text_color_swapped")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _hex(value: object) -> str | None:
    from app.agents._schemas.reaction_beats import normalise_background_color  # noqa: PLC0415

    return normalise_background_color(value)


def _luminance(hex_color: str) -> float:
    def channel(index: int) -> float:
        c = int(hex_color[1 + 2 * index : 3 + 2 * index], 16) / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(0) + 0.7152 * channel(1) + 0.0722 * channel(2)


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio of two ``#RRGGBB`` colours (1 = same, 21 = black on white)."""
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable_on(background: str) -> str:
    """White or black, whichever reads better on ``background``."""
    return (
        "#FFFFFF"
        if contrast_ratio("#FFFFFF", background) >= contrast_ratio("#000000", background)
        else "#000000"
    )


def asks_closing_card(req: Any) -> bool:
    """A creator's colour card at the end: a text ask with its ending words (``closing``)
    AND a ``background_color``. Facts only (KRI-667). Without the colour it is the closing
    text (KRI-514's tag); a colour without ``closing`` is a background behind the speaker."""
    facts = getattr(req, "facts", None) or {}
    if getattr(req, "kind", None) != "text":
        return False
    words = facts.get("closing")
    colour = facts.get("background_color")
    return bool(
        isinstance(words, str) and words.strip() and isinstance(colour, str) and colour.strip()
    )


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def closing_card_from_requirements(
    requirements: Iterable[Any], *, closing_title: str | None = None
) -> ClosingCardSpec | None:
    """The card the live brief asks for, or None. The words are the creator's own: the
    ``closing`` fact, else the requirement's literal, else the confirmed ``closing_title``. A
    colour Kria cannot name exactly (not a hex, not a preset) is no card: guessing a shade
    would draw the wrong colour and call it done."""
    for req in requirements:
        if not asks_closing_card(req):
            continue
        facts = req.facts or {}
        background = _hex(facts.get("background_color"))
        if background is None:
            log.info("closing_card.colour_unnamed", value=str(facts.get("background_color"))[:40])
            return None
        raw_words = facts.get("closing")
        words = raw_words if isinstance(raw_words, str) and raw_words.strip() else req.literal
        # Every word the creator wrote, whitespace normalised only: never cut short here. A text
        # that does not fit the card is refused at layout (`closing_card_text_row_or_reason`).
        words = " ".join(str(words or closing_title or "").split())
        if not words:
            return None
        asked_text = _hex(facts.get("text_color"))
        text_color = asked_text or readable_on(background)
        swapped = False
        if contrast_ratio(text_color, background) < MIN_TEXT_CONTRAST:
            text_color, swapped = readable_on(background), True
        duration = _seconds(facts.get("duration_s"))
        duration = CLOSING_CARD_DEFAULT_S if duration is None else duration
        return ClosingCardSpec(
            text=words,
            background_color=background,
            text_color=text_color,
            duration_s=round(min(CLOSING_CARD_MAX_S, max(CLOSING_CARD_MIN_S, duration)), 3),
            requirement_id=getattr(req, "id", None),
            text_color_asked=asked_text is not None,
            text_color_swapped=swapped,
        )
    return None


def closing_card_text_row(
    spec: ClosingCardSpec, *, start_s: float, end_s: float, canvas: Any
) -> dict | None:
    """The card's words as one row, or None when no card can carry them (see below)."""
    return closing_card_text_row_or_reason(spec, start_s=start_s, end_s=end_s, canvas=canvas)[0]


def closing_card_text_row_or_reason(
    spec: ClosingCardSpec, *, start_s: float, end_s: float, canvas: Any
) -> tuple[dict | None, str | None]:
    """The card's words as one editable text row centred on the canvas, shrunk until the
    measured block fits inside the safe width and height; ``(None, reason)`` when no card can
    carry ALL of them: ``undrawable_text`` (a character the card's font cannot draw) or
    ``text_too_long`` (over a text row's limit, or still too big at the smallest size). The
    words are never cut short, so a card on the video always shows everything asked."""
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415
    from app.pipeline.phone_subtitled_title import _measure_title_box  # noqa: PLC0415
    from app.pipeline.phone_text_style import undrawable_text  # noqa: PLC0415

    if len(spec.text) > CLOSING_CARD_TEXT_MAX_CHARS:
        return None, TOO_LONG
    if undrawable_text([spec.text], fonts=(_FONT,)) is not None:
        return None, UNDRAWABLE
    height = float(canvas.height)
    size = round(_SIZE_FRAC * height)
    min_size = round(_MIN_SIZE_FRAC * height)
    while True:
        row = TextElement(
            id=CLOSING_CARD_TEXT_ID,
            text=spec.text,
            start_s=start_s,
            end_s=end_s,
            role="generative_intro",
            position="custom",
            x_frac=0.5,
            y_frac=0.5,
            font_family=_FONT,
            size_px=size,
            color=spec.text_color,
            shadow_enabled=False,
            stroke_width=0,
            alignment="center",
            max_width_frac=_MAX_WIDTH_FRAC,
            effect="fade-in",
            source_params={"source": CLOSING_CARD_SOURCE},
        ).model_dump(mode="json", exclude_none=True)
        box = _measure_title_box(row, canvas=canvas)
        fits = box.width <= _MAX_WIDTH_FRAC + 0.02 and box.height <= _MAX_HEIGHT_FRAC
        if fits:
            return row, None
        if size <= min_size:
            return None, TOO_LONG
        size = max(min_size, round(size * _SHRINK))


def is_closing_card_row(row: Mapping[str, Any]) -> bool:
    return (row.get("source_params") or {}).get("source") == CLOSING_CARD_SOURCE


def retime_card_rows(
    rows: Sequence[Mapping[str, Any]], *, start_s: float, end_s: float
) -> list[dict]:
    """Every card row moved onto the card's window; other rows unchanged."""
    return [
        {**row, "start_s": round(start_s, 3), "end_s": round(end_s, 3)}
        if is_closing_card_row(row)
        else dict(row)
        for row in rows
    ]


def closing_card_from_recipe(recipe: Any) -> dict[str, Any] | None:
    """The card a pinned recipe ends on (``{"duration_s", "background_color"}``), for the
    editor's Save, which compiles the recipe from scratch. None without one."""
    for fill in getattr(recipe, "visual_fills", None) or []:
        if fill.id != CLOSING_CARD_FILL_ID:
            continue
        channels = (fill.color.red, fill.color.green, fill.color.blue)
        return {
            "duration_s": round(float(fill.end) - float(fill.start), 3),
            "background_color": "#" + "".join(f"{round(c * 255):02X}" for c in channels),
        }
    return None


def closing_card_receipt(
    spec: ClosingCardSpec, recipe: Any, rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """What the compiled recipe really ends on, read back from it (never from the plan):
    ``applied`` only with the fill, the held frame under it and the words on it."""
    fill = next(
        (f for f in getattr(recipe, "visual_fills", None) or [] if f.id == CLOSING_CARD_FILL_ID),
        None,
    )
    card_rows = [row for row in rows if is_closing_card_row(row)]
    base = {
        "text": spec.text,
        "background_color": spec.background_color,
        "text_color": spec.text_color,
        "text_color_asked": spec.text_color_asked,
        "text_color_swapped": spec.text_color_swapped,
        "requirement_id": spec.requirement_id,
    }
    if fill is None or not card_rows:
        return {
            **base,
            "status": "dropped",
            "reason": "not_compiled" if fill is None else "no_text",
        }
    return {
        **base,
        # What the card really shows (the brief check compares it with the creator's words).
        "shown_text": " ".join(str(card_rows[0].get("text") or "").split()),
        "status": "applied",
        "start_s": round(float(fill.start), 3),
        "end_s": round(float(fill.end), 3),
        "duration_s": round(float(fill.end) - float(fill.start), 3),
    }


def align_rows_to_recipe(rows: Sequence[Mapping[str, Any]], recipe: Any) -> list[dict]:
    """The persisted rows as the compiled ``recipe`` plays them: card rows on the card's
    window, whole-video rows (corner text) to the recipe's end. The editor's Save recompiles
    from these rows, so they must say what the device drew."""
    fill = next(
        (f for f in getattr(recipe, "visual_fills", None) or [] if f.id == CLOSING_CARD_FILL_ID),
        None,
    )
    end = round(float(recipe.duration), 3)
    out = []
    for row in rows:
        params = row.get("source_params") or {}
        if fill is not None and is_closing_card_row(row):
            out.append(
                {**row, "start_s": round(float(fill.start), 3), "end_s": round(float(fill.end), 3)}
            )
        elif params.get("whole_video"):
            out.append({**row, "end_s": end})
        else:
            out.append(dict(row))
    return out
