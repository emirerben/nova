"""Opening titles built from what the creator said, offered for her approval (KRI-1560).

When a creator asks for a title, or asks Kria to find one, she gets up to three to compare in
ONE question. Each is built on a sentence she said and shows it: "0:12 · “Everyone says black
goes with everything.”". She taps one, edits one (her edit is her own words) or says no title.
Nothing is written on the video before she taps (`creative_copy_decisions` folds the tap into
the approved wording; `creative_copy_gate` refuses a draft without it).

Decision (agents/DECISIONS.md, 2026-11-22): "Gemini metadata never becomes on-screen text"
still holds for text applied without the creator's approval. A candidate shown here may quote
or paraphrase her own speech; it carries the time of the line it rests on and becomes her text
only when she taps it.

How this can go wrong, and what stops it:

* the model invents a fact: every number and every name in a title must be in the sentences it
  cites (`check_title`), else the title is dropped. A title that shares no content word with
  its sentences is dropped too, so a line about something else cannot borrow a real timestamp;
* a bare invented name opens the title ("Paris changed my plans" over a line that never says
  Paris), where sentence case capitalises every first word: that word must be a small word, a
  word of the cited line, or a word she says in lower case somewhere in her talk
  (`_invents_a_name`);
* German capitalises every noun: there a capital marks a name only at the start of the title
  or after a preposition or conjunction ("in Berlin"), so "Pläne" for "geplant" is not a name;
* the cited sentence does not exist, or the stretch is long: dropped (``bad_span``);
* a title too long for two seconds, with quotation marks, in the other language, or bait
  ("wait for it", prompt-injection residue from the transcript): dropped;
* two titles from the same moment, or the same words twice: the later one is dropped;
* the model fails, times out or answers nothing usable: no candidates; the caller asks the
  older "do you have an idea, or should I write one?" question, never an empty card;
* her opening already hooks: the model says so, "No title" is recommended and at most two
  titles follow;
* a talk too long for one request: the first `MAX_SENTENCES` sentences are read.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.agents.opening_title import (
    MAX_CANDIDATES,
    MAX_SENTENCES,
    MAX_TITLE_WORDS,
    OpeningTitleAgent,
    OpeningTitleInput,
    OpeningTitleOutput,
    SpokenSentence,
    TitleIdea,
)
from app.kria.reply_language import say
from app.services.creative_copy_decisions import (
    CANDIDATE_KEY_PREFIX,
    CREATIVE_COPY_CONFLICT,
    KIND_CANDIDATES,
    OPT_CANCEL,
    SKIP_ALIASES,
    Target,
    normalize_copy,
)
from app.text_fold import loose_words

log = structlog.get_logger()

# The longest stretch of her talk one title may rest on.
MAX_SPAN_SENTENCES = 3
_QUOTE_SHOWN_CHARS = 110

# Words too common to tie a title to a sentence ("the", "bir"): the anchor rule needs one that
# is not. Folded like `loose_words` (accent-free, lower case).
_FILLER = frozenset(
    (
        "the a an and or but of to in on at for with from by is are was were be been this that "
        "these those it its my your our their his her me you we they i not no do does did have "
        "has had just about into over than then so very what when how why who which there here "
        "bir ve ile bu su o da de ki mi mu ne cok daha ama icin gibi kadar sonra once ben sen "
        "biz siz onlar benim senin bunu sunu onu var yok degil her hic en"
    ).split()
)
# German small words a title may open with, and the words after which a capital is a name
# rather than a noun ("in Berlin", "mit Zara"); both folded like `loose_words`.
_GERMAN_SMALL = frozenset(
    (
        "der die das den dem des ein eine einen einem einer eines kein keine mein meine meinen "
        "meinem meiner dein deine sein seine ihr ihre unser unsere euer eure dieser diese dieses "
        "jeder jede jedes alle viele ich du er sie es wir mich mir dich dir uns und oder aber "
        "nicht ist war sind hat habe hatte wie was wer warum wann wo zu zum zur im am vom ins "
        "beim so noch nur auch schon dann mit fur uber von bei aus nach in an auf als dass"
    ).split()
)
_GERMAN_BEFORE_NAME = frozenset(
    "in nach aus mit von bei uber fur gegen ohne um durch bis seit ab und oder wie als".split()
)
_TERMINAL = re.compile(r"[.!?…:]$")
_NAME_SUFFIX = re.compile(r"\w['’]\w")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_QUOTE_CHARS = frozenset('"“”„‟«»‹›`')


@dataclass(frozen=True)
class TalkSentence:
    """One sentence she said: its number in the talk, the clip it is in and its time there
    (None when the clip has a transcript but no timed sentences)."""

    n: int
    media_id: str
    clip_ordinal: int
    start_s: float | None
    end_s: float | None
    text: str
    # The language whisper heard in that clip ("tr"), "" when unknown.
    language: str = ""


@dataclass(frozen=True)
class TitleCandidate:
    text: str
    first: int
    last: int
    why: str
    evidence: tuple[TalkSentence, ...]


@dataclass(frozen=True)
class OpeningTitles:
    candidates: tuple[TitleCandidate, ...]
    opening_already_strong: bool
    first_sentence: TalkSentence | None


# ── Her talk ──────────────────────────────────────────────────────────────────────────


def talk_sentences(records: Iterable[tuple[str, Any]]) -> list[TalkSentence]:
    """Her sentences, in clip order then time, from ``(media_id, clip record)`` pairs.

    A record is a ``ClipUnderstanding`` or its dict dump. ``speech.segments`` (the verbatim
    whisper sentences, seconds in the source clip) are read first; a clip with a transcript but
    no timed sentences adds its transcript split at sentence ends, without times. Clips without
    speech add nothing.
    """
    out: list[TalkSentence] = []
    ordinal = 0
    for media_id, record in records:
        speech = _field(record, "speech")
        if speech is None or not _field(speech, "has_speech", True):
            continue
        rows: list[tuple[float | None, float | None, str]] = []
        for segment in _field(speech, "segments", None) or []:
            text = " ".join(str(_field(segment, "text", "") or "").split())
            start, end = _field(segment, "start_s", None), _field(segment, "end_s", None)
            if text and isinstance(start, int | float) and isinstance(end, int | float):
                rows.append((float(start), float(end), text))
        rows.sort(key=lambda row: row[0] or 0.0)
        if not rows:
            transcript = " ".join(str(_field(speech, "transcript", "") or "").split())
            rows = [(None, None, part) for part in _SENTENCE_END.split(transcript) if part.strip()]
        if not rows:
            continue
        ordinal += 1
        spoken = str(_field(speech, "language", "") or "").lower().split("-")[0][:8]
        for start, end, text in rows:
            if len(out) >= MAX_SENTENCES:
                return out
            out.append(
                TalkSentence(len(out) + 1, str(media_id), ordinal, start, end, text[:300], spoken)
            )
    return out


def item_talk_sentences(item: Any) -> list[TalkSentence]:
    """`talk_sentences` of a plan item's video clips, in the order the creator added them."""
    from app.services.clip_understanding import clip_record  # noqa: PLC0415

    pairs = [
        (row["media_id"], clip_record(row.get("analysis"), kind="video"))
        for row in getattr(item, "clip_assignments", None) or []
        if isinstance(row, Mapping)
        and isinstance(row.get("media_id"), str)
        and row.get("kind") != "image"
    ]
    return talk_sentences(pairs)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


