"""The job's own names for its caption words (KRI-1345).

yorum3: the creator typed "Vela" twice, whisper-1 and Gemini both heard "Vila", and the captions
read "vila posted on instagram". The creator-spelling pass (`caption_spelling`) only respells
names of 5+ letters, because a short name fuzzy-matched against every caption word turns
ordinary words into brands. This module adds the names that pass leaves out, each behind
evidence from the take itself:

- **4-letter names the creator typed** ("Vela", "Lumo"; same sentence and "don't" rules as
  ``caption_spelling.creator_request_names``);
- **names from the job's own media**: the text read off its photos and videos (the stored
  understanding record, no new model call), the brand labels of its photos, and its file names
  (camera names such as ``IMG_7212.MOV`` dropped).

A heard word is respelled only when all of these hold:

1. the take writes it as a name: a capital NOT at a sentence start, in whisper's captions or in
   Gemini's own hearing of the take (``references``). "villa" mid-sentence or "Villa" opening a
   sentence is never evidence;
2. it never sits next to another capitalised word that is not one of the job's names ("Aston
   Villa", "Villa Park" are somebody else's name; a Turkish case ending after the next word ends
   the name: "Vila Instagram'da", and a Turkish "from X" adjective before it is not part of it:
   "Ekvadorlu Karo");
3. it sounds like exactly one name: a 4-letter name sounds the same with other letters ("Karo"
   -> "Caro") or differs by one vowel only ("Vila" -> "Vela", never "Bela", "Vilo"); a longer
   name uses the creator-spelling sound rule (``caption_spelling._match_word``);
4. it is not a word the creator typed, a spelling they asked for, or one of the names itself
   (``protected``).

The decision is made once per job, so the same heard word reads the same everywhere, and it is
applied word for word: every cue and word keeps its timing; a Turkish case ending after an
apostrophe stays ("Vila'nın" -> "Vela'nın"). Pure: no I/O, no model call.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.pipeline.caption_spelling import (
    _MIN_TARGET_LEN,
    _SUFFIXES,
    _TOKEN,
    _fold,
    _match_word,
    _reads_as_one_of,
    _recase,
    _request_names,
    _sound,
    _split_ending,
)

SHORT_NAME_LEN = 4
MAX_MEDIA_NAMES = 40
_VOWELS = frozenset("aeiou")
_SENTENCE_END = re.compile(r"[.!?…:][\"'”’)\]]*$")
_LETTERS = re.compile(r"[^\W\d_]+")
_CAMEL = re.compile(r"[A-ZÇĞİÖŞÜ]?[a-zçğıöşü]+|[A-ZÇĞİÖŞÜ]+(?![a-zçğıöşü])")
_ALWAYS_UPPER = frozenset({"i", "i'm", "i've", "i'll", "i'd"})

# Words a photo, sign or file name says that are not names (folded). Media names are
# evidence-gated anyway; this keeps the vocabulary short and readable in the trace.
_NOT_NAMES = frozenset(
    """
    the this that these those with from your yours their there here what when where which while
    about after before again also just only very more most much many some such than then them
    they will would could should have been being were into over under onto upon each every
    other another same like make made new now today tonight tomorrow week weekend year month
    sale sales open opening closed close grand show live free off best great good more less
    follow share comment comments post posts story stories link bio shop store buy order menu
    price prices offer offers deal deals limited edition collection launch coming soon here
    hello thanks thank welcome please episode part video videos photo photos image images
    picture pictures screenshot screen recording record final edit edited export copy untitled
    camera clip clips frame render draft version proxy analysis upload img mov mp4 jpg jpeg png
    heic heif gif webp dng raw whatsapp telegram download downloads file files desktop
    monday tuesday wednesday thursday friday saturday sunday january february march april may
    june july august september october november december
    yeni indirim acik kapali kampanya bugun simdi hemen takip icin daha cok bize sizin bizim
    bir iki uc ben sen biz siz onlar ama gibi kadar sonra once hafta ay yil kasim aralik ocak
    subat mart nisan mayis haziran temmuz agustos eylul ekim pazartesi sali carsamba persembe
    cuma cumartesi pazar bolum yorumla gece sabah aksam
    okay nice fans tree good cool wow yeah
    prompt footage visual visuals sheet manifest script voiceover fallback folder folders readme
    notes take takes test
    """.split()
)


# Ordinary words that often open a sentence (folded, EN + TR): capitalised there, never half of
# a two-word name ("Yesterday Vila replied", "Dün Vila'nın ..." keep "Vila" a candidate).
_SENTENCE_OPENERS = frozenset(
    """
    yesterday today tomorrow later then now so but and also finally first second third next
    last after before when while because since anyway well okay yes no maybe actually honestly
    basically meanwhile still even just here there this that these those it its we you they he
    she our my your their his her what why how who where which if or the a an in on at for with
    dun bugun yarin sonra simdi ama ve de da ayrica sonunda once ilk ikinci ucuncu cunku yani
    peki evet hayir belki aslinda hatta hala bile sadece burada orada bu su o biz siz onlar ben
    sen bizim benim onun ne neden nasil kim nerede hangi gecen gecenlerde iste hem ya
    """.split()
)


@dataclass(frozen=True)
class NameVocabulary:
    """The evidence-gated names of one job: typed 4-letter names first, then media names."""

    short_names: tuple[str, ...] = ()
    media_names: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (*self.short_names, *self.media_names)


def short_request_names(text: str | None) -> tuple[str, ...]:
    """The 4-letter names the creator typed ("Vela", "Lumo"): capitalised, not starting a
    sentence, never typed in a "don't" clause (``caption_spelling.creator_request_names``'
    rules; that function keeps the 5+ letter ones)."""
    return _request_names(
        text, keep=lambda name: len(name) == SHORT_NAME_LEN and _fold(name) not in _NOT_NAMES
    )


def _name_form(token: str) -> str | None:
    """``token`` as a name, or None: letters only, 4+ letters, capitalised; ALL CAPS reads as a
    name in title case ("KERVAN" -> "Kervan"); a lowercase file-name word is title-cased."""
    if not _LETTERS.fullmatch(token) or len(token) < SHORT_NAME_LEN:
        return None
    if _fold(token) in _NOT_NAMES:
        return None
    if token.isupper() and len(token) > 1:
        return token[0] + token[1:].lower().replace("i̇", "i")
    return token


def _text_names(text: str) -> list[str]:
    """Capitalised (or ALL CAPS) words of photo text, their case ending left off."""
    out: list[str] = []
    for found in _TOKEN.finditer(text or ""):
        stem = _split_ending(found.group(0))[0]
        if stem[:1].isupper() and (name := _name_form(stem)):
            out.append(name)
    return out


def _filename_names(filename: str) -> list[str]:
    """Words of an upload's file name ("kervan_billboard.png" -> "Kervan", "Billboard"); camera
    and screenshot names give none. 5+ letters only: a file name says nothing about case."""
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", filename or "")
    out: list[str] = []
    for chunk in _LETTERS.findall(stem):
        for part in _CAMEL.findall(chunk) or [chunk]:
            if len(part) < _MIN_TARGET_LEN or set(part.casefold()) <= set("abcdef"):
                continue
            name = _name_form(part[0].upper() + part[1:] if part.islower() else part)
            if name:
                out.append(name)
    return out


def media_names(
    *,
    on_screen_texts: Iterable[str] = (),
    photo_brands: Iterable[str] = (),
    filenames: Iterable[str] = (),
) -> tuple[str, ...]:
    """The names the job's media spell: photo/video text, photo brand labels, file names. In
    order, unique (folded), at most ``MAX_MEDIA_NAMES``."""
    seen: set[str] = set()
    out: list[str] = []
    candidates = [
        *(n for brand in photo_brands for n in _text_names(str(brand or ""))),
        *(n for text in on_screen_texts for n in _text_names(str(text or ""))),
        *(n for name in filenames for n in _filename_names(str(name or ""))),
    ]
    for name in candidates:
        if _fold(name) not in seen:
            seen.add(_fold(name))
            out.append(name)
    return tuple(out[:MAX_MEDIA_NAMES])


def _row_sources(row: Mapping[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """(on-screen texts, photo brands, file names) of one media row: a snapshot clip assignment
    or a Visuals-pool asset. A video's brand list is never read: for a talking take Gemini can
    list what it HEARD (the very mishearing) as a brand."""
    from app.services.clip_understanding import clip_record  # noqa: PLC0415

    kind = "image" if str(row.get("kind") or "") == "image" else "video"
    analysis = row.get("analysis")
    record = clip_record(analysis if isinstance(analysis, dict) else None, kind=kind)
    texts = [record.on_screen_text or "", str(row.get("user_context") or "")]
    brands = list(record.brands or []) if kind == "image" else []
    return texts, brands, [str(row.get("source_filename") or "")]


def _sounds_alike(a: str, b: str) -> bool:
    return _fold(a) == _fold(b) or _matches(a, b) or _matches(b, a)


def name_vocabulary(
    request: str | None,
    media_rows: Iterable[Mapping[str, Any]] = (),
    *,
    typed_names: Iterable[str] = (),
) -> NameVocabulary:
    """The job's evidence-gated names. A media name that sounds like a name the creator typed
    (``typed_names`` + the 4-letter names here) is dropped: the creator's spelling wins over a
    photo's or an OCR misread."""
    short = short_request_names(request)
    texts: list[str] = []
    brands: list[str] = []
    files: list[str] = []
    for row in media_rows:
        if not isinstance(row, Mapping):
            continue
        try:
            t, b, f = _row_sources(row)
        except Exception:  # noqa: BLE001 - an unreadable record adds no names
            continue
        texts += t
        brands += b
        files += f
    typed = [*typed_names, *short]
    found = media_names(on_screen_texts=texts, photo_brands=brands, filenames=files)
    kept = tuple(name for name in found if not any(_sounds_alike(name, t) for t in typed))
    return NameVocabulary(short_names=short, media_names=kept)


