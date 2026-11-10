"""Find where the voiceover says a creator's cue words when the exact matcher can't (KRI-1356).

A phone Voiceover reaction beat ("show the 0:0 photo on 'Null zu null'") is placed by the
phrase matcher in ``app.services.phone_reaction_grounding``: exact after folding, plus narrow
plural / near-spelling fallbacks. Speech-to-text writes what the creator typed in another form
-- "0 zu 0" for "Null zu null", "FC Nürnberg" for "1. FC Nürnberg", an inflected or misspelled
word -- and the matcher only knows English and Turkish number words. Instead of growing word
tables per language (DECISIONS.md, 2026-10-21 language policy), the cues the matcher never
heard are given to this agent once per render, with the timed transcript words.

The model never owns timestamps or text: it names a ``start_word_id`` / ``end_word_id`` from
the supplied words, or ``null`` when the voice never says the cue. The worker turns that span
into the words the matcher then places, and keeps the creator's own trigger on the receipt.
``parse`` is strict: an unknown id, a reversed or overlong span, a cue answered twice or a cue
nobody asked about rejects the whole answer, and the worker keeps today's ``never_heard``.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar

from pydantic import BaseModel, Field, model_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt

_WORD_ID_RE = re.compile(r"^w\d{6}$")
# A cue is a word or a short phrase; a longer span is a different moment, not the cue.
MAX_CUE_SPAN_WORDS = 8


class SpokenCue(BaseModel):
    cue_id: str = Field(min_length=1, max_length=80)
    trigger: str = Field(min_length=1, max_length=160)


class SpokenCueResolverInput(BaseModel):
    # Rows like the alignment agent's: {"word_id": "w000000", "text", "start_s", "end_s"}.
    words: list[dict] = Field(min_length=1, max_length=600)
    cues: list[SpokenCue] = Field(min_length=1, max_length=24)
    language: str = Field(default="", max_length=20)

    @model_validator(mode="after")
    def _unique_cues(self) -> SpokenCueResolverInput:
        ids = [cue.cue_id for cue in self.cues]
        if len(set(ids)) != len(ids):
            raise ValueError("cue ids must be unique")
        return self


class SpokenCueSpan(BaseModel):
    cue_id: str
    # Both set (the span the voice says the cue in) or both None (never said).
    start_word_id: str | None = None
    end_word_id: str | None = None


class SpokenCueResolverOutput(BaseModel):
    # One row per input cue, in input order; a cue the model left out reads as never said.
    resolutions: list[SpokenCueSpan] = Field(default_factory=list)

    def span_for(self, cue_id: str) -> SpokenCueSpan | None:
        return next((row for row in self.resolutions if row.cue_id == cue_id), None)


class SpokenCueResolverAgent(Agent[SpokenCueResolverInput, SpokenCueResolverOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.spoken_cue_resolver",
        prompt_id="spoken_cue_resolver",
        prompt_version="2026-11-10-kri1356-v1",
        model="gemini-2.5-flash",
        thinking_budget=256,
        timeout_s=20.0,
        max_attempts=2,
        backoff_s=(2.0,),
    )
    Input = SpokenCueResolverInput
    Output = SpokenCueResolverOutput

    def required_fields(self) -> list[str]:
        return ["resolutions"]

    def render_prompt(self, input: SpokenCueResolverInput) -> str:  # noqa: A002
        return load_prompt(
            "spoken_cue_resolver",
            words_json=json.dumps(
                [{"word_id": row.get("word_id"), "text": row.get("text")} for row in input.words],
                ensure_ascii=False,
            ),
            cues_json=json.dumps(
                [cue.model_dump(mode="json") for cue in input.cues], ensure_ascii=False
            ),
            language=input.language or "(unknown)",
        )

    def parse(
        self,
        raw_text: str,
        input: SpokenCueResolverInput,  # noqa: A002
    ) -> SpokenCueResolverOutput:
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"spoken_cue_resolver: invalid JSON — {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("resolutions"), list):
            raise SchemaError("spoken_cue_resolver: response must contain a resolutions list")

        word_index = {str(word.get("word_id")): index for index, word in enumerate(input.words)}
        known_cues = [cue.cue_id for cue in input.cues]
        by_cue: dict[str, SpokenCueSpan] = {}
        for position, raw in enumerate(payload["resolutions"]):
            if not isinstance(raw, dict):
                raise SchemaError(f"spoken_cue_resolver: resolution {position} is not an object")
            cue_id = str(raw.get("cue_id") or "")
            if cue_id not in known_cues:
                raise SchemaError(f"spoken_cue_resolver: unknown cue {cue_id!r}")
            if cue_id in by_cue:
                raise SchemaError(f"spoken_cue_resolver: cue {cue_id!r} answered twice")
            start, end = raw.get("start_word_id"), raw.get("end_word_id")
            if start is None and end is None:
                by_cue[cue_id] = SpokenCueSpan(cue_id=cue_id)
                continue
            start, end = str(start or ""), str(end or "")
            for word_id in (start, end):
                if not _WORD_ID_RE.fullmatch(word_id) or word_id not in word_index:
                    raise SchemaError(f"spoken_cue_resolver: unknown word {word_id!r}")
            first, last = word_index[start], word_index[end]
            if last < first:
                raise SchemaError(f"spoken_cue_resolver: cue {cue_id!r} ends before it starts")
            if last - first + 1 > MAX_CUE_SPAN_WORDS:
                raise SchemaError(
                    f"spoken_cue_resolver: cue {cue_id!r} spans {last - first + 1} words"
                )
            by_cue[cue_id] = SpokenCueSpan(cue_id=cue_id, start_word_id=start, end_word_id=end)
        return SpokenCueResolverOutput(
            resolutions=[
                by_cue.get(cue_id) or SpokenCueSpan(cue_id=cue_id) for cue_id in known_cues
            ]
        )