# ── The check ─────────────────────────────────────────────────────────────────────────


def check_title(
    idea: TitleIdea, sentences: Sequence[TalkSentence], *, language: str
) -> tuple[TitleCandidate | None, str | None]:
    """``(candidate, None)`` when ``idea`` may be offered, else ``(None, reason)``."""
    text = normalize_copy(idea.text).strip(" -–—·")
    if not text:
        return None, "empty"
    if len(text.split()) > MAX_TITLE_WORDS:
        return None, "words"
    if _has_quote(text):
        return None, "quote"
    count = len(sentences)
    if not (1 <= idea.first <= idea.last <= count) or idea.last - idea.first >= MAX_SPAN_SENTENCES:
        return None, "bad_span"
    evidence = tuple(sentences[idea.first - 1 : idea.last])
    said = {word for sentence in evidence for word in loose_words(sentence.text)}
    mine = loose_words(text)
    if any(any(ch.isdigit() for ch in word) and word not in said for word in mine):
        return None, "number_not_said"
    if not _anchored(mine, said):
        return None, "not_about_that_line"
    if _invents_a_name(text, said, sentences, language=language):
        return None, "name_not_said"
    from app.services.suggestions.kinds.hook import is_bait, wrong_language  # noqa: PLC0415

    if is_bait(text):
        return None, "bait"
    if wrong_language(text, language):
        return None, "language"
    why = normalize_copy(idea.why)[:160]
    return TitleCandidate(text, idea.first, idea.last, why, evidence), None


