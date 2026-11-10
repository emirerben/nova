"""The language Kria replies in on one chat (KRI-520).

A creator who writes Turkish gets Turkish back: the model-written text (Main
Creator, edit copilot) and the copy the server writes itself (status answers,
receipts, review, refusals, questions). English is the default and stays
byte-identical.

How a chat's language is decided, newest evidence first:

1. The creator's own typed message (``detect_chat_language``). Stock sentences the
   apps send on the creator's behalf ("Suggest an edit.", "Use this order: …") are
   ignored, so a tap never flips a Turkish chat to English.
2. The chat's language so far (``CreationThread.state["reply_language"]``).
   Switching away from it needs clear evidence, so a stray English phrase in a
   Turkish chat does not flip it.
3. The device or browser language (``Accept-Language``), for a first message that
   says too little to tell ("tamam", a tap, media only).

Only ``SUPPORTED_REPLY_LANGUAGES`` are ever returned. Anything else is ``None``:
the server copy stays English and the models keep mirroring the creator.

Server copy is picked with ``say(en=..., tr=...)`` against the language bound for
the current turn by ``reply_language_for``. Unbound (or the kill switch
``KRIA_REPLY_LANGUAGE_ENABLED=false``) means English.
"""

from __future__ import annotations

import contextvars
import re
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from app.kria.brief_route import fold_text, loose_text

SUPPORTED_REPLY_LANGUAGES: frozenset[str] = frozenset({"en", "tr"})

# Prompt names for the languages a model is told to write in.
LANGUAGE_NAMES: Mapping[str, str] = {"en": "English", "tr": "Turkish"}

# The chat's language lives on the thread row, next to the rest of its state.
STATE_KEY = "reply_language"
# Clear words a message needs to move an established chat to the other language.
_SWITCH_STRENGTH = 3
# Most words a "Title:"-style label before a colon has (KRI-1363).
_LABEL_MAX_WORDS = 4

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
# Letters only Turkish uses among the languages Kria creators write in.
_STRONG_TR_LETTERS = re.compile(r"[ğşıİĞŞ]")
# Turkish letters that German, French or Portuguese share; they count only when
# the message has no letters Turkish never uses.
_SOFT_TR_LETTERS = re.compile(r"[çöüÇÖÜ]")
_NON_TR_LETTERS = re.compile(r"[äßéèàñãõêëïôœæåøáíóúÄÉÈÀÑÃÕÊÁÍÓÚ]")

# Loose-folded (ASCII) Turkish words common in edit chats, so Turkish typed on an
# English keyboard ("baslik ekle", "hayir") still counts. Words other Latin-script
# languages also use as plain words ("de", "mi", "o", "ne", "en") are left out.
_TR_WORDS = frozenset(
    """
    ve bir bu su cok ama icin ben sen biz siz degil gibi daha sey var yok olarak ile
    diye bence yani evet hayir nasil neden niye tamam olsun olmasin olmaz olur yap
    yapar yapalim yapin yapabilir yapma misin musun lutfen simdi sonra once kadar
    bunu sunu onu bunlari hepsi hepsini tum butun sadece biraz baslik basligi yazi
    yazilar metin altyazi altyazilar muzik muzigi sarki klip klibi klipler klipleri
    videoyu videom videoda videoya ekle kaldir sil cikar degistir kisalt uzat kisa
    uzun hizli yavas guzel iyi kotu bitti hazir durum yardim tesekkurler sagol
    merhaba selam istiyorum istemiyorum baska yeni eski ilk son basa sona sirayla
    sira saniye saniyelik dakika gun gece aksam sabah ust alt orta buyuk kucuk renk
    rengi tekrar yeniden sec secim sana seni bana beni neler hangi nerede burada devam
    """.split()
)
_EN_WORDS = frozenset(
    """
    the and is are was were you your to of it its that this these those in on for
    with my we our i im what so just have has be not but they do does dont can could
    would should please make add remove delete change put use more less longer
    shorter faster slower bigger smaller title text music song clip clips caption
    captions subtitles yes thanks thank hi hello want like let lets start end first
    last an all only about from at how why where when which there here now again
    too very great good nice cut edit done ready help status keep give show move
    """.split()
)

