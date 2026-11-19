"""Provider boundary failures specified before adapter implementation; no network."""

import json

import httpx
import pytest

from app.agents._runtime import (
    ProviderDeadlineExceededError,
    ProviderOutcomeUnknownError,
    TerminalError,
    TransientError,
)
from app.agents._text_providers import AnthropicTextClient, OpenAITextClient
from app.services.ai_cost_control import UsageMeter, calculate_cost_usd


def test_sol_wire_and_reasoning_accounting():
    from openai import OpenAI

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_contract",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-6.1-sol",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": '{"ok":true}', "annotations": []}
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "total_tokens": 1200,
                    "input_tokens_details": {"cached_tokens": 100},
                    "output_tokens_details": {"reasoning_tokens": 80},
                },
            },
        )

    sdk = OpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    result = OpenAITextClient(client=sdk).invoke(
        model="gpt-6.1-sol", prompt="JSON please", thinking_level="high", max_output_tokens=1024
    )
    assert requests[0]["store"] is False
    assert requests[0]["reasoning"] == {"effort": "high"}
    assert "temperature" not in requests[0]
    assert result.tokens_out == 120 and result.tokens_thoughts == 80
    assert result.tokens_cached == 100 and result.finish_status == "completed"
    assert calculate_cost_usd(
        "gpt-6.1-sol",
        UsageMeter(tokens_in=1000, tokens_cached=100, tokens_out=120, tokens_thoughts=80),
    ) == pytest.approx(0.00381)


@pytest.mark.parametrize(
    "provider,model", [("openai", "gpt-6.1-sol"), ("anthropic", "claude-sonnet-5-5")]
)
@pytest.mark.parametrize(
    "failure,expected",
    [
        # A provider status is the provider's decision: nothing billed, retry or fall back.
        ("server", TransientError),
        # The connection never opened: nothing sent, so nothing billed.
        ("refused", TransientError),
        # Our deadline ran out while the provider worked: may bill, the deadline ladder decides.
        ("timeout", ProviderDeadlineExceededError),
        # The request went out and the answer broke off: may bill, never repeated as transient.
        ("broken", ProviderOutcomeUnknownError),
    ],
)
def test_provider_failures_are_classified_and_never_sdk_retried(provider, model, failure, expected):
    import anthropic
    import openai

    calls = []

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private payload must not appear", request=request)
        if failure == "refused":
            raise httpx.ConnectError("private payload must not appear", request=request)
        if failure == "broken":
            raise httpx.RemoteProtocolError("private payload must not appear", request=request)
        return httpx.Response(500, json={"error": {"type": "api_error", "message": "private"}})

    http = httpx.Client(transport=httpx.MockTransport(respond))
    if provider == "openai":
        client = OpenAITextClient(
            client=openai.OpenAI(api_key="test", max_retries=0, http_client=http)
        )
    else:
        client = AnthropicTextClient(
            client=anthropic.Anthropic(api_key="test", max_retries=0, http_client=http)
        )
    with pytest.raises(expected) as raised:
        client.invoke(model=model, prompt="JSON", max_output_tokens=1000, thinking_level="high")
    assert type(raised.value) is expected
    assert "private" not in str(raised.value)
    assert len(calls) == 1


def test_sonnet_usage_and_only_visible_text():
    from anthropic import Anthropic

    def respond(request):
        wire = json.loads(request.content)
        assert wire["thinking"] == {"type": "adaptive"}
        assert wire["output_config"]["effort"] == "high"
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "content": [
                    {"type": "thinking", "thinking": "never retain this", "signature": "secret"},
                    {"type": "text", "text": '{"ok":true}'},
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 200,
                    "cache_creation_input_tokens": 300,
                },
            },
        )

    sdk = Anthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    result = AnthropicTextClient(client=sdk).invoke(
        model="claude-sonnet-5-5", prompt="JSON", thinking_level="high", max_output_tokens=1024
    )
    assert result.raw_text == '{"ok":true}' and result.raw_response is None
    assert result.tokens_in == 600 and result.tokens_cached == 200
    assert calculate_cost_usd(
        "claude-sonnet-5-5",
        UsageMeter(
            tokens_in=600, tokens_cached=200, tokens_out=50, token_details=result.token_details
        ),
    ) == pytest.approx(0.00147)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"media_uri": "gs://private/video"},
        {"thinking_level": "minimal"},
        {"thinking_level": "max"},
        {"thinking_budget": 100},
    ],
)
def test_unsupported_inputs_fail_without_sdk(kwargs, monkeypatch):
    monkeypatch.setattr(
        OpenAITextClient, "_get", lambda _: pytest.fail("SDK construction forbidden")
    )
    with pytest.raises(TerminalError):
        OpenAITextClient().invoke(model="gpt-6.1-sol", prompt="JSON", **kwargs)