# --- matching ------------------------------------------------------------------------------


def _short_sounds_like(stem: str, name: str) -> bool:
    """A 4-letter ``name`` heard with one vowel changed ("Vila" for "Vela")."""
    got, want = _sound(stem), _sound(name)
    if len(want) != SHORT_NAME_LEN or len(got) != SHORT_NAME_LEN or _fold(stem) == _fold(name):
        return False
    if got == want:
        return True  # the same sounds, other letters ("Karo" for "Caro")
    diffs = [(g, w) for g, w in zip(got, want, strict=True) if g != w]
    return got[0] == want[0] and len(diffs) == 1 and all(c in _VOWELS for c in diffs[0])


def _matches(heard: str, name: str) -> bool:
    letters = len(_fold(name))
    if letters == SHORT_NAME_LEN:
        return _short_sounds_like(_split_ending(heard)[0], name)
    return letters >= _MIN_TARGET_LEN and _match_word(heard, name) is not None


def _adjective(key: str) -> bool:
    """A Turkish "from X" adjective ("Ekvadorlu", "İstanbullu", folded): capitalised, yet the
    name after it is its own ("Ekvadorlu Karo")."""
    return len(key) > SHORT_NAME_LEN and key.endswith(("li", "lu"))


@dataclass
class _Heard:
    evidence: set[str]
    blocked: set[str]
    forms: dict[str, str]  # folded heard word -> one heard spelling of it