# Sentences the iOS/web apps send as the creator's message for a tap or an
# attachment-only send. They are English on the wire (some are server commands),
# so they say nothing about the language the creator writes in.
_CLIENT_STOCK_MESSAGES = frozenset(
    {
        "suggest an edit",
        "retry preparing my clips",
        "keep the same plan with my current footage",
        "try generating this edit again",
        "skip decide for me",
        "try a stronger opening",
        "make it warmer",
        "let s make a montage",
        "lets make a montage",
        "use this order",
    }
)
_CLIENT_STOCK_PREFIXES = ("use this order clips", "none of these for ")
_CLIENT_STOCK_SHAPES = (
    # ClipSelection answers: "<label>: clips 3, 7".
    re.compile(r"^.{1,80} clips?(?: \d+)+$"),
    # RequirementChips correction stub sent unfinished: "clip 4 isn't x it's".
    re.compile(r"^clip \d+ isn t .{1,80} it s$"),
)

_CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kria_reply_language", default=None
)


def _stock_key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", fold_text(text)))


def _is_stock_piece(key: str) -> bool:
    if key in _CLIENT_STOCK_MESSAGES or key.startswith(_CLIENT_STOCK_PREFIXES):
        return True
    return any(shape.match(key) for shape in _CLIENT_STOCK_SHAPES)


def is_client_stock_message(text: str | None) -> bool:
    """True for a sentence an app sends on the creator's behalf, not one they typed.

    The iOS clip picker joins one answer per category with ". " ("Kapadokya: clips 3, 7.
    None of these for balon"), so a message counts when every piece is stock.
    """
    pieces = [_stock_key(piece) for piece in re.split(r"\.\s+|\n+", text or "")]
    pieces = [piece for piece in pieces if piece]
    return bool(pieces) and all(_is_stock_piece(piece) for piece in pieces)


# KRI-1363: marks that open and close text meant for the video. A straight or single mark is
# its own closer; an apostrophe inside a word ("Kadıköy'de", "don't", "İstanbul’da") is neither.
_QUOTE_CLOSERS = {'"': '"', "“": "”", "„": "“", "«": "»", "‹": "›", "'": "'", "‘": "’"}
_APOSTROPHES = frozenset({"'", "’"})


def own_words_enabled() -> bool:
    from app.config import settings  # noqa: PLC0415

    return enabled() and bool(getattr(settings, "kria_reply_language_own_words_enabled", True))


def _closing_quote(text: str, start: int, closer: str) -> int:
    """Index of the mark closing a quote opened just before ``start``, or -1."""
    for index in range(start, len(text)):
        if text[index] != closer:
            continue
        after = text[index + 1] if index + 1 < len(text) else ""
        if closer in _APOSTROPHES and after.isalnum():
            continue  # an apostrophe inside a quoted word, not the end of the quote
        return index
    return -1


def _without_quotes(text: str) -> str:
    kept: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        closer = _QUOTE_CLOSERS.get(char)
        before = text[index - 1] if index else ""
        if closer is None or (char in _APOSTROPHES and before.isalnum()):
            kept.append(char)
            index += 1
            continue
        end = _closing_quote(text, index + 1, closer)
        if end < 0:  # never closed: not a quote, the creator's own words
            kept.append(char)
            index += 1
            continue
        kept.append(" ")
        index = end + 1
    return "".join(kept)


def _own_words(text: str) -> str:
    """The words the creator says to Kria: ``text`` without the quoted text for the video.

    Quoted text is what the video should show, in whatever language the video is in
    (KRI-1363). No word is read to decide this. Text after a colon is NOT dropped: creators
    open a whole request with "Episode 9: ..." (Green 4).
    """
    return _without_quotes(unicodedata.normalize("NFC", text))


def _scores(text: str) -> tuple[int, int, int]:
    """(Turkish evidence, English evidence, word count) for one message."""
    normalized = unicodedata.normalize("NFC", text)
    words = _WORD.findall(normalized)
    foreign = bool(_NON_TR_LETTERS.search(normalized))
    tr = en = 0
    for word in words:
        if _STRONG_TR_LETTERS.search(word):
            tr += 1
        elif _SOFT_TR_LETTERS.search(word) and not foreign:
            tr += 1
        elif loose_text(word) in _TR_WORDS:
            tr += 1
        elif fold_text(word) in _EN_WORDS:
            en += 1
    return tr, en, len(words)


def _detect(text: str | None) -> tuple[str | None, int]:
    """(language, strength) for one message; (None, 0) when it doesn't clearly say."""
    if not text or is_client_stock_message(text):
        return None, 0
    return _language_of(*_scores(_own_words(text) if own_words_enabled() else text))


