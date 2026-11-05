"""Item 14's text rules for the creator's CTA ending ("comment KEYWORD" + arrow ask).

Kept free of render imports (stdlib only, no skia) so the creator-render contract
verifier and the brief receipts can import them anywhere: the creator-contract replay CI
job has no libEGL for skia. The drawing and placement live in `phone_cta_ending`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

CTA_LEAD_FONT = "Caveat Brush"
CTA_LEAD_COLOR = "#FFFFFF"
CTA_KEYWORD_FONT = "Bricolage Grotesque"
CTA_KEYWORD_COLOR = "#FCF3C3"
CTA_ARROW_ID = "closing-arrow"
CTA_KEYWORD_ELEMENT_ID = "closing-title-keyword"
CTA_PART_KEY = "cta_part"

_PARTS_RE = re.compile(r"^(comment)\s+(\S.*)$", re.IGNORECASE)


def cta_ending_parts(text: str | None) -> tuple[str, str] | None:
    """("comment", KEYWORD) from "comment KEYWORD", else None.

    The two parts joined by one space equal the whitespace-collapsed text exactly (the
    approved contract matches the creator's exact closing text), so the keyword keeps its
    punctuation and quotes. "Comment: READ" or a text wrapped in quotes is not split."""
    if not text:
        return None
    collapsed = " ".join(str(text).split())
    match = _PARTS_RE.match(collapsed)
    if match is None:
        return None
    keyword = match.group(2)
    if not any(ch.isalnum() for ch in keyword):
        return None
    if len(keyword.split()) > 3 or len(keyword) > 24:
        return None
    return match.group(1), keyword


_QUOTED_RE = re.compile(r'"[^"]*"|“[^”]*”')
_ARROW_RES = (
    re.compile(r"\barrows?\b", re.IGNORECASE),
    re.compile(r"\bok işaret", re.IGNORECASE),
    re.compile(r"\bbir ok\b", re.IGNORECASE),
    re.compile(r"\bok çiz", re.IGNORECASE),
    re.compile(r"\bok ekle\b", re.IGNORECASE),
    re.compile(r"\bok koy\b", re.IGNORECASE),
    re.compile(r"\boklu\b", re.IGNORECASE),
    re.compile(r"\bokla\b", re.IGNORECASE),
    # Not a bare "oku": it is also "read!", a likely keyword ("yorum yaz OKU").
    re.compile(r"[→↘⤵➡⬇↓➜➝↗]"),
)
# Refusals: "no arrow", "without an arrow", "don't add an arrow", and the Turkish "oksuz",
# "ok olmasın", "ok koyma", "ok ekleme".
_NEGATED_RES = (
    # Only plain refusals: "no arrow(s)", "without an arrow", "don't add an arrow". A wider
    # window would swallow real asks ("no captions and an arrow", "not just text but an
    # arrow", "don't forget the arrow").
    re.compile(r"\b(?:no|without)\s+(?:an?\s+|any\s+|the\s+)?arrows?\b", re.IGNORECASE),
    re.compile(
        r"\b(?:don['’]?t|do not|never)\s+(?:add|put|draw|use|include|want|need)\s+"
        r"(?:an?\s+|any\s+|the\s+)?arrows?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\boksuz", re.IGNORECASE),
    re.compile(r"\bok (?:olmas[ıi]n|koyma|ekleme)", re.IGNORECASE),
)


def requirement_asks_arrow(req: Any) -> bool:
    """Does this brief requirement ask for an arrow? Quoted text and the literal are
    the creator's words to show, not a request, so they are ignored; a negated mention
    ("no arrow", "oksuz") is not an ask."""
    try:
        pieces = [getattr(req, "description", None)]
        facts = getattr(req, "facts", None)
        if isinstance(facts, dict):
            pieces.extend(v for v in facts.values() if isinstance(v, str))
        literal = getattr(req, "literal", None)
        for piece in pieces:
            if not isinstance(piece, str) or not piece:
                continue
            text = _QUOTED_RE.sub(" ", piece)
            if isinstance(literal, str) and literal:
                text = re.sub(re.escape(literal), " ", text, flags=re.IGNORECASE)
            for rx in _NEGATED_RES:
                text = rx.sub(" ", text)
            if any(rx.search(text) for rx in _ARROW_RES):
                return True
    except Exception:  # noqa: BLE001 - detection never breaks a draft
        return False
    return False


_ENDING_NAMED_RES = (
    re.compile(r"\b(?:end|ending|ends|last|closing|close|finish|outro)\b", re.IGNORECASE),
    re.compile(r"\b(?:son|sonunda|sonda|kapanış|bitiş)", re.IGNORECASE),
)


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def _is_about_ending(req: Any, closing_text: str) -> bool:
    target = _fold(closing_text)
    if not target:
        return False
    literal = getattr(req, "literal", None)
    if isinstance(literal, str) and _fold(literal) == target:
        return True
    facts = getattr(req, "facts", None)
    if isinstance(facts, dict):
        closing = facts.get("closing")
        if isinstance(closing, str) and _fold(closing) == target:
            return True
    description = getattr(req, "description", None)
    if not isinstance(description, str) or not description:
        return False
    if target in _fold(description):
        return True
    unquoted = _QUOTED_RE.sub(" ", description)
    return any(rx.search(unquoted) for rx in _ENDING_NAMED_RES)


def closing_arrow_asked(requirements: Iterable[Any], closing_text: str) -> bool:
    """An arrow asked for the ENDING: a requirement about the closing text (its literal or
    closing fact is that text, or its description names it or the ending) that asks for one.
    An arrow asked for other text ("MENU" pointing at the board) is not the closing arrow."""
    try:
        return any(
            _is_about_ending(req, closing_text) and requirement_asks_arrow(req)
            for req in requirements
        )
    except Exception:  # noqa: BLE001
        return False
