"""What Kria says when the creator's own words can't be a clip label (KRI-678, KRI-804).

KRI-826: the creator's own label words print as written ("chimichurri + salsa criolla",
emoji, a line break they typed). The only reason left is the label bar itself: the
words run past its two lines (``creator_label_fits``), or there is nothing to show. The
reply names that, in the chat's language, and offers ways forward the creator can tap:

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
from app.pipeline.text_fit import CREATOR_LABEL_MAX_LINES
from app.schemas.clip_intents import (
    clean_creator_caption_text,
    creator_label_problems,
    shorter_creator_label,
)
from app.schemas.edit_proposal import CREATOR_SHOT_LABEL_MAX_CHARS
from app.text_fold import loose_text

# The reply list the chat shows as tappable chips holds at most four answers.
MAX_REPLY_OPTIONS = 4
_QUOTE_MAX_CHARS = 80
# The chat reply holds 1200 characters (`KriaTurnPlan.response`); the turn may append a
# line ("Your current draft is unchanged."), so the question stays well inside it.
_QUESTION_MAX_CHARS = 1000
# Room kept for the closing lines ("3 more of your labels ...", "What would you like?").
_QUESTION_TRAILER_CHARS = 120


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
    """The described-shot caption lane prints these words as written.

    Its bound is the montage's for the creator's clip text (``_cap_label`` refuses longer),
    which is tighter than a caption intent's own.
    """
    copy = clean_creator_caption_text(text)
    return copy is not None and len(copy) <= CREATOR_SHOT_LABEL_MAX_CHARS


def _quoted(text: str) -> str:
    shown = text if len(text) <= _QUOTE_MAX_CHARS else f"{text[: _QUOTE_MAX_CHARS - 1]}…"
    return f'"{shown}"'


def _reasons(text: str) -> str:
    """Every reason ``text`` can't be a label, as one clause in the chat's language."""
    reasons: list[str] = []
    for problem in creator_label_problems(text):
        if problem == "too_long":
            reasons.append(
                say(
                    en=f"it runs past the {CREATOR_LABEL_MAX_LINES} lines a clip label holds",
                    tr=f"klip etiketinin {CREATOR_LABEL_MAX_LINES} satırına sığmıyor",
                )
            )
        elif problem == "characters":
            reasons.append(
                say(
                    en="it has a hidden control character a label can't show",
                    tr="etiketin gösteremeyeceği gizli bir kontrol karakteri var",
                )
            )
        else:
            reasons.append(say(en="it has nothing to show", tr="gösterilecek bir şey yok"))
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


def label_text_reply(texts: Sequence[str], *, keep: bool = True) -> LabelTextReply | None:
    """One answerable question for every creator label text that can't print; None if all can.

    Each text gets every reason it can't be a label and its ways forward; the options
    are the tappable answers (keep as written first: it is exactly what they asked for).
    ``keep=False`` leaves that answer out where nothing would act on it: the caller can't
    turn the label into the shot's caption.
    """
    parts: list[str] = []
    options: list[str] = []
    seen: set[str] = set()
    left_out = 0
    for text in texts:
        key = _key(text)
        if not creator_label_problems(text) or key in seen:
            continue
        seen.add(key)
        reasons = _reasons(text)
        keeps = keep and can_keep_as_written(text)
        shorter = shorter_creator_label(text)
        about = [
            say(
                en=f"{_quoted(text)} can't be a clip label: {reasons}.",
                tr=f"{_quoted(text)} klip etiketi olamaz: {reasons}.",
            ),
            _way_forward(keeps, shorter),
        ]
        budget = _QUESTION_MAX_CHARS - _QUESTION_TRAILER_CHARS
        if parts and len(" ".join([*parts, *about])) > budget:
            left_out += 1
            continue
        parts.extend(about)
        if keeps:
            options.append(keep_as_written_option(text))
        if shorter:
            options.append(shorter_label_option(shorter))
    if not parts:
        return None
    if left_out:
        parts.append(
            say(
                en=f"{left_out} more of your labels can't be clip labels either.",
                tr=f"{left_out} etiketin daha klip etiketi olamıyor.",
            )
        )
    options = options[:MAX_REPLY_OPTIONS]
    if options:
        # Never "which ...?": this is a question about the words, not about clips (KRI-678).
        parts.append(say(en="What would you like?", tr="Ne yapmamı istersin?"))
    return LabelTextReply(question=" ".join(parts), options=tuple(options))