def _language_of(tr: int, en: int, count: int) -> tuple[str | None, int]:
    """(language, strength) for ``_scores`` output; (None, 0) when it doesn't clearly say."""
    if count == 0:
        return None, 0
    if tr and tr >= 2 * en and tr / count >= 0.2:
        return "tr", tr
    if en and en >= 2 * tr and en / count >= 0.25:
        return "en", en
    return None, 0


def detect_chat_language(text: str | None) -> str | None:
    """ "tr"/"en" when one creator message clearly reads as that language, else None."""
    return _detect(text)[0]


def locale_language(header: str | None) -> str | None:
    """The first supported primary language in an ``Accept-Language`` value.

    Only the creator's top preference counts: "de-DE,tr;q=0.8" is a German device,
    and Kria has no German copy, so this returns None rather than Turkish.
    """
    for part in (header or "").split(","):
        tag = part.split(";", 1)[0].strip().lower()
        if not tag or tag == "*":
            continue
        primary = tag.replace("_", "-").split("-", 1)[0]
        return primary if primary in SUPPORTED_REPLY_LANGUAGES else None
    return None


def _labels_video_text(language: str, detected: str, message: str | None) -> bool:
    """True for "<short label in ``language``>: <text in ``detected``>" (KRI-1363).

    "Title: Bugün İstanbul'da ..." in an English chat is the creator labelling text for the
    video, not changing language. Only that shape holds the chat: a colon followed by
    whitespace (so "0:05" is no label) after at most ``_LABEL_MAX_WORDS`` words that read as
    ``language``, with the text after it reading as ``detected``. One stray word of the old
    language anywhere else ("my son", "the İstanbul clip") never holds a clear switch.
    """
    if not message or not own_words_enabled():
        return False
    own = _own_words(message)
    colon = own.find(":")
    while colon >= 0 and not own[colon + 1 : colon + 2].isspace():
        colon = own.find(":", colon + 1)
    if colon < 0:
        return False
    label_tr, label_en, label_count = _scores(own[:colon])
    if not 1 <= label_count <= _LABEL_MAX_WORDS or not (label_tr if language == "tr" else label_en):
        return False
    return _language_of(*_scores(own[colon + 1 :]))[0] == detected


def asks_for_video_text(events: Any) -> bool:
    """True when the open question asks the creator to type words for the video (KRI-1363).

    ``events`` are ``(role, payload)`` in order, as `choice_questions.tag_event` builds them.
    A title's words typed as the answer are the video's language, not the chat's.
    """
    if not own_words_enabled():
        return False
    from app.services.choice_questions import (  # noqa: PLC0415
        CONFLICT_TITLE_TEXT,
        latest_open_choice_question,
    )
    from app.services.creative_copy_decisions import (  # noqa: PLC0415
        latest_open_creative_question,
    )

    events = list(events)
    question = latest_open_choice_question(events)
    if question is not None and question.get("conflict") == CONFLICT_TITLE_TEXT:
        return True
    return latest_open_creative_question(events) is not None


def resolve_reply_language(
    message: str | None,
    *,
    previous: str | None = None,
    locale: str | None = None,
    typed_elsewhere: str | None = None,
    answers_video_text: bool = False,
) -> str | None:
    """The chat's reply language after ``message``.

    ``previous`` is the chat's language before this message; ``locale`` is the
    raw ``Accept-Language`` header. A message that clearly reads as one language
    sets it; leaving an established language needs at least three clear words, so
    "make it pop" inside a Turkish chat keeps Turkish. ``typed_elsewhere`` is the
    language the creator last typed in another chat (`creator_typed_language`): for a
    chat nothing has told yet it comes before the device (Green 5: a "Suggest an edit."
    tap on a Turkish phone by a creator who types English).

    Only the creator's own words count (KRI-1363): quoted text and (``answers_video_text``)
    a typed answer to a wording question are text for the video, and so is the text after a
    short label in the chat's language ("Title: <Turkish words>" in an English chat stays
    English). One stray word of the old language never holds a clear switch.
    """
    previous = previous if previous in SUPPORTED_REPLY_LANGUAGES else None
    detected, strength = _detect(message)
    if previous is not None and answers_video_text and own_words_enabled():
        detected = None  # the words typed for a title are the video's language (KRI-1363)
    if detected is not None and (
        previous is None
        or detected == previous
        or (strength >= _SWITCH_STRENGTH and not _labels_video_text(previous, detected, message))
    ):
        return detected
    if previous is not None:
        return previous
    if typed_elsewhere in SUPPORTED_REPLY_LANGUAGES:
        return typed_elsewhere
    return locale_language(locale)


