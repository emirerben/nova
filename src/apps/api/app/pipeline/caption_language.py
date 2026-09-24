"""Caption-language policy for talking-to-camera captions (KRI-177).

Default rule: captions are in the language the person SPEAKS. The only thing that
may change that is the creator explicitly asking for captions in another language
("captions in English", "altyazılar Türkçe olsun"). Nothing else — the job's
UI/content language, a hard-coded "en", a previous item — may silently win.

Three pure helpers:

- ``parse_caption_language_request`` — grounded, deterministic extraction of an
  explicit caption-language request from the creator's own words. Requires a
  caption/subtitle keyword AND a language name in the same clause, so a prompt
  merely *written* in Turkish never changes the caption language.
- ``infer_language_from_text`` — a conservative EN/TR guess from transcript text,
  used only when whisper reports no language.
- ``resolve_spoken_caption_language`` — detected → transcript text → deliberate,
  recorded fallback. Returns the source so callers can trace the decision.
"""

from __future__ import annotations

import re

# The languages the caption stack (correction prompt, editor chip, D5 override)
# is tuned for. Detection outside this set still captions the spoken language.
SUPPORTED_CAPTION_LANGUAGES: frozenset[str] = frozenset({"en", "tr"})

# Sources reported by ``resolve_spoken_caption_language``.
SOURCE_DETECTED = "detected"
SOURCE_TRANSCRIPT_TEXT = "transcript_text"
SOURCE_FALLBACK = "fallback"

_FOLD = str.maketrans({"ı": "i", "ş": "s", "ç": "c", "ğ": "g", "ö": "o", "ü": "u", "â": "a"})


def _fold(text: str) -> str:
    """Lowercase + ASCII-fold Turkish letters so one pattern matches both spellings."""
    # "İ".lower() is "i̇" (i + combining dot) — map it before lowercasing.
    return (text or "").replace("İ", "i").replace("I", "i").lower().translate(_FOLD)


_CAPTION_KEYWORD = re.compile(
    r"\b(?:captions?|captioned|captioning|subtitles?|subtitled|subs|"
    r"alt\s?yazi\w*)\b"
)
_LANGUAGE_NAMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("en", re.compile(r"\b(?:english|ingilizce\w*)\b")),
    ("tr", re.compile(r"\b(?:turkish|turkce\w*)\b")),
)
# A clause ends at sentence punctuation or a contrastive conjunction — "Türkçe
# konuşuyorum AMA altyazılar İngilizce olsun" binds İngilizce to the captions.
_CLAUSE_SPLIT = re.compile(r"[.!?;,\n]+|\b(?:but|though|however|ama|fakat|ancak|lakin)\b")
_NEGATION = re.compile(
    r"\b(?:don'?t|do\s+not|dont|never|without|no|not|"
    r"istemiyorum|istemem|olmasin\w*|degil|yapma|koyma)\b"
)
# In a clause naming two languages, the target is the one after a direction word
# ("turkish captions IN english", "İngilizceye çevir").
_TARGET_MARKER = re.compile(r"\b(?:in|into|to)\s+$")
_DATIVE = re.compile(r"\w*ye\b")


def parse_caption_language_request(text: str | None) -> str | None:
    """Return "en"/"tr" when the creator explicitly asks for captions in that language.

    Grounded on the creator's own words: a clause must contain a caption/subtitle
    keyword AND a language name, and must not be negated. Returns None otherwise —
    including when the prompt is merely written in a language, or requests conflict.
    """
    folded = _fold(text or "").replace("’", "'")
    if not folded.strip():
        return None
    requested: str | None = None
    for clause in _CLAUSE_SPLIT.split(folded):
        if not clause or not _CAPTION_KEYWORD.search(clause):
            continue
        if _NEGATION.search(clause):
            continue
        hits: list[tuple[int, str]] = []
        for code, pattern in _LANGUAGE_NAMES:
            hits.extend((m.start(), code) for m in pattern.finditer(clause))
        codes = {code for _, code in hits}
        if not codes:
            continue
        if len(codes) == 1:
            requested = codes.pop()
            continue
        targeted = {code for pos, code in hits if _TARGET_MARKER.search(clause[:pos])}
        # Turkish dative marks the target too: "İngilizceye çevir".
        targeted |= {code for pos, code in hits if _DATIVE.match(clause, pos)}
        if len(targeted) == 1:
            requested = targeted.pop()
    return requested


_TR_CHARS = re.compile(r"[ğşıİçöüĞŞÇÖÜ]")
_TR_WORDS = frozenset(
    "ve bir bu çok ama için ne da de ben sen biz mi mı değil gibi daha şey var yok "
    "olarak ile diye ki şu o bence yani evet hayır nasıl neden".split()
)
_EN_WORDS = frozenset(
    "the and is you to of i it that this in for with my we are was what so just "
    "have be not but they on your do".split()
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def infer_language_from_text(text: str | None) -> str:
    """Conservative EN/TR guess from transcript text. "" when not clearly one of them."""
    tokens = [t.lower() for t in _WORD.findall(text or "")]
    if len(tokens) < 3:
        return ""
    tr_score = sum(1 for t in tokens if t in _TR_WORDS or _TR_CHARS.search(t))
    en_score = sum(1 for t in tokens if t in _EN_WORDS)
    if tr_score >= 2 and tr_score >= 2 * max(en_score, 1):
        return "tr"
    if en_score >= 2 and en_score >= 2 * max(tr_score, 1):
        return "en"
    return ""


def resolve_spoken_caption_language(
    detected: str | None, *, transcript_text: str | None, fallback: str | None
) -> tuple[str, str]:
    """Pick the caption language for a talking-to-camera render.

    Order: whisper's detected language → a guess from the transcript text →
    ``fallback`` (the job language, else "en"). The fallback is deliberate and the
    returned source lets the caller record it — it never wins silently.
    """
    lang = (detected or "").strip().lower()
    if lang:
        return lang, SOURCE_DETECTED
    inferred = infer_language_from_text(transcript_text)
    if inferred:
        return inferred, SOURCE_TRANSCRIPT_TEXT
    return ((fallback or "").strip().lower() or "en"), SOURCE_FALLBACK
