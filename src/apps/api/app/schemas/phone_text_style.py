"""Account text rules; stored in the existing versioned direction ledger."""

from __future__ import annotations

import re
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


def explicit_text_rule(instruction: str) -> tuple[str, dict] | None:
    """Only executable styling words. The caller decides durable vs local scope."""
    from app.agents._schemas.text_element import _ALLOWED_FONTS

    text = instruction.casefold()
    roles = [
        role
        for role, pattern in (
            ("title", r"\btitles?\b|başlık"),
            ("caption", r"\bcaptions?\b|altyazı"),
            ("label", r"\blabels?\b|etiket"),
            ("pinned", r"\bpinned\b"),
            ("lyric", r"\blyrics?\b"),
        )
        if re.search(pattern, text)
    ]
    if len(roles) > 1:
        return None
    role = roles[0] if roles else "all_text"
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
    places = [
        position
        for phrase, position in (
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
        )
        if phrase in text
    ]
    if len(set(places)) == 1:
        values["position"] = places[0]
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
    colors = {
        name: color
        for name, color in (
            ("white", "#FFFFFF"),
            ("black", "#000000"),
            ("yellow", "#FFFF00"),
            ("red", "#FF0000"),
            ("blue", "#0000FF"),
        )
        if re.search(
            rf"\b{name} (?:text|titles?|captions?|labels?)\b"
            r"|\b(?:text|titles?|captions?|labels?)\s+"
            rf"(?:(?:should be|must be|in|are)\s+)?{name}\b",
            text,
        )
    }
    if len(colors) == 1:
        values["color"] = next(iter(colors.values()))
    if not values:
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
