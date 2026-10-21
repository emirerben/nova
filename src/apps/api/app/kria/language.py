"""Conversation-quality guards for creator-visible Kria responses."""

from __future__ import annotations

import re

from app.kria.brief_route import loose_text

# Matched against ``_normalized`` text (Turkish letters folded to ASCII, punctuation
# dropped). "you'd like" can never match that form; it is kept as it was so English
# behavior is unchanged.
_ACKNOWLEDGEMENT_PREFIXES = (
    "i understand",
    "i see that",
    "it sounds like",
    "you asked me to",
    "you said",
    "you want",
    "you would like",
    "you'd like",
    # KRI-520: Turkish openers that only restate the ask. "Anladım," / "İstediğin
    # gibi ..." are left out: like English "Got it,", they usually lead into real work.
    "gorunuse gore",
    "dedin ki",
)

# KRI-783: Turkish restates with the verb at the END of the sentence ("Başlığın daha
# büyük olmasını istiyorsun."), so the English prefix check cannot see it. Matched as the
# last word of the reply's first sentence, in ``_normalized`` form.
_RESTATEMENT_SENTENCE_ENDINGS = frozenset(
    {"istiyorsun", "istiyorsunuz", "istedin", "istediniz", "diyorsun", "diyorsunuz"}
)

# KRI-783: Turkish model refusals/meta replies ("Daha fazla bilgiye ihtiyacım var"), in
# ``_normalized`` form, matched as whole-word phrases. The writers' English refusal
# patterns stay in their modules; this is the Turkish half they share.
_TURKISH_REFUSAL_PHRASES = (
    "daha fazla bilgi",
    "daha fazla bilgiye",
    "yapay zeka olarak",
    "bir yapay zeka",
    "yazamam",
    "olusturamam",
    "yardimci olamam",
    "bu istegi yerine getiremem",
)

_STATUS_QUESTIONS = {
    "are you done",
    "hows it going",
    "how is it going",
    "is it done",
    "status",
    "status update",
    "what is happening",
    "whats happening",
    "where are we",
    # KRI-520: Turkish, ASCII-folded.
    "durum",
    "durum ne",
    "durum nedir",
    "son durum",
    "son durum ne",
    "ne durumda",
    "ne durumdayiz",
    "ne asamada",
    "ne asamadayiz",
    "bitti mi",
    "hazir mi",
    "oldu mu",
    "nasil gidiyor",
    "ne oluyor",
    "neredeyiz",
    # Turkish with the English product word ("status ne?").
    "status ne",
    "durumu ne",
    "durumu nedir",
    "bitmedi mi",
    "daha bitmedi mi",
    "hala bitmedi mi",
    "ne kadar kaldi",
    "daha ne kadar",
    "guncelleme",
    "guncelleme var mi",
}

# A subject before a Turkish status predicate ("video hazır mı?", "render bitti mi?").
_STATUS_SUBJECTS = frozenset({"video", "videom", "render", "edit", "islem", "kurgu"})
# The Turkish question particle, often typed joined to its word ("bittimi", "hazırmı").
_QUESTION_PARTICLES = ("mi", "mu")

_HELP_QUESTIONS = {
    "help",
    "help me",
    "what can i ask you",
    "what can you do",
    "what do you do",
    # KRI-520: Turkish, ASCII-folded.
    "yardim",
    "yardim et",
    "yardim eder misin",
    "bana yardim et",
    "ne yapabilirsin",
    "neler yapabilirsin",
    "sana ne sorabilirim",
    "sana neler sorabilirim",
    "ne ise yariyorsun",
}


def _normalized(value: str) -> str:
    """Words only, case- and Turkish-letter-folded ("Nasıl gidiyor?" -> "nasil gidiyor").

    Letters of every script are kept, so a reply written in Japanese or Arabic is
    never mistaken for an empty one.
    """
    # An apostrophe joins a contraction instead of splitting it: "how's" -> "hows", so
    # the "hows it going" / "whats happening" entries match (KRI-782).
    return " ".join(re.findall(r"[^\W_]+", loose_text(value).replace("'", "").replace("’", "")))


def is_paraphrase_only(*, user_message: str, assistant_message: str) -> bool:
    """Reject acknowledgement-shaped turns that add no observable value.

    This is a narrow safety net, not a semantic judge. The planner's turn-value
    contract and eval corpus remain the primary quality controls.
    """

    user = _normalized(user_message)
    assistant = _normalized(assistant_message)
    if not assistant:
        return True
    if any(assistant.startswith(prefix) for prefix in _ACKNOWLEDGEMENT_PREFIXES):
        return True
    if _first_sentence_restates(assistant_message):
        return True
    return len(user) >= 20 and assistant in {user, f"got it {user}", f"sure {user}"}


def _first_sentence_restates(message: str) -> bool:
    """True when the first sentence is a statement ending in a Turkish restating verb.

    A question ("... istiyor musun?") ends in a particle, never in these forms, and a
    first sentence that ends with "?" is skipped outright.
    """
    stripped = message.strip()
    end = next((i for i, ch in enumerate(stripped) if ch in ".!?\n"), len(stripped))
    if stripped[end : end + 1] == "?":
        return False
    tokens = _normalized(stripped[:end]).split()
    return bool(tokens) and tokens[-1] in _RESTATEMENT_SENTENCE_ENDINGS


def is_turkish_refusal(text: str) -> bool:
    """A model reply that refuses or asks for more input in Turkish, any spelling."""
    padded = f" {_normalized(text)} "
    return any(f" {phrase} " in padded for phrase in _TURKISH_REFUSAL_PHRASES)


def is_status_question(message: str) -> bool:
    """Return true only for an inert, status-only interruption."""

    normalized = _normalized(message)
    if normalized in _STATUS_QUESTIONS:
        return True
    words = normalized.split()
    if len(words) > 1 and words[0] in _STATUS_SUBJECTS:
        words = words[1:]
    if words and words[-1].endswith(_QUESTION_PARTICLES) and len(words[-1]) > 4:
        words = [*words[:-1], words[-1][:-2], words[-1][-2:]]
    return " ".join(words) in _STATUS_QUESTIONS


def is_help_question(message: str) -> bool:
    """Return true only for capability help, never an edit request containing 'help'."""

    return _normalized(message) in _HELP_QUESTIONS
