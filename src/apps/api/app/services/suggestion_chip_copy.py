"""Deterministic copy validation for suggestion chips (KRI-756).

The chip wording agent (``nova.chat.suggester``) may reword a chip's DISPLAY label and write
a one-line ``why`` subtitle. It never writes ``message`` (the only text a tap sends) and it
never names a capability. This module is the gate between the model and the screen:

* ``validate_label``  a closed-vocabulary check: every word must belong to the chip kind's
  lexicon or to a small function/verb list, the label must contain a word that names the
  kind's action, and it must not name a DIFFERENT kind's action (label parity: a label can
  never promise something the tap does not do).
* ``validate_why``    a lexical grounding check: the subtitle must share a content word with
  what the footage analysis actually said (fold-and-overlap, the technique
  ``edit_copilot`` uses for caption wording). Gemini-derived text lives ONLY here.
* ``footage_vocabulary`` the folded word set that check reads.

Everything is pure and language-aware for ``en`` and ``tr`` (any other language falls back to
``en``). A failure never raises: the caller falls back to the template label (or drops the
``why``). Rules and rollout: ``docs/pipelines/suggestion-chips.md``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from app.kria.brief_route import loose_text

MAX_LABEL_CHARS = 36
MAX_WHY_CHARS = 48
SUPPORTED = ("en", "tr")
# Placeholders a label may carry. Each must be filled identically in ``message`` by the same
# renderer, so a label can never name something the tap does not send.
ALLOWED_PLACEHOLDERS = frozenset({"time", "filename"})

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_HANDLE = re.compile(r"\bc\d+(?:\.m\d+)?\b", re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_QUOTES = set("\"'`“”‘’«»")
# Letters, spaces and light punctuation only: no emoji, no symbols, no markup.
_LABEL_CHARS = re.compile(r"^[^\W\d_]+(?:[ \-,.!?][^\W\d_]+)*[.!?]?$", re.UNICODE)

# G6 stoplist: generic chips that would fit any video, plus the register the empathy and
# cringe rubrics reject. Case-insensitive substring match over the loosely folded text.
STOPLIST = (
    "try a stronger opening",
    "make it warmer",
    "make it pop",
    "make it better",
    "add effects",
    "viral",
    "banger",
    "elevate",
    "level up",
    "next level",
    "unlock",
    "supercharge",
    "stunning",
    "amazing",
    "perfect",
    "strongest",
    "best moment",
    "engagement",
    "audience",
    "algorithm",
    "brand",
    "tone of voice",
    "call to action",
    "game changer",
    "game-changer",
    "must see",
    "must-see",
    "go viral",
    "blow up",
    "hack",
    "slay",
    "epic",
    "insane",
    "mind blowing",
    "mind-blowing",
    "scroll stopp",
    "thumb stopp",
)

# ── Vocabulary ───────────────────────────────────────────────────────────────────────
# Stems are matched by prefix on the loosely folded token with a bounded suffix slack (Turkish
# is agglutinative, English only inflects a little). One table per language; keys are the
# ``ChipKind`` values the engine emits.

_LEXICON_SRC: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "default_edit": (
            "cut",
            "edit",
            "draft",
            "version",
            "build",
            "assemble",
            "footage",
            "kria",
            "together",
            "begin",
        ),
        "hook": ("hook", "open", "intro", "start", "title", "grab", "catch", "line", "first"),
        "captions": ("caption", "subtitle", "word", "speech", "spoken", "say", "talk", "line"),
        "sfx": ("sound", "effect", "sfx", "whoosh", "pop", "hit", "punch", "accent"),
        "transition": (
            "transition",
            "cut",
            "smooth",
            "blend",
            "flow",
            "fade",
            "between",
            "join",
            "link",
            "jump",
            "hard",
            "clip",
        ),
        "music": (
            "music",
            "song",
            "track",
            "volume",
            "quiet",
            "loud",
            "lower",
            "level",
            "voice",
            "drown",
            "mix",
            "turn",
            "down",
            "up",
            "back",
            "speech",
        ),
        "title_animation": (
            "title",
            "animat",
            "motion",
            "move",
            "entrance",
            "appear",
            "text",
            "slide",
            "intro",
            "opening",
        ),
        "opener_clip": ("open", "start", "first", "clip", "scene", "begin", "lead"),
    },
    "tr": {
        "default_edit": (
            "kes",
            "düzenle",
            "versiyon",
            "taslak",
            "yap",
            "kur",
            "kria",
            "ilk",
            "başla",
            "birleştir",
            "hazırla",
            "montaj",
        ),
        "hook": (
            "hook",
            "açılış",
            "giriş",
            "başlangıç",
            "ilk",
            "başlı",
            "yakala",
            "aç",
        ),
        "captions": (
            "altyaz",
            "yazı",
            "kelime",
            "konuş",
            "söyl",
            "metin",
            "söz",
        ),
        "sfx": ("ses", "efekt", "sfx", "whoosh", "vurgu"),
        "transition": (
            "geçiş",
            "kes",
            "akıcı",
            "yumuşat",
            "birleş",
            "arasına",
            "geç",
            "klip",
        ),
        "music": (
            "müzi",
            "şarkı",
            "ses",
            "kıs",
            "seviye",
            "yüksel",
            "düşür",
            "mix",
            "altında",
        ),
        "title_animation": (
            "başlı",
            "canlan",
            "animasyon",
            "hareket",
            "giriş",
            "yazı",
            "metin",
        ),
        "opener_clip": ("aç", "başla", "ilk", "klip", "sahne"),
    },
}

_COMMON_SRC: dict[str, tuple[str, ...]] = {
    "en": (
        "a",
        "an",
        "the",
        "to",
        "on",
        "in",
        "of",
        "for",
        "with",
        "and",
        "or",
        "your",
        "my",
        "our",
        "this",
        "that",
        "it",
        "its",
        "add",
        "give",
        "let",
        "put",
        "make",
        "kria",
        "try",
        "show",
        "use",
        "some",
        "more",
        "bit",
        "little",
        "quick",
        "quickly",
        "punchier",
        "tighter",
        "cleaner",
        "smoother",
        "stronger",
        "video",
        "clip",
        "edit",
        "version",
        "scene",
        "moment",
        "over",
        "out",
        "into",
        "from",
        "as",
        "so",
        "you",
        "me",
        "we",
        "do",
        "get",
        "right",
        "away",
        "now",
        "then",
        "just",
        "no",
        "not",
        "without",
        "extra",
        "fresh",
        "new",
        "at",
        "by",
        "up",
        "down",
        "pick",
        "bring",
        "keep",
        "drop",
        "first",
        "start",
        "all",
        "each",
        "every",
        "one",
        "two",
        "off",
        "soft",
        "softer",
        "light",
        "lighter",
        "clear",
        "clearer",
        "also",
        "few",
        "under",
        "have",
        "what",
        "they",
        "them",
        "their",
        "he",
        "she",
        "his",
        "her",
        "top",
        "beginning",
        "end",
        "same",
        "way",
        "when",
        "where",
        "once",
        "again",
        "while",
        "too",
        "is",
        "be",
        "are",
        "can",
    ),
    "tr": (
        "bir",
        "bu",
        "ve",
        "veya",
        "ile",
        "için",
        "de",
        "da",
        "ekle",
        "ver",
        "koy",
        "yap",
        "kria",
        "biraz",
        "daha",
        "videoya",
        "video",
        "klip",
        "kliplere",
        "klipler",
        "ilk",
        "benim",
        "senin",
        "hemen",
        "şimdi",
        "sadece",
        "az",
        "çok",
        "net",
        "temiz",
        "hızlı",
        "kontrol",
        "düzenle",
        "sahne",
        "an",
        "içine",
        "üstüne",
        "altına",
        "başa",
        "sona",
        "her",
        "bütün",
        "şu",
        "o",
        "ben",
        "sen",
        "biz",
        "kes",
    ),
}


def _fold(value: str) -> str:
    return loose_text(value)


def _prep(table: Mapping[str, Iterable[str]]) -> dict[str, tuple[str, ...]]:
    return {key: tuple(_fold(stem) for stem in stems) for key, stems in table.items()}


LEXICON: dict[str, dict[str, tuple[str, ...]]] = {
    lang: _prep(table) for lang, table in _LEXICON_SRC.items()
}
_COMMON: dict[str, frozenset[str]] = {
    lang: frozenset(_fold(word) for word in words) for lang, words in _COMMON_SRC.items()
}
_SLACK = {"en": 3, "tr": 7}

# Polarity guard (label parity). The closed vocabulary only checks that a word belongs to the
# kind, not which way it points, so a reworded label could promise the OPPOSITE of the fixed
# tap message ("Turn the music up" on a chip whose tap lowers it). These stems never appear in
# a label: negations for every kind, plus the reverse-direction words of the kinds whose tap
# has one fixed direction (music: always lowers; transition: always adds smooth transitions).
_CONTRADICTS_ANY_SRC: dict[str, tuple[str, ...]] = {
    "en": ("no", "not", "without", "off", "remove", "skip", "stop", "never"),
    "tr": ("olmadan", "değil", "kaldır", "sil", "durdur", "hariç"),
}
_CONTRADICTS_SRC: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "music": ("up", "loud", "raise", "boost", "high", "increas", "amplif", "pump", "blast"),
        "transition": ("hard", "cut", "jump", "abrupt", "straight"),
    },
    "tr": {
        "music": ("yüksel", "artır", "aç", "güçlen", "bağır"),
        "transition": ("kes", "sert", "ani", "doğrudan"),
    },
}
_CONTRADICTS_ANY: dict[str, tuple[str, ...]] = {
    lang: tuple(_fold(w) for w in words) for lang, words in _CONTRADICTS_ANY_SRC.items()
}
_CONTRADICTS: dict[str, dict[str, tuple[str, ...]]] = {
    lang: _prep(table) for lang, table in _CONTRADICTS_SRC.items()
}

# Capability words for the ``why`` subtitle. A subtitle describes the footage; it may not name
# a capability the tapped chip does not run (or one that is off for the account). A kind's own
# capability is allowed on that kind only. ``None`` = never allowed in a subtitle.
_CAPABILITY_SRC: dict[str, dict[str | None, tuple[str, ...]]] = {
    "en": {
        "captions": ("caption", "subtitle"),
        "music": ("music", "song", "soundtrack"),
        "sfx": ("sfx", "whoosh", "sound effect"),
        "transition": ("transition",),
        "title_animation": ("animat",),
        "hook": ("hook",),
        None: ("overlay", "sticker", "filter", "voiceover", "voice-over"),
    },
    "tr": {
        "captions": ("altyaz",),
        "music": ("müzi", "şarkı"),
        "sfx": ("efekt", "sfx", "whoosh"),
        "transition": ("geçiş",),
        "title_animation": ("animasyon", "canlan"),
        "hook": ("hook",),
        None: ("bindirme", "çıkartma", "filtre", "seslendirme"),
    },
}
_CAPABILITY: dict[str, dict[str | None, tuple[str, ...]]] = {
    lang: {kind: tuple(_fold(w) for w in words) for kind, words in table.items()}
    for lang, table in _CAPABILITY_SRC.items()
}
# English fillers that survive the 4-letter cut. They carry no footage meaning, so they may
# neither enter the vocabulary nor count as an overlap.
_FOOTAGE_STOP = frozenset(
    _fold(w)
    for w in (
        "with that this from into onto over then than them they their there here when where "
        "what which while about after before around near been being have were will would could "
        "should some more very just also only each both other your ours "
        "için gibi daha kadar sonra önce olan bunu şunu veya ile çok"
    ).split()
)
_STOP_FOLDED = tuple(_fold(phrase) for phrase in STOPLIST)


def language_of(language: str | None) -> str:
    """``en`` or ``tr``; anything else falls back to ``en`` (the only other copy we ship)."""
    return language if language in SUPPORTED else "en"


def _stem_match(token: str, stems: Iterable[str], slack: int) -> bool:
    return any(token == s or (token.startswith(s) and len(token) <= len(s) + slack) for s in stems)


def _contra_match(token: str, stems: Iterable[str], slack: int) -> bool:
    """Like ``_stem_match`` but a stem of 3 letters or fewer must match the whole word
    (``no`` must not catch ``now``, ``off`` must not catch ``offer``)."""
    return any(
        token == s or (len(s) > 3 and token.startswith(s) and len(token) <= len(s) + slack)
        for s in stems
    )


def lexicon_words(kind: str, language: str) -> tuple[str, ...]:
    """The kind's action words, for the agent prompt's vocabulary hint (display-folded)."""
    lang = language_of(language)
    return tuple(_LEXICON_SRC[lang].get(kind, ()))


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(_fold(text)) if t]


def _hits_stoplist(text: str) -> bool:
    low = _fold(text)
    return any(phrase and phrase in low for phrase in _STOP_FOLDED)


def _exclusive_stems(kind: str, lang: str) -> list[str]:
    """Action stems that belong to OTHER kinds and not to this one (label parity)."""
    own = LEXICON[lang].get(kind, ())
    common = _COMMON[lang]
    out: list[str] = []
    for other, stems in LEXICON[lang].items():
        if other == kind:
            continue
        for stem in stems:
            if stem in common or _stem_match(stem, own, _SLACK[lang]):
                continue
            # A stem that the own lexicon would also match by prefix is shared, not exclusive.
            if any(o.startswith(stem) or stem.startswith(o) for o in own):
                continue
            out.append(stem)
    return out


def validate_label(
    template: str,
    *,
    kind: str,
    language: str,
    slots: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """``(rendered_label, None)`` when the label may be shown, else ``(None, reason)``.

    ``template`` may contain ``{time}`` / ``{filename}`` placeholders. Each must appear in
    ``slots`` (the candidate's ``label_slots``), which supplies the value the same renderer
    also writes into ``message``. The reason is a short slug for telemetry only.
    """
    lang = language_of(language)
    slots = slots or {}
    text = " ".join(str(template or "").split())
    if not text:
        return None, "empty"
    names = _PLACEHOLDER.findall(text)
    for name in names:
        if name not in ALLOWED_PLACEHOLDERS or name not in slots:
            return None, "placeholder"
    body = _PLACEHOLDER.sub(" ", text)
    if "{" in body or "}" in body:
        return None, "placeholder"
    if any(ch.isdigit() for ch in body):
        return None, "digit"
    if any(ch in _QUOTES for ch in body):
        return None, "quote"
    body = " ".join(body.split())
    if body and not _LABEL_CHARS.match(body):
        return None, "chars"
    if _hits_stoplist(body):
        return None, "stoplist"
    tokens = _tokens(body)
    if not tokens and not names:
        return None, "empty"
    own = LEXICON[lang].get(kind, ())
    common = _COMMON[lang]
    slack = _SLACK[lang]
    for token in tokens:
        if token in common or _stem_match(token, own, slack):
            continue
        return None, "off_lexicon"
    anchored = any(_stem_match(t, own, slack) for t in tokens) or bool(names)
    if not anchored:
        return None, "no_anchor"
    foreign = _exclusive_stems(kind, lang)
    if any(_stem_match(t, foreign, slack) for t in tokens):
        return None, "cross_kind"
    opposite = _CONTRADICTS_ANY[lang] + _CONTRADICTS[lang].get(kind, ())
    if any(_contra_match(t, opposite, slack) for t in tokens):
        return None, "polarity"
    rendered = _PLACEHOLDER.sub(lambda m: str(slots[m.group(1)]), text)
    rendered = " ".join(rendered.split())
    if not rendered or len(rendered) > MAX_LABEL_CHARS:
        return None, "length"
    if any(ch in _QUOTES for ch in rendered):
        return None, "quote"
    return rendered, None


def footage_vocabulary(clips: Sequence[Mapping[str, object]]) -> frozenset[str]:
    """Folded content words (4+ letters) the footage analysis actually used."""
    words: set[str] = set()
    for clip in clips:
        if not isinstance(clip, Mapping):
            continue
        texts: list[str] = [str(clip.get("subject") or ""), str(clip.get("setting") or "")]
        for moment in clip.get("notable_moments") or []:
            if isinstance(moment, Mapping):
                texts.append(str(moment.get("label") or moment.get("description") or ""))
        for text in texts:
            words.update(t for t in _tokens(text) if len(t) >= 4 and t not in _FOOTAGE_STOP)
    return frozenset(words)


def _singular(token: str) -> str:
    return token[:-1] if len(token) > 4 and token.endswith("s") else token


def validate_why(
    why: object, *, language: str, vocabulary: frozenset[str], kind: str | None = None
) -> tuple[str | None, str | None]:
    """``(why, None)`` when the subtitle is grounded in the footage, else ``(None, reason)``.

    Grounded = it shares at least one content word (4+ letters, folded, filler words
    excluded) with the footage vocabulary. It carries no digits, quotes, handles, links,
    marketing register or markup, fits the 48 character subtitle line, and (when ``kind`` is
    given) names no capability other than the chip's own: a subtitle never promises what the
    tap does not do. The vocabulary is whatever language the analyzer wrote; a ``why`` in
    another language is therefore dropped unless it shares a footage word, by design.
    """
    lang = language_of(language)
    if not isinstance(why, str):
        return None, "empty"
    text = " ".join(why.split())
    if not text:
        return None, "empty"
    if len(text) > MAX_WHY_CHARS:
        return None, "length"
    if any(ch.isdigit() for ch in text):
        return None, "digit"
    if any(ch in _QUOTES for ch in text):
        return None, "quote"
    if _HANDLE.search(text) or "http" in text.casefold() or "@" in text or "#" in text:
        return None, "handle"
    if _hits_stoplist(text):
        return None, "stoplist"
    tokens = _tokens(text)
    if len(tokens) < 2:
        return None, "short"
    # Language check: the subtitle must be in the chat's language. A line with no word from
    # that language's function list and non-ASCII letters (or vice versa) is another language.
    ascii_only = all(ord(ch) < 128 for ch in text)
    if lang == "en" and not ascii_only:
        return None, "language"
    if kind is not None:
        slack = _SLACK[lang]
        for cap_kind, stems in _CAPABILITY[lang].items():
            if cap_kind == kind:
                continue
            if any(_stem_match(t, stems, slack) for t in tokens) or any(
                stem in _fold(text) for stem in stems if " " in stem
            ):
                return None, "capability"
    content = {_singular(t) for t in tokens if len(t) >= 4 and t not in _FOOTAGE_STOP}
    vocab = {_singular(v) for v in vocabulary}
    if not content & vocab:
        return None, "ungrounded"
    return text, None


__all__ = [
    "ALLOWED_PLACEHOLDERS",
    "LEXICON",
    "MAX_LABEL_CHARS",
    "MAX_WHY_CHARS",
    "STOPLIST",
    "footage_vocabulary",
    "language_of",
    "lexicon_words",
    "validate_label",
    "validate_why",
]
