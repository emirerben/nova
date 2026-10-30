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

from collections.abc import Callable, Iterable
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


# Only the unambiguous verb endings: a bare -ma/-me is also a noun ("kesme" = cut,
# "çekme"), so it counts as negation only as the last word of its clause.
_STRICT_NEGATED_TAILS = ("masin", "mesin", "mayin", "meyin", "mamali", "memeli")


def _negated_strict(word: str) -> bool:
    if word in _NEGATION_WORDS or "miyor" in word or "muyor" in word:
        return True
    return any(word.endswith(tail) and len(word) > len(tail) + 1 for tail in _STRICT_NEGATED_TAILS)


# --------------------------------------------------------------------------- titles
# Opening-title nouns spliced into the title grammar beside English title/intro/hook/
# text. Only the forms that introduce copy: bare, possessive and accusative ("başlık",
# "başlığı", "başlığını", "yazı", "yazısı", "yazısını", "yazıyı"). Case forms that point
# at existing text ("yazıda 'Sen' yerine 'Siz'", "yazıdan") fail the trailing \b, as
# do plurals (captions); "yazı tipi" is a font.
TITLE_NOUN_PATTERN_TR = (
    r"ba[sş]l[iı](?:[gğ][iı](?:n[iı])?|k)"
    r"|yaz[iı](?:s[iı](?:n[iı])?|y[iı])?(?!\s+tip)"
)
# A closing cue between the copy and the noun ("'Görüşürüz' kapanış başlığı") or
# before the noun ("kapanış başlığı", "bitiş yazısı").
CLOSING_CUE_PATTERN_TR = r"kapan[iı][sş]\w*|biti[sş]\w*"
# Words for "the end" that may sit before the copy ("sona 'X' yazısı ekle", "videonun
# sonunda 'X' yazısı"). Read as whole words, Turkish messages only: "sonra" (then) and
# English "son" are not cues.
_CLOSING_WORDS = frozenset(
    "son sona sonda sonu sonuna sonunda sondaki sonundaki kapanis kapanista kapanisa"
    " bitis bitiste bitise".split()
)
_CLAUSE_JOINERS = frozenset({"ve", "ama", "fakat", "ancak", "sonra", "ile"})


def turkish_closing_cue(request: str, clause_prefix: str) -> bool:
    """True when the last words of ``clause_prefix`` name the end of the video, in a
    Turkish ``request`` ("sona", "videonun sonunda", "son yazı")."""

    if not loose_creator_text(request):
        return False
    clauses = _clauses(loose_text(clause_prefix))
    tail = clauses[-1][-2:] if clauses else []
    return any(word in _CLOSING_WORDS for word in tail)


# Verbs that put copy on screen; a bare -ma/-me on one of them closes a refusal
# ("koyma", "kullanma", "ekleme"), where on any other word it may be English or a
# noun ("theme", "same time", "kesme").
_COPY_VERBS = ("koy", "kullan", "ekle", "yaz", "goster", "yerlestir", "olustur", "cikar", "sec")


def turkish_clause_refused(request: str, clause_rest: str) -> bool:
    """True when the Turkish verb closing a clause negates it ("'Emir' başlığını
    koyma", "başlık 'Emir' olmasın"). ``clause_rest`` is the text after the match, up
    to the next clause break; only its first sub-clause (before "ve"/"ama") counts."""

    if not loose_creator_text(request):
        return False
    clauses = _clauses(loose_text(clause_rest))
    words = clauses[0] if clauses else []
    for index, word in enumerate(words):
        if word in _CLAUSE_JOINERS:
            words = words[:index]
            break
    if any(_negated_strict(word) for word in words):
        return True
    return bool(words) and _starts(words[-1], _COPY_VERBS) and _negated(words[-1])


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


# Affirmative and negated endings of the SFX verbs, after ASCII folding (ı->i,
# ü->u). A verb is a whole word: the stem plus one of these, so English "second",
# "calm" or "calls" never reads as "seç" / "çal".
_SFX_VERB_SUFFIXES = frozenset(
    "_ in un yin yun iniz unuz yiniz yunuz sin sun sene sana elim alim yelim yalim"
    " ir ur ar er r abilir ebilir yabilir yebilir abilirsin ebilirsin yabilirsin"
    " yebilirsin nsin nsun nir ip yip yup".split()
)
_SFX_VERB_REFUSED = frozenset(
    "ma me masin mesin mayin meyin maz mez mamali memeli mayalim meyelim masana mesene"
    " mayiniz meyiniz".split()
)
# Verbal nouns ("eklemeyi unutma", "kullanmak istemiyorum"): refused only when the
# next word refuses, and never before "unutma" (don't forget to).
_SFX_VERBAL_NOUN = frozenset("mak mek mayi meyi masini mesini".split())
# Whole words that close an SFX ask with no verb ("Vine Boom ses efekti olsun").
_SFX_WANT_WORDS = frozenset("olsun istiyorum isterim lazim".split())
_SFX_REFUSE_WORDS = frozenset("olmasin istemiyorum istemem olmaz yok".split())
_NOT_FORGET = ("unutma",)
# Words before the noun that are never part of an effect's name.
_SFX_FILLERS = frozenset(
    "ve ile veya lutfen de da ki icin ustune ustunde uzerine sonra once bana buraya"
    " oraya suraya simdi gibi mi cok olarak".split()
)
# Case endings that make the word before the noun an argument of the verb, not the
# effect's name: "videoya", "yerlere", "geçişte", "klipten", "müziğin üstüne".
_CASE_TAILS = (
    "ya",
    "ye",
    "da",
    "de",
    "ta",
    "te",
    "dan",
    "den",
    "tan",
    "ten",
    "na",
    "ne",
    "lere",
    "lara",
    "nin",
    "nun",
    "in",
    "un",
)
_MEDIA_STEMS = ("klip", "klib", "video", "foto", "sahne", "muzik", "sarki", "gecis", "kesim")
UNNAMED_SFX_PHRASE_TR = "bir"


