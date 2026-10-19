"""Provider boundary failures specified before adapter implementation; no network."""

import json

import httpx
import pytest

from app.agents._runtime import ProviderOutcomeUnknownError, TerminalError
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
@pytest.mark.parametrize("failure", ["timeout", "server"])
def test_accepted_unknown_never_sdk_retried(provider, model, failure):
    import anthropic
    import openai

    calls = []

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private payload must not appear", request=request)
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
    with pytest.raises(ProviderOutcomeUnknownError, match="outcome unknown"):
        client.invoke(model=model, prompt="JSON", max_output_tokens=1000, thinking_level="high")
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
    [{"media_uri": "gs://private/video"}, {"thinking_level": "minimal"}, {"thinking_budget": 100}],
)
def test_unsupported_inputs_fail_without_sdk(kwargs):
    with pytest.raises(TerminalError):
        OpenAITextClient().invoke(model="gpt-6.1-sol", prompt="JSON", **kwargs)
