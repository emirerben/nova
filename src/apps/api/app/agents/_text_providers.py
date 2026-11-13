"""Native text APIs. No SDK retries, no provider fallbacks, no hidden reasoning storage.

Models, providers and reasoning efforts come from ``model_registry``. Media requires an
explicit future contract; a Gemini URI must never be sent to another provider. A caller that
passes ``thought_summary_callback`` gets the provider's reasoning summary streamed to it; the
summary never enters ``raw_text``.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Event
from typing import Any

import httpx
import structlog

from app.agents._runtime import (
    ModelInvocation,
    ProviderDeadlineExceededError,
    ProviderOutcomeUnknownError,
    TerminalError,
    TransientError,
)
from app.agents.model_registry import Provider, exact_spec
from app.config import settings

log = structlog.get_logger()
_STREAM_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="text-stream")
# Raised before any byte of the request left this process.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _preflight(model, provider: Provider, media_uri, media_mime, thinking_budget, thinking_level):
    spec = exact_spec(model)
    if spec is None or spec.provider != provider:
        raise TerminalError("unsupported text model")
    if ((media_uri or media_mime) and not spec.supports_media) or thinking_budget is not None:
        raise TerminalError("text provider does not support media or thinking_budget")
    if thinking_level is not None and thinking_level not in spec.efforts:
        raise TerminalError("unsupported reasoning effort")


def _causes(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    link: BaseException | None = exc
    while link is not None and all(link is not seen for seen in chain):
        chain.append(link)
        link = link.__cause__ or link.__context__
    return chain


def _translate(exc: Exception) -> Exception:
    # A provider response with a status is the provider's decision: a 5xx or 429 bills nothing
    # and may be retried, another 4xx is final. Without one, only a connection that never
    # opened is safe to retry. A read that ran out is our own deadline; any other failure after
    # the request was sent may follow accepted, billable work. Never include SDK bodies.
    status = getattr(exc, "status_code", None)
    if status == 429 or (isinstance(status, int) and status >= 500):
        return TransientError(f"text provider unavailable (HTTP {status})")
    if status is not None and 400 <= status < 500 and status != 408:
        return TerminalError(f"text provider rejected request (HTTP {status})")
    chain = _causes(exc)
    if any(isinstance(link, _NEVER_SENT) for link in chain):
        return TransientError("text provider unreachable")
    if any(isinstance(link, httpx.TimeoutException) for link in chain):
        return ProviderDeadlineExceededError("text provider outcome unknown after its deadline")
    return ProviderOutcomeUnknownError("text provider outcome unknown")


def _publish(callback: Callable[[str], None], text: str) -> None:
    if not text:
        return
    try:
        callback(text)
    except Exception:  # noqa: BLE001 - display delivery is optional
        log.warning("text_thought_summary_publish_failed")


def _stream_within(timeout_s: float, consume: Callable[[Event], Any]) -> Any:
    """Run a streamed call against one total deadline, as the Gemini client does.

    The SDK timeout bounds each read, not the stream, so a model that keeps sending summary
    chunks could otherwise outlive the turn. On the deadline the worker stops at its next
    event; anything it publishes meanwhile lands on a fenced publisher attempt and is dropped.
    """
    stop = Event()
    future = _STREAM_POOL.submit(consume, stop)
    try:
        return future.result(timeout=max(0.1, timeout_s))
    except FutureTimeoutError as exc:
        stop.set()
        if future.cancel():
            raise TransientError(f"text stream cancelled before start after {timeout_s:.1f}s")
        raise ProviderDeadlineExceededError(
            f"text provider outcome unknown after {timeout_s:.1f}s"
        ) from exc


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
        _preflight(model, "openai", media_uri, media_mime, thinking_budget, thinking_level)
        reasoning = {"effort": thinking_level or "medium"}
        if thought_summary_callback is not None:
            # Sol returned no summary parts for "auto" or "detailed" in 4 of 4 Main Creator calls
            # and returned them for "concise" in 2 of 2 (docs/reviews/kri-1384/thinking-row.md).
            reasoning["summary"] = "concise"
        kwargs = dict(
            model=model,
            input=prompt,
            store=False,
            reasoning=reasoning,
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

        def consume(stop: Event):
            final = None
            with client.responses.create(**kwargs, stream=True) as stream:
                for event in stream:
                    if stop.is_set():
                        return None
                    kind = getattr(event, "type", "")
                    if kind == "response.reasoning_summary_text.delta":
                        _publish(thought_summary_callback, event.delta)
                    elif kind == "response.reasoning_summary_part.added" and event.summary_index:
                        _publish(thought_summary_callback, "\n\n")
                    elif kind in ("response.completed", "response.incomplete", "response.failed"):
                        final = event.response
            if final is None:
                raise ValueError("stream ended without a response")
            return final

        try:
            response = (
                client.responses.create(**kwargs)
                if thought_summary_callback is None
                else _stream_within(timeout_s, consume)
            )
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
                for c in getattr(item, "content", None) or []
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
        except (TransientError, TerminalError):
            raise
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
        _preflight(model, "anthropic", media_uri, media_mime, thinking_budget, thinking_level)
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

        def consume(stop: Event):
            # Thinking text is empty unless a summary is asked for (the "omitted" default).
            summarized = {**kwargs, "thinking": {"type": "adaptive", "display": "summarized"}}
            blocks = 0
            with client.messages.stream(**summarized) as stream:
                for event in stream:
                    if stop.is_set():
                        return None
                    if event.type == "content_block_start" and (
                        event.content_block.type == "thinking"
                    ):
                        if blocks:
                            _publish(thought_summary_callback, "\n\n")
                        blocks += 1
                    elif event.type == "content_block_delta" and (
                        event.delta.type == "thinking_delta"
                    ):
                        _publish(thought_summary_callback, event.delta.thinking)
                return stream.get_final_message()

        try:
            response = (
                client.messages.create(**kwargs)
                if thought_summary_callback is None
                else _stream_within(timeout_s, consume)
            )
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
        except (TransientError, TerminalError):
            raise
        except Exception as exc:
            raise _translate(exc) from exc