def test_gemini_sdk_retries_cannot_bypass_runtime_accounting(monkeypatch):
    from google import genai
    from google.genai import types

    from app.agents._model_client import GeminiClient
    from app.agents._runtime import TransientError

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            500, json={"error": {"code": 500, "message": "failure", "status": "INTERNAL"}}
        )

    sdk = genai.Client(
        api_key="test",
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=2, initial_delay=0.001, max_delay=0.001),
            client_args={"transport": httpx.MockTransport(respond)},
        ),
    )
    client = GeminiClient()
    monkeypatch.setattr(client, "_get", lambda: sdk)
    with pytest.raises(TransientError):
        client.invoke(model="gemini-3.1-pro-preview", prompt="JSON please", timeout_s=5)
    assert len(calls) == 1


def test_gemini_transport_has_the_same_deadline_as_runtime(monkeypatch):
    from google import genai
    from google.genai import types

    from app.agents._model_client import GeminiClient

    captured = []

    def respond(request):
        captured.append(request.extensions.get("timeout"))
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            },
        )

    sdk = genai.Client(
        api_key="test",
        http_options=types.HttpOptions(client_args={"transport": httpx.MockTransport(respond)}),
    )
    client = GeminiClient()
    monkeypatch.setattr(client, "_get", lambda: sdk)
    client.invoke(model="gemini-3.1-pro-preview", prompt="JSON", timeout_s=7)
    assert captured[0]["read"] == 7.0


@pytest.mark.parametrize("model_version", [None, "", 42, {"unexpected": "value"}])
def test_gemini_malformed_model_metadata_keeps_requested_cost_identity(monkeypatch, model_version):
    """Optional provider metadata cannot replace a priceable requested identity with an object."""
    from unittest.mock import MagicMock

    from app.agents._model_client import GeminiClient

    response = MagicMock(text="{}", usage_metadata=None, model_version=model_version)
    client = GeminiClient()
    sdk = MagicMock()
    sdk.models.generate_content.return_value = response
    monkeypatch.setattr(client, "_get", lambda: sdk)
    result = client.invoke(model="gemini-2.5-flash", prompt="JSON")
    assert result.model_used == "gemini-2.5-flash"
    assert result.provider_request_id is None
    assert calculate_cost_usd(result.model_used, UsageMeter(tokens_in=100)) > 0


def test_gemini_missing_optional_metadata_on_legacy_sdk_double(monkeypatch):
    from unittest.mock import MagicMock

    from app.agents._model_client import GeminiClient

    sdk = MagicMock()
    sdk.models.generate_content.return_value = MagicMock(text="{}", usage_metadata=None)
    client = GeminiClient()
    monkeypatch.setattr(client, "_get", lambda: sdk)
    result = client.invoke(model="gemini-2.5-flash", prompt="JSON")
    assert result.model_used == "gemini-2.5-flash"
    assert result.provider_request_id is None


def test_gemini_valid_provider_identity_is_preserved_even_when_unpriced(monkeypatch):
    from types import SimpleNamespace

    from app.agents._model_client import GeminiClient
    from app.agents._runtime import AiCostControlPolicyError

    response = SimpleNamespace(
        text="{}",
        usage_metadata=None,
        model_version="unknown-provider-model",
        response_id="req_real",
    )
    client = GeminiClient()
    monkeypatch.setattr(
        client,
        "_get",
        lambda: SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_: response)),
    )
    result = client.invoke(model="gemini-2.5-flash", prompt="JSON")
    assert result.model_used == "unknown-provider-model"
    assert result.provider_request_id == "req_real"
    with pytest.raises(AiCostControlPolicyError, match="unpriced_model"):
        calculate_cost_usd(result.model_used, UsageMeter(tokens_in=100))


# KRI-1385: the thinking row on OpenAI and Anthropic. Ways it can fail, written before the
# tests:
# 1. Summary text reaches `raw_text`, or answer text reaches the creator's thinking row.
# 2. Streaming loses the final usage, so the call settles at $0.
# 3. A failing thinking-row write fails the model call.
# 4. A stream that keeps sending chunks outlives the turn deadline.
# 5. Anthropic is never asked for a summary, so the row stays empty ("omitted" default).


