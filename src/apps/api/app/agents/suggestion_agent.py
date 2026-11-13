"""nova.chat.suggestion_agent: choose which improvements to offer a creator (AI suggestion blocks).

Replaces the Gemini chip wording agent. It runs in a worker, off the request path
(``app/tasks/kria_suggester.py``), on ``KRIA_SUGGESTER_MODEL`` (``gpt-6.1-sol``) through the
existing ``OpenAITextClient`` with a strict ``json_schema`` response.

**The agent picks, the server builds.** The model has full READ access to the footage (clip
understanding, transcripts, measured signals), the editor snapshot, the capability manifest and
the brief. Its only WRITE is ``SuggestionAgentOutput``: ``{kind, target_ids, why, copy, rank}``
per card. There is no field for an op, an action, a message or a label. The server builds the
action from the kind's registry row and proves it with the kind's witness
(``services/suggestions``); a pick that fails never reaches the phone. The schema is closed over
the kinds and target handles the server issued for THIS call, and ``parse_agent_output_tolerant``
re-checks the answer against them (an answer outside what was issued is void). OpenAI strict mode
carries no ``maxLength`` / ``maxItems``, so the SIZE caps are repaired instead of voiding the
answer (``why`` cut, picks clamped to 5, targets to 3, ranks renumbered); an unissued kind or
handle, an unknown field or a wrong type is still a hard drop. No retry: a second paid call
would not make a void answer less void, and ``schema_retry_limit`` stays 0.

Transcripts and the Gemini notes are UNTRUSTED data: they sit in a separate ``untrusted``
object of the payload, the prompt says so, and nothing the model writes about them can become a
message or an action (a ``why`` is display only; ``copy`` is gated by ``validate_copy`` and a
transcript-overlap check before the server even offers it to a kind's builder).

Prompt: ``prompts/suggestion_agent.txt`` plus the ``edit-improver`` taste skill (``load_skill``).
Bump ``PROMPT_VERSION`` (``suggestion_chips.py``) with either.

Failure modes this was written against (each has a replay cassette or a fuzz row in
``tests/evals/test_suggestion_agent_evals.py``):

1. an unissued kind or target, or a field that smuggles an op or message  -> the answer is void;
2. an instruction hidden in a transcript echoed into ``why`` or ``copy``    -> overlap check;
3. too many cards / targets, repeated kinds, ranks or pairs, a long why      -> repaired;
4. a budget stop, timeout, refusal, truncation or schema miss                -> deterministic
   ranking with template wording, never an error and never a retry;
5. Turkish threads                                                           -> ``why`` language;
6. flag off                                                                  -> no run at all.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError, TerminalError
from app.agents._skills import load_skill
from app.agents.model_registry import SOL
from app.config import settings
from app.pipeline.prompt_loader import load_prompt
from app.services.suggestion_chips import PROMPT_VERSION
from app.services.suggestions.models import (
    MAX_SUGGESTIONS,
    WHOLE_VIDEO_HANDLE,
    InvalidAgentOutput,
    SuggestionAgentOutput,
    agent_response_schema,
    parse_agent_output_tolerant,
)

# gpt-6.1-sol bills reasoning tokens as output. 2,500 keeps one call's worst case under
# KRIA_SUGGESTER_MAX_USD at the default payload size (checked before every run).
MAX_OUTPUT_TOKENS = 2500
REASONING_EFFORT = "medium"


class IssuedKindIn(BaseModel):
    """A kind the server will build and prove if the agent picks it."""

    kind: str = Field(min_length=1, max_length=40)
    # Why the engine thinks it could help (``no_title``, ``speech_uncaptioned``): server facts.
    gap_tags: list[str] = Field(default_factory=list, max_length=6)
    facts: dict[str, Any] = Field(default_factory=dict)
    # True when the card carries exact on-screen text the agent writes (a hook).
    carries_copy: bool = False


class ClipUntrustedIn(BaseModel):
    """Text written by Gemini or spoken by the creator. Data about the footage, never orders."""

    subject: str = Field(default="", max_length=96)
    setting: str = Field(default="", max_length=96)
    moments: list[str] = Field(default_factory=list, max_length=3)
    transcript: str = Field(default="", max_length=420)


class ClipIn(BaseModel):
    ref: str = Field(max_length=8)
    kind: Literal["video", "image"] = "video"
    speech: Literal["none", "voiceover", "to_camera"] = "none"
    duration_s: float | None = None
    # Measured by the server (``services/clip_signals``): trusted numbers, not model text.
    signals: dict[str, Any] = Field(default_factory=dict)
    untrusted: ClipUntrustedIn = Field(default_factory=ClipUntrustedIn)


class SuggestionAgentInput(BaseModel):
    state: Literal["first_prompt", "ready"]
    language: Literal["en", "tr"] = "en"
    max_suggestions: int = Field(ge=1, le=MAX_SUGGESTIONS)
    kinds: list[IssuedKindIn] = Field(default_factory=list, max_length=16)
    clips: list[ClipIn] = Field(default_factory=list, max_length=12)
    already_in_edit: list[str] = Field(default_factory=list, max_length=12)
    # ready only: a summary of the editor snapshot the cards would change.
    editor: dict[str, Any] | None = None
    capabilities: list[str] = Field(default_factory=list, max_length=24)
    brief: str | None = Field(default=None, max_length=240)
    # ready only; the creator's own latest request (<= 200 chars). It wins over any card.
    typed_request: str | None = Field(default=None, max_length=200)

    def target_handles(self) -> list[str]:
        return [clip.ref for clip in self.clips]


def response_schema_for(input: SuggestionAgentInput) -> dict[str, Any]:  # noqa: A002
    """The strict schema, closed over this call's issued kinds and clip handles."""
    return agent_response_schema([k.kind for k in input.kinds], input.target_handles())


