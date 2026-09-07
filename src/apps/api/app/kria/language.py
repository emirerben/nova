"""Conversation-quality guards for creator-visible Kria responses."""

from __future__ import annotations

import re

_ACKNOWLEDGEMENT_PREFIXES = (
    "i understand",
    "i see that",
    "it sounds like",
    "you asked me to",
    "you said",
    "you want",
    "you would like",
    "you'd like",
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
}

_HELP_QUESTIONS = {
    "help",
    "help me",
    "what can i ask you",
    "what can you do",
    "what do you do",
}


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


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
