"""Account text rules; stored in the existing versioned direction ledger."""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TextStyleRole = Literal["all_text", "title", "label", "pinned", "caption", "lyric"]
TEXT_STYLE_ROLES = ("all_text", "title", "label", "pinned", "caption", "lyric")
TEXT_STYLE_KEYS = frozenset(f"text_style_{role}" for role in TEXT_STYLE_ROLES)
PHONE_TEXT_STYLE_VERSION = 1
CURATED_PHONE_FONTS = (
    "Alte Haas Grotesk",
    "Big Curls",
    "Hanken Grotesk",
    "Inter",
    "Inter Tight",
    "Montserrat",
    "Montserrat Bold",
    "Nunito ExtraBold",
    "Pacifico",
    "Permanent Marker",
    "Poppins Bold",
    "Satisfy",
    "TikTok Sans Bold",
    "Unbounded",
    "Inter Medium",
)
CAPTION_PREFERRED_FONT = "TikTok Sans Bold"
DEFAULT_STROKE_WIDTH = 0
DEFAULT_SHADOW_ENABLED = False

# Named looks a creator-confirmed line can carry (``PinnedText.look``). Read-only on purpose:
# callers copy (``dict(TIKTOK_TEXT_LOOK)``) and then apply their own overrides.
CLIP_TEXT_LOOKS = ("tiktok",)
# Qendresa item 21, measured on her videos: TikTok Sans Bold, white, 56 px on a 1080x1920
# canvas, wrapping at 0.72 of the width, with a faint soft shadow (the authored shadow paint
# at opacity 0.2).
TIKTOK_TEXT_LOOK = MappingProxyType(
    {
        "font_family": "TikTok Sans Bold",
        "color": "#FFFFFF",
        "size_px": 56,
        "max_width_frac": 0.72,
        "shadow_enabled": True,
        "shadow_color": "#000000",
        "shadow_opacity": 0.2,
        "stroke_width": 0,
        "alignment": "center",
    }
)


class TextStyleRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    font_family: str | None = None
    automatic_font: bool | None = None
    favorite_fonts: list[str] = Field(default_factory=list, max_length=52)
    excluded_fonts: list[str] = Field(default_factory=list, max_length=52)
    position: (
        Literal["top_left", "top", "top_right", "middle", "bottom_left", "bottom", "bottom_right"]
        | None
    ) = None
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    stroke_width: float | None = Field(default=None, ge=0, le=20, allow_inf_nan=False)
    shadow_enabled: bool | None = None

    @field_validator("font_family", "favorite_fonts", "excluded_fonts")
    @classmethod
    def supported_fonts(cls, value):
        from app.agents._schemas.text_element import _ALLOWED_FONTS

        names = [value] if isinstance(value, str) else value or []
        if any(name not in _ALLOWED_FONTS for name in names):
            raise ValueError("Choose a supported bundled font")
        return list(dict.fromkeys(names)) if isinstance(value, list) else value

    @model_validator(mode="after")
    def consistent_fonts(self):
        if self.automatic_font is True and self.font_family is not None:
            raise ValueError("Choose automatic selection or one required font")
        if set(self.favorite_fonts) & set(self.excluded_fonts):
            raise ValueError("A favorite font cannot also be excluded")
        if self.font_family in self.excluded_fonts:
            raise ValueError("A required font cannot also be excluded")
        return self

    def values(self) -> dict:
        return self.model_dump(exclude_none=True, exclude_defaults=True)


def preferences_from_direction(typed: dict) -> dict[str, dict]:
    return {
        role: TextStyleRule.model_validate(typed[f"text_style_{role}"]).values()
        for role in TEXT_STYLE_ROLES
        if f"text_style_{role}" in typed
    }