def history_enabled() -> bool:
    from app.config import settings  # noqa: PLC0415

    return enabled() and bool(getattr(settings, "kria_reply_language_history_enabled", True))


def needs_typed_history(message: str | None, previous: str | None) -> bool:
    """Only a chat nothing has told yet, on a message that does not say, reads the history."""
    return (
        history_enabled()
        and previous not in SUPPORTED_REPLY_LANGUAGES
        and _detect(message)[0] is None
    )


# How many of the creator's latest messages in other chats are read for their language.
_HISTORY_MESSAGES = 20


async def creator_typed_language(db: Any, *, creator_id: Any, exclude_thread_id: Any) -> str | None:
    """The language of the creator's most recent message they typed in another chat, or None.

    Stock sentences and taps never count (`detect_chat_language` ignores them), nor do
    messages too short to tell. Only this creator's own chats are read."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import CreationThread, CreationThreadEvent  # noqa: PLC0415

    statement = (
        select(CreationThreadEvent.content)
        .join(CreationThread, CreationThread.id == CreationThreadEvent.thread_id)
        .where(
            CreationThread.creator_id == creator_id,
            CreationThread.id != exclude_thread_id,
            CreationThreadEvent.role == "user",
            CreationThreadEvent.event_type == "user_message",
            CreationThreadEvent.content.is_not(None),
        )
        .order_by(CreationThreadEvent.created_at.desc())
        .limit(_HISTORY_MESSAGES)
    )
    try:
        rows = list((await db.execute(statement)).scalars())
    except Exception:  # noqa: BLE001 - a language hint never fails a chat: the device decides
        return None
    for content in rows:
        if not isinstance(content, str):
            continue
        language = detect_chat_language(content)
        if language is not None:
            return language
    return None


def enabled() -> bool:
    from app.config import settings  # noqa: PLC0415

    return bool(getattr(settings, "kria_reply_language_enabled", True))


def thread_reply_language(thread: Any) -> str | None:
    """The chat's language so far, kept on ``CreationThread.state["reply_language"]``.

    None when the kill switch is off, the thread predates KRI-520, or nothing has
    told yet.
    """
    if not enabled():
        return None
    state = getattr(thread, "state", None)
    stored = state.get(STATE_KEY) if isinstance(state, Mapping) else None
    return stored if stored in SUPPORTED_REPLY_LANGUAGES else None


def remember_reply_language(thread: Any, language: str | None) -> None:
    """Store the chat's language on the locked thread row; a no-op when unchanged."""
    if language not in SUPPORTED_REPLY_LANGUAGES or not enabled():
        return
    state = getattr(thread, "state", None)
    current = dict(state) if isinstance(state, Mapping) else {}
    if current.get(STATE_KEY) == language:
        return
    current[STATE_KEY] = language
    thread.state = current


def bind_reply_language(language: str | None) -> contextvars.Token[str | None]:
    """Bind the reply language; pair with ``release_reply_language`` in a ``finally``.

    For long task bodies that already end in try/finally; prefer
    ``reply_language_for`` everywhere else.
    """
    bound = language if language in SUPPORTED_REPLY_LANGUAGES and enabled() else None
    return _CURRENT.set(bound)


def release_reply_language(token: contextvars.Token[str | None]) -> None:
    _CURRENT.reset(token)


@contextmanager
def reply_language_for(language: str | None) -> Iterator[None]:
    """Bind the reply language for one turn's server copy; always restored on exit."""
    token = bind_reply_language(language)
    try:
        yield
    finally:
        release_reply_language(token)


def current_reply_language() -> str | None:
    """The language bound for this turn, or None (English copy, model mirrors)."""
    return _CURRENT.get()


def say(*, en: str, tr: str) -> str:
    """Pick server copy for the current turn's language. English unless bound to tr."""
    return tr if _CURRENT.get() == "tr" else en


