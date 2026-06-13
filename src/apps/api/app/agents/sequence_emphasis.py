"""nova.compose.sequence_emphasis — per-phrase word emphasis for transcript-synced typography.

A deterministic engine splits a video's spoken transcript into phrases of 1-6
words (scenes); each phrase renders as an editorial word cluster while it is
spoken. This agent decides — for ALL phrases in ONE LLM call — which words carry
the visual weight, using the EXISTING role vocabulary from
`app/pipeline/intro_cluster.py`:

  - "hero"      — rendered in a script face; THE emphasis group
  - "connector" — small ordinary glue words
  - "closer"    — slightly larger serif tail; final word only

The agent annotates roles ONLY. It never authors on-screen text (the words come
verbatim from the user's own transcript) and it owns no geometry (the
intro_cluster layout engine does). Validation is strict by design: any
per-phrase violation raises SchemaError naming the phrase index so the runtime
retries once with a clarification suffix; on terminal failure the CALLER falls
back to `intro_cluster.derive_word_roles` per phrase, so a bad annotation can
never block a render.
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.music_matcher import _sanitize_text
from app.pipeline.intro_cluster import ROLE_CLOSER, ROLE_HERO, VALID_ROLES
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

# The deterministic splitter guarantees 1-6 words per phrase (mirrors
# intro_cluster's cluster-suitable band). Enforced here as defense-in-depth so
# an internal caller can't ship an unannotatable phrase into the prompt.
_MAX_PHRASE_WORDS = 6
# Hard cap on phrase count — a runaway transcript can't flood one prompt.
_MAX_PHRASES = 64


class SequenceEmphasisInput(BaseModel):
    # Phrases in display order, each a list of 1-6 words exactly as they will
    # render. UNTRUSTED transcript content — wrapped as DATA in the prompt; the
    # output is roles only, so no transcript text can reach the screen via this
    # agent.
    phrases: list[list[str]] = Field(min_length=1, max_length=_MAX_PHRASES)
    language_hint: str = "en"

    @field_validator("phrases")
    @classmethod
    def _phrases_well_formed(cls, v: list[list[str]]) -> list[list[str]]:
        for i, words in enumerate(v):
            if not words:
                raise ValueError(f"phrase {i}: empty phrase")
            if any(not (w or "").strip() for w in words):
                raise ValueError(f"phrase {i}: blank word")
            if len(words) > _MAX_PHRASE_WORDS:
                raise ValueError(f"phrase {i}: {len(words)} words > {_MAX_PHRASE_WORDS}")
        return v


class PhraseEmphasis(BaseModel):
    index: int
    # Aligned 1:1 to the input phrase's words. Vocabulary: intro_cluster
    # VALID_ROLES ("hero" | "connector" | "closer").
    word_roles: list[str]


class SequenceEmphasisOutput(BaseModel):
    # One entry per input phrase, in input order (parse() guarantees this).
    phrases: list[PhraseEmphasis]


class SequenceEmphasisAgent(Agent[SequenceEmphasisInput, SequenceEmphasisOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.sequence_emphasis",
        prompt_id="sequence_emphasis",
        prompt_version="1.0.0",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Structured annotation, not creative writing — a small thinking budget
        # keeps latency low (this single call covers every phrase in the video).
        thinking_budget=256,
    )
    Input = SequenceEmphasisInput
    Output = SequenceEmphasisOutput

    def required_fields(self) -> list[str]:
        return ["phrases"]

    def render_prompt(self, input: SequenceEmphasisInput) -> str:  # noqa: A002
        # Words are DATA (spoken transcript), sanitized for prompt hygiene only.
        # Roles map back to the ORIGINAL words by index, so sanitization here
        # can never desynchronize the output alignment.
        phrases_payload = [
            {"index": i, "words": [_sanitize_text(w) for w in words]}
            for i, words in enumerate(input.phrases)
        ]
        return load_prompt(
            "sequence_emphasis",
            phrases_json=json.dumps(phrases_payload, ensure_ascii=False),
            language_hint=_sanitize_text(input.language_hint) or "en",
        )

    def parse(
        self,
        raw_text: str,
        input: SequenceEmphasisInput,  # noqa: A002
    ) -> SequenceEmphasisOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"sequence_emphasis: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("sequence_emphasis: response is not a JSON object")
        raw_phrases = data.get("phrases")
        if not isinstance(raw_phrases, list):
            raise SchemaError("sequence_emphasis: 'phrases' is not a list")

        by_index: dict[int, list[str]] = {}
        for entry in raw_phrases:
            if not isinstance(entry, dict):
                raise SchemaError("sequence_emphasis: phrase entry is not a JSON object")
            idx = entry.get("index")
            if not isinstance(idx, int) or isinstance(idx, bool):
                raise SchemaError(f"sequence_emphasis: non-integer phrase index {idx!r}")
            if idx < 0 or idx >= len(input.phrases):
                raise SchemaError(
                    f"sequence_emphasis: phrase {idx}: index out of range "
                    f"(input has {len(input.phrases)} phrases)"
                )
            if idx in by_index:
                raise SchemaError(f"sequence_emphasis: phrase {idx}: duplicate annotation")
            roles = entry.get("word_roles")
            if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
                raise SchemaError(
                    f"sequence_emphasis: phrase {idx}: word_roles is not a list of strings"
                )
            by_index[idx] = list(roles)

        validated: list[PhraseEmphasis] = []
        for i, words in enumerate(input.phrases):
            roles = by_index.get(i)
            if roles is None:
                raise SchemaError(f"sequence_emphasis: phrase {i}: missing annotation")
            _validate_phrase_roles(i, words, roles)
            validated.append(PhraseEmphasis(index=i, word_roles=roles))

        try:
            return SequenceEmphasisOutput(phrases=validated)
        except ValidationError as exc:
            raise SchemaError(f"sequence_emphasis: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: Return ONLY a JSON object of the form "
            '{"phrases": [{"index": 0, "word_roles": ["connector", "hero", "hero"]}, ...]} '
            "with one entry per input phrase (same index values), word_roles aligned 1:1 "
            'to that phrase\'s words, every role exactly one of "hero" | "connector" | '
            '"closer", at least one "hero" per phrase, and "closer" only ever as the '
            "final word's role."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


def _validate_phrase_roles(index: int, words: list[str], roles: list[str]) -> None:
    """Strict per-phrase validation. Raises SchemaError naming the phrase index
    so the runtime's clarification retry (and the caller's heuristic fallback to
    `intro_cluster.derive_word_roles`) can act on it."""
    if len(roles) != len(words):
        raise SchemaError(
            f"sequence_emphasis: phrase {index}: {len(roles)} roles for "
            f"{len(words)} words (word_roles must align 1:1)"
        )
    unknown = [r for r in roles if r not in VALID_ROLES]
    if unknown:
        raise SchemaError(
            f"sequence_emphasis: phrase {index}: unknown role(s) {unknown!r} "
            f"(vocabulary: {list(VALID_ROLES)})"
        )
    if ROLE_CLOSER in roles[:-1]:
        raise SchemaError(
            f"sequence_emphasis: phrase {index}: 'closer' before the final word "
            "(closer is final-word-only)"
        )
    if ROLE_HERO not in roles:
        raise SchemaError(
            f"sequence_emphasis: phrase {index}: no 'hero' role "
            "(every phrase needs at least one hero)"
        )
