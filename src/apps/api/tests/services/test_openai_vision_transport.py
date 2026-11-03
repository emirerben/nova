"""Failure matrix rows 4 (text adapter keeps rejecting media) and 7/8 (error classes).

The only fake is the OpenAI SDK object; the transport, error translation and the text
adapter run for real. Nothing here reaches the network.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.agents._model_client import ModelDispatcher
from app.agents._openai_vision import OpenAIVisionClient
from app.agents._runtime import ProviderOutcomeUnknownError, TerminalError, TransientError
from app.agents._text_providers import OpenAITextClient

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class HttpError(Exception):
    def __init__(self, status_code=None):
        super().__init__("provider failure")
        self.status_code = status_code


def response(*, status="completed", reason=None, refusal=False, text='{"findings":[]}'):
    content = [SimpleNamespace(type="refusal")] if refusal else []
    return SimpleNamespace(
        id="resp_1",
        status=status,
        model="gpt-6.1-sol",
        output_text=text,
        output=[SimpleNamespace(content=content)],
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        usage=SimpleNamespace(
            input_tokens=9100,
            output_tokens=700,
            output_tokens_details=SimpleNamespace(reasoning_tokens=400),
            input_tokens_details=SimpleNamespace(cached_tokens=32),
        ),
    )


class Sdk:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []
        self.options = []
        self.responses = SimpleNamespace(create=self._create)

    def with_options(self, **options):
        self.options.append(options)
        return self

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def test_request_carries_inline_frames_and_is_unretried_and_unstored():
    sdk = Sdk(response())
    result = OpenAIVisionClient(client=sdk).invoke_images("review", [JPEG, JPEG], SCHEMA)
    (call,) = sdk.calls
    assert sdk.options == [{"max_retries": 0}]
    assert call["model"] == "gpt-6.1-sol"
    assert call["store"] is False
    assert call["reasoning"] == {"effort": "low"}
    assert call["max_output_tokens"] == 2500
    assert call["timeout"] == 60.0
    assert call["text"]["format"] == {
        "type": "json_schema",
        "name": "intent_review",
        "strict": True,
        "schema": SCHEMA,
    }
    (message,) = call["input"]
    assert message["role"] == "user"
    parts = message["content"]
    assert parts[0] == {"type": "input_text", "text": "review"}
    images = parts[1:]
    assert len(images) == 2
    assert all(
        p["type"] == "input_image"
        and p["image_url"].startswith("data:image/jpeg;base64,")
        and p["detail"] == "high"
        for p in images
    )
    assert result.status == "completed"
    assert result.response_id == "resp_1"
    assert (
        result.usage.input_tokens,
        result.usage.output_tokens,
        result.usage.reasoning_tokens,
        result.usage.cached_tokens,
    ) == (9100, 700, 400, 32)


@pytest.mark.parametrize(
    "frames",
    [
        [],
        [JPEG] * 7,
        [b"GIF89a" + b"\x00" * 10],
        [JPEG + b"\x00" * (128 * 1024)],
        ["https://generativelanguage.googleapis.com/v1beta/files/abc"],
    ],
    ids=["none", "seven", "not-jpeg", "oversize", "gemini-uri-string"],
)
def test_bad_frames_never_reach_the_sdk(frames):
    sdk = Sdk(response())
    with pytest.raises(TerminalError):
        OpenAIVisionClient(client=sdk).invoke_images("review", frames, SCHEMA)
    assert sdk.calls == []


@pytest.mark.parametrize(
    "failure,expected",
    [
        (HttpError(500), ProviderOutcomeUnknownError),
        (HttpError(None), ProviderOutcomeUnknownError),
        (TimeoutError("read timed out"), ProviderOutcomeUnknownError),
        (HttpError(400), TerminalError),
        (HttpError(422), TerminalError),
        (HttpError(429), TransientError),
    ],
)
def test_provider_errors_classify_as_unknown_or_rejected(failure, expected):
    with pytest.raises(expected):
        OpenAIVisionClient(client=Sdk(failure)).invoke_images("review", [JPEG], SCHEMA)


def test_a_success_without_usage_is_unknown_not_free():
    broken = response()
    broken.usage = None
    with pytest.raises(ProviderOutcomeUnknownError):
        OpenAIVisionClient(client=Sdk(broken)).invoke_images("review", [JPEG], SCHEMA)


@pytest.mark.parametrize(
    "kwargs,status",
    [
        ({}, "completed"),
        ({"status": "incomplete", "reason": "max_output_tokens"}, "truncated"),
        ({"status": "incomplete"}, "incomplete"),
        ({"refusal": True}, "refused"),
        ({"status": "incomplete", "reason": "content_filter"}, "refused"),
    ],
)
def test_response_status_distinguishes_unusable_outcomes(kwargs, status):
    result = OpenAIVisionClient(client=Sdk(response(**kwargs))).invoke_images(
        "review", [JPEG], SCHEMA
    )
    assert result.status == status


@pytest.mark.parametrize(
    "media",
    [
        {"media_uri": "https://generativelanguage.googleapis.com/v1beta/files/abc"},
        {"media_mime": "image/jpeg"},
    ],
)
def test_text_adapter_still_rejects_media(media):
    sdk = Sdk(response())
    with pytest.raises(TerminalError):
        OpenAITextClient(client=sdk).invoke(model="gpt-6.1-sol", prompt="hi", **media)
    assert sdk.calls == []


def test_dispatcher_never_forwards_media_to_openai():
    sdk = Sdk(response())
    dispatcher = ModelDispatcher(
        gemini=MagicMock(), whisper=MagicMock(), openai=OpenAITextClient(client=sdk)
    )
    with pytest.raises(TerminalError):
        dispatcher.invoke(
            model="gpt-6.1-sol",
            prompt="hi",
            media_uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
            media_mime="image/jpeg",
        )
    assert sdk.calls == []
