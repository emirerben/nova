"""The creator's caption look, as typed facts on the brief (KRI-1177).

"Captions small, white, lowercase, in the middle" lands on the brief's captions requirement as
``caption_size`` / ``caption_position`` / ``caption_color`` / ``caption_case``
(`main_creator._BRIEF_PROMPT_SECTION`). The phone Talking writer applies them to the FIRST draft
(`phone_text_styling.stamp_phone_text_style` / `generation_caption_look`): the creator's ask wins
over the default look, also where it lands on a face or the corner speaker, and the receipt says
so (`brief_checks`). Only typed values are read, never the creator's words (KRI-667); a value
outside the vocabulary below is dropped, so it can never reach a variant.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.kria.reply_language import say

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

# The pixel size each asked size draws at on the 1080x1920 canvas. The default caption is 78 px
# (`phone_captions._CAPTION_TEXT_SIZE_PX`); the readable floor is 40 px.
CAPTION_ASK_SIZE_PX: dict[str, int] = {"small": 52, "large": 96}
CAPTION_ASK_POSITIONS = (
    "top_left",
    "top",
    "top_right",
    "middle",
    "bottom_left",
    "bottom",
    "bottom_right",
)
CAPTION_ASK_CASES = ("lower", "upper")
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")  # kri667: validates a typed hex fact, reads no creator text
# A colour word the brief writer may still write instead of its hex (EN / TR).
_COLOR_NAMES = {
    "white": "#FFFFFF",
    "beyaz": "#FFFFFF",
    "black": "#000000",
    "siyah": "#000000",
    "yellow": "#FFFF00",
    "sarı": "#FFFF00",
    "red": "#FF0000",
    "kırmızı": "#FF0000",
    "blue": "#0000FF",
    "mavi": "#0000FF",
}
_COLOR_WORDS = {
    "#FFFFFF": ("white", "beyaz"),
    "#000000": ("black", "siyah"),
    "#FFFF00": ("yellow", "sarı"),
    "#FF0000": ("red", "kırmızı"),
    "#0000FF": ("blue", "mavi"),
}
_SIZE_WORDS = {"small": ("small", "küçük"), "large": ("large", "büyük")}
_CASE_WORDS = {"lower": ("lowercase", "küçük harfli"), "upper": ("uppercase", "büyük harfli")}
_POSITION_WORDS = {
    "top_left": ("at the top left", "sol üstte"),
    "top": ("at the top", "üstte"),
    "top_right": ("at the top right", "sağ üstte"),
    "middle": ("in the middle", "ortada"),
    "bottom_left": ("at the bottom left", "sol altta"),
    "bottom": ("at the bottom", "altta"),
    "bottom_right": ("at the bottom right", "sağ altta"),
}

# Where each asked position puts the caption block on the canvas (x0, y0, x1, y1), from the
# writer's own anchors (`phone_text_style._candidates`): a left / right corner at x 0.06 / 0.82
# with the 0.68 caption width, a bottom block ending at y 0.70, a top block starting at 0.12.
_ZONE_X = {"left": (0.06, 0.74), "center": (0.16, 0.84), "right": (0.14, 0.82)}
_ZONE_Y = {"top": (0.12, 0.24), "middle": (0.44, 0.56), "bottom": (0.58, 0.70)}


@dataclass(frozen=True)
class CaptionLookAsk:
    """The typed caption look one brief asks for; ``None`` fields were not asked."""

    size: str | None = None
    position: str | None = None
    color: str | None = None
    case: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("size", self.size),
                ("position", self.position),
                ("color", self.color),
                ("case", self.case),
            )
            if value is not None
        }

    @classmethod
    def from_dict(cls, raw: object) -> CaptionLookAsk | None:
        """A stamped ``phone_text_style.caption_ask`` back, re-validated; None when empty."""
        if not isinstance(raw, Mapping):
            return None
        return _ask(
            size=raw.get("size"),
            position=raw.get("position"),
            color=raw.get("color"),
            case=raw.get("case"),
        )


def _color(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _HEX.match(text):
        return text.upper()
    return _COLOR_NAMES.get(text.casefold())


def _ask(*, size: object, position: object, color: object, case: object) -> CaptionLookAsk | None:
    ask = CaptionLookAsk(
        size=size if size in CAPTION_ASK_SIZE_PX else None,
        position=position if position in CAPTION_ASK_POSITIONS else None,
        color=_color(color),
        case=case if case in CAPTION_ASK_CASES else None,
    )
    return ask if ask.as_dict() else None


def caption_look_ask(req: BriefRequirement) -> CaptionLookAsk | None:
    """The typed look on ``req``'s facts, invalid values dropped; None when it asks for none."""
    facts: Mapping[str, Any] = req.facts if isinstance(req.facts, Mapping) else {}
    return _ask(
        size=facts.get("caption_size"),
        position=facts.get("caption_position"),
        color=facts.get("caption_color"),
        case=facts.get("caption_case"),
    )


