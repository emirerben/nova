"""Conversation-quality guards for creator-visible Kria responses."""

from __future__ import annotations

import re

from app.kria.brief_route import loose_text

# Matched against ``_normalized`` text: Turkish letters folded to ASCII ("anladım"
# -> "anladim"), punctuation dropped ("you'd" -> "you d").
_ACKNOWLEDGEMENT_PREFIXES = (
    "i understand",
    "i see that",
    "it sounds like",
    "you asked me to",
    "you said",
    "you want",
    "you would like",
    "you d like",
    # KRI-520: the same empty acknowledgements in Turkish.
    "anladim",
    "anliyorum",
    "gorunuse gore",
    "istedigin",
    "istediginiz",
    "benden istedigin",
    "dedin ki",
    "soyledigin",
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
}

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
    return " ".join(re.findall(r"[^\W_]+", loose_text(value)))


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
    return len(user) >= 20 and assistant in {user, f"got it {user}", f"sure {user}"}


def is_status_question(message: str) -> bool:
    """Return true only for an inert, status-only interruption."""

    return _normalized(message) in _STATUS_QUESTIONS


def is_help_question(message: str) -> bool:
    """Return true only for capability help, never an edit request containing 'help'."""

    return _normalized(message) in _HELP_QUESTIONS
