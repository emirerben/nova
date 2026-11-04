"""Deterministic copy rules for suggestion chips and cards (KRI-756, AI suggestion blocks).

The suggestion agent (``nova.chat.suggestion_agent``) writes a one-line ``why`` and, for a hook,
the on-screen ``copy``. It never writes a label, the ``message`` a tap sends, or an action. This
module is the gate between the model and the screen:

* ``validate_label``  a closed-vocabulary check that every TEMPLATE label obeys (a regression
  guard: the template table is the only source of a label now). Every word must belong to the
  kind's lexicon or to a small function/verb list, and a label must not name a DIFFERENT kind's
  action (label parity: a label can never promise something the tap does not do).
* ``validate_why``    a lexical grounding check: the subtitle must share a content word with
  what the footage analysis actually said (fold-and-overlap, the technique
  ``edit_copilot`` uses for caption wording). Gemini-derived text lives ONLY here.
* ``validate_copy``   the on-screen text gate (the Gemini-metadata invariant): hook copy must be
  plain, in the thread language, and must not reuse a run of words from a transcript or from
  the Gemini subject / setting / moment text.
* ``shares_run``      the laundering check both ``why`` and ``copy`` use against transcripts.
* ``footage_vocabulary`` the folded word set the grounding check reads.
* ``SUGGESTION_COPY`` the title / chat-prompt templates of the kinds the KRI-756 table never had.

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
# A v2 card carries a longer one-line reason than a chip subtitle.
MAX_CARD_WHY_CHARS = 96
# On-screen hook text is read in under three seconds: far shorter than the schema's 200.
MAX_COPY_CHARS = 60
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
    why: object,
    *,
    language: str,
    vocabulary: frozenset[str],
    kind: str | None = None,
    max_chars: int = MAX_WHY_CHARS,
) -> tuple[str | None, str | None]:
    """``(why, None)`` when the subtitle is grounded in the footage, else ``(None, reason)``.

    Grounded = it shares at least one content word (4+ letters, folded, filler words
    excluded) with the footage vocabulary. It carries no digits, quotes, handles, links,
    marketing register or markup, fits the ``max_chars`` line (48 for a chip subtitle), and
    (when ``kind`` is given) names no capability other than the chip's own: a subtitle never
    promises what the tap does not do. The vocabulary is whatever language the analyzer wrote;
    a ``why`` in another language is therefore dropped unless it shares a footage word, by
    design.
    """
    lang = language_of(language)
    if not isinstance(why, str):
        return None, "empty"
    text = " ".join(why.split())
    if not text:
        return None, "empty"
    if len(text) > max_chars:
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


_RUN = 3


def _runs(text: str, n: int = _RUN) -> set[tuple[str, ...]]:
    words = _tokens(text)
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def shares_run(text: str, sources: Iterable[str], *, n: int = _RUN) -> bool:
    """Does ``text`` repeat ``n`` consecutive words of any source (loosely folded)?

    The laundering check: a transcript, or an instruction hidden in one, must not come back out
    of the model as a card's reason or a hook's words. Whole-clause overlap is what an echo
    looks like; a shared word or two is ordinary language.
    """
    mine = _runs(text, n)
    return bool(mine) and any(mine & _runs(source, n) for source in sources if source)


_COPY_CHARS = re.compile(r"^[^\W_]+(?:[ \-',.!?:][^\W_]+)*[.!?]?$", re.UNICODE)


def validate_copy(
    copy: object, *, language: str, transcripts: Iterable[str] = (), footage: Iterable[str] = ()
) -> tuple[str | None, str | None]:
    """``(copy, None)`` when the exact on-screen text may be offered, else ``(None, reason)``.

    Hook and title text is the only generated text that can reach the screen, so it is checked
    harder than a ``why``: one plain line, in the thread language, no markup, links, handles or
    marketing register, and no run of words taken from a transcript or from what the footage
    analysis wrote (a subject, setting or moment label). Gemini metadata never becomes
    on-screen text.
    """
    lang = language_of(language)
    if not isinstance(copy, str):
        return None, "empty"
    text = " ".join(copy.split())
    if not text:
        return None, "empty"
    if len(text) > MAX_COPY_CHARS:
        return None, "length"
    if any(ch in text for ch in "{}<>$`\\|[]*_#@") or "http" in text.casefold() or "www." in text:
        return None, "markup"
    if not _COPY_CHARS.match(text):
        return None, "chars"
    if _hits_stoplist(text):
        return None, "stoplist"
    if len(_tokens(text)) < 2:
        return None, "short"
    if lang == "en" and not all(ord(ch) < 128 for ch in text):
        return None, "language"
    footage_texts = [t for t in footage if t]
    if shares_run(text, [*transcripts, *footage_texts]):
        return None, "reused_footage_text"
    folded = _fold(text)
    for source in footage_texts:
        if len(_tokens(source)) >= 2 and _fold(source) in folded:
            return None, "reused_footage_text"
    return text, None


# Title and chat-prompt templates for the kinds the KRI-756 ``_COPY`` table never had. ``{clip}``
# is filled with the card's clip ordinals ("2" or "2, 3"); a template that needs it renders
# nothing for a whole-video target, so a card never names a clip it does not touch. No quotes,
# and never any footage-derived word: a tap sends only this closed text.
SUGGESTION_COPY: dict[str, dict[str, tuple[str, str]]] = {
    "mute_clip": {
        "en": ("Mute clip {clip}", "Mute the sound of clip {clip}"),
        "tr": ("{clip}. klibi sustur", "{clip}. klibin sesini kapat"),
    },
    "filler_words": {
        "en": ("Cut the filler words", "Cut the filler words"),
        "tr": ("Dolgu sözcüklerini kes", "Dolgu sözcüklerini kes"),
    },
    "duplicates": {
        "en": ("Keep the better clip", "Keep the better of clips {clip}"),
        "tr": ("Daha iyi klibi tut", "{clip}. kliplerden daha iyisini tut"),
    },
    # A v2 hook card shows the agent's text on the card itself. The chat prompt a tap sends is a
    # closed sentence that never carries or quotes that text: a creator message that quotes copy
    # skips the approval question (`creator_quoted`), and the tap IS the approval already.
    "hook": {
        "en": ("Add this hook", "Add the hook shown on the card"),
        "tr": ("Bu kancayı ekle", "Kartta gösterilen kancayı ekle"),
    },
    "generated_overlay": {
        "en": ("Add a generated image", "Add a generated image overlay"),
        "tr": ("Üretilmiş görsel ekle", "Üretilmiş bir görsel bindirmesi ekle"),
    },
}


def render_suggestion_copy(
    kind: str, language: str, clip_ordinals: Sequence[int]
) -> tuple[str, str] | None:
    """``(title, prompt)`` for a kind outside the KRI-756 table, or None when unrenderable."""
    table = SUGGESTION_COPY.get(kind)
    if table is None:
        return None
    title, prompt = table.get(language_of(language)) or table["en"]
    if "{clip}" in title + prompt:
        if not clip_ordinals:
            return None
        clips = ", ".join(str(n) for n in sorted(set(clip_ordinals))[:3])
        title, prompt = title.replace("{clip}", clips), prompt.replace("{clip}", clips)
    return title, prompt


__all__ = [
    "ALLOWED_PLACEHOLDERS",
    "LEXICON",
    "MAX_CARD_WHY_CHARS",
    "MAX_COPY_CHARS",
    "MAX_LABEL_CHARS",
    "MAX_WHY_CHARS",
    "STOPLIST",
    "SUGGESTION_COPY",
    "footage_vocabulary",
    "language_of",
    "render_suggestion_copy",
    "shares_run",
    "validate_copy",
    "validate_label",
    "validate_why",
]
