"""Real adapter calls over injected SDK and websocket transports, without credentials."""

import base64
import io
import json
from unittest.mock import Mock

import pytest
from PIL import Image

from app.services.generated_asset_contracts import ImageRequest, NarrationRequest
from app.services.generated_media_transport import (
    OpenAIGeneratedTransport,
    ProviderRejected,
    generated_usage_cost,
)


def png():
    out = io.BytesIO()
    Image.new("RGB", (1024, 1536)).save(out, format="PNG")
    return out.getvalue()


def image_response():
    return {
        "data": [{"b64_json": base64.b64encode(png()).decode()}],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "input_tokens_details": {"text_tokens": 10, "image_tokens": 0},
        },
    }


def test_generate_and_edit_use_sdk_without_retries():
    client = Mock()
    client.with_options.return_value = client
    client.images.generate.return_value.model_dump.return_value = image_response()
    client.images.edit.return_value.model_dump.return_value = image_response()
    transport = OpenAIGeneratedTransport(client=client)
    request = ImageRequest(prompt="Background", editable_title="Editable")
    assert transport.generate(request).editable_title == "Editable"
    assert transport.edit(request, png()).mime_type == "image/png"
    assert client.with_options.call_args.kwargs["max_retries"] == 0
    assert client.images.generate.call_args.kwargs["n"] == 1
    assert client.images.edit.call_args.kwargs["image"][0] == "reference.png"
    with pytest.raises(ValueError):
        transport.edit(request, b"gs://gemini/file")
    assert client.images.edit.call_count == 1


def test_rejected_provider_is_distinct_from_unknown_transport():
    class Rejected(Exception):
        status_code = 429

    client = Mock()
    client.with_options.return_value = client
    client.images.generate.side_effect = Rejected("private body")
    with pytest.raises(ProviderRejected) as error:
        OpenAIGeneratedTransport(client=client).generate(ImageRequest(prompt="Background"))
    assert error.value.retryable
    assert "private" not in str(error.value)


def test_realtime_uses_finite_recv_and_separate_api():
    from tests.agents.test_credit_plan_contracts import CreditPlanContracts

    fixture = CreditPlanContracts().audio_events()
    fixture[-1]["response"]["usage"].update(
        output_token_details={"text_tokens": 0, "audio_tokens": 20}
    )
    ws = Mock()
    ws.recv.side_effect = [
        '{"type":"session.updated","session":{"model":"gpt-realtime-2.1-mini","audio":{"output":{"format":{"type":"audio/pcm","rate":24000},"voice":"marin"}}}}',
        *[json.dumps(event) for event in fixture],
    ]
    manager = Mock()
    manager.__enter__ = Mock(return_value=ws)
    manager.__exit__ = Mock(return_value=False)
    connect = Mock(return_value=manager)
    transport = OpenAIGeneratedTransport(
        websocket_connect=connect, api_key="offline", organization="org-test"
    )
    result = transport.narrate(NarrationRequest(approved_script="Merhaba dünya."))
    assert result.mime_type == "audio/wav"
    assert all(call.kwargs["timeout"] > 0 for call in ws.recv.call_args_list)
    assert len(ws.send.call_args_list) == 2
    assert "gpt-realtime-2.1-mini" in connect.call_args.args[0]
    assert connect.call_args.kwargs["additional_headers"]["OpenAI-Organization"] == "org-test"


def test_usage_price_requires_complete_modality_details():
    assert generated_usage_cost(image_response()["usage"], "image") == pytest.approx(0.00065)
    usage = {
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "output_token_details": {"text_tokens": 0, "audio_tokens": 20},
    }
    assert generated_usage_cost(usage, "narration") == pytest.approx(0.000406)
    with pytest.raises(ValueError):
        generated_usage_cost({"input_tokens": 10, "output_tokens": 20}, "image")


def test_image_edit_reference_preserves_transparency():
    from app.services.generated_media_transport import validate_reference

    source = io.BytesIO()
    Image.new("RGBA", (128, 128), (20, 50, 90, 80)).save(source, format="PNG")
    _, normalized, _ = validate_reference(source.getvalue())
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.getpixel((0, 0)) == (20, 50, 90, 80)


def test_iphone_sized_reference_is_bounded_without_changing_original():
    from app.services.generated_media_transport import validate_reference

    source = io.BytesIO()
    Image.new("RGB", (4032, 3024), "red").save(source, format="JPEG")
    original = source.getvalue()
    _, normalized, _ = validate_reference(original)
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.size == (1536, 1152)
    with Image.open(io.BytesIO(original)) as image:
        assert image.size == (4032, 3024)
