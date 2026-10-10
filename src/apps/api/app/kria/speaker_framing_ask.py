"""Did the creator ask for a vertical, face-in-frame video? (KRI-547)

A creator who films a talk-to-camera take sideways and writes "I filmed it
horizontally but I want a vertical video, keep my face in frame" ("Yatay çektim
ama dikey video istiyorum, yüzüm hep kadrajda kalsın") lands in the Creative
Brief as a ``style`` requirement with no facts. The Main Creator strategy has no
framing field, and adding one would be a prompt/schema change, so the ask is read
deterministically off the brief requirement's own words instead -- the same
words the creator approved -- by the phone Talking worker (to frame the speaker,
`app.pipeline.phone_speaker_framing`) and by `app.kria.brief_checks` (to answer
the requirement from the render's receipt).

What counts (English and Turkish, case/diacritic-insensitive via `loose_text`):

* a 9:16 ratio ("9:16", "9x16", "9 by 16");
* turning the video vertical/portrait ("make it vertical", "crop it to
  portrait", "a vertical video", "vertical format"; "dikey video istiyorum",
  "dikeye çevir", "dikey formata getir");
* keeping the face in frame ("keep my face in frame", "track my face", "don't
  cut off my head"; "yüzüm hep kadrajda kalsın", "beni kadrajda tut", "yüzümü
  takip et");
* filling the screen / no black bars ("fill the screen", "full screen", "no
  black bars"; "tam ekran", "ekranı doldursun", "siyah bant olmasın") -- unless
  the same requirement is about showing a photo/video/sticker that way ("show
  the brewing video full screen" is a full-screen cutaway, KRI-297, not framing).

What does not: a vertical camera move or graphic ("vertical pan", "vertical
wipe", "dikey kaydırma", "dikey yazı"), or picking footage that was filmed
vertically ("use my vertical clip", "dikey çektiğim videoyu kullan").
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.kria.brief_route import loose_text

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

# A framing ask is a look of the whole edit. Other kinds (text, order, select,
# timing, audio) have their own checkers and are never read as one.
FRAMING_KINDS = frozenset({"style"})

_END = r"[^.;!?]"  # stay inside one sentence

_RATIO_RE = re.compile(r"\b9\s*[:x/]\s*16\b|\b9\s+by\s+16\b")

# Words that make "vertical" a motion or a graphic, not the video's shape.
_NOT_SHAPE = (
    r"(?:pans?|panning|wipes?|scroll\w*|lines?|split\w*|slides?|swipe\w*|motion|moves?|"
    r"movement|stripes?|bars?|text|titles?|captions?|subtitles?|labels?|words?|letters?)"
)
_EN_SUBJECT = (
    r"(?:it|this|that|everything|the\s+(?:video|clip|edit|reel|whole\s+thing)"
    r"|my\s+(?:video|clip|edit|reel))"
)
_EN_VERB = r"(?:make|turn|convert|crop|reframe|change|switch|flip|render|export|rotate)"

_EN_STRONG = (
    # "make it vertical", "turn the video into a vertical one", "crop it to portrait"
    re.compile(
        rf"\b{_EN_VERB}\s+{_EN_SUBJECT}\s+(?:(?:in)?to\s+|into\s+|as\s+)?(?:a\s+|an\s+)?"
        rf"(?:vertical|portrait)\b(?!\s+{_NOT_SHAPE}\b)"
    ),
    # "switch to portrait", "crop to vertical"
    re.compile(
        rf"\b{_EN_VERB}\s+(?:it\s+)?(?:in)?to\s+(?:a\s+)?(?:vertical|portrait)\b(?!\s+{_NOT_SHAPE}\b)"
    ),
    # "I want it vertical", "I need it in portrait"
    re.compile(
        rf"\b(?:want|need|prefer|like)\s+{_EN_SUBJECT}\s+(?:in\s+|as\s+)?(?:a\s+)?"
        rf"(?:vertical|portrait)\b(?!\s+{_NOT_SHAPE}\b)"
    ),
    # "vertical format", "portrait orientation", "a vertical crop"
    re.compile(
        r"\b(?:vertical|portrait)\s+(?:format|version|crop|aspect(?:\s+ratio)?|orientation|layout)\b"
    ),
    # keep my face / me in (the) frame; my face stays in frame
    re.compile(
        rf"\b(?:keep|keeps|keeping|stay|stays|staying|remain|remains)\b{_END}{{0,25}}?"
        rf"\b(?:face|head|me|myself)\b{_END}{{0,20}}?\b(?:in|inside|within)\s+(?:the\s+)?"
        r"(?:frame|shot|picture|view)\b"
    ),
    re.compile(
        rf"\b(?:face|head)\b{_END}{{0,30}}?\b(?:in|inside|within)\s+(?:the\s+)?"
        r"(?:frame|shot|picture|view)\b"
    ),
    re.compile(
        r"\b(?:follow|track|center|centre|frame)\s+(?:on\s+)?(?:my|the\s+speaker'?s?)\s+(?:face|head)\b"
    ),
    re.compile(r"\bface[\s-]?(?:tracking|tracked|following)\b"),
    re.compile(
        r"\b(?:don'?t|do\s+not|never|without)\s+(?:cut(?:ting)?|crop(?:ping)?)\s+(?:off\s+|out\s+)?"
        r"(?:my|the)\s+(?:face|head)\b"
    ),
)

# "a vertical video" is the output shape; "use my vertical video" / "the vertical
# clip first" picks footage. Judged with the word before it (`_weak_vertical_video`).
_EN_VERTICAL_NOUN_RE = re.compile(
    r"\b(?:vertical|portrait)\s+(?:video|reel|tiktok|short|edit|output)\b"
)
_SELECTING_WORDS = frozenset(
    {
        "the",
        "my",
        "that",
        "this",
        "these",
        "those",
        "your",
        "use",
        "pick",
        "choose",
        "select",
        "include",
        "skip",
        "drop",
        "with",
        "other",
        "only",
    }
)

# Turkish. "dikey" (vertical) turned into the video's shape: "dikey video istiyorum",
# "dikeye çevir", "videoyu dikey yap", "dikey (9:16) formata getir". Not a vertical
# motion/graphic, nor footage filmed vertically ("dikey çektiğim", "dikey olan klip").
_TR_NOT_SHAPE = (
    r"(?:yazi|metin|cizgi|kaydir|pan|gecis|serit|bant|bolun|cekti|cekil|cekim|olan\b|kayan)"
)
_TR_SHAPE_VERB = (
    r"(?:cevir|getir|donustur|yap|olsun|olmali|olacak|kalsin|istiyorum|isterim|istiyoruz|"
    r"kirp|ayarla|formata|formatta|format\b|kadraj)"
)
_TR_STRONG = (
    re.compile(
        rf"\bdikey(?:e|de)?\b(?!\s+{_TR_NOT_SHAPE})(?:\s+\S+){{0,3}}?\s+\S*?{_TR_SHAPE_VERB}"
    ),
    re.compile(r"\bdikey\s+(?:formata?|formatta|versiyon\w*|kadraj\w*|kirp\w*)"),
    # yüzüm hep kadrajda kalsın / yüzü kadrajda tut / beni kadrajda tut
    re.compile(rf"\b(?:yuz|surat|kafa)\w*\b{_END}{{0,30}}?\bkadraj\w*"),
    re.compile(rf"\bbeni\b{_END}{{0,20}}?\bkadraj\w*"),
    # yüzümü takip et / yüzümü ortala
    re.compile(r"\b(?:yuz|surat)\w*\s+(?:takip|ortala|merkez)\w*"),
    # yüzüm kesilmesin
    re.compile(r"\b(?:yuz|kafa)\w*\s+(?:\S+\s+)?(?:kesilme|kirpilma)\w*"),
)

# Filling the screen: framing only when the requirement is not about showing a
# photo/video/sticker that way.
_FILL = (
    re.compile(
        r"\b(?:fill|fills|filling)\s+(?:up\s+)?(?:the\s+|my\s+)?(?:whole\s+|entire\s+|full\s+)?"
        r"(?:screen|frame)\b"
    ),
    re.compile(r"\bfull[\s-]?screen\b"),
    re.compile(
        r"\b(?:no|without(?:\s+any)?|remove(?:\s+the)?|get\s+rid\s+of(?:\s+the)?|lose(?:\s+the)?|"
        r"drop(?:\s+the)?)\s+(?:black\s+)?(?:bars|letterbox\w*|borders)\b"
    ),
    re.compile(r"\b(?:not|never|don'?t)\s+letterbox\w*"),
    re.compile(r"\btam\s+ekran\w*"),
    re.compile(r"\bekran\w*\s+(?:tamamen\s+)?doldur\w*"),
    re.compile(
        r"\bsiyah\s+(?:bant|serit|bosluk|cubuk|kenar|cerceve)\w*\s+(?:\S+\s+)?"
        r"(?:olmasin|istemiyorum|kaldir\w*|olmadan|yok)\b"
    ),
    re.compile(r"\b(?:bant|serit)siz\b"),
)
_MEDIA_SUBJECT = (
    re.compile(
        r"\b(?:photos?|pictures?|images?|pics?|stickers?|logos?|visuals?|b-?roll|cutaways?|"
        r"screenshots?)\b"
    ),
    re.compile(
        rf"\b(?:show|display|pop|cut\s+to|put|play)\b{_END}{{0,40}}?\b(?:video|clip|footage)s?\b"
    ),
    re.compile(r"\b(?:foto\w*|resim\w*|gorsel\w*|cikartma\w*|logo\w*|ekran\s+goruntu\w*)"),
    re.compile(rf"\b(?:video|klip)\w*\b{_END}{{0,30}}?\b(?:goster|koy|ekle|ac|oynat)\w*"),
)


def _weak_vertical_video(folded: str) -> bool:
    """ "a vertical video" as the output, not "use my vertical video"."""
    for match in _EN_VERTICAL_NOUN_RE.finditer(folded):
        before = folded[: match.start()].split()
        if not before or before[-1] not in _SELECTING_WORDS:
            return True
    return False


def asks_vertical_framing(text: str | None) -> bool:
    """True when ``text`` asks for a vertical (9:16), full-screen or face-in-frame
    result. Pure and deterministic; see the module docstring for what counts."""
    folded = loose_text(text or "")
    if not folded:
        return False
    if _RATIO_RE.search(folded):
        return True
    if any(pattern.search(folded) for pattern in (*_EN_STRONG, *_TR_STRONG)):
        return True
    if _weak_vertical_video(folded):
        return True
    if any(pattern.search(folded) for pattern in _MEDIA_SUBJECT):
        return False
    return any(pattern.search(folded) for pattern in _FILL)


def requirement_asks_vertical_framing(req: BriefRequirement) -> bool:
    """A live ``style`` requirement whose words ask for a vertical, face-in-frame video."""
    if not req.live or req.kind not in FRAMING_KINDS:
        return False
    return asks_vertical_framing(" ".join(part for part in (req.description, req.literal) if part))


def framing_requirement_ids(requirements: Iterable[BriefRequirement]) -> list[str]:
    """Ids of the requirements that ask for a vertical, face-in-frame video."""
    return [req.id for req in requirements if requirement_asks_vertical_framing(req)]


def brief_framing_requirement_ids(brief: CreativeBrief | None) -> list[str]:
    """`framing_requirement_ids` over a brief's live requirements (``[]`` without one)."""
    return framing_requirement_ids(brief.live()) if brief is not None else []


__all__ = [
    "FRAMING_KINDS",
    "asks_vertical_framing",
    "brief_framing_requirement_ids",
    "framing_requirement_ids",
    "requirement_asks_vertical_framing",
]
