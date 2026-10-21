"""Turkish wording for the legacy creator path's literal fallbacks (KRI-782).

The Creator Agent model reads Turkish; the literal recognizers in
``routes/creator_agent.py``, ``routes/plan_items.py`` and
``services/creator_sessions.py`` only back it up (old or failed model responses,
fields the model did not ground). Those fallbacks were English-only, so a Turkish
creator lost them. Their Turkish vocabulary lives here, in one place, instead of a
regex twin beside every English rule.

KRI-667: nothing here calls ``re``. Free-text readers compare folded word tokens
against small vocabularies; ``loose_creator_text`` folds Turkish letters to ASCII and
returns "" for a message that reads as English, so English input never reaches a
Turkish rule and English behavior is unchanged. The pattern fragments below are
spliced into the callers' existing grammars (no new call sites); they spell both
forms (``ba[sş]l[iı]``), and case-insensitive matching treats I/İ/ı/i as one letter.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from app.kria.brief_route import loose_text
from app.schemas.edit_proposal import loose_creator_text

_CLAUSE_END = frozenset(".!?;,\n")


def _clauses(loose: str) -> list[list[str]]:
    """Word tokens of already-folded text, split into clauses at . ! ? ; , and newlines."""

    clauses: list[list[str]] = []
    current: list[str] = []
    word = ""
    for ch in loose + "\n":
        if ch.isalnum():
            word += ch
            continue
        if ch in "'’":
            continue  # "timing'i" -> "timingi": the suffix stays on its word
        if word:
            current.append(word)
            word = ""
        if ch in _CLAUSE_END and current:
            clauses.append(current)
            current = []
    return clauses


def _starts(word: str, stems: Iterable[str]) -> bool:
    return any(word.startswith(stem) for stem in stems)


# A clause-closing negation: kullanma, geçirmesin, koymayın, değiştirmez, istemiyorum.
_NEGATION_WORDS = frozenset({"degil", "istemem", "istemiyorum", "olmasin", "olmaz", "yok"})
_NEGATED_TAILS = ("ma", "me", "masin", "mesin", "mayin", "meyin", "maz", "mez", "mamali", "memeli")


def _negated(word: str) -> bool:
    if word in _NEGATION_WORDS or "miyor" in word or "muyor" in word:
        return True
    return any(word.endswith(tail) and len(word) > len(tail) + 1 for tail in _NEGATED_TAILS)


# --------------------------------------------------------------------------- titles
# Opening-title nouns ("başlık", "başlığı", "yazısı"), spliced into the title grammar
# beside English title/intro/hook/text. Plurals (captions) and "yazı tipi" (font) are
# not titles.
TITLE_NOUN_PATTERN_TR = r"ba[sş]l[iı](?:k(?!lar)|[gğ](?![iı]?lar))\w*|yaz[iı](?!lar)(?!\s+tip)\w*"
# Closing cue before a title noun ("kapanış başlığı", "bitiş yazısı").
CLOSING_CUE_PATTERN_TR = r"kapan[iı][sş]\w*|biti[sş]\w*"

# --------------------------------------------------------------------------- SFX
# "ses efekti" / "ses efektini" / "SES EFEKTİ" beside English sound effect/sfx.
SFX_NOUN_PATTERN_TR = r"ses\s*efekt\w*"
_SFX_VERBS = ("ekle", "kullan", "koy", "sec", "yerlestir", "cal")
# Generic words before the noun ask for an effect in general ("bir ses efekti ekle").
SFX_GENERIC_WORDS_TR = frozenset(
    "bir bi herhangi başka baska farklı farkli yeni komik eğlenceli eglenceli güzel guzel"
    " hoş hos aynı ayni daha ekstra birkaç birkac bazı bazi bu şu su o her tek sadece"
    " küçük kucuk hafif rastgele biraz".split()
)


def _strip_punct(token: str) -> str:
    start, end = 0, len(token)
    while start < end and not token[start].isalnum():
        start += 1
    while end > start and not token[end - 1].isalnum():
        end -= 1
    return token[start:end]


def _sfx_refused(verb_suffix: str) -> bool:
    """ekle|me, kullan|mayın, koy|masın, ekle|mez -> refused; ekle|yin, ekle|meni -> not."""
    if "miyor" in verb_suffix or "muyor" in verb_suffix:
        return True
    if not verb_suffix.startswith(("ma", "me")):
        return False
    rest = verb_suffix[2:]
    return rest == "" or rest.startswith(("s", "y", "z", "li", "dan", "den"))


def turkish_sfx_phrases(request: str) -> list[tuple[str, bool]]:
    """Every "<name> ses efekti ekle" / "<name> sfx kullan" phrase, in order, paired
    with whether it was refused ("whoosh ses efekti ekleme").

    The name is the word before the noun, or the two words before it when both are
    capitalized ("Vine Boom"). A generic word ("bir ses efekti ekle") comes back as
    the phrase, so the caller treats it as an unnamed request.
    """

    raw = str(request or "").split()
    tokens = [_strip_punct(token) for token in raw]
    loose = [loose_text(token) for token in tokens]
    phrases: list[tuple[str, bool]] = []
    for index, word in enumerate(loose):
        if word.startswith("sfx"):
            verb_at = index + 1
        elif word == "ses" and index + 1 < len(loose) and loose[index + 1].startswith("efekt"):
            verb_at = index + 2
        else:
            continue
        # Name, noun and verb share one clause ("whoosh sesi. Ekle" is two sentences).
        if index == 0 or raw[index - 1][-1:] in _CLAUSE_END or raw[verb_at - 1][-1:] in _CLAUSE_END:
            continue
        if verb_at < len(loose) and loose[verb_at] in {"de", "da"}:
            verb_at += 1
        if verb_at >= len(loose):
            continue
        stem = next((stem for stem in _SFX_VERBS if loose[verb_at].startswith(stem)), None)
        if stem is None or not tokens[index - 1]:
            continue
        name = [tokens[index - 1]]
        if (
            index >= 2
            and raw[index - 2][-1:] not in _CLAUSE_END
            and tokens[index - 2][:1].isupper()
            and tokens[index - 1][:1].isupper()
            and not tokens[index - 1].isupper()
            and tokens[index - 2].casefold() not in SFX_GENERIC_WORDS_TR
        ):
            name.insert(0, tokens[index - 2])
        phrases.append((" ".join(name), _sfx_refused(loose[verb_at][len(stem) :])))
    return phrases


# --------------------------------------------------------------------------- clip order
_ORDER_NOUNS = frozenset(
    "sira sirayi sirasi sirasini siralama siralamayi siralamasi siralamasini "
    "dizilis dizilisi dizilisini duzen duzeni duzenini".split()
)
_KEEP_STEMS = ("koru", "tut")
_DO_NOT_CHANGE = ("bozma", "degistirme", "elleme", "dokunma")
_IN_ORDER = frozenset({"sirada", "sirayi", "siraya"})
_FOOTAGE_STEMS = ("klip", "klib", "clip", "video")


def turkish_keeps_clip_order(text: str) -> bool:
    """True for "kliplerin sırasını koru", "sırayı değiştirme", "aynı sırada kalsın".

    "sırayla" (one after another, alternating) is not an order noun here.
    """

    for words in _clauses(loose_creator_text(text)):
        for index, word in enumerate(words):
            following = words[index + 1 : index + 4]
            if word in _ORDER_NOUNS:
                if any(_starts(verb, _KEEP_STEMS) and not _negated(verb) for verb in following):
                    return True
                if any(_starts(verb, _DO_NOT_CHANGE) for verb in following):
                    return True
                if any(
                    a == "ayni" and b.startswith("kal")
                    for a, b in zip(following, following[1:], strict=False)
                ):
                    return True
            if word == "ayni":
                rest = [w for w in following[:2] if not _starts(w, _FOOTAGE_STEMS)]
                if rest[:1] and rest[0] in _IN_ORDER:
                    return True
    return False


# --------------------------------------------------------------------------- pacing
_PACE_NOUNS = ("tempo", "ritim", "ritm", "pacing", "kurgu", "akis")
_PACES: tuple[tuple[Literal["fast", "relaxed", "balanced"], frozenset[str]], ...] = (
    ("fast", frozenset({"hizli", "seri", "cabuk", "dinamik", "enerjik"})),
    ("relaxed", frozenset({"yavas", "sakin", "rahat", "dingin"})),
    ("balanced", frozenset({"dengeli", "orta"})),
)


def turkish_pacing(text: str) -> Literal["fast", "relaxed", "balanced"] | None:
    """A stated pace: "hızlı tempo", "tempo yavaş olsun", "dengeli ritim"; never a
    negated one ("hızlı tempo olmasın")."""

    for words in _clauses(loose_creator_text(text)):
        for index, word in enumerate(words):
            if not _starts(word, _PACE_NOUNS):
                continue
            for at in (index - 1, index + 1, index + 2):
                if not 0 <= at < len(words):
                    continue
                pace = next((pace for pace, adjectives in _PACES if words[at] in adjectives), None)
                if pace is None:
                    continue
                after = max(at, index) + 1
                if not any(_negated(follow) for follow in words[after : after + 3]):
                    return pace
    return None


# --------------------------------------------------------------------------- seconds
# Number words for a stated duration, paired only with the Turkish seconds unit, so
# English "on second thought" is never ten seconds (the kria/planner KRI-591 rule).
TR_SECONDS_WORDS = {
    "yarım": 0.5,
    "yarim": 0.5,
    "bir": 1.0,
    "iki": 2.0,
    "üç": 3.0,
    "uc": 3.0,
    "dört": 4.0,
    "dort": 4.0,
    "beş": 5.0,
    "bes": 5.0,
    "altı": 6.0,
    "alti": 6.0,
    "yedi": 7.0,
    "sekiz": 8.0,
    "dokuz": 9.0,
    "on": 10.0,
}
TR_SECONDS_UNIT_PATTERN = r"(?:saniye\w*|sn\b)"

# --------------------------------------------------------------------------- mixed timing review
_TIMING_STEMS = ("timing", "zamanlama", "tempo", "gecis")
_RESET_STEMS = ("sifirla", "kaldir", "iptal", "normal", "standart", "varsayilan")
_PRESERVE_STEMS = ("degistirme", "elleme", "dokunma", "bozma")
_PHOTO_STEMS = ("foto", "resim", "resm")
_FAST_STEMS = ("hizli", "cabuk", "seri", "flash")


def _resets(words: list[str]) -> bool:
    if any(_starts(word, _RESET_STEMS) for word in words):
        return True
    return any(
        (a == "geri" and b.startswith("al"))
        or (a.startswith("eski") and _starts(b, ("hal", "gibi")))
        for a, b in zip(words, words[1:], strict=False)
    )


def turkish_mixed_timing_review(text: str) -> Literal["preserve", "reset"] | None:
    """A draft-review opt-out in Turkish: "zamanlamayı sıfırla" / "fotoğrafları hızlı
    geçirme" -> "reset"; "zamanlamayı değiştirme" -> "preserve"; otherwise None."""

    clauses = _clauses(loose_creator_text(text))
    for words in clauses:
        for index, word in enumerate(words):
            if _starts(word, _TIMING_STEMS) and any(
                _starts(follow, _PRESERVE_STEMS) for follow in words[index + 1 : index + 5]
            ):
                return "preserve"
    for words in clauses:
        if any(_starts(word, _TIMING_STEMS) for word in words) and _resets(words):
            return "reset"
        fast_at = next((i for i, word in enumerate(words) if _starts(word, _FAST_STEMS)), None)
        if (
            fast_at is not None
            and any(_starts(word, _PHOTO_STEMS) for word in words)
            and any(_negated(word) for word in words[fast_at + 1 :])
        ):
            return "reset"
    return None
