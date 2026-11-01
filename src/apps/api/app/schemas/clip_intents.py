"""Generic creator intents over clips, and the grounding fence for labels and
captions (KRI-127, extended by KRI-129).

One open-vocabulary intent replaces the per-feature strategy fields
(``sport_labels``, ``context_label.kind``...): "label / group / order / include
/ caption / exclude clips by <creator-described attribute>". A model step resolves an
intent into per-clip assignments with evidence and a confidence; this module
owns the contract and the ONLY rule by which resolved text may reach the
screen.

On-screen text fence (replaces the closed sport allowlist): a label (per-clip
corner text) or a caption (one on-screen phrase for a whole chapter — the
group of clips an intent's membership resolves to) is rendered only when it is

* ``creator_text``     the creator's own words (found in the confirmed request),
* ``record_span``      every word of it appears in what the vision model wrote
                       about the clip (a label: THAT clip; a caption: the
                       UNION of every member clip's record), resolver
                       confidence >= 0.8, or
* ``vision_verified``  the vision model answered it when asked (a label: about
                       THAT clip; a caption: about one representative member
                       clip), confidence >= 0.8.

Anything else is omitted and the creator is asked instead. Spoken transcripts
never ground a label or caption here (speech is third-party text, not vision
evidence).

A caption differs from a label in shape, not in the fence: a label is a <=3
word per-clip tag (``label_text``/``LABEL_MAX_*``); a caption is a <=10 word
phrase for the whole chapter (``clean_caption_text``/``CAPTION_MAX_*``,
``ground_caption``). ``op="caption"`` assignments stay membership-only (like
``group``/``order``/``include``/``exclude`` — ``value`` is always ``None``); the one
authored/quoted caption phrase lives on ``ResolvedClipIntent.caption_text`` +
``caption_grounding`` instead of per-assignment ``value``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Literal

import regex
from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.clip_understanding import ClipUnderstanding

# KRI-667: ``exclude`` leaves the clips matching ``attribute`` OUT of the edit ("Skip
# the quad bike clip"). Membership only, like ``include`` but inverted: the resolver
# keeps a clip for it only at ``LABEL_MIN_CONFIDENCE`` (a wrong drop removes footage the
# creator wanted) and never asks the creator about it. Always accepted here, so a
# strategy persisted while CLIP_INTENT_EXCLUDE_ENABLED was on still loads after it is off.
# Leverkusen M2 (2026-10-30): ``hide_part`` keeps ONE clip in the edit but never shows a part
# at its start or end ("the kids at the start of the second goal clip are another family's
# children, so don't show them"). A separate op, never a field on ``exclude``: every
# whole-file consumer of ``exclude`` (scope, picker, ties, receipts) would read a partial
# exclusion as "drop this file" and lose the rest of the clip, and one that forgot the field
# would show the part. Membership only for the resolver; the seconds are the creator's
# (``part_s``) and the window is enforced at planning and on the pinned recipe
# (``app.pipeline.hidden_parts``).
ClipIntentOp = Literal["label", "group", "order", "include", "caption", "exclude", "hide_part"]
# Which end of the clip a ``hide_part`` part sits at.
HiddenPartEdge = Literal["start", "end"]
# The longest part a creator can hide by seconds (same bound as ``under_s``).
MAX_HIDDEN_PART_S = 600.0
ClipOrderPosition = Literal["first", "last"]
# KRI-189: a fact-based ORDER over EVERY clip (not a first/last placement of a
# described subset): "in the order I filmed them" -> capture_time, "along my
# route" -> route. `route` uses capture-time order for now (the order a creator
# walked a route is the order they filmed it in); the distinction is kept so a
# later geographic ordering needs no schema change.
ClipOrderBy = Literal["capture_time", "route"]
# ``placeholder`` (KRI-282) is system-owned copy the creator explicitly asked for
# ("add a text placeholder so I can replace it with their real names"). It is
# the fixed ``PLACEHOLDER_LABEL_TEXT``, never model output, so it needs no
# source-provenance or vision evidence -- see ``ground_placeholder_label``.
GroundingSource = Literal["creator_text", "record_span", "vision_verified", "placeholder"]
ResolutionStatus = Literal["resolved", "needs_creator"]

# A word-for-word story edit is one caption per chapter plus its orders and
# include: a 6-chapter request is 8+ operations on its own, 16 with a group per
# chapter. KRI-1122 (Leverkusen M4): a 10-beat story with labels, one skip, one
# keep and one sound rule is ~18 operations, and at 16 every "restate just those"
# re-read the same request and hit the cap again. 40 holds a 10-beat story with a
# label, an order and a group per beat.
MAX_CLIP_INTENTS = 40
INTENT_ID_MAX_CHARS = 40
# KRI-282: the generic stand-in printed on every clip a "placeholder" label
# targets. System-chosen on purpose: the fence's invariant is that model or
# Gemini output never becomes on-screen text, and the creator replaces this in
# the editor anyway.
PLACEHOLDER_LABEL_TEXT = "Name"
LABEL_MIN_CONFIDENCE = 0.8
LABEL_MAX_CHARS = 24
LABEL_MAX_WORDS = 3
# KRI-678: the creator's OWN label words ("Parque de la Costa", "Sagrada Família de
# Gaudí") are a name, not a model-authored tag, so they may run longer. KRI-826: the
# label bar, not a word count, bounds them. Model and vision copy keep LABEL_MAX_WORDS
# and LABEL_MAX_CHARS.
# KRI-804: 24 characters was never the renderer's bound. The montage label bar (Fraunces
# 58px in a 0.82 x 1080 = 885.6px box, cloud Skia and the phone alike) draws
# "chimichurri & salsa criolla" (27) on one line at 805.5px, and both renderers wrap a
# longer label, never clip it (`make verify-overlays`: kri804_montage_clip_label_width).
# 28 is what that bar holds on ONE line at the measured ~30px per character.
# KRI-826: this is a hint, not a limit. A creator label is bounded by what the bar draws
# (`app.pipeline.text_fit.creator_label_fits`: at most two lines).
CREATOR_LABEL_MAX_CHARS = 28
# KRI-826: a storage sanity bound for the creator's own label words (code points, the
# unit pydantic's max_length counts). Two lines of the bar hold well under it.
CREATOR_LABEL_STORAGE_MAX = 80
# Membership (group/order/include/caption) moves clips but prints nothing
# itself, so it needs less certainty than a label's per-clip printed value
# does.
MEMBERSHIP_MIN_CONFIDENCE = 0.6
# KRI-667: an `exclude` REMOVES a clip from the edit, and a wrong removal loses footage
# the creator wanted, so it is held to the label bar, never the membership bar.
EXCLUDE_MIN_CONFIDENCE = LABEL_MIN_CONFIDENCE
# KRI-761: the evidence on an assignment the creator settled themself, by tapping clips in
# the picker or typing a file name. Never a resolver guess, so never asked about again.
CREATOR_SELECTED_EVIDENCE = "creator selected"
FILE_NAME_EVIDENCE = "file name"
CREATOR_EVIDENCE = frozenset({CREATOR_SELECTED_EVIDENCE, FILE_NAME_EVIDENCE})
# KRI-779: the evidence on an assignment settled because the project's only video is the
# only place the described moment can be. Nobody located the moment INSIDE that take, so
# this is not the creator's word (never in CREATOR_EVIDENCE).
ONLY_VIDEO_EVIDENCE = "the only video"
# KRI-1031: the evidence on an `exclude` assignment the server settled from the clip's
# measured length ("too short to use"). Not the creator's tap, and not a resolver guess.
LENGTH_EVIDENCE = "clip length"

# A caption is a phrase, not a tag: longer than a label, but still short
# on-screen text for one chapter (never a full sentence). These shape bounds
# limit what a MODEL may author or ground from footage.
CAPTION_MAX_CHARS = 60
CAPTION_MAX_WORDS = 10
# The creator's own word-for-word caption copy ("show each chapter line word for
# word") is printed as written, so only its length is bounded: the same bound as
# a Creative Brief literal (`app/kria/brief.py`) for the same words; a story beat
# line holds 280.
CREATOR_CAPTION_MAX_CHARS = 200

# KRI-804: the typographic apostrophe too; iOS smart punctuation types "Julio’s".
_LABEL_ALLOWED = re.compile(r"[^\w\s&'’\-]", re.UNICODE)
# Captions are ordinary short phrases: allow common sentence punctuation on
# top of the label charset. Newlines can never survive `_clean` (it collapses
# all whitespace, including newlines, to single spaces) so no explicit
# newline check is needed here.
_CAPTION_ALLOWED = re.compile(r"[^\w\s&'\-.,!?:;\"()]", re.UNICODE)


def _clean(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


# KRI-826: invisible characters that are part of what the creator sees: the joiners
# inside emoji ZWJ sequences and Indic/Persian words, and variation selectors (U+FE0F
# makes "☕️" the emoji). Every other format character is invisible and is dropped.
_KEPT_FORMAT_CHARS = frozenset({"\u200c", "\u200d"})
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _kept_char(char: str) -> bool:
    category = unicodedata.category(char)
    # Only controls and lone surrogates are dropped. "Cn" (unassigned) and "Co" stay:
    # the prod image's Python 3.11 has Unicode 14 tables, so every newer emoji (🩷 🫨,
    # Unicode 15+) reads as unassigned there, and dropping it would strip the
    # creator's emoji from the printed text (KRI-826).
    if category in {"Cc", "Cs"}:
        return char == "\n"
    if category == "Cf":
        code = ord(char)
        return (
            char in _KEPT_FORMAT_CHARS
            or 0xFE00 <= code <= 0xFE0F
            or 0xE0100 <= code <= 0xE01EF
            # Tag characters spell subdivision flags (England, Scotland, Wales).
            or 0xE0020 <= code <= 0xE007F
        )
    return True


def _clean_literal(value: object, limit: int) -> str:
    """``_clean`` that keeps the creator's explicit line breaks (KRI-826).

    Within a line, whitespace runs become one space and the ends are trimmed, exactly
    as ``_clean`` does, so text without a newline cleans to the same string ``_clean``
    gives (every stored row re-validates byte-identically). At most one blank line
    stays between two lines. Nothing visible is ever rewritten: no NFC/NFKC, no quote
    or apostrophe swaps.
    """
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(char for char in text if _kept_char(char) or char.isspace())
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip("\n ")[:limit].strip("\n ")


def _clean_stored(value: object, limit: int) -> str:
    """The row validators' clean: a one-line value is exactly ``_clean`` (format marks
    such as U+200B or an RTL mark included), so every row stored before KRI-826
    re-validates and re-dumps byte-identically. New creator input is cleaned by
    ``clean_creator_literal_text`` before it is stored."""
    if isinstance(value, str) and "\n" not in value and "\r" not in value:
        return _clean(value, limit)
    return _clean_literal(value, limit)


def _has_control(text: str) -> bool:
    """A control character (not whitespace) is never typed copy: something broke it."""
    return any(unicodedata.category(c) == "Cc" and not c.isspace() for c in text)


def _has_printable(text: str) -> bool:
    # "Cn"/"Co": an emoji newer than this Python's Unicode tables still shows (KRI-826).
    return any(
        c.isalnum() or unicodedata.category(c) in {"So", "Sm", "Sc", "Cn", "Co"} for c in text
    )


def clean_creator_literal_text(value: object, *, limit: int) -> str | None:
    """The creator's own words as they will print, or None when nothing would show.

    KRI-826: any printable Unicode is the creator's copy: "+", "’", "!", emoji,
    diacritics, Turkish ı İ ş ğ and explicit line breaks. Invisible format characters
    are dropped (they never show); the joiners and variation selectors emoji need are
    kept. A control character refuses the text (``_has_control``). ``limit`` is a
    code-point bound; longer text is refused, never cut.
    """
    if not isinstance(value, str) or _has_control(value):
        return None
    text = _clean_literal(value, len(value) + 1)
    if not text or len(text) > limit or not _has_printable(text):
        return None
    return text


def fit_intent_id(value: str) -> str:
    """Shorten a model-minted intent id to the stored bound, deterministically.

    The id is only a handle the model invents ("caption_guy_glasses_navy_tshirt_
    bowling_video"); its length says nothing about whether the creator's
    instruction is valid. KRI-422: rejecting an over-long id discarded a correctly
    understood caption and asked the creator to restate it. Ids within the bound
    are returned unchanged, so every persisted intent re-validates byte-identically.
    """
    if len(value) <= INTENT_ID_MAX_CHARS:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{value[: INTENT_ID_MAX_CHARS - len(digest) - 1]}-{digest}"


class ClipIntent(BaseModel):
    """What the creator wants done with clips matching a described attribute."""

    intent_id: str = Field(min_length=1, max_length=INTENT_ID_MAX_CHARS)
    op: ClipIntentOp
    # Free text, the creator's own framing: "sport being played", "pub videos",
    # "people not playing sports", "me talking to the camera", "dish".
    attribute: str = Field(min_length=1, max_length=160)
    source_kind: Literal["video", "image"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def accepts_source_kind(self, kind: str) -> bool:
        return self.source_kind is None or self.source_kind == kind

    # Transcript labels are resolved only against the pinned narration and final
    # timeline, never by the visual resolver. The kind selects an existing
    # deterministic narration grammar, not model-authored on-screen copy.
    label_source: Literal["clip", "transcript"] = Field(
        default="clip", exclude_if=lambda value: value == "clip"
    )
    transcript_kind: Literal["participant", "score", "topic"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _validate_label_source(self) -> ClipIntent:
        if self.label_source == "transcript":
            if self.op != "label" or self.transcript_kind is None:
                raise ValueError("transcript labels require op=label and transcript_kind")
            if self.creator_text is not None or self.caption_attribute is not None:
                raise ValueError("transcript label copy comes only from the narration materializer")
        elif self.transcript_kind is not None:
            raise ValueError("transcript_kind requires label_source=transcript")
        if self.order_by is not None and (self.op != "order" or self.position is not None):
            raise ValueError("order_by requires op=order and no position")
        if self.ordered_members is not None and (self.op != "order" or self.order_by is not None):
            raise ValueError("ordered_members requires a described order")
        if self.placeholder and (
            self.op != "label"
            or self.label_source != "clip"
            or self.creator_text is not None
            or self.caption_attribute is not None
        ):
            raise ValueError("placeholder requires a clip label with no creator_text")
        if self.exclusive and (
            self.op != "include" or self.creator_text is not None or self.placeholder
        ):
            raise ValueError("exclusive requires op=include with no creator_text")
        if self.op == "exclude" and (
            self.creator_text is not None
            or self.caption_attribute is not None
            or self.position is not None
        ):
            # Nothing is printed or placed for a clip that is left out.
            raise ValueError("exclude carries no creator_text, caption_attribute or position")
        if self.by_length is not None and self.op != "exclude":
            raise ValueError("by_length requires op=exclude")
        if self.under_s is not None and self.by_length is None:
            raise ValueError("under_s requires by_length")
        if self.op == "hide_part":
            if (
                self.creator_text is not None
                or self.caption_attribute is not None
                or self.position is not None
                or self.placeholder
            ):
                # Nothing is printed or placed for a hidden part.
                raise ValueError(
                    "hide_part carries no creator_text, caption_attribute, position or placeholder"
                )
        elif (
            self.part_edge is not None or self.part_s is not None or self.part_attribute is not None
        ):
            raise ValueError("part_edge, part_s and part_attribute require op=hide_part")
        return self

    # Exact creator-written copy for this intent ("post match pub"), if any. A
    # label's copy is still fenced to LABEL_MAX_* when it is grounded.
    creator_text: str | None = Field(default=None, max_length=CREATOR_CAPTION_MAX_CHARS)
    # Only for op="caption" with no `creator_text`: what the caption should be
    # ABOUT ("the weather"), as distinct from `attribute` (WHICH clips it's
    # for, "the park clips"). Never set for any other op.
    caption_attribute: str | None = Field(default=None, max_length=160)
    # KRI-282: "add a text placeholder so I can replace it with their real names".
    # A per-clip label whose text is the fixed ``PLACEHOLDER_LABEL_TEXT``; the
    # resolver only decides WHICH clips (``attribute``, e.g. "individual shots of
    # people") and never writes the text. Omitted from dumps when false, so every
    # persisted intent written before this field loads and re-dumps byte-identically.
    placeholder: bool = Field(default=False, exclude_if=lambda value: not value)
    # Only for op="order".
    position: ClipOrderPosition | None = None
    # Only for op="order", and never together with `position`.
    order_by: ClipOrderBy | None = Field(default=None, exclude_if=lambda value: value is None)
    ordered_members: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    # KRI-993: only for op="include". "Only about the steak": the edit is limited to the
    # clips this intent resolves to (plus what the creator pinned or the strategy places),
    # not just guaranteed to contain them. Omitted from dumps when false, so every intent
    # persisted before this field loads and re-dumps byte-identically.
    exclusive: bool = Field(default=False, exclude_if=lambda value: not value)

    # KRI-994: for op="exclude" only, the Creative Brief `select` requirement ("r3") this
    # exclusion carries out, as the planner model declared it. Unverified here: the receipt
    # reader (`app.kria.select_asks.exclusion_facts`) keeps it only when it names a live
    # `select` requirement. Omitted from dumps when unset, so every intent persisted before
    # this field loads and re-dumps byte-identically.
    requirement_id: str | None = Field(
        default=None, max_length=24, exclude_if=lambda value: value is None
    )
    # KRI-1031: for op="exclude" only. "Skip anything too short to use", "drop the mis-taps",
    # "nothing under a second": clips told apart by their LENGTH, which the server measures
    # (`app.services.too_short_clips`), never by what they show. Omitted from dumps when
    # unset, so every intent persisted before these fields re-dumps byte-identically.
    by_length: Literal["too_short"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # Only with `by_length`: the creator's own number of seconds ("under a second" -> 1).
    under_s: float | None = Field(
        default=None, gt=0, le=600, exclude_if=lambda value: value is None
    )
    # Leverkusen M2: for op="hide_part" only. Where the part sits in the clip ``attribute``
    # names ("start" / "end"); None when the creator placed it in the middle, which the
    # server cannot keep out and asks about instead. Omitted from dumps when unset, so every
    # intent persisted before these fields re-dumps byte-identically.
    part_edge: HiddenPartEdge | None = Field(default=None, exclude_if=lambda value: value is None)
    # How many seconds from that edge are never shown: the creator's own number ("the first
    # 3 seconds" -> 3, "start it after about 2.5 seconds" -> 2.5). None until the creator
    # says one; a resolved hide_part always carries it (`app.kria.hidden_parts`).
    part_s: float | None = Field(
        default=None, gt=0, le=MAX_HIDDEN_PART_S, exclude_if=lambda value: value is None
    )
    # What the part shows, in the creator's words ("the kids"), for the question asking for
    # the seconds. Never on-screen text.
    part_attribute: str | None = Field(
        default=None, max_length=160, exclude_if=lambda value: value is None
    )

    @field_validator("intent_id", mode="before")
    @classmethod
    def _intent_id(cls, v: object) -> object:
        return fit_intent_id(v) if isinstance(v, str) else v

    @field_validator("attribute", mode="before")
    @classmethod
    def _attribute(cls, v: object) -> str:
        return _clean(v, 160)

    @field_validator("creator_text", mode="before")
    @classmethod
    def _creator_text(cls, v: object) -> str | None:
        # KRI-826: the creator's line breaks survive (``_clean_literal``).
        return _clean_stored(v, CREATOR_CAPTION_MAX_CHARS) or None

    @field_validator("caption_attribute", mode="before")
    @classmethod
    def _caption_attribute(cls, v: object) -> str | None:
        return _clean(v, 160) or None

    @field_validator("requirement_id", mode="before")
    @classmethod
    def _requirement_id(cls, v: object) -> str | None:
        return _clean(v, 24) or None

    @field_validator("part_attribute", mode="before")
    @classmethod
    def _part_attribute(cls, v: object) -> str | None:
        return _clean(v, 160) or None


class ClipAssignment(BaseModel):
    media_id: str = Field(min_length=1, max_length=200)
    # Per-clip value for op="label" ("Volleyball"); None for pure membership. A
    # creator_text label holds the creator's own words (KRI-826), so it takes the
    # creator storage bound and keeps their line breaks.
    value: str | None = Field(default=None, max_length=CREATOR_LABEL_STORAGE_MAX)
    evidence: str = Field(default="", max_length=240)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    grounding: GroundingSource | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, v: object) -> str | None:
        return _clean_stored(v, CREATOR_LABEL_STORAGE_MAX) or None

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, v: object) -> str:
        return _clean(v, 240)


class ResolvedClipIntent(ClipIntent):
    status: ResolutionStatus = "resolved"
    assignments: list[ClipAssignment] = Field(default_factory=list)
    # Set when status == "needs_creator": one concise question for the chat.
    question: str | None = Field(default=None, max_length=300)
    # Only for op="caption": the ONE on-screen phrase for the whole chapter
    # (the member clips in `assignments`) -- `creator_text` verbatim, or the
    # resolver's grounded phrase. `assignments[i].value` stays None for
    # caption, same as group/order/include -- membership only.
    caption_text: str | None = Field(default=None, max_length=CREATOR_CAPTION_MAX_CHARS)
    caption_grounding: GroundingSource | None = None

    def media_ids(self) -> list[str]:
        return [a.media_id for a in self.assignments]


class GroundedLabel(BaseModel):
    """The only shape the context-label render lane accepts when the flag is on."""

    media_id: str
    # The creator's own words are bounded by the label bar (KRI-826) and stored up to
    # CREATOR_LABEL_STORAGE_MAX; every other source is fenced to LABEL_MAX_CHARS
    # before it gets here (`ground_label`).
    text: str = Field(min_length=1, max_length=CREATOR_LABEL_STORAGE_MAX)
    grounding: GroundingSource
    confidence: float = Field(ge=0.0, le=1.0)
    intent_id: str = ""


def _match_key(text: str) -> str:
    """Accent/case/punctuation-insensitive key (same rule as creator shot labels)."""
    # KRI-520: Turkish dotless ı is the lower case of the I in "KIRMIZI" (casefold gives
    # i), so it folds to i like the rest of the accents.
    decomposed = unicodedata.normalize("NFKD", text).replace("ı", "i")
    return "".join(c for c in decomposed.casefold() if c.isalnum() and not unicodedata.combining(c))


def _word_list(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text).replace("ı", "i").casefold()
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return [t for t in re.split(r"[^\w]+", plain, flags=re.UNICODE) if t]


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """True when ``phrase``'s words appear in ``haystack`` as WHOLE words, in order,
    contiguously. A letters-only substring test would let "the me" ground "Theme"."""
    words, needle = _word_list(haystack), _word_list(phrase)
    if not needle:
        return False
    span = len(needle)
    return any(words[i : i + span] == needle for i in range(len(words) - span + 1))


_RAW_WORD_RE = regex.compile(r"[\p{L}\p{N}_][\p{L}\p{N}\p{M}_]*")
# Quotes around a label are the chat's punctuation, not the label's.
_OUTER_QUOTES = '"“”«»„'
# The same apostrophe, typed by different keyboards (iOS smart punctuation types ’).
# Dashes and double quotes fold as the planner fence folds them (`_QUOTE_TABLE`), so a
# creator_text the planner accepts is one the label fence can ground.
_APOSTROPHES = str.maketrans(
    {"’": "'", "‘": "'", "ʼ": "'", "`": "'", "‚": "'", "‛": "'", "′": "'"}
    | {"“": '"', "”": '"', "„": '"', "‟": '"', "«": '"', "»": '"'}
    | {"–": "-", "—": "-", "−": "-"}
)


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_" or unicodedata.combining(char) > 0


def _symbols(text: str) -> list[str]:
    """The characters a word match ignores: punctuation, symbols, emoji (KRI-826)."""
    return [
        c.translate(_APOSTROPHES)
        for c in text
        if not (_is_word_char(c) or c.isspace() or c in _KEPT_FORMAT_CHARS)
        and not 0xFE00 <= ord(c) <= 0xFE0F
    ]


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    remaining = iter(haystack)
    return all(any(item == other for other in remaining) for item in needle)


def creator_literal_span(haystack: str, phrase: str) -> str | None:
    """The creator's own bytes for ``phrase``, taken from ``haystack`` (KRI-826).

    ``_contains_phrase`` matches words only: it ignores case, accents, punctuation and
    emoji. So the printed text is never the model's copy of the words, it is the
    creator's span: from the first matched word to the last, with their "+", "’",
    casing and line breaks in between. A symbol the model puts before or after the
    words ("🔥 asado", "criolla!") extends the span only when the creator wrote it
    right there.

    Literal provenance: every symbol in ``phrase`` must appear in the span, in order. A
    symbol the creator never wrote ("!", "🔥") grounds nothing; a "+" the model wrote
    as "and" is a different word list and never matches. A symbol the model dropped
    is printed anyway, since the creator wrote it.
    """
    phrase = (phrase or "").strip().strip(_OUTER_QUOTES).strip()
    needle = _word_list(phrase)
    if not needle or not haystack:
        return None
    words: list[tuple[int, int, str]] = [
        (match.start(), match.end(), folded)
        for match in _RAW_WORD_RE.finditer(haystack)
        for folded in _word_list(match.group())
    ]
    phrase_words = list(_RAW_WORD_RE.finditer(phrase))
    if not phrase_words:
        return None
    leading = _symbols(phrase[: phrase_words[0].start()])
    trailing = _symbols(phrase[phrase_words[-1].end() :])
    wanted = _symbols(phrase)
    span = len(needle)
    for i in range(len(words) - span + 1):
        if [w for _s, _e, w in words[i : i + span]] != needle:
            continue
        start, end = words[i][0], words[i + span - 1][1]
        start = _extend(haystack, start, leading, step=-1)
        end = _extend(haystack, end, trailing, step=1)
        if start is None or end is None:
            continue
        text = haystack[start:end]
        if _is_subsequence(wanted, _symbols(text)):
            return text
    return None


def _extend(text: str, at: int, symbols: list[str], *, step: int) -> int | None:
    """Move ``at`` over the creator's characters next to the span until it covers
    ``symbols`` (nearest first); never past a word or a line break."""
    if not symbols:
        return at
    pending = list(reversed(symbols)) if step < 0 else list(symbols)
    index = at
    while pending:
        nxt = index - 1 if step < 0 else index
        if nxt < 0 or nxt >= len(text):
            return None
        char = text[nxt]
        if char == "\n" or _is_word_char(char):
            return None
        index = index - 1 if step < 0 else index + 1
        mine = _symbols(char)
        if mine and mine[0] == pending[0]:
            pending.pop(0)
    # Keep the joiners and selectors that belong to the last symbol taken.
    while (
        step > 0
        and index < len(text)
        and (text[index] in _KEPT_FORMAT_CHARS or 0xFE00 <= ord(text[index]) <= 0xFE0F)
    ):
        index += 1
    return index


def _same_but_case(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` differ only in letter case and spacing (KRI-826).

    A line break the creator typed is not spacing, and a dropped accent or a dotted i
    for a dotless ı is not case: both are real changes. Turkish pairs ı/I and i/İ.
    """

    def lines(text: str) -> list[str]:
        return [" ".join(line.split()) for line in text.strip().split("\n")]

    def same(x: str, y: str) -> bool:
        return x == y or x.casefold() == y.casefold() or {x, y} in _TURKISH_CASE_PAIRS

    left, right = lines(a), lines(b)
    return len(left) == len(right) and all(
        len(x) == len(y) and all(same(c, d) for c, d in zip(x, y, strict=True))
        for x, y in zip(left, right, strict=True)
    )


_TURKISH_CASE_PAIRS = ({"ı", "I"}, {"i", "İ"})


def creator_span(haystack: str, phrase: str) -> str | None:
    """The creator's span for ``phrase`` when it is their words (`_contains_phrase`)."""
    if not _contains_phrase(haystack or "", phrase or ""):
        return None
    return creator_literal_span(haystack, phrase)


def _tokens(text: str) -> set[str]:
    # Every word counts, including one-letter ones: dropping short tokens would
    # let an unverified word ride along inside an otherwise grounded label.
    return set(_word_list(text))


def ground_placeholder_label(*, media_id: str, intent_id: str = "") -> GroundedLabel:
    """The fixed placeholder label for one clip a placeholder intent targets.

    The only on-screen text that bypasses the provenance fence, because it is
    the constant ``PLACEHOLDER_LABEL_TEXT`` (system-owned, explicitly requested
    by the creator) -- never anything a model wrote.
    """
    return GroundedLabel(
        media_id=media_id,
        text=PLACEHOLDER_LABEL_TEXT,
        grounding="placeholder",
        confidence=1.0,
        intent_id=intent_id,
    )


def clean_label_text(
    value: object, *, max_words: int = LABEL_MAX_WORDS, max_chars: int = LABEL_MAX_CHARS
) -> str | None:
    """Normalise candidate label copy; None when it can never be a label.

    The longer creator bounds are only for the creator's own words
    (``clean_creator_label_text``).
    """
    text = _clean(value, 200)
    if not text or _LABEL_ALLOWED.search(text):
        return None
    if len(text) > max_chars or len(text.split()) > max_words:
        return None
    if not any(c.isalpha() for c in text):
        return None
    return text


def clean_creator_label_text(value: object) -> str | None:
    """The creator's own label words as they print, or None when the bar can't hold them.

    KRI-826: the creator's words are their own authority. Any printable character
    prints as written ("chimichurri + salsa criolla", "Julio’s 🔥", a line break they
    typed); the only bound is the label bar itself, at most two lines
    (``creator_label_fits``). Model and vision copy keep ``clean_label_text``.
    """
    text = clean_creator_literal_text(value, limit=CREATOR_LABEL_STORAGE_MAX)
    if text is None:
        return None
    from app.pipeline.text_fit import creator_label_fits  # noqa: PLC0415 - lazy skia

    return text if creator_label_fits(text) else None


CreatorLabelProblem = Literal["too_long", "characters", "empty"]


def creator_label_problems(value: object) -> tuple[CreatorLabelProblem, ...]:
    """Every reason the creator's own label words can never be printed; () when they can.

    KRI-804 named every reason; KRI-826 leaves three. ``"empty"``: nothing would show.
    ``"too_long"``: the label bar can't hold the words on two lines. ``"characters"``:
    only a control character (never typed copy). The creator's "+" or emoji prints.
    """
    if clean_creator_label_text(value) is not None:
        return ()
    if not isinstance(value, str) or not _has_printable(_clean_literal(value, len(value))):
        return ("empty",)
    problems: list[CreatorLabelProblem] = []
    if _has_control(value):
        problems.append("characters")
    shown = clean_creator_literal_text(
        "".join(c for c in value if not _has_control(c)), limit=len(value)
    )
    if shown is not None and clean_creator_label_text(shown) is None:
        problems.append("too_long")
    return tuple(problems)


def creator_label_problem(value: object) -> CreatorLabelProblem | None:
    """The first reason the creator's own label words can never be printed, or None.

    KRI-678: such a label can never be grounded, so asking "which clips?" about it
    loops forever. The caller says why instead of showing the clip picker; the reply
    names every reason (``creator_label_problems``).
    """
    problems = creator_label_problems(value)
    return problems[0] if problems else None


# Joining words a shorter label never starts or ends on ("chimichurri &").
_LABEL_JOINERS = frozenset({"&", "+", "and", "ve", "y", "e", "et", "und"})


def shorter_creator_label(value: object) -> str | None:
    """A label made only of the creator's own words that CAN print, or None (KRI-804).

    When the words run past the label bar, ONE word (never the first) is left out,
    keeping the longest result that fits. Cutting more would break names apart
    ("Parque de la Costa Aires"), so there is no suggestion then. Never a word or a
    character the creator didn't write, so it is still their copy if they pick it.
    """
    if not creator_label_problems(value):
        return None
    original = " ".join(str(value or "").split())
    words = original.split()

    def joiner(word: str) -> bool:
        return word.casefold() in _LABEL_JOINERS

    def fits(candidate: list[str]) -> str | None:
        if not candidate or joiner(candidate[0]) or joiner(candidate[-1]):
            return None
        if any(joiner(a) and joiner(b) for a, b in zip(candidate, candidate[1:], strict=False)):
            return None
        label = clean_creator_label_text(" ".join(candidate))
        return label if label != original else None

    # Never the joiner itself: "chimichurri salsa" would read as one thing, not two. On a
    # tie the later word goes, so the creator's phrase keeps its opening words in order.
    fitting = [
        (len(label), index, label)
        for index in range(1, len(words))
        if not joiner(words[index])
        and (label := fits(words[:index] + words[index + 1 :])) is not None
    ]
    return max(fitting)[2] if fitting else None


def vision_evidence_text(record: ClipUnderstanding) -> str:
    """Everything the vision model WROTE about a clip. Never the transcript.

    KRI-845: its ``props`` and ``actions`` are the model's own words too ("black round tray"),
    and the resolver sees them, so a label picked from one grounds as ``record_span``. Never
    ``visual_quality``: "blurry, fast pan" is a judgment of the footage, not label or caption
    text. A record without props / actions yields exactly what it did before.
    """
    parts = [record.subject, record.summary, record.setting, record.activity, record.people.note]
    parts.extend(m.description for m in record.notable_moments)
    parts.extend(record.props)
    parts.extend(record.actions)
    return " ".join(p for p in parts if p)


# KRI-612: "explain the footages", "a short summary on each clip", "say what's happening
# in each video", "videoları açıkla". The creator asks Kria to write what each clip shows:
# a label whose value every clip's own record already holds. Matched on `_word_list`
# folding (case, accents, Turkish ı), so "Açıkla" and "acikla" are the same word.
# "described" / "as I explained" / "açıkladığım" point at words said elsewhere and
# "anlatım" is narration: none of them asks for a description.
_CLIP_DESCRIPTION_RE = re.compile(
    r"\b(?:explain|explaining|explanations?|describe|describing|descriptions?"
    r"|summar(?:y|ies|ise|ize|ising|izing)|recaps?"
    r"|what (?:s |is |was )?(?:happening|going on)|what happens"
    r"|acikla(?!dig)\w*|ozet\w*|ne(?:ler)? oluyor)\b"
)
# The intent the inventory planner (and the server, see
# ``clip_intent_planning.with_clip_descriptions``) writes for such an ask.
CLIP_DESCRIPTION_ATTRIBUTE = "what is happening in the clip"


def asks_clip_description(text: str | None) -> bool:
    """Does ``text`` ask Kria to say what each clip shows (KRI-612)?"""
    return bool(text) and bool(_CLIP_DESCRIPTION_RE.search(" ".join(_word_list(text or ""))))


def is_clip_description_label(intent: ClipIntent) -> bool:
    """A footage label that describes each clip, rather than naming one attribute of it.

    Its value for every clip is in that clip's own record, so it never needs a question
    or a vision re-check. The creator's own words and the fixed placeholder are never one.
    """
    return (
        intent.op == "label"
        and intent.label_source == "clip"
        and not intent.placeholder
        and intent.creator_text is None
        and asks_clip_description(intent.attribute)
    )


# Where a footage line stops: "football players jogging | and stretching on the pitch".
_LINE_STOP_WORDS = frozenset(
    "and or but while with at on in into onto near by during before after for to from of "
    "as under over inside outside behind through across around about".split()
)
_LEADING_ARTICLES_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def footage_lines(record: ClipUnderstanding) -> list[str]:
    """Short lines (label shape) saying what a clip shows, best first (KRI-612).

    Every word is the vision model's own (``vision_evidence_text``), so each line passes
    ``ground_label`` as ``record_span``. A whole field that already fits a label comes
    first; then the start of a longer field up to its first linking word, two or three
    words before one; never a cut mid-phrase ("players on") or a lone article. The
    clip's setting is the last resort: it says where, not what happens.
    """

    def ranked(sources: list[str]) -> list[str]:
        whole: list[str] = []
        heads: list[str] = []
        singles: list[str] = []
        for source in sources:
            text = _LEADING_ARTICLES_RE.sub("", " ".join((source or "").split()))
            if not text:
                continue
            if clean_label_text(text):
                whole.append(text)
                continue
            words: list[str] = []
            for word in text.split(",", 1)[0].split():
                if word.casefold() in _LINE_STOP_WORDS:
                    break
                words.append(word)
            head = clean_label_text(" ".join(words))
            if head is not None:
                (heads if len(words) > 1 else singles).append(head)
        return [*whole, *heads, *singles]

    what = [
        record.activity,
        record.subject,
        *(moment.description for moment in record.notable_moments),
    ]
    lines: list[str] = []
    for line in [*ranked(what), *ranked([record.setting])]:
        line = line[:1].upper() + line[1:]
        if _match_key(line) not in {_match_key(kept) for kept in lines}:
            lines.append(line)
    return lines


def ground_label(
    *,
    media_id: str,
    value: object,
    confidence: float,
    creator_request: str,
    record: ClipUnderstanding,
    vision_answer: str | None = None,
    vision_confidence: float | None = None,
    intent_id: str = "",
) -> GroundedLabel | None:
    """Apply the on-screen text fence. Returns None when the label must not render.

    KRI-826: the creator's own words print as THEY wrote them (``creator_span``), bounded
    only by the label bar. Everything else keeps the short-tag taste rules.
    """
    span = creator_span(creator_request or "", value if isinstance(value, str) else "")
    if span is not None:
        # The planner's stored value keeps its casing when only case or spacing differs
        # (same rule as ``ground_caption``): a regen from persisted intents re-grounds
        # against the chat request, and an accepted label must not change text after an
        # unrelated edit (KRI-824 scoped edits). A dropped "+", emoji or diacritic still
        # prints the creator's own span.
        stored = value.strip().strip(_OUTER_QUOTES).strip() if isinstance(value, str) else ""
        printed = stored if stored and _same_but_case(stored, span) else span
        creator_text = clean_creator_label_text(printed)
        if creator_text is not None:
            return GroundedLabel(
                media_id=media_id,
                text=creator_text,
                grounding="creator_text",
                confidence=1.0,
                intent_id=intent_id,
            )
    # KRI-678 / KRI-804: only the creator's own words get the longer bounds. Copy
    # grounded from a clip record or a vision answer keeps the short-tag bound and
    # the label charset.
    text = clean_label_text(value)
    if text is None:
        return None
    words = _tokens(text)
    if not words:
        return None
    if (
        vision_answer
        and vision_confidence is not None
        and vision_confidence >= LABEL_MIN_CONFIDENCE
        and words <= _tokens(vision_answer)
    ):
        return GroundedLabel(
            media_id=media_id,
            text=text,
            grounding="vision_verified",
            confidence=float(vision_confidence),
            intent_id=intent_id,
        )
    if confidence >= LABEL_MIN_CONFIDENCE and words <= _tokens(vision_evidence_text(record)):
        return GroundedLabel(
            media_id=media_id,
            text=text,
            grounding="record_span",
            confidence=float(confidence),
            intent_id=intent_id,
        )
    return None


_LEADING_ARTICLES = frozenset({"the", "a", "an"})


def chapter_name_caption(
    *, attribute: str, caption_attribute: str | None, creator_request: str
) -> str | None:
    """The creator's own name for a chapter, when "a text for the pub" means just that.

    KRI-282: for ``a text for the pub and the pregame`` the planner emits a caption
    whose ``attribute`` ("the pub") and ``caption_attribute`` ("The Pub") name the SAME
    chapter. The text the creator asked for is the chapter's own name, in their own
    words, not a sentence the resolver writes about the footage ("Gathering and
    relaxing on a grassy field"). Returns that name only when both fields agree
    (ignoring a leading article) and the creator literally wrote it; otherwise None
    and the caller falls back to the grounded authored phrase.
    """
    topic = clean_caption_text(caption_attribute)
    if topic is None or not _contains_phrase(creator_request or "", topic):
        return None

    def core(text: str) -> list[str]:
        words = _word_list(text)
        return words[1:] if words and words[0] in _LEADING_ARTICLES else words

    chapter = core(attribute or "")
    return topic if chapter and chapter == core(topic) else None


class GroundedCaption(BaseModel):
    """The only shape the caption render lane accepts. Intent-level (one
    caption per chapter), unlike ``GroundedLabel`` which is per-clip."""

    text: str = Field(min_length=1, max_length=CREATOR_CAPTION_MAX_CHARS)
    grounding: GroundingSource
    confidence: float = Field(ge=0.0, le=1.0)
    intent_id: str = ""


def clean_creator_caption_text(value: object) -> str | None:
    """Normalise the creator's own caption copy; None when it cannot be printed.

    Unlike ``clean_caption_text`` there is no word or punctuation bound: the
    creator wrote these words to be shown as written ("#1 Antoni Gaudí…").
    """
    text = clean_creator_literal_text(value, limit=CREATOR_CAPTION_MAX_CHARS)
    if text is None or not any(c.isalnum() for c in text):
        return None
    return text


def clean_caption_text(value: object) -> str | None:
    """Normalise candidate caption copy; None when it can never be a caption."""
    text = _clean(value, 200)
    if not text or _CAPTION_ALLOWED.search(text):
        return None
    if len(text) > CAPTION_MAX_CHARS or len(text.split()) > CAPTION_MAX_WORDS:
        return None
    if not any(c.isalpha() for c in text):
        return None
    return text


def ground_caption(
    *,
    value: object,
    confidence: float,
    creator_request: str,
    records: list[ClipUnderstanding],
    vision_answer: str | None = None,
    vision_confidence: float | None = None,
    intent_id: str = "",
    creator_copy: bool = False,
) -> GroundedCaption | None:
    """Apply the on-screen text fence to an intent-level caption phrase.

    Same three-source fence as ``ground_label``, except ``record_span`` checks
    the caption's words against the UNION of every member clip's vision
    evidence (a caption spans a whole chapter, not one clip) instead of a
    single clip's record.

    ``creator_copy`` marks the planner-verified ``creator_text`` of the intent:
    the creator's own words print as written (``clean_creator_caption_text``)
    when the request still contains them. Anything else, including a resolver
    phrase, keeps the short-phrase shape bounds.
    """
    if creator_copy:
        # KRI-826: the creator's span, never the planner's copy of their words.
        span = creator_span(creator_request or "", value if isinstance(value, str) else "")
        copy = clean_creator_caption_text(span)
        if copy is not None:
            return GroundedCaption(
                text=copy, grounding="creator_text", confidence=1.0, intent_id=intent_id
            )
    text = clean_caption_text(value)
    # A resolver phrase with the creator's "+" or emoji fails the caption charset but
    # may still be the creator's own words (KRI-826).
    # It still has to hold the caption's short-phrase shape.
    candidate = text
    if candidate is None and isinstance(value, str):
        shaped = clean_caption_text(_CAPTION_ALLOWED.sub("x", " ".join(value.split())))
        candidate = clean_creator_caption_text(value) if shaped is not None else None
    if candidate is None:
        return None
    span = (
        creator_literal_span(creator_request, candidate)
        if _contains_phrase(creator_request or "", candidate)
        else None
    )
    if span is not None:
        # A phrase the resolver made of the creator's words keeps its caption casing
        # ("The Pub" for "the pub"). KRI-826: anything else it changed, a dropped "+",
        # emoji or diacritic, a straightened "’", prints the creator's own span, since
        # creator_text authority means the creator's bytes. A symbol the creator never
        # wrote finds no span and falls through to the footage checks below.
        printed = candidate if _same_but_case(candidate, span) else clean_creator_caption_text(span)
        if printed is not None:
            return GroundedCaption(
                text=printed, grounding="creator_text", confidence=1.0, intent_id=intent_id
            )
    if text is None:
        return None
    words = _tokens(text)
    if not words:
        return None
    if (
        vision_answer
        and vision_confidence is not None
        and vision_confidence >= LABEL_MIN_CONFIDENCE
        and words <= _tokens(vision_answer)
    ):
        return GroundedCaption(
            text=text,
            grounding="vision_verified",
            confidence=float(vision_confidence),
            intent_id=intent_id,
        )
    union_evidence = " ".join(vision_evidence_text(r) for r in records)
    if confidence >= LABEL_MIN_CONFIDENCE and words <= _tokens(union_evidence):
        return GroundedCaption(
            text=text, grounding="record_span", confidence=float(confidence), intent_id=intent_id
        )
    return None