def brief_caption_look(brief: CreativeBrief | None) -> CaptionLookAsk | None:
    """The look every live requirement asks for, merged in brief order (a later value wins)."""
    if brief is None:
        return None
    merged: dict[str, str] = {}
    for req in brief.live():
        ask = caption_look_ask(req)
        if ask is not None:
            merged.update(ask.as_dict())
    return CaptionLookAsk(**merged) if merged else None


def caption_alignment(position: str) -> str:
    """The horizontal alignment the writer draws ``position`` with."""
    return (
        "left"
        if position.endswith("_left")
        else "right"
        if position.endswith("_right")
        else "center"
    )


def _zone(position: str) -> tuple[float, float, float, float]:
    row = (
        "top"
        if position.startswith("top")
        else "bottom"
        if position.startswith("bottom")
        else "middle"
    )
    x0, x1 = _ZONE_X[caption_alignment(position)]
    y0, y1 = _ZONE_Y[row]
    return x0, y0, x1, y1


def caption_over_speaker(position: str | None, frame: str | None) -> bool:
    """True when captions asked at ``position`` sit on the corner speaker preset ``frame``.

    The speaker box is the preset's width around its centre, as tall as it is wide (a portrait
    take, the tallest the corner gets), so the answer never under-reports an overlap."""
    from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS  # noqa: PLC0415

    if position not in CAPTION_ASK_POSITIONS or frame not in SPEAKER_FRAME_PRESETS:
        return False
    preset = SPEAKER_FRAME_PRESETS[frame]
    half = preset["width_fraction"] / 2
    speaker = (
        preset["x_fraction"] - half,
        preset["y_fraction"] - half,
        preset["x_fraction"] + half,
        preset["y_fraction"] + half,
    )
    zone = _zone(position)
    return min(zone[2], speaker[2]) > max(zone[0], speaker[0]) and min(zone[3], speaker[3]) > max(
        zone[1], speaker[1]
    )


def _join(parts: list[str], conjunction: str) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} {conjunction} {parts[-1]}"


def caption_look_reason(ask: CaptionLookAsk, *, over_speaker: bool, over_face: bool) -> str:
    """What the captions look like, in the chat's language, plus where they cover the speaker.

    "Captions are small, white, lowercase and in the middle." -- the creator's ask is applied
    as given, so a spot on the corner speaker or a face is said, never silently moved."""
    en_parts: list[str] = []
    tr_parts: list[str] = []
    if ask.size is not None:
        en_parts.append(_SIZE_WORDS[ask.size][0])
        tr_parts.append(_SIZE_WORDS[ask.size][1])
    if ask.color is not None:
        en, tr = _COLOR_WORDS.get(ask.color, (f"coloured {ask.color}", f"{ask.color} renginde"))
        en_parts.append(en)
        tr_parts.append(tr)
    if ask.case is not None:
        en_parts.append(_CASE_WORDS[ask.case][0])
        tr_parts.append(_CASE_WORDS[ask.case][1])
    if ask.position is not None:
        en_parts.append(_POSITION_WORDS[ask.position][0])
        tr_parts.append(_POSITION_WORDS[ask.position][1])
    en = f"Captions are {_join(en_parts, 'and')}"
    tr = f"Altyazılar {_join(tr_parts, 've')}"
    if over_speaker:
        en += "; they sit over you in the corner, as you asked"
        tr += "; istediğin gibi köşedeki görüntünün üstünde duruyorlar"
    if over_face:
        en += "; they cover a face in places, as you asked"
        tr += "; istediğin gibi yer yer bir yüzün üstüne geliyorlar"
    return say(en=en + ".", tr=tr + ".")


def apply_caption_case(
    cues: Iterable[Mapping[str, Any]], case: str | None, language: str | None = None
) -> list[dict[str, Any]]:
    """``cues`` with the asked case applied to each cue's text AND its timed words.

    The cue text is what the editor shows and a Save keeps, so the case lives there, not only in
    the drawn layer. Turkish dotted / dotless i follow `apply_text_case`'s rules (KRI-781)."""
    from app.agents._schemas.text_element import apply_text_case  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for cue in cues:
        row = dict(cue)
        if case in CAPTION_ASK_CASES:
            row["text"] = apply_text_case(str(row.get("text") or ""), case, language)
            if isinstance(row.get("words"), list):
                row["words"] = [
                    {**word, "text": apply_text_case(str(word.get("text") or ""), case, language)}
                    if isinstance(word, Mapping)
                    else word
                    for word in row["words"]
                ]
        out.append(row)
    return out