def _sse(events):
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


_SOL_RESPONSE = {
    "id": "resp_stream",
    "object": "response",
    "created_at": 1,
    "status": "completed",
    "model": "gpt-6.1-sol",
    "output": [
        {"id": "rs_1", "type": "reasoning", "summary": []},
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": '{"ok":true}', "annotations": []}],
        },
    ],
    "usage": {
        "input_tokens": 1000,
        "output_tokens": 200,
        "total_tokens": 1200,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 80},
    },
}


def _sol_stream_events():
    base = {"item_id": "rs_1", "output_index": 0}
    return [
        {"type": "response.reasoning_summary_part.added", "summary_index": 0, **base},
        {
            "type": "response.reasoning_summary_text.delta",
            "summary_index": 0,
            "delta": "Pick",
            **base,
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "summary_index": 0,
            "delta": "ing clips",
            **base,
        },
        {"type": "response.reasoning_summary_part.added", "summary_index": 1, **base},
        {
            "type": "response.reasoning_summary_text.delta",
            "summary_index": 1,
            "delta": "Ordering",
            **base,
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 1,
            "content_index": 0,
            "delta": '{"ok":true}',
        },
        {"type": "response.completed", "response": _SOL_RESPONSE},
    ]


def test_sol_streams_its_reasoning_summary_to_the_thinking_row_only():
    from openai import OpenAI

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(_sol_stream_events()),
        )

    sdk = OpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    chunks = []
    result = OpenAITextClient(client=sdk).invoke(
        model="gpt-6.1-sol",
        prompt="JSON please",
        thinking_level="medium",
        thought_summary_callback=chunks.append,
    )
    assert requests[0]["stream"] is True
    assert requests[0]["reasoning"] == {"effort": "medium", "summary": "concise"}
    assert "".join(chunks) == "Picking clips\n\nOrdering"
    assert result.raw_text == '{"ok":true}' and result.finish_status == "completed"
    assert result.tokens_thoughts == 80 and result.tokens_out == 120
    assert calculate_cost_usd(
        result.model_used,
        UsageMeter(tokens_in=result.tokens_in, tokens_out=120, tokens_thoughts=80),
    ) == pytest.approx(0.004)


def test_a_failing_thinking_row_never_fails_the_call():
    from openai import OpenAI

    def respond(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(_sol_stream_events()),
        )

    def broken(_chunk):
        raise RuntimeError("thought store down")

    sdk = OpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    result = OpenAITextClient(client=sdk).invoke(
        model="gpt-6.1-sol", prompt="JSON", thinking_level="low", thought_summary_callback=broken
    )
    assert result.raw_text == '{"ok":true}'


def test_a_stream_that_keeps_talking_ends_at_the_turn_deadline():
    import time

    from openai import OpenAI

    def endless():
        event = _sol_stream_events()[1]
        while True:
            time.sleep(0.05)
            yield _sse([event])

    def respond(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=endless())

    sdk = OpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    started = time.monotonic()
    with pytest.raises(ProviderDeadlineExceededError):
        OpenAITextClient(client=sdk).invoke(
            model="gpt-6.1-sol",
            prompt="JSON",
            thinking_level="medium",
            timeout_s=1.0,
            thought_summary_callback=lambda _chunk: None,
        )
    assert time.monotonic() - started < 2.0


def test_sonnet_streams_a_requested_thinking_summary():
    from anthropic import Anthropic

    requests = []
    message = {
        "id": "msg_stream",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 1},
    }
    events = [
        {"type": "message_start", "message": message},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Weighing the clips"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "thinking_delta", "thinking": "Choosing a hook"},
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "text_delta", "text": '{"ok":true}'},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 50},
        },
        {"type": "message_stop"},
    ]

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_sse(events)
        )

    sdk = Anthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    chunks = []
    result = AnthropicTextClient(client=sdk).invoke(
        model="claude-sonnet-5-5",
        prompt="JSON",
        thinking_level="high",
        thought_summary_callback=chunks.append,
    )
    assert requests[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "".join(chunks) == "Weighing the clips\n\nChoosing a hook"
    assert result.raw_text == '{"ok":true}' and result.finish_status == "completed"
    assert result.tokens_in == 100 and result.tokens_out == 50
