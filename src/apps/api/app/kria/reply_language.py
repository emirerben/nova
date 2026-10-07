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


def is_client_stock_message(text: str | None) -> bool:
    """True for a sentence an app sends on the creator's behalf, not one they typed."""
    key = _stock_key(text or "")
    if not key:
        return False
    if key in _CLIENT_STOCK_MESSAGES or key.startswith(_CLIENT_STOCK_PREFIXES):
        return True
    return any(shape.match(key) for shape in _CLIENT_STOCK_SHAPES)


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
    tr, en, count = _scores(text)
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


def resolve_reply_language(
    message: str | None,
    *,
    previous: str | None = None,
    locale: str | None = None,
) -> str | None:
    """The chat's reply language after ``message``.

    ``previous`` is the chat's language before this message; ``locale`` is the
    raw ``Accept-Language`` header. A message that clearly reads as one language
    sets it; leaving an established language needs at least three clear words, so
    "make it pop" inside a Turkish chat keeps Turkish.
    """
    previous = previous if previous in SUPPORTED_REPLY_LANGUAGES else None
    detected, strength = _detect(message)
    if detected is not None and (
        previous is None or detected == previous or strength >= _SWITCH_STRENGTH
    ):
        return detected
    if previous is not None:
        return previous
    return locale_language(locale)


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
