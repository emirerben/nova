"""nova.compose.creative_direction — Pass-1 freeform editorial style description.

Replaces `app.pipeline.agents.gemini_analyzer._extract_creative_direction`.
Output is intentionally NOT JSON — Gemini returns a natural-language paragraph
describing the template's editing style. The runtime's response_json=False
disables the response_mime_type=application/json config.

Graceful degradation is the legacy contract: any failure returns an empty string
rather than raising. The agent's TerminalError is caught in the shim.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt


class CreativeDirectionInput(BaseModel):
    file_uri: str
    file_mime: str = "video/mp4"


class CreativeDirectionOutput(BaseModel):
    text: str


class CreativeDirectionAgent(Agent[CreativeDirectionInput, CreativeDirectionOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.creative_direction",
        prompt_id="analyze_template_pass1",
        prompt_version="2026-05-09",
        model="gemini-2.5-flash",
        # Single attempt — caller (template_recipe shim) ignores failures and
        # proceeds with empty creative_direction. Retries here only delay Pass 2.
        max_attempts=1,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = CreativeDirectionInput
    Output = CreativeDirectionOutput

    response_json: ClassVar[bool] = False
    # 2048, not 400: GeminiClient.invoke() rewrites every gemini-* model to
    # `settings.gemini_model`. When that's set to `gemini-2.5-pro` (the prod
    # default), Gemini's thinking step silently consumes the entire budget
    # (observed 397/400 tokens spent on `thoughts_token_count`), leaving zero
    # for the visible response. finish_reason=MAX_TOKENS, candidates_token_count
    # is None, and the parser raises SchemaError("empty response") on every
    # call. 2048 leaves comfortable headroom (~1600 tokens after thinking) for
    # the ~400-token creative-direction summary without being wasteful.
    max_output_tokens: ClassVar[int | None] = 2048

    def media_uri(self, input: CreativeDirectionInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: CreativeDirectionInput) -> str:  # noqa: A002
        return input.file_mime or "video/mp4"

    def render_prompt(self, input: CreativeDirectionInput) -> str:  # noqa: A002, ARG002
        return load_prompt("analyze_template_pass1")

    def parse(self, raw_text: str, input: CreativeDirectionInput) -> CreativeDirectionOutput:  # noqa: A002, ARG002
        text = (raw_text or "").strip()
        if not text:
            raise SchemaError("creative_direction: empty response")
        return CreativeDirectionOutput(text=text)