_TEXT_SUBJECT = re.compile(
    r"\b(?:texts?|titles?|captions?|subtitles?|labels?|lyrics?|pinned)\b"
    r"|altyaz[ıi]|başl[ıi]k|yaz[ıi]|etiket"
)
_OTHER_SUBJECT = re.compile(
    r"\b(?:me|myself|i'm|i am|my face|the speaker|ben|beni|benim|bana)\b"
    r"|\b(?:photo|picture|image|video|clip|card|logo|sticker|background|backdrop)\w*"
    r"|\b(?:fotoğraf|resim|resm|video|kart|arka plan)"
)
# A colour word before one of these paints something else, not the text.
_NOT_TEXT_COLOUR = "background|outline|stroke|shadow|box|card|frame|bar|logo|arka plan"
# The text roles a message can style, each with the words that name it.
_ROLE_WORDS = (
    ("title", r"\btitles?\b|başlık"),
    ("caption", r"\bcaptions?\b|altyazı"),
    ("label", r"\blabels?\b|etiket"),
    ("pinned", r"\bpinned\b"),
    ("lyric", r"\blyrics?\b"),
)
_PLACES = (
    ("lower left", "bottom_left"),
    ("bottom left", "bottom_left"),
    ("upper left", "top_left"),
    ("top left", "top_left"),
    ("lower right", "bottom_right"),
    ("bottom right", "bottom_right"),
    ("upper right", "top_right"),
    ("top right", "top_right"),
    ("sol alt", "bottom_left"),
    ("sol üst", "top_left"),
    ("in the middle", "middle"),
    ("in the center", "middle"),
    ("in the centre", "middle"),
    ("ortada", "middle"),
    ("ortaya", "middle"),
)
_COLOURS = (
    ("white|beyaz", "#FFFFFF"),
    ("black|siyah", "#000000"),
    ("yellow|sar[ıi]", "#FFFF00"),
    ("red|k[ıi]rm[ıi]z[ıi]", "#FF0000"),
    ("blue|mavi", "#0000FF"),
)
# What a segment is about when it names no text role: on-screen text in general, or not text.
_ALL_TEXT, _OTHER = "all_text", "other"


def _roles_named(text: str) -> list[str]:
    return [role for role, pattern in _ROLE_WORDS if re.search(pattern, text)]


def _subject(part: str) -> str | None:
    """The text role one segment names, "mixed" for several, ``_ALL_TEXT`` for plain text words,
    ``_OTHER`` for the speaker or another object, None for no subject."""
    if _TEXT_SUBJECT.search(part):
        roles = _roles_named(part)
        return roles[0] if len(roles) == 1 else "mixed" if roles else _ALL_TEXT
    return _OTHER if _OTHER_SUBJECT.search(part) else None


def _text_scope(text: str) -> list[tuple[str, str]]:
    """The parts of a message about on-screen text, each with the text role it is about: a place
    or colour counts only there.

    Sentences split into segments (commas, colons, "and"); a segment naming no subject
    follows the subject before it, or the first one after it, and a sentence naming none
    follows the last one ("Captions in white. I'd like them in the middle"). A quoted string
    is text. Plain text words after a named role describe that role ("Captions word by word,
    black bold text ...", "Make the title bold. The text should be white"); only plain text
    named before any role is all text ("Small white text, no captions")."""
    scope = []
    carried = None
    named = None
    text = re.sub(r'"[^"]*"|“[^”]*”', " text ", text)
    for sentence in re.split(r"[.!?;…]+(?=\s|$)|\n", text):
        segments = re.split(r"[,:]|\s(?:and|ve|but|ama)\s", sentence)
        kinds = [_subject(part) for part in segments]
        kind = next((k for k in kinds if k), carried)
        for part, own in zip(segments, kinds, strict=True):
            if own == _ALL_TEXT and named is not None:
                own = named
            elif own not in (None, _OTHER, _ALL_TEXT):
                named = own
            kind = own or kind
            if kind not in (None, _OTHER):
                scope.append((part, kind))
        carried = kind
    return scope


def _styled_role(named: str, owners: set[str], *, whole_message_values: bool) -> str | None:
    """The role a rule styles. ``named`` is the one role word anywhere in the message (or
    ``_ALL_TEXT``); ``owners`` are the roles of the segments holding its place / colour words.

    Qendresa case 3: a role named only where no place or colour sits ("Small white text in the
    middle, no captions") is not the styled role; the words style all text. Outline, shadow and
    font words are read from the whole message, so with any of them the role is unknown."""
    if named == _ALL_TEXT or not owners or named in owners:
        return named
    return None if whole_message_values else _ALL_TEXT


