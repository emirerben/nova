"""What Kria says when the creator's own words can't be a clip label (KRI-678, KRI-804).

A clip label is a short tag: the label charset, ``CREATOR_LABEL_MAX_CHARS`` and
``CREATOR_LABEL_MAX_WORDS`` for the creator's own words. "chimichurri + salsa criolla"
has a character a label can't show; the reply used to name one reason at a time, and
"What shorter label?" gave the creator nothing to pick. Now it names every reason, in the
chat's language, and offers ways forward the creator can tap:

* keep the words exactly as written: the described-shot caption lane prints them on that
  clip (KRI-296), with none of a label's bounds;
* a shorter label made only of their own words that passes the same fence
  (``shorter_creator_label``), when one exists.

A tapped "keep" option is the creator's message, so every later turn still sees it in
the thread and the label keeps going as a caption (``keeps_as_written``).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.kria.reply_language import say
from app.schemas.clip_intents import (
    CREATOR_LABEL_MAX_CHARS,
    CREATOR_LABEL_MAX_WORDS,
    clean_creator_caption_text,
    creator_label_problems,
    label_blocked_characters,
    shorter_creator_label,
)
from app.text_fold import loose_text

# The reply list the chat shows as tappable chips holds at most four answers.
MAX_REPLY_OPTIONS = 4
_QUOTE_MAX_CHARS = 80


@dataclass(frozen=True)
class LabelTextReply:
    question: str
    options: tuple[str, ...]


def _key(text: str) -> str:
    return " ".join(re.findall(r"\w+", loose_text(text)))


# The "keep" answer in both languages: a creator who switched language still matches.
_KEEP_EN = 'Keep "{}" exactly as written'
_KEEP_TR = '"{}" yazdığım gibi kalsın'


def keep_as_written_option(text: str) -> str:
    return say(en=_KEEP_EN.format(text), tr=_KEEP_TR.format(text))


def shorter_label_option(text: str) -> str:
    return say(en=f'Use "{text}" instead', tr=f'Onun yerine "{text}" yaz')


def keeps_as_written(text: str, creator_messages: str | None) -> bool:
    """Did the creator answer "keep these words exactly as written" (either language)?

    Read from all of the creator's messages, so the choice holds on every later turn.
    """
    said = f" {_key(creator_messages or '')} "
    return any(f" {_key(form.format(text))} " in said for form in (_KEEP_EN, _KEEP_TR))


def can_keep_as_written(text: str) -> bool:
    """The caption lane prints the creator's own words up to its own bound."""
    return clean_creator_caption_text(text) is not None


def _quoted(text: str) -> str:
    shown = text if len(text) <= _QUOTE_MAX_CHARS else f"{text[: _QUOTE_MAX_CHARS - 1]}…"
    return f'"{shown}"'


def _reasons(text: str) -> str:
    """Every reason ``text`` can't be a label, as one clause in the chat's language."""
    limit = CREATOR_LABEL_MAX_CHARS
    reasons: list[str] = []
    for problem in creator_label_problems(text):
        if problem == "characters":
            shown = say(en=" or ", tr=" ya da ").join(
                f'"{chars}"' for chars in label_blocked_characters(text)
            )
            reasons.append(
                say(
                    en=f"a label can't show {shown}",
                    tr=f"etiket {shown} gösteremez",
                )
            )
        elif problem == "too_long":
            reasons.append(
                say(
                    en=f"it's {len(text)} characters (a label holds up to {limit})",
                    tr=f"{len(text)} karakter (etiket en fazla {limit} karakter alır)",
                )
            )
        elif problem == "too_many_words":
            words = len(text.split())
            reasons.append(
                say(
                    en=f"it's {words} words (a label holds up to {CREATOR_LABEL_MAX_WORDS})",
                    tr=f"{words} kelime (etiket en fazla {CREATOR_LABEL_MAX_WORDS} kelime alır)",
                )
            )
        else:
            reasons.append(say(en="it has no letters to show", tr="gösterilecek harf yok"))
    return say(en=", and ", tr=" ve ").join(reasons)


def _way_forward(keep: bool, shorter: str | None) -> str:
    if keep and shorter:
        return say(
            en=(
                "I can keep your words exactly as written on that clip, or use the shorter "
                f'label "{shorter}".'
            ),
            tr=(
                "Yazdığın gibi aynen o klipte gösterebilirim ya da daha kısa "
                f'"{shorter}" etiketini kullanabilirim.'
            ),
        )
    if keep:
        return say(
            en=(
                "I can keep your words exactly as written on that clip, or you can give me "
                "a shorter label."
            ),
            tr=(
                "Yazdığın gibi aynen o klipte gösterebilirim ya da bana daha kısa bir etiket "
                "yazabilirsin."
            ),
        )
    if shorter:
        return say(
            en=f'I can use the shorter label "{shorter}" instead, or you can write another one.',
            tr=(
                f'Onun yerine daha kısa "{shorter}" etiketini kullanabilirim ya da başka bir '
                "tane yazabilirsin."
            ),
        )
    return say(
        en="What shorter label should I use instead?",
        tr="Yerine hangi daha kısa etiketi yazayım?",
    )


def label_text_reply(texts: Sequence[str]) -> LabelTextReply | None:
    """One answerable question for every creator label text that can't print; None if all can.

    Each text gets every reason it can't be a label and its ways forward; the options
    are the tappable answers (keep as written first: it is exactly what they asked for).
    """
    parts: list[str] = []
    options: list[str] = []
    seen: set[str] = set()
    for text in texts:
        key = _key(text)
        if not creator_label_problems(text) or key in seen:
            continue
        seen.add(key)
        reasons = _reasons(text)
        parts.append(
            say(
                en=f"{_quoted(text)} can't be a clip label: {reasons}.",
                tr=f"{_quoted(text)} klip etiketi olamaz: {reasons}.",
            )
        )
        keep = can_keep_as_written(text)
        shorter = shorter_creator_label(text)
        parts.append(_way_forward(keep, shorter))
        if keep:
            options.append(keep_as_written_option(text))
        if shorter:
            options.append(shorter_label_option(shorter))
    if not parts:
        return None
    options = options[:MAX_REPLY_OPTIONS]
    if options:
        # Never "which ...?": this is a question about the words, not about clips (KRI-678).
        parts.append(say(en="What would you like?", tr="Ne yapmamı istersin?"))
    return LabelTextReply(question=" ".join(parts), options=tuple(options))