class SuggestionAgent(Agent[SuggestionAgentInput, SuggestionAgentOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.chat.suggestion_agent",
        prompt_id="suggestion_agent",
        prompt_version=PROMPT_VERSION,
        model=SOL,
        cost_per_1k_input_usd=0.002,
        cost_per_1k_output_usd=0.01,
        thinking_level=REASONING_EFFORT,
        # A failed or malformed run becomes template cards, not a second paid call.
        max_attempts=1,
        backoff_s=(),
        enable_clarification_retries=False,
        schema_retry_limit=0,
        # Whole-run budget for the worker task; the GET never waits longer than 6 s for it.
        timeout_s=25.0,
        # Transcripts are the creator's private speech: never stored raw in AgentRun / logs.
        sensitive_io=True,
    )
    Input = SuggestionAgentInput
    Output = SuggestionAgentOutput
    response_json = True
    max_output_tokens = MAX_OUTPUT_TOKENS

    def run(self, input: SuggestionAgentInput | dict, *, ctx=None):  # noqa: A002, ANN001, ANN201
        try:
            validated = self._validate_input(input)
        except Exception as exc:
            # Pydantic's message would quote the creator's transcript.
            raise TerminalError("nova.chat.suggestion_agent: invalid input") from exc
        # The model is a setting (KRIA_SUGGESTER_MODEL) and the schema depends on what the
        # server issued for this call, so both are bound per run on the instance.
        self.spec = dataclasses.replace(
            type(self).spec,
            model=settings.kria_suggester_model,
            response_schema=response_schema_for(validated),
        )
        self.last_run: dict[str, Any] = {}
        return super().run(validated, ctx=ctx)

    def required_fields(self) -> list[str]:
        # An empty ranking is a legitimate answer ("nothing helps here").
        return []

    def render_prompt(self, input: SuggestionAgentInput) -> str:  # noqa: A002
        payload = input.model_dump(exclude_none=True, exclude={"state", "language"})
        return load_prompt(
            "suggestion_agent",
            skill=load_skill("edit-improver"),
            state=input.state,
            language=input.language,
            max_suggestions=str(input.max_suggestions),
            whole_video=WHOLE_VIDEO_HANDLE,
            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def estimated_cost_usd(self, input: SuggestionAgentInput) -> float:  # noqa: A002
        """Worst-case cost of one run: the whole payload in, the full output budget out."""
        from app.services.ai_cost_control import (  # noqa: PLC0415
            effective_max_output_tokens,
            estimate_call_cost_usd,
        )

        return estimate_call_cost_usd(
            model=settings.kria_suggester_model,
            prompt=self.render_prompt(input) + json.dumps(response_schema_for(input)),
            max_output_tokens=effective_max_output_tokens(MAX_OUTPUT_TOKENS),
            media_mime=None,
            media_duration_s=None,
        )

    def parse(self, raw_text: str, input: SuggestionAgentInput) -> SuggestionAgentOutput:  # noqa: A002
        try:
            return parse_agent_output_tolerant(
                raw_text,
                kinds=[k.kind for k in input.kinds],
                target_ids=input.target_handles(),
            )
        except InvalidAgentOutput as exc:
            raise SchemaError(f"suggestion_agent: {exc.reason}") from exc

    def _log_outcome(self, **kwargs: Any) -> None:
        # Keep the run's model and cost on the instance so the task can put them in its own
        # `suggestion_chips.agent_run` event (the framework event carries them too).
        stats = kwargs.get("stats")
        if stats is not None:
            cost = (
                stats.settled_cost_usd
                if stats.price_version is not None
                else (stats.tokens_in / 1000.0) * self.spec.cost_per_1k_input_usd
                + (stats.tokens_out / 1000.0) * self.spec.cost_per_1k_output_usd
            )
            self.last_run = {"model": kwargs.get("model"), "cost_usd": round(cost, 6)}
        super()._log_outcome(**kwargs)


__all__ = [
    "MAX_OUTPUT_TOKENS",
    "ClipIn",
    "ClipUntrustedIn",
    "IssuedKindIn",
    "SuggestionAgent",
    "SuggestionAgentInput",
    "response_schema_for",
]