def _sfx_verb(words: list[str], at: int) -> bool | None:
    """None when ``words[at]`` is no SFX verb form, else whether it refuses."""

    word = words[at]
    nxt = words[at + 1] if at + 1 < len(words) else ""
    if word in _SFX_WANT_WORDS:
        return False
    if word in _SFX_REFUSE_WORDS:
        return True
    for stem in _SFX_VERBS:
        if not word.startswith(stem):
            continue
        suffix = word[len(stem) :] or "_"
        if suffix in _SFX_VERB_SUFFIXES:
            return False
        if suffix in _SFX_VERB_REFUSED or "miyor" in suffix or "muyor" in suffix:
            return True
        if suffix in _SFX_VERBAL_NOUN:
            if nxt.startswith(_NOT_FORGET):
                return False
            return nxt in _SFX_REFUSE_WORDS or (nxt.startswith("iste") and _negated(nxt))
    return None


def _name_word(token: str, folded: str) -> bool:
    if not token or folded in _SFX_FILLERS:
        return False
    return token.casefold() not in SFX_GENERIC_WORDS_TR


def _argument_word(folded: str) -> bool:
    return folded.startswith(_MEDIA_STEMS) or any(
        folded.endswith(tail) and len(folded) > len(tail) + 1 for tail in _CASE_TAILS
    )


def turkish_sfx_phrases(
    request: str, *, resolves: Callable[[str], bool] | None = None
) -> list[tuple[str, bool]]:
    """Every "<name> ses efekti ekle" / "<name> sfx kullan" phrase in a Turkish
    message, in order, paired with whether it was refused ("whoosh ses efekti ekleme").

    English messages yield nothing. With ``resolves`` (the catalog), the longest of
    the up-to-three words before the noun that names a catalog effect wins ("vine boom
    ses efekti ekle"). Otherwise the name is the word before the noun, or the two
    words before it when both are capitalized ("Vine Boom"); a word that carries a
    case ending ("videoya", "yerlere") or a filler ("lütfen", "ve") is no name, and the
    ask comes back as ``UNNAMED_SFX_PHRASE_TR`` so the caller treats it as unnamed.
    """

    if not loose_creator_text(request):
        return []
    raw = str(request or "").split()
    tokens = [_strip_punct(token) for token in raw]
    loose = [loose_text(token) for token in tokens]
    phrases: list[tuple[str, bool]] = []
    for index, word in enumerate(loose):
        if word.startswith("sfx"):
            noun_end = index
        elif word == "ses" and index + 1 < len(loose) and loose[index + 1].startswith("efekt"):
            noun_end = index + 1
        else:
            continue
        if raw[noun_end][-1:] in _CLAUSE_END:
            continue  # "whoosh sesi. Ekle" is two sentences
        verb_at = noun_end + 1
        if verb_at < len(loose) and loose[verb_at] in {"de", "da"}:
            verb_at += 1
        after: list[str] = []
        if verb_at < len(loose) and loose[verb_at] == "olarak":
            # "ses efekti olarak Vine Boom kullan": the name follows the noun.
            for at in range(verb_at + 1, min(verb_at + 5, len(loose))):
                if _sfx_verb(loose, at) is not None:
                    verb_at = at
                    break
                after.append(tokens[at])
                if raw[at][-1:] in _CLAUSE_END:
                    break
            else:
                continue
        if verb_at >= len(loose):
            continue
        refused = _sfx_verb(loose, verb_at)
        if refused is None:
            continue
        if after:
            phrases.append((" ".join(after), refused))
            continue
        span: list[int] = []
        at = index - 1
        while at >= 0 and len(span) < 3 and raw[at][-1:] not in _CLAUSE_END:
            if not _name_word(tokens[at], loose[at]):
                break
            span.insert(0, at)
            at -= 1
        name: list[str] = []
        if resolves is not None:
            for start in range(len(span)):
                candidate = " ".join(tokens[i] for i in span[start:])
                if resolves(candidate):
                    name = [candidate]
                    break
        if not name and span and not _argument_word(loose[span[-1]]):
            name = [tokens[span[-1]]]
            if (
                len(span) >= 2
                and tokens[span[-2]][:1].isupper()
                and tokens[span[-1]][:1].isupper()
                and not tokens[span[-1]].isupper()
                and not loose[span[-2]].startswith(_MEDIA_STEMS)
            ):
                name.insert(0, tokens[span[-2]])
        phrases.append((" ".join(name) or UNNAMED_SFX_PHRASE_TR, refused))
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
            # "hızlı tempo", "hızlı bir tempo", "tempo hızlı olsun".
            before = index - 2 if index >= 2 and words[index - 1] == "bir" else index - 1
            for at in (before, index + 1, index + 2):
                if not 0 <= at < len(words):
                    continue
                pace = next((pace for pace, adjectives in _PACES if words[at] in adjectives), None)
                if pace is None:
                    continue
                after = max(at, index) + 1
                following = words[after : after + 3]
                closes = after + len(following) == len(words)
                for cut, follow in enumerate(following):
                    if follow in _CLAUSE_JOINERS:
                        following, closes = following[:cut], True  # "... ve sert kesme"
                        break
                # A bare -ma/-me word is a verb only when it closes the clause
                # ("hızlı tempo kullanma"); "sert kesme" is a noun (a hard cut).
                if not any(_negated_strict(follow) for follow in following) and not (
                    following and closes and _negated(following[-1])
                ):
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
# A number word that ends a compound ("on beş" = 15, "yirmi iki") is not that number.
TR_SECONDS_WORD_LEAD = r"(?<!\w)(?<!on\s)(?<!yirmi\s)(?<!otuz\s)"


