"""Native text APIs. No SDK retries, no provider fallbacks, no hidden reasoning storage.

Only the two priced experiment models are enabled. Media requires an explicit
future contract; a Gemini URI must never be sent to another provider.
"""

from __future__ import annotations

from typing import Any

from app.agents._runtime import (
    ModelInvocation,
    ProviderOutcomeUnknownError,
    TerminalError,
    TransientError,
)
from app.config import settings

TEXT_MODELS = {"gpt-6.1-sol": "openai", "claude-sonnet-5-5": "anthropic"}
TEXT_EFFORTS = {
    "gpt-6.1-sol": ("low", "medium", "high", "xhigh"),
    "claude-sonnet-5-5": ("low", "medium", "high", "xhigh", "max"),
}


def paid_provider(model: str) -> str | None:
    return "google" if model.startswith("gemini") else TEXT_MODELS.get(model)


def _preflight(model, expected, media_uri, media_mime, thinking_budget, thinking_level):
    if model != expected:
        raise TerminalError("unsupported text model")
    if media_uri or media_mime or thinking_budget is not None:
        raise TerminalError("text provider does not support media or thinking_budget")
    if thinking_level is not None and thinking_level not in TEXT_EFFORTS[model]:
        raise TerminalError("unsupported reasoning effort")


def _translate(exc: Exception) -> Exception:
    # A 5xx or transport failure can follow accepted, billable work. Only a
    # concrete rejection is safe to release/retry. Never include SDK bodies.
    status = getattr(exc, "status_code", None)
    if status == 429:
        return TransientError("text provider rate limited")
    if status is not None and 400 <= status < 500 and status != 408:
        return TerminalError(f"text provider rejected request (HTTP {status})")
    return ProviderOutcomeUnknownError("text provider outcome unknown")


class OpenAITextClient:
    def __init__(self, *, client: Any = None):
        self._client = client

    def _get(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=settings.openai_api_key, max_retries=0)
        return self._client

    def invoke(
        self,
        *,
        model,
        prompt,
        media_uri=None,
        media_mime=None,
        response_json=True,
        max_output_tokens=None,
        thinking_budget=None,
        thinking_level=None,
        timeout_s=30.0,
        response_schema=None,
        thought_summary_callback=None,
    ):
        _preflight(model, "gpt-6.1-sol", media_uri, media_mime, thinking_budget, thinking_level)
        kwargs = dict(
            model=model,
            input=prompt,
            store=False,
            reasoning={"effort": thinking_level or "medium"},
            max_output_tokens=max_output_tokens or 8192,
            timeout=timeout_s,
        )
        if response_schema is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "agent_output",
                    "strict": True,
                    "schema": response_schema,
                }
            }
        elif response_json:
            kwargs["text"] = {"format": {"type": "json_object"}}
        client = self._get().with_options(max_retries=0)
        try:
            response = client.responses.create(**kwargs)
            usage = response.usage
            if usage is None:
                raise ValueError("missing usage")
            details = getattr(usage, "output_tokens_details", None)
            thoughts = int(getattr(details, "reasoning_tokens", 0) or 0)
            cached = int(
                getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
            )
            refused = any(
                getattr(c, "type", "") == "refusal"
                for item in response.output
                for c in getattr(item, "content", [])
            )
            reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
            status = (
                "refused"
                if refused or reason == "content_filter"
                else "truncated"
                if reason == "max_output_tokens"
                else "completed"
                if response.status == "completed"
                else "incomplete"
            )
            return ModelInvocation(
                raw_text=response.output_text,
                tokens_in=usage.input_tokens,
                tokens_out=max(0, usage.output_tokens - thoughts),
                tokens_thoughts=thoughts,
                tokens_cached=cached,
                model_used=response.model,
                provider_request_id=response.id,
                finish_status=status,
                token_details={"output_tokens_inclusive": usage.output_tokens},
            )
        except Exception as exc:
            raise _translate(exc) from exc


class AnthropicTextClient:
    def __init__(self, *, client: Any = None):
        self._client = client

    def _get(self):
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=settings.anthropic_api_key, max_retries=0)
        return self._client

    def invoke(
        self,
        *,
        model,
        prompt,
        media_uri=None,
        media_mime=None,
        response_json=True,
        max_output_tokens=None,
        thinking_budget=None,
        thinking_level=None,
        timeout_s=30.0,
        response_schema=None,
        thought_summary_callback=None,
    ):
        _preflight(
            model, "claude-sonnet-5-5", media_uri, media_mime, thinking_budget, thinking_level
        )
        config = {"effort": thinking_level or "high"}
        if response_schema is not None:
            config["format"] = {"type": "json_schema", "schema": response_schema}
        # Anthropic has no JSON-object mode. Without a native schema this uses
        # the unchanged agent prompt and MUST be reported as prompt_json.
        kwargs = dict(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_output_tokens or 8192,
            timeout=timeout_s,
            thinking={"type": "adaptive"},
            output_config=config,
        )
        client = self._get().with_options(max_retries=0)
        try:
            response = client.messages.create(**kwargs)
            usage = response.usage
            writes = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            reads = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            cache = getattr(usage, "cache_creation", None)
            hour = int(getattr(cache, "ephemeral_1h_input_tokens", 0) or 0)
            details = getattr(usage, "output_tokens_details", None)
            thoughts = int(getattr(details, "thinking_tokens", 0) or 0)
            reason = response.stop_reason
            status = (
                "truncated"
                if reason == "max_tokens"
                else "refused"
                if reason == "refusal"
                else "completed"
                if reason in ("end_turn", "stop_sequence")
                else "incomplete"
            )
            return ModelInvocation(
                raw_text="".join(block.text for block in response.content if block.type == "text"),
                tokens_in=usage.input_tokens + writes + reads,
                tokens_out=max(0, usage.output_tokens - thoughts),
                tokens_thoughts=thoughts,
                tokens_cached=reads,
                model_used=response.model,
                provider_request_id=response.id,
                finish_status=status,
                token_details={
                    "cache_creation_input_tokens": writes,
                    "cache_creation_1h_input_tokens": hour,
                    "output_tokens_inclusive": usage.output_tokens,
                },
            )
        except Exception as exc:
            raise _translate(exc) from exc