def explicit_text_rule(instruction: str) -> tuple[str, dict] | None:
    """Only executable styling words. The caller decides durable vs local scope."""
    from app.agents._schemas.text_element import _ALLOWED_FONTS
    from app.config import settings

    text = instruction.casefold()
    roles = _roles_named(text)
    if len(roles) > 1:
        return None
    role = roles[0] if roles else _ALL_TEXT
    values = {}
    if re.search(
        r"\b(no|without|never (?:add|use)) (?:text )?(?:outlines?|strokes?)\b"
        r"|kenarlık (?:olmasın|yok)",
        text,
    ):
        values["stroke_width"] = 0
    if re.search(
        r"\b(no|without|never (?:add|use)) (?:text )?shadows?\b|gölge (?:olmasın|yok)", text
    ) or re.search(r"\b(no|without) outlines? (?:or|and) shadows?\b", text):
        values["shadow_enabled"] = False
    if re.search(r"\b(add|use|with) (?:text )?outlines?\b", text) and "stroke_width" not in values:
        values["stroke_width"] = 2
    if re.search(r"\b(add|use|with) (?:text )?shadows?\b", text) and "shadow_enabled" not in values:
        values["shadow_enabled"] = True
    # Each place and colour, with the role of the text segment it sits in.
    scope = _text_scope(text)
    places = [
        (position, owner) for part, owner in scope for phrase, position in _PLACES if phrase in part
    ]
    if len({position for position, _ in places}) == 1:
        values["position"] = places[0][0]
    remaining = text
    fonts = []
    for font in sorted(_ALLOWED_FONTS, key=len, reverse=True):
        pattern = rf"(?<!\w){re.escape(font.casefold())}(?!\w)"
        if re.search(pattern, remaining):
            fonts.append(font)
            remaining = re.sub(pattern, "", remaining)
    font_cue = re.search(r"\bfonts?\b|typeface|yazı tipi", text) or any(
        re.search(
            rf"\b(?:use|choose|select|set|kullan)\s+(?:the\s+)?{re.escape(font.casefold())}"
            r"(?=\s+(?:for|on|as|and|with|without|\d)|\s*[,.;!]|\s*$)",
            text,
        )
        for font in fonts
    )
    if fonts and font_cue:
        if re.search(r"\bnever use|\bexclude|\bdon't use|asla", text):
            values["excluded_fonts"] = fonts
        elif re.search(r"\bfavou?rites?\b", text):
            values["favorite_fonts"] = fonts
        elif len(fonts) == 1:
            values["font_family"] = fonts[0]
    colors = [
        (color, owner)
        for part, owner in scope
        for words, color in _COLOURS
        if re.search(rf"\b(?:{words})\b(?!\s+(?:{_NOT_TEXT_COLOUR}))", part)
    ]
    if len({color for color, _ in colors}) == 1:
        values["color"] = colors[0][0]
    if not values:
        return None
    if settings.text_style_segment_role_enabled:
        role = _styled_role(
            role,
            {owner for _, owner in places + colors},
            whole_message_values=bool(values.keys() - {"position", "color"}),
        )
        if role is None:
            return None
    return role, TextStyleRule.model_validate(values).values()


def effective_text_rule(preferences: dict, requested: dict, role: str) -> dict:
    values = {}
    for source in (
        preferences.get("all_text", {}),
        preferences.get(role, {}),
        requested.get("all_text", {}),
        requested.get(role, {}),
    ):
        values.update(source)
        if source.get("automatic_font") is True:
            values.pop("font_family", None)
        if source.get("font_family"):
            values["automatic_font"] = False
            values["excluded_fonts"] = [
                font for font in values.get("excluded_fonts", []) if font != source["font_family"]
            ]
        if "favorite_fonts" in source:
            values["excluded_fonts"] = [
                font
                for font in values.get("excluded_fonts", [])
                if font not in source["favorite_fonts"]
            ]
        if "excluded_fonts" in source:
            values["favorite_fonts"] = [
                font
                for font in values.get("favorite_fonts", [])
                if font not in source["excluded_fonts"]
            ]
        if (
            "excluded_fonts" in source
            and values.get("font_family") in source["excluded_fonts"]
            and not source.get("font_family")
        ):
            values.pop("font_family", None)
    return TextStyleRule.model_validate(values).values()