def _has_quote(text: str) -> bool:
    return any(ch in _QUOTE_CHARS for ch in text)


def _invents_a_name(
    text: str, said: set[str], talk: Sequence[TalkSentence], *, language: str
) -> bool:
    """The title writes a name its cited sentences (``said``) do not say.

    Titles are asked for in sentence case, so a capital past the first word is a name ("Paris",
    "Zara"), and so is any word marked as one: an apostrophe suffix ("Paris'te", "Zara's") or all
    capitals ("NASA"); such a name must be a word of the cited sentences. The first word (and the
    first after a colon or full stop) is capitalised anyway, so it passes only as a small word, a
    word of the cited sentences in any form (same 4-letter stem: "Burnt" for "burned"), or a word
    she says in lower case somewhere in her talk; else it is taken for an invented name. German
    capitalises every noun, so past the first word a capital is a name only after a preposition
    or conjunction ("in Berlin"). English "I" is not a name.
    """
    german = language.lower().startswith("de")
    small = _GERMAN_SMALL if german else _FILLER
    stems = {word[:4] for word in said if len(word) >= 3}
    lower_case: set[str] | None = None
    previous = ""
    starts_sentence = True
    for raw in text.split():
        core = raw.strip("([{-–—,;")
        folded = loose_words(core)
        word = folded[0] if folded else ""
        if word and core[0].isupper() and core not in {"I", "I'm", "I’m"}:
            letters = [ch for ch in core if ch.isalpha()]
            marked = bool(_NAME_SUFFIX.search(core)) or (
                len(letters) >= 2 and all(ch.isupper() for ch in letters)
            )
            if marked or (not starts_sentence and (not german or previous in _GERMAN_BEFORE_NAME)):
                if word not in said:
                    return True
            elif starts_sentence and word not in small and word not in said:
                if len(word) < 3 or word[:4] not in stems:
                    if lower_case is None:
                        lower_case = _lower_case_words(talk)
                    if word not in lower_case:
                        return True
        previous = folded[-1] if folded else ""
        starts_sentence = bool(_TERMINAL.search(raw))
    return False


def _lower_case_words(talk: Sequence[TalkSentence]) -> set[str]:
    """The folded words she says written in lower case: ordinary words, never names."""
    words: set[str] = set()
    for sentence in talk:
        for raw in sentence.text.split():
            core = raw.strip("([{-–—,;.!?…:\"'“”„«»")
            if core and core[0].islower():
                words.update(loose_words(core)[:1])
    return words


def _anchored(mine: Sequence[str], said: set[str]) -> bool:
    """At least one content word of the title is a word of its sentences (same 4-letter stem,
    so "renkler" meets "renk" and "arguing" meets "argued")."""
    stems = {word[:4] for word in said if len(word) >= 3}
    content = [word for word in mine if word not in _FILLER and len(word) >= 3]
    return any(word[:4] in stems for word in content)


