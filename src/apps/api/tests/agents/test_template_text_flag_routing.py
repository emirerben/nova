"""Unit tests for TemplateTextAgent flag-gated routing (slice G).

Verifies that:
  - When `settings.text_overlay_v2_enabled=False`, the existing Layer-1
    single-Gemini-call path runs and the Layer-2 pipeline is NOT called.
  - When `settings.text_overlay_v2_enabled=True`, the Layer-2 pipeline IS
    called and the single-Gemini-call path is NOT entered.

Uses monkeypatch to flip the settings flag per test. The LLM client and the
Layer-2 pipeline are mocked so no network or filesystem access is needed.
"""

from __future__ import annotations

from unittest.mock import patch

from app.agents._schemas.template_text import (
    TemplateTextInput,
    TemplateTextOutput,
    TemplateTextOverlay,
    TextBBox,
)
from app.agents.template_text import TemplateTextAgent
from tests.agents.conftest import MockModelClient

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_input(**kwargs) -> TemplateTextInput:
    defaults = {
        "file_uri": "files/test-template",
        "file_mime": "video/mp4",
        "slot_boundaries_s": [(0.0, 3.0), (3.0, 6.0)],
    }
    defaults.update(kwargs)
    return TemplateTextInput(**defaults)


def _make_overlay(
    slot_index: int = 1,
    sample_text: str = "test overlay",
    start_s: float = 0.5,
    end_s: float = 2.0,
) -> TemplateTextOverlay:
    return TemplateTextOverlay(
        slot_index=slot_index,
        sample_text=sample_text,
        start_s=start_s,
        end_s=end_s,
        bbox=TextBBox(x_norm=0.5, y_norm=0.5, w_norm=0.4, h_norm=0.1, sample_frame_t=1.0),
        font_color_hex="#FFFFFF",
        effect="none",
        role="label",
        size_class="medium",
    )


def _valid_layer1_response() -> dict:
    """Valid JSON response the Layer-1 agent would parse from Gemini."""
    return {
        "overlays": [
            {
                "slot_index": 1,
                "sample_text": "layer1 overlay",
                "start_s": 0.5,
                "end_s": 2.0,
                "bbox": {
                    "x_norm": 0.5,
                    "y_norm": 0.5,
                    "w_norm": 0.4,
                    "h_norm": 0.1,
                    "sample_frame_t": 1.0,
                },
                "font_color_hex": "#FFFFFF",
                "effect": "none",
                "role": "label",
                "size_class": "medium",
            }
        ]
    }


# ── Flag routing tests ────────────────────────────────────────────────────────


def test_run_uses_layer1_when_flag_off(monkeypatch) -> None:
    """When flag=False, the Layer-1 single-Gemini-call path runs.

    Asserts:
    - super().run() is executed (mock_client receives an invocation)
    - _run_layer2() is NOT called
    """
    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", False)

    mock_client = MockModelClient()
    mock_client.queue("gemini-2.5-flash", _valid_layer1_response())

    agent = TemplateTextAgent(mock_client)
    inp = _make_input()

    # Track whether _run_layer2 is called.
    layer2_called = []
    original_run_layer2 = agent._run_layer2

    def _spy_layer2(input, *, ctx=None):  # noqa: A002
        layer2_called.append(True)
        return original_run_layer2(input, ctx=ctx)

    agent._run_layer2 = _spy_layer2  # type: ignore[method-assign]

    result = agent.run(inp)

    # Layer-1 path: Gemini client was invoked.
    assert len(mock_client.invocations) == 1, "Layer-1 client should have been called once"
    # Layer-2 path: must NOT have been called.
    assert layer2_called == [], "Layer-2 must NOT run when flag is False"
    # Output is a valid TemplateTextOutput.
    assert isinstance(result, TemplateTextOutput)
    assert len(result.overlays) == 1
    assert result.overlays[0].sample_text == "layer1 overlay"


def test_run_uses_layer2_when_flag_on(monkeypatch) -> None:
    """When flag=True, the Layer-2 pipeline runs.

    Asserts:
    - _run_layer2() is called with the validated input
    - The Gemini mock client receives NO invocations (Layer-1 bypassed)
    - The output comes from the Layer-2 mock, not from Gemini parsing
    """
    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", True)

    mock_client = MockModelClient()
    # Queue a Layer-1 response — if this gets consumed, the test fails.
    mock_client.queue("gemini-2.5-flash", _valid_layer1_response())

    layer2_output = TemplateTextOutput(overlays=[_make_overlay(sample_text="layer2 overlay")])

    agent = TemplateTextAgent(mock_client)
    inp = _make_input()

    # Replace _run_layer2 with a stub that returns a known output.
    layer2_calls: list[TemplateTextInput] = []

    def _stub_layer2(input, *, ctx=None):  # noqa: A002
        layer2_calls.append(input)
        return layer2_output

    agent._run_layer2 = _stub_layer2  # type: ignore[method-assign]

    result = agent.run(inp)

    # Layer-2 path ran exactly once.
    assert len(layer2_calls) == 1, "Layer-2 must run exactly once when flag is True"
    # Layer-1 path: Gemini client must NOT have been called.
    # Layer-1 Gemini client must NOT be called when flag is True.
    assert len(mock_client.invocations) == 0
    # Output is from the Layer-2 stub.
    assert isinstance(result, TemplateTextOutput)
    assert len(result.overlays) == 1
    assert result.overlays[0].sample_text == "layer2 overlay"