@dataclass(frozen=True)
class _Word:
    key: str  # folded, case ending off
    text: str
    capital: bool  # written with a capital, wherever it stands
    as_name: bool  # a capital NOT at a sentence start: the take writes it as a name
    ending: bool  # a case ending after an apostrophe ("Instagram'da")
    joined_after: bool  # only a space before the next word


def _words(text: str) -> list[_Word]:
    """One hearing word by word, with where each stands in its sentence."""
    out: list[_Word] = []
    sentence_start = True
    for chunk in re.finditer(r"\S+", text):
        token = chunk.group(0)
        word = _TOKEN.search(token)
        if word is None:
            sentence_start = sentence_start or bool(_SENTENCE_END.search(token))
            if out:
                out[-1] = dataclasses.replace(out[-1], joined_after=False)
            continue
        letters = word.group(0)
        stem, ending = _split_ending(letters)
        capital = letters[:1].isupper() and _fold(letters) not in _ALWAYS_UPPER
        out.append(
            _Word(
                key=_fold(stem),
                text=letters,
                capital=capital,
                as_name=capital and not sentence_start,
                ending=bool(ending),
                joined_after=word.end() == len(token),
            )
        )
        if word.start() != 0 and len(out) > 1:
            out[-2] = dataclasses.replace(out[-2], joined_after=False)
        sentence_start = bool(_SENTENCE_END.search(token))
    return out