# ── Asking the model ──────────────────────────────────────────────────────────────────


def find_opening_titles(
    sentences: Sequence[TalkSentence],
    *,
    language: str,
    request: str = "",
    client: Any = None,
    job_id: str | None = None,
) -> OpeningTitles:
    """Up to three checked titles from her talk; none when the model fails or nothing holds.

    Sync (one model call); an async caller runs it in a thread. Never raises: a provider error,
    a timeout or an unknown outcome all mean "no candidates", and the caller asks the older
    question. The call is not retried here, so a title ask costs at most one paid call.
    """
    first = sentences[0] if sentences else None
    if not sentences:
        return OpeningTitles((), False, None)
    try:
        request_in = OpeningTitleInput(
            sentences=[SpokenSentence(n=s.n, text=s.text) for s in sentences],
            language=(language or "")[:8],
            request=(request or "")[:600],
        )
    except ValueError as exc:
        log.warning("opening_title_input_invalid", error_type=type(exc).__name__)
        return OpeningTitles((), False, first)
    try:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RunContext  # noqa: PLC0415

        ctx = RunContext(job_id=job_id) if job_id else None
        output: OpeningTitleOutput = OpeningTitleAgent(client or default_client()).run(
            request_in, ctx=ctx
        )
    except Exception as exc:  # noqa: BLE001 - any failure means "ask the older question"
        log.warning("opening_title_failed", error_type=type(exc).__name__)
        return OpeningTitles((), False, first)
    return checked_titles(output, sentences, language=language)


def checked_titles(
    output: OpeningTitleOutput, sentences: Sequence[TalkSentence], *, language: str
) -> OpeningTitles:
    """The model's answer through `check_title`, de-duplicated, capped."""
    strong = output.opening_already_strong
    cap = 2 if strong else MAX_CANDIDATES
    kept: list[TitleCandidate] = []
    seen_text: set[str] = set()
    seen_span: set[tuple[int, int]] = set()
    for idea in output.candidates:
        candidate, reason = check_title(idea, sentences, language=language)
        if candidate is None:
            log.info("opening_title_dropped", reason=reason)
            continue
        folded = " ".join(loose_words(candidate.text))
        span = (candidate.first, candidate.last)
        if folded in seen_text or span in seen_span:
            log.info("opening_title_dropped", reason="duplicate")
            continue
        seen_text.add(folded)
        seen_span.add(span)
        kept.append(candidate)
        if len(kept) == cap:
            break
    return OpeningTitles(tuple(kept), strong, sentences[0] if sentences else None)


# ── The question ──────────────────────────────────────────────────────────────────────


def candidate_digest(text: str) -> str:
    return hashlib.sha256(normalize_copy(text).encode()).hexdigest()[:24]