def test_run_layer2_passes_transcript_words_to_pipeline(monkeypatch) -> None:
    """When layer-2 is active, transcript_words from the input are forwarded."""
    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", True)

    mock_client = MockModelClient()
    agent = TemplateTextAgent(mock_client)

    transcript = [
        {"text": "hello", "start_s": 0.1, "end_s": 0.5},
        {"text": "world", "start_s": 0.6, "end_s": 1.0},
    ]
    inp = _make_input(transcript_words=transcript)

    captured_kwargs: dict = {}

    def _mock_run_full_pipeline(video_path, **kwargs):
        captured_kwargs.update(kwargs)
        return TemplateTextOutput(overlays=[])

    # The lazy imports in _run_layer2 do:
    #   from app.storage import download_to_file
    #   from app.pipeline.text_overlay_v2.pipeline import run_full_pipeline
    # Patch at their source modules so the lazy import gets our mock.
    with (
        patch("app.storage.download_to_file"),
        patch(
            "app.pipeline.text_overlay_v2.pipeline.run_full_pipeline",
            side_effect=_mock_run_full_pipeline,
        ),
    ):
        agent.run(inp)

    # transcript_words should have been parsed and forwarded.
    assert "transcript_words" in captured_kwargs
    words = captured_kwargs["transcript_words"]
    assert len(words) == 2
    assert words[0].text == "hello"
    assert words[1].text == "world"


def test_run_layer2_backward_compat_no_transcript(monkeypatch) -> None:
    """Layer-2 path works when transcript_words is not provided (default empty list)."""
    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", True)

    mock_client = MockModelClient()
    agent = TemplateTextAgent(mock_client)

    # No transcript_words — should use empty default.
    inp = _make_input()  # transcript_words defaults to []

    captured_kwargs: dict = {}

    def _mock_run_full_pipeline(video_path, **kwargs):
        captured_kwargs.update(kwargs)
        return TemplateTextOutput(overlays=[])

    with (
        patch("app.storage.download_to_file"),
        patch(
            "app.pipeline.text_overlay_v2.pipeline.run_full_pipeline",
            side_effect=_mock_run_full_pipeline,
        ),
    ):
        result = agent.run(inp)

    # Transcript words should be an empty list, not None.
    assert "transcript_words" in captured_kwargs
    assert captured_kwargs["transcript_words"] == []
    assert isinstance(result, TemplateTextOutput)


def test_layer1_unchanged_when_flag_off(monkeypatch) -> None:
    """Layer-1 parse logic is byte-for-byte identical when flag is False.

    Specifically: the timing salvage (start_s > end_s swap), slot_index
    clamping, and sample_frame_t clamping all still work via the original
    parse() method.
    """
    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", False)

    mock_client = MockModelClient()
    # Inverted timing — Layer-1 salvage should swap it.
    mock_client.queue(
        "gemini-2.5-flash",
        {
            "overlays": [
                {
                    "slot_index": 1,
                    "sample_text": "inverted timing",
                    "start_s": 3.0,  # > end_s — inverted
                    "end_s": 1.0,
                    "bbox": {
                        "x_norm": 0.5,
                        "y_norm": 0.5,
                        "w_norm": 0.4,
                        "h_norm": 0.1,
                        "sample_frame_t": 5.0,  # outside [1.0, 3.0] — will be clamped
                    },
                    "font_color_hex": "#FFFFFF",
                    "effect": "none",
                    "role": "label",
                    "size_class": "medium",
                }
            ]
        },
    )

    agent = TemplateTextAgent(mock_client)
    inp = _make_input()

    result = agent.run(inp)

    # Timing salvage ran: start/end are swapped.
    assert len(result.overlays) == 1
    ov = result.overlays[0]
    assert ov.start_s == 1.0  # salvaged
    assert ov.end_s == 3.0  # salvaged
    # sample_frame_t clamped into [1.0, 3.0]
    assert 1.0 <= ov.bbox.sample_frame_t <= 3.0