def _read_hearings(
    hearings: Sequence[Sequence[_Word]], known: frozenset[str], heard: _Heard
) -> None:
    """Which heard words the take writes as names (a capital not at a sentence start) and which
    are ever part of another capitalised name, in ANY position (Emir's review: "Mila Kunis
    starred. Later Mila returned." -- "Mila" opening the sentence is still half of "Mila Kunis",
    so neither "Mila" may become "Mela").

    A capitalised word at a sentence start says nothing by its capital, so as the left half of a
    pair it counts as a name unless the hearings also write it in lower case or it is a common
    word ("Then Vila posted" keeps "Vila"); "Aston Villa scored" blocks "Villa"."""
    lower = {w.key for words in hearings for w in words if not w.capital}

    def name_like(w: _Word) -> bool:
        ordinary = w.key in lower or w.key in _NOT_NAMES or w.key in _SENTENCE_OPENERS
        return w.as_name or (w.capital and not ordinary)

    for words in hearings:
        for w in words:
            heard.forms.setdefault(w.key, w.text)
            if w.as_name:
                heard.evidence.add(w.key)
        for a, b in zip(words, words[1:], strict=False):
            if not (a.joined_after and a.capital and b.capital):
                continue
            # The word after a name joins it unless a case ending closes it ("Vila Instagram'da").
            if name_like(b) and b.key not in known and not b.ending:
                heard.blocked.add(a.key)
            # The word before it joins it unless it is a Turkish "from X" adjective.
            if name_like(a) and a.key not in known and not _adjective(a.key):
                heard.blocked.add(b.key)


def plan_respellings(
    texts: Sequence[str],
    names: Sequence[str],
    *,
    references: Sequence[str] = (),
    protected: Iterable[str] = (),
) -> dict[str, str]:
    """Folded heard word -> the name it is respelled to, for the whole job."""
    names = [n for n in names if len(_fold(n)) >= SHORT_NAME_LEN]
    if not names:
        return {}
    keep = frozenset({_fold(w) for w in protected} | {_fold(n) for n in names})
    heard = _Heard(evidence=set(), blocked=set(), forms={})
    hearings = [_words(" ".join(texts)), *(_words(r or "") for r in references)]
    _read_hearings(hearings, keep, heard)
    plan: dict[str, str] = {}
    for key in sorted(heard.evidence - heard.blocked):
        form = heard.forms[key]
        if _reads_as_one_of(form, keep):
            continue
        hits = {_fold(n): n for n in names if _matches(form, n)}
        if len(hits) == 1:
            plan[key] = next(iter(hits.values()))
    return plan


def _planned_name(stem: str, plan: Mapping[str, str]) -> str | None:
    """The name planned for ``stem``, or for it without a glued Turkish case ending when that
    name has 5+ letters ("Curvanın" -> the plan for "Curvan")."""
    low = _fold(stem)
    if low in plan:
        return plan[low]
    for cut in range(len(low) - 1, _MIN_TARGET_LEN - 1, -1):
        name = plan.get(low[:cut])
        if name is not None and low[cut:] in _SUFFIXES and len(_fold(name)) > SHORT_NAME_LEN:
            return name
    return None


def _respell_token(found: re.Match[str], plan: Mapping[str, str]) -> str:
    token = found.group(0)
    stem, ending = _split_ending(token)
    name = _planned_name(stem, plan)
    if name is None:
        return token
    if len(_fold(name)) == SHORT_NAME_LEN or ending:
        return _recase(token[0], name) + ending
    return _match_word(token, name) or token


def _respell(text: str, plan: Mapping[str, str]) -> str:
    return _TOKEN.sub(lambda found: _respell_token(found, plan), text) if text else text


def apply_name_vocabulary(
    cues: Iterable[dict[str, Any]],
    names: Sequence[str],
    *,
    references: Sequence[str] = (),
    protected: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """``cues`` with heard words respelled as the job's ``names`` (see the module rules), and
    the plan applied (folded heard word -> name). Word for word: no cue or word timing changes;
    a touched cue is a copy, an untouched cue is the same object."""
    cues = list(cues)
    plan = plan_respellings(
        [str(cue.get("text") or "") for cue in cues],
        names,
        references=references,
        protected=protected,
    )
    if not plan:
        return cues, {}
    out: list[dict[str, Any]] = []
    for cue in cues:
        text = _respell(str(cue.get("text") or ""), plan)
        words = cue.get("words")
        new_words = (
            [{**w, "text": _respell(str(w.get("text") or ""), plan)} for w in words]
            if isinstance(words, list) and all(isinstance(w, dict) for w in words)
            else words
        )
        if text == cue.get("text") and new_words == words:
            out.append(cue)
            continue
        changed = {**cue, "text": text}
        if new_words is not None:
            changed["words"] = new_words
        out.append(changed)
    return out, plan
