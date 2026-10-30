"""Closed vocabularies that LLM prompts name, rendered from their source enums.

A prompt writes ``{{TEXT_ANIMATION_EDGES}}`` instead of typing the values, so a value added
to the enum reaches the model with no prompt edit. ``tests/agents/test_prompt_vocab_drift.py``
fails when a prompt or an AI-layer literal hand-lists part of a registered vocabulary.
"""

import re
from typing import NamedTuple, get_args

from app.agents._schemas.text_animation_phases import TEXT_ANIMATION_VOCAB, TextAnimationPhases


class PromptVocabulary(NamedTuple):
    members: tuple[str, ...]
    rendered: str


def _edges() -> PromptVocabulary:
    members = tuple(TEXT_ANIMATION_VOCAB)
    rendered = ", ".join(
        edge if vocab.label.casefold() == edge.casefold() else f"{edge} ({vocab.label})"
        for edge, vocab in TEXT_ANIMATION_VOCAB.items()
    )
    return PromptVocabulary(members, rendered)


def _loops() -> PromptVocabulary:
    members = get_args(TextAnimationPhases.model_fields["loop"].annotation)
    return PromptVocabulary(members, ", ".join(members))


PROMPT_VOCABULARIES: dict[str, PromptVocabulary] = {
    "TEXT_ANIMATION_EDGES": _edges(),
    "TEXT_ANIMATION_LOOPS": _loops(),
}

_TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


def render_vocab_tokens(text: str) -> str:
    """``text`` with every ``{{TOKEN}}`` replaced by its vocabulary; unknown tokens raise."""

    def _sub(match: re.Match[str]) -> str:
        vocab = PROMPT_VOCABULARIES.get(match.group(1))
        if vocab is None:
            raise KeyError(f"unknown prompt vocabulary token {match.group(0)}")
        return vocab.rendered

    return _TOKEN.sub(_sub, text)