def tr_seconds_spellings(seconds: float) -> list[tuple[str, str]]:
    """(regex lead, spelling) pairs that state ``seconds`` before a Turkish unit:
    number words ("iki", "bir buçuk"), and digits with a decimal point or comma
    ("2", "1,5", "1.5"), which the English digit reader misses before "sn"."""

    spellings = [
        (TR_SECONDS_WORD_LEAD, word)
        for word, number in TR_SECONDS_WORDS.items()
        if abs(number - seconds) < 1e-6
    ]
    spellings += [
        (TR_SECONDS_WORD_LEAD, rf"{word}\s+bu[cç]uk")
        for word, number in TR_SECONDS_WORDS.items()
        if number >= 1 and abs(number + 0.5 - seconds) < 1e-6
    ]
    if seconds > 0:
        whole = f"{seconds:g}"
        digits = {whole, whole.replace(".", ",")}
        if float(seconds).is_integer():
            digits |= {f"{int(seconds)}.0", f"{int(seconds)},0"}
        spellings += [(r"(?<![\w.,])", digit.replace(".", r"\.")) for digit in sorted(digits)]
    return spellings


# --------------------------------------------------------------------------- mixed timing review
_TIMING_STEMS = ("timing", "zamanlama", "tempo", "gecis")
_RESET_STEMS = ("sifirla", "kaldir", "iptal", "normal", "standart", "varsayilan")
_PRESERVE_STEMS = ("degistirme", "elleme", "dokunma", "bozma")


def preserves_editor_text(text: str) -> bool:
    """An explicit preservation request tied to text, not another editor lane."""
    for words in _clauses(loose_text(text.replace("'", "").replace("’", ""))):
        for index, word in enumerate(words):
            if word not in {"text", "texts", "word", "words"} and not _starts(
                word, ("metin", "metn", "yazi", "kelime")
            ):
                continue
            before, after = words[max(0, index - 4) : index], words[index + 1 : index + 5]
            if any(w in {"unchanged", "untouched"} for w in after) and "not" not in before:
                return True
            if any(w in {"dont", "never", "not"} for w in before) and any(
                w in {"change", "touch", "alter", "modify", "retime", "edit", "move"}
                for w in before
            ):
                return True
            if any(
                _starts(w, _PRESERVE_STEMS) and (_negated(w) or w.endswith(("madan", "meden")))
                for w in before + after
            ):
                return True
    return False


_PHOTO_STEMS = ("foto", "resim", "resm")
_FAST_STEMS = ("hizli", "cabuk", "seri", "flash")


_LIGHT_VERB_NOT = frozenset(
    "etme etmeyin etmesin yapma yapmayin yapmasin donme dondurme getirme".split()
)


def _refused_reset(words: list[str], index: int) -> bool:
    """A negated reset: "sıfırlama", "kaldırma", "iptal etme", "normale döndürme"."""
    word = words[index]
    nxt = words[index + 1] if index + 1 < len(words) else ""
    return _starts(word, _RESET_STEMS) and (_negated(word) or nxt in _LIGHT_VERB_NOT)


def _resets(words: list[str]) -> bool:
    if any(
        _starts(word, _RESET_STEMS) and not _refused_reset(words, index)
        for index, word in enumerate(words)
    ):
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
            if not _starts(word, _TIMING_STEMS):
                continue
            window = range(index + 1, min(index + 5, len(words)))
            # "zamanlamayı değiştirme"; a negated reset keeps it too ("zamanlamayı
            # sıfırlama", "zamanlamayı iptal etme"), like English "don't reset".
            if any(
                _starts(words[at], _PRESERVE_STEMS) or _refused_reset(words, at) for at in window
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