def _clock(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def _quote(evidence: Sequence[TalkSentence]) -> str:
    text = " ".join(sentence.text for sentence in evidence)
    if len(text) > _QUOTE_SHOWN_CHARS:
        text = text[: _QUOTE_SHOWN_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return text


def where_said(evidence: Sequence[TalkSentence], *, clips: int) -> str:
    """The line under a title: "0:12 · “...”", with the clip when her talk has several (and
    only the quote when the clip has no timed sentences)."""
    first = evidence[0]
    parts: list[str] = []
    if clips > 1:
        parts.append(say(en="Clip {n}", tr="{n}. klip", n=first.clip_ordinal))
    if first.start_s is not None:
        parts.append(_clock(first.start_s))
    parts.append(f"“{_quote(evidence)}”")
    return " · ".join(parts)


_NO_TITLE_LABELS = {"en": "No title", "tr": "Başlık olmasın", "de": "Kein Titel"}


def no_title_label(language: str | None) -> str:
    code = (language or "en").lower().split("-")[0]
    return _NO_TITLE_LABELS.get(code, _NO_TITLE_LABELS["en"])


def candidates_question(
    titles: OpeningTitles,
    *,
    target: Target = "opening_title",
    dependency_digest: str,
    clips: int,
    language: str | None,
) -> dict[str, Any] | None:
    """The one question that offers ``titles``; None when there is nothing to offer.

    Each title is an option (label = its words, description = where she said it). "No title"
    is the last option, and the recommended one when her opening already hooks. Typing stays
    open ("Type my own"); typed words are her own and need no approval.
    """
    if not titles.candidates:
        return None
    candidates = []
    options = []
    for index, candidate in enumerate(titles.candidates, start=1):
        key = f"{CANDIDATE_KEY_PREFIX}{index}"
        first, last = candidate.evidence[0], candidate.evidence[-1]
        candidates.append(
            {
                "key": key,
                "text": candidate.text,
                "digest": candidate_digest(candidate.text),
                "why": candidate.why,
                "evidence": {
                    "media_id": first.media_id,
                    "start_s": None if first.start_s is None else round(first.start_s, 2),
                    "end_s": None if last.end_s is None else round(last.end_s, 2),
                    "sentences": [candidate.first, candidate.last],
                    "quote": " ".join(sentence.text for sentence in candidate.evidence),
                },
            }
        )
        options.append(
            {
                "key": key,
                "label": candidate.text,
                "description": where_said(candidate.evidence, clips=clips),
                "recommended": index == 1 and not titles.opening_already_strong,
                # The app draws a pencil that puts these words in the composer, quoted, so
                # what she sends back is her own wording (`creator_quoted`).
                "editable": True,
            }
        )
    options.append(
        {
            "key": OPT_CANCEL,
            "label": no_title_label(language),
            "recommended": titles.opening_already_strong,
            "aliases": list(SKIP_ALIASES),
        }
    )
    return {
        "version": 1,
        "question_id": str(uuid.uuid4()),
        "conflict": CREATIVE_COPY_CONFLICT,
        "kind": KIND_CANDIDATES,
        "creative_target": target,
        "dependency_digest": dependency_digest,
        "allow_free_text": True,
        "opening_already_strong": titles.opening_already_strong,
        "candidates": candidates,
        "options": options,
    }


def reasked(question: Mapping[str, Any]) -> dict[str, Any]:
    """The same titles asked again under a new id (no second model call)."""
    return {**dict(question), "question_id": str(uuid.uuid4())}


def candidates_message(titles: OpeningTitles) -> str:
    count = len(titles.candidates)
    if titles.opening_already_strong and titles.first_sentence is not None:
        return say(
            en=(
                "Your first sentence already pulls people in: “{line}”. I'd keep it without a "
                "title, or you can pick one of these."
            ),
            tr=(
                "İlk cümlen zaten izleyiciyi içine çekiyor: “{line}”. Başlıksız bırakmanı "
                "öneririm, istersen bunlardan birini seç."
            ),
            line=_quote([titles.first_sentence]),
        )
    if count == 1:
        return say(
            en="I found one opening in what you said. Use it, or type your own.",
            tr="Söylediklerinde bir açılış buldum. Bunu kullan ya da kendin yaz.",
        )
    return say(
        en=(
            "Here are {count} openings from what you said. Pick one and I'll open the video "
            "with it."
        ),
        tr="Söylediklerinden {count} açılış çıkardım. Birini seç, videoyu onunla açayım.",
        count=count,
    )


__all__ = [
    "CANDIDATE_KEY_PREFIX",
    "KIND_CANDIDATES",
    "OpeningTitles",
    "TalkSentence",
    "TitleCandidate",
    "candidate_digest",
    "candidates_message",
    "candidates_question",
    "check_title",
    "checked_titles",
    "find_opening_titles",
    "item_talk_sentences",
    "no_title_label",
    "reasked",
    "talk_sentences",
    "where_said",
]