def prompt_language_line(language: str | None) -> str:
    """A model instruction naming the reply language; "" for English or unknown.

    English and unknown languages render nothing, so those prompts stay
    byte-identical and the model keeps mirroring the creator as before.
    """
    if language not in SUPPORTED_REPLY_LANGUAGES or language == "en":
        return ""
    name = LANGUAGE_NAMES[language]
    return (
        f"Creator chat language: {name} ({language}). Write every creator-facing "
        f"sentence you return (replies, summaries, rationales, questions, option labels, "
        f"suggestions, reasons) in natural, everyday {name}, even when the context above "
        "is in English. Text that appears on the video stays exactly as the creator "
        "wrote or asked for it, in whatever language that is."
    )


# Loose-folded Turkish verb endings ("cekiyorum", "isletiyoruz", "yapacagim") that
# English never ends a word with; they count as Turkish grammar even when typed
# without diacritics.
_TR_VERB_ENDINGS = tuple(
    f"{stem}{person}"
    for stem in ("iyor", "uyor")
    for person in ("", "um", "uz", "sun", "lar", "du", "mus")
) + ("acagim", "ecegim", "acagiz", "ecegiz")


def _strict_scores(text: str) -> tuple[int, int, int, int]:
    """(Turkish evidence, Turkish grammar words, English evidence, word count).

    Stricter than chat detection, for text that has no chat behind it: a
    capitalized word is likely a name ("Göreme", "Kadıköy", "Çeşme"), so its
    letters say nothing about the language the sentence is written in. Grammar
    words and verb endings are the only evidence that can make text Turkish.
    """
    normalized = unicodedata.normalize("NFC", text)
    words = _WORD.findall(normalized)
    foreign = bool(_NON_TR_LETTERS.search(normalized))
    tr = grammar = en = 0
    for word in words:
        loose = loose_text(word)
        if loose in _TR_WORDS or loose.endswith(_TR_VERB_ENDINGS):
            tr += 1
            grammar += 1
        elif word[:1].isupper():
            if fold_text(word) in _EN_WORDS:
                en += 1
        elif _STRONG_TR_LETTERS.search(word) or (_SOFT_TR_LETTERS.search(word) and not foreign):
            tr += 1
        elif fold_text(word) in _EN_WORDS:
            en += 1
    return tr, grammar, en, len(words)


def _strict_detect(text: str) -> str | None:
    if not text or is_client_stock_message(text):
        return None
    tr, grammar, en, count = _strict_scores(text)
    if count == 0:
        return None
    if grammar and tr >= 2 * en and tr / count >= 0.2:
        return "tr"
    if en and en >= 2 * tr and en / count >= 0.25:
        return "en"
    return None


def creator_text_language(*texts: object) -> str | None:
    """The language the creator's own words clearly read as, else None (KRI-784).

    For agents with no chat behind them (onboarding, plan, voiceover, slide posts):
    pass only text the creator wrote (answers, a brief, a request), never
    model-written summaries, most telling first. Each argument (a string, or a
    list of strings read together) is checked on its own and the first that
    clearly reads as one language wins, so an English idea does not drown out a
    Turkish request. Turkish needs grammar words, not just Turkish letters: an
    English sentence about Göreme or Kadıköy stays English. None when the
    ``KRIA_REPLY_LANGUAGE_ENABLED`` kill switch is off.
    """
    if not enabled():
        return None
    for text in texts:
        if isinstance(text, str):
            joined = text.strip()
        elif isinstance(text, list | tuple):
            joined = " ".join(t.strip() for t in text if isinstance(t, str) and t.strip())
        else:
            continue
        language = _strict_detect(joined) if joined else None
        if language is not None:
            return language
    return None


def agent_reply_language(explicit: str | None, *creator_texts: object) -> str | None:
    """An agent's reply language: the caller's, else the turn's, else the creator's words.

    The order is the evidence order (KRI-784): a language the caller passed (the
    thread's or device's), then the language bound for this chat turn, then
    ``creator_text_language`` over the creator-authored fields. None when the
    kill switch is off.
    """
    if not enabled():
        return None
    if explicit in SUPPORTED_REPLY_LANGUAGES:
        return explicit
    return current_reply_language() or creator_text_language(*creator_texts)


def with_prompt_language(prompt: str, language: str | None) -> str:
    """``prompt`` plus the reply-language line; unchanged for English or unknown (KRI-784)."""
    line = prompt_language_line(language) if enabled() else ""
    return f"{prompt}\n\n{line}" if line else prompt
