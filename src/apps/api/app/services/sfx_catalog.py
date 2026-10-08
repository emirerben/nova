"""Deterministic sound-effect selection shared by the AI placers (KRI-173).

The creator library (~120 effects) outgrew every flat prompt budget: the chat
planner's manifest holds ~20 effects and the placement agent 30. These pure
helpers choose those subsets from the whole library the same way every time,
and match words as whole words, so "tap" never selects "Tape rewind".

No I/O and no settings: callers pass plain rows (ORM objects or dicts).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SFX_CATEGORIES: tuple[str, ...] = (
    "rejection",
    "approval",
    "suspense",
    "transition",
    "impact",
    "comedy",
    "ui",
    "sports",
    "money",
)
# Category-wide vocabulary every effect in the category carries in its search
# terms (the seed script appends it). Good for browsing and ranking, but never
# enough on its own to name ONE effect: "the right sound effect" is not a ding.
SFX_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "rejection": ("wrong", "fail", "no", "incorrect", "error", "reject", "lose", "bad"),
    "approval": ("correct", "right", "yes", "success", "win", "approve", "good", "celebrate"),
    "suspense": ("suspense", "reveal", "tension", "build up", "anticipation", "dramatic"),
    "transition": ("transition", "cut", "scene change", "movement", "fast"),
    "impact": ("impact", "hit", "punch", "drop", "emphasis", "boom"),
    "comedy": ("funny", "comedy", "meme", "joke", "cartoon", "silly", "awkward"),
    "ui": ("ui", "text", "title", "caption", "interface", "appear", "list"),
    "sports": ("sports", "football", "soccer", "match", "game", "stadium", "team"),
    "money": ("money", "cash", "rich", "price", "sale", "payday", "win"),
}

# Words that describe the request, not the sound ("add a sound effect").
_FILLER = frozenset(
    {
        "a", "an", "the", "some", "any", "sound", "sounds", "effect", "effects", "sfx",
        "noise", "my", "this", "that", "it", "of", "to", "for", "with", "and", "please",
        "like", "kind", "type", "little", "quick", "one",
    }
)  # fmt: skip
# A description containing these is a refusal, never a request.
_REFUSAL = frozenset({"no", "not", "none", "without", "never", "don", "dont"})

# KRI-540: Turkish creators describe sounds in Turkish ("küçük bir fincan sesi")
# while the library is English-only. Everything below applies ONLY to requests
# that read as Turkish (see ``_is_turkish``), so English requests tokenise and
# match exactly as before. Kept apart from ``_FILLER``/``_REFUSAL``, which the
# web picker mirrors (``test_sfx_catalog_web_parity``). All keys are ASCII-folded.
_FILLER_TR = frozenset(
    {
        "ses", "sesi", "sesini", "sesiyle", "sesler", "efekt", "efekti", "efektleri",
        "kucuk", "ufak", "minik", "kisa", "hafif", "bir", "bi", "her", "gibi", "cal",
        "calsin", "ekle", "koy", "olsun",
    }
)  # fmt: skip
_REFUSAL_TR = frozenset({"olmasin", "istemiyorum", "istemem", "kaldir", "yok", "hayir"})
# Turkish words that are also ordinary English tokens (or too short to trust):
# translated inside a Turkish request, but never what makes a request Turkish.
_TR_AMBIGUOUS = frozenset({"her", "can", "cam", "bi", "koy", "cal"})
# Turkish sound word -> the English words the library names/search terms use.
_TR_VOCAB: dict[str, tuple[str, ...]] = {
    "fincan": ("cup",),
    "bardak": ("glass",),
    "kadeh": ("glass",),
    "cam": ("glass",),
    "kirilma": ("break",),
    "zil": ("bell",),
    "can": ("bell",),  # çan
    "alkis": ("applause",),
    "duduk": ("whistle",),
    "islik": ("whistle",),
    "hakem": ("referee",),
    "kapi": ("door",),
    "carpma": ("slam",),
    "tik": ("click",),
    "tiklama": ("click",),
    "kalabalik": ("crowd",),
    "patlama": ("explosion",),
    "davul": ("drum",),
    "para": ("money",),
    "kasa": ("cash", "register"),
    "gulme": ("laugh",),
    "kahkaha": ("laugh",),
    "korna": ("horn",),
    "kamera": ("camera",),
    "fotograf": ("photo",),
    "klavye": ("keyboard",),
    "tebrik": ("win",),
    "zafer": ("win",),
    "yanlis": ("wrong",),
    "dogru": ("correct",),
    "cevap": ("answer",),
    "kalp": ("heart",),
    "saat": ("clock",),
    "sayfa": ("page",),
    "vurus": ("hit",),
    "yumruk": ("punch",),
    "bildirim": ("notification",),
    "mesaj": ("message",),
    "gol": ("goal",),
}
# Soft-consonant stems before a vowel suffix: düdüğü, kalabalığı, ıslığı.
_TR_VOCAB.update(
    {k[:-1] + "g": v for k, v in list(_TR_VOCAB.items()) if k.endswith("k") and len(k) > 2}
)
_TR_PHRASES: dict[tuple[str, ...], tuple[str, ...]] = {("bozuk", "para"): ("coin",)}
# Case/possessive/plural endings after a vocabulary stem (ASCII-folded).
_TR_SUFFIX = re.compile(
    r"(?:l[ae]r)?(?:s?[iu]|n?[iu]n|[iu]?n[iu]|y?[ae]|[dt][ae]n?|n[dt][ae]n?"
    r"|[iu]?yl[ae]|s[iu]yl[ae]|l[ae]|n[ae])?"
)
_ONE_SHOT_MAX_S = 3.0
# Level-matched creator library first; the quiet smart-* sound-design layer
# ("core") and untiered legacy uploads only win when nothing else fits.
_TIER_RANK = {"library": 0, "core": 1}


# Turkish letters folded to the ASCII the (English) library is written in.
_TR_FOLD = str.maketrans("çğıöşü", "cgiosu")
_TR_LETTERS = frozenset("çğıöşü")


def _raw_words(text: object) -> list[str]:
    """Casefolded letter/digit runs of any script (Turkish dotted İ keeps its i)."""
    folded = str(text or "").casefold().replace("\u0307", "")
    return re.findall(r"[^\W_]+", folded)


def words(text: object) -> list[str]:
    """Whole-word tokens, Turkish letters folded to ASCII.

    Pure-ASCII text tokenises exactly as ``[a-z0-9]+`` on the casefolded text.
    """
    return [w.translate(_TR_FOLD) for w in _raw_words(text)]


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


_CATEGORY_WORDS: dict[str, frozenset[str]] = {
    category: frozenset(_stem(w) for term in (*terms, category) for w in words(term))
    for category, terms in SFX_CATEGORY_TERMS.items()
}


@dataclass(frozen=True)
class SfxEntry:
    id: str
    name: str
    category: str | None = None
    search_terms: tuple[str, ...] = ()
    role_tags: tuple[str, ...] = ()
    contains_voice: bool = False
    quality_tier: str | None = None
    duration_s: float | None = None
    catalog_rank: int | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> SfxEntry:
        get = row.get if isinstance(row, Mapping) else lambda key: getattr(row, key, None)
        return cls(
            id=str(get("id")),
            name=str(get("name") or ""),
            category=get("category"),
            search_terms=tuple(str(t) for t in (get("search_terms") or []) if t),
            role_tags=tuple(str(t) for t in (get("role_tags") or []) if t),
            contains_voice=bool(get("contains_voice")),
            quality_tier=get("quality_tier"),
            duration_s=get("duration_s"),
            catalog_rank=get("catalog_rank"),
            created_at=get("created_at"),
        )

    @property
    def name_words(self) -> set[str]:
        return {_stem(w) for w in words(self.name)}

    @property
    def term_words(self) -> set[str]:
        return {_stem(w) for term in self.search_terms for w in words(term)}

    @property
    def specific_words(self) -> set[str]:
        """Words that point at THIS effect, not merely at its category."""
        shared = _CATEGORY_WORDS.get(self.category or "", frozenset())
        return self.name_words | (self.term_words - shared)

    @property
    def prominence(self) -> tuple[int, float, float, int, str, str]:
        """Tie-break order: library tier, curated rank, upload order, then name.

        ``catalog_rank`` is the seed catalog's headline-first order ("Wrong
        buzzer" before "Wrong buzzer long"); it survives re-uploads, which
        reset ``created_at``. Upload order only breaks ties among unranked rows.
        """
        rank = float(self.catalog_rank) if self.catalog_rank is not None else float("inf")
        created = self.created_at.timestamp() if self.created_at else float("inf")
        return (
            _TIER_RANK.get(self.quality_tier or "", 2),
            rank,
            created,
            len(self.name),
            self.name.casefold(),
            self.id,
        )


def _tr_stem(word: str) -> str | None:
    """The vocabulary stem a (possibly suffixed) folded Turkish word starts with."""
    if word in _TR_VOCAB:
        return word
    for end in range(len(word) - 1, 2, -1):
        if word[:end] in _TR_VOCAB and _TR_SUFFIX.fullmatch(word[end:]):
            return word[:end]
    return None


def _is_turkish(raw: Sequence[str], folded: Sequence[str]) -> bool:
    """A Turkish letter, or an exact (unsuffixed) Turkish word, makes a request Turkish.

    Suffix matching is deliberately NOT a signal here: "golden", "parade" and
    "tiki" would otherwise read as Turkish stems with a case ending.
    """
    if any(_TR_LETTERS & set(w) for w in raw):
        return True
    for w in folded:
        if w in _TR_AMBIGUOUS:
            continue
        if w in _TR_VOCAB or w in _FILLER_TR or w in _REFUSAL_TR:
            return True
    return False


def is_refusal(text: object) -> bool:
    """ "no sound" / "ses olmasın": the creator is declining a sound."""
    raw = _raw_words(text)
    folded = [w.translate(_TR_FOLD) for w in raw]
    if set(folded) & _REFUSAL:
        return True
    return _is_turkish(raw, folded) and bool(set(folded) & _REFUSAL_TR)


def request_words(text: object) -> list[str]:
    """``words`` for a request: Turkish filler dropped, sound words in English."""
    raw = _raw_words(text)
    folded = [w.translate(_TR_FOLD) for w in raw]
    if not _is_turkish(raw, folded):
        return folded
    out: list[str] = []
    i = 0
    while i < len(folded):
        for phrase, english in _TR_PHRASES.items():
            n = len(phrase)
            window = folded[i : i + n]
            if len(window) == n and all((_tr_stem(w) or w) == p for w, p in zip(window, phrase)):
                out.extend(english)
                i += n
                break
        else:
            w = folded[i]
            i += 1
            if w in _FILLER_TR:
                continue
            stem = _tr_stem(w)
            out.extend(_TR_VOCAB[stem] if stem is not None else (w,))
    return out


def content_words(text: object) -> list[str]:
    return [_stem(w) for w in request_words(text) if w not in _FILLER]


def _entry_content_words(text: object) -> list[str]:
    """Library text is English: no Turkish vocabulary applies."""
    return [_stem(w) for w in words(text) if w not in _FILLER]


def match_score(query: object, entry: SfxEntry) -> float:
    """How well a free-text request describes an effect (0 = unrelated)."""
    wanted = set(content_words(query))
    if not wanted:
        return 0.0
    names, terms = entry.name_words, entry.term_words
    score = sum(3.0 if w in names else 1.5 if w in terms else 0.0 for w in wanted)
    joined = " ".join(content_words(query))
    for term in entry.search_terms:
        phrase = " ".join(_stem(w) for w in words(term))
        if " " in phrase and f" {phrase} " in f" {joined} ":
            score += 2.0
    if entry.category and entry.category in wanted:
        score += 1.0
    # The first search term is the effect's primary keyword ("pop" for Soft
    # pop, "slide whistle" for Slide whistle up): a bare "a pop" / "a
    # whistle" should land on the effect that IS that sound.
    if entry.search_terms and " ".join(_entry_content_words(entry.search_terms[0])) == joined:
        score += 1.0
    return score


def rank_for_request(
    entries: Iterable[SfxEntry], query: object, limit: int | None = None
) -> list[SfxEntry]:
    scored = [(match_score(query, e), e) for e in entries]
    ranked = [e for s, e in sorted(scored, key=lambda p: (-p[0], p[1].prominence)) if s > 0]
    return ranked if limit is None else ranked[:limit]


def resolve_described_effect(entries: Iterable[SfxEntry], description: object) -> SfxEntry | None:
    """Best effect for a creator's description ("a buzzer", "wrong answer buzzer").

    Every content word must be covered by the effect's name or search terms,
    and at least one must be specific to that effect rather than to its whole
    category ("right", "good", "text" name no effect). A refusal ("no", "not",
    "without") names nothing. Otherwise return None rather than guess. Ties go
    to the base variant.
    """
    if is_refusal(description):
        return None
    wanted = set(content_words(description))
    if not wanted:
        return None
    covering = [
        e for e in entries if wanted <= (e.name_words | e.term_words) and wanted & e.specific_words
    ]
    ranked = rank_for_request(covering, description, limit=1)
    return ranked[0] if ranked else None


def _is_variant(entry: SfxEntry, siblings: Sequence[SfxEntry]) -> bool:
    """ "Wrong buzzer long" is a variant of "Wrong buzzer"."""
    name = entry.name.casefold()
    return any(name.startswith(s.name.casefold() + " ") for s in siblings if s is not entry)


def _round_robin(entries: Sequence[SfxEntry]) -> list[SfxEntry]:
    """One per category per round; base effects before their variants."""
    groups: dict[str, list[SfxEntry]] = {}
    for entry in sorted(entries, key=lambda e: e.prominence):
        category = entry.category if entry.category in SFX_CATEGORIES else "other"
        groups.setdefault(category, []).append(entry)
    for category, members in groups.items():
        groups[category] = sorted(members, key=lambda e: _is_variant(e, members))
    order = [c for c in (*SFX_CATEGORIES, "other") if c in groups]
    out: list[SfxEntry] = []
    while any(groups[c] for c in order):
        out.extend(groups[c].pop(0) for c in order if groups[c])
    return out


def planner_catalog(rows: Iterable[Any], budget: int) -> list[SfxEntry]:
    """Request-independent, category-diverse slice for the creator manifest.

    It is part of the manifest hash that confirmation re-derives without the
    chat, so it must depend only on the library, never on the request.
    """
    if budget <= 0:
        return []
    return _round_robin([SfxEntry.from_row(r) for r in rows])[:budget]


def placement_catalog(rows: Iterable[Any], limit: int) -> list[SfxEntry]:
    """Voice-free one-shots for the placement agent: role-tagged first, then diverse."""
    usable = [
        e
        for e in (SfxEntry.from_row(r) for r in rows)
        if not e.contains_voice and (e.duration_s is None or e.duration_s <= _ONE_SHOT_MAX_S)
    ]
    tagged = sorted((e for e in usable if e.role_tags), key=lambda e: e.prominence)
    return [*tagged, *_round_robin([e for e in usable if not e.role_tags])][:limit]
