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

import pytest

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
    inp = _make_input(transcript_words=transcript, gcs_path="templates/test/video.mp4")

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

    # No transcript_words — should use empty default. gcs_path required for Layer-2.
    inp = _make_input(gcs_path="templates/test/video.mp4")  # transcript_words defaults to []

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


# ── Per-request Layer-2 override tests (force_layer2 field) ──────────────────


def test_run_uses_layer2_when_input_force_layer2_true_even_if_flag_off(
    monkeypatch,
) -> None:
    """force_layer2=True on input routes to Layer-2 even when the global flag is False.

    Asserts:
    - _run_layer2() is called
    - The Gemini client receives no invocations (Layer-1 bypassed)
    - A structured log event ``text_overlay_layer2_forced_via_request`` is emitted
    """
    from unittest.mock import patch  # noqa: PLC0415

    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", False)

    mock_client = MockModelClient()
    # Queue a Layer-1 response — if consumed, the test fails.
    mock_client.queue("gemini-2.5-flash", _valid_layer1_response())

    layer2_output = TemplateTextOutput(
        overlays=[_make_overlay(sample_text="layer2 forced overlay")]
    )

    agent = TemplateTextAgent(mock_client)
    inp = _make_input(force_layer2=True)

    layer2_calls: list[TemplateTextInput] = []

    def _stub_layer2(input, *, ctx=None):  # noqa: A002
        layer2_calls.append(input)
        return layer2_output

    agent._run_layer2 = _stub_layer2  # type: ignore[method-assign]

    log_events: list[str] = []

    import app.agents.template_text as _mod  # noqa: PLC0415

    with patch.object(_mod.log, "info", side_effect=lambda event, **kw: log_events.append(event)):
        result = agent.run(inp)

    # Layer-2 ran.
    assert len(layer2_calls) == 1
    # Layer-1 Gemini client NOT called.
    assert len(mock_client.invocations) == 0
    # Output came from Layer-2 stub.
    assert isinstance(result, TemplateTextOutput)
    assert result.overlays[0].sample_text == "layer2 forced overlay"
    # Audit log event was emitted.
    assert "text_overlay_layer2_forced_via_request" in log_events, (
        f"Expected structured log event text_overlay_layer2_forced_via_request; got {log_events}"
    )


def test_run_uses_layer2_when_flag_on_regardless_of_input_force_layer2(
    monkeypatch,
) -> None:
    """Global flag=True routes to Layer-2 even when input.force_layer2=False.

    The override is additive (OR logic): the flag alone is sufficient.
    No duplicate log event should fire (override path only logs when the
    global flag is off).
    """
    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", True)

    mock_client = MockModelClient()
    mock_client.queue("gemini-2.5-flash", _valid_layer1_response())

    layer2_output = TemplateTextOutput(overlays=[_make_overlay(sample_text="layer2 flag overlay")])

    agent = TemplateTextAgent(mock_client)
    # force_layer2 is False — global flag alone should trigger Layer-2.
    inp = _make_input(force_layer2=False)

    layer2_calls: list[TemplateTextInput] = []

    def _stub_layer2(input, *, ctx=None):  # noqa: A002
        layer2_calls.append(input)
        return layer2_output

    agent._run_layer2 = _stub_layer2  # type: ignore[method-assign]

    result = agent.run(inp)

    assert len(layer2_calls) == 1
    assert len(mock_client.invocations) == 0
    assert result.overlays[0].sample_text == "layer2 flag overlay"


def test_run_uses_layer1_when_flag_off_and_input_force_layer2_false(
    monkeypatch,
) -> None:
    """flag=False AND force_layer2=False → Layer-1 path (existing behavior).

    This is a regression guard: the override must NOT accidentally activate
    Layer-2 when both the flag and the per-request override are False.
    """
    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", False)

    mock_client = MockModelClient()
    mock_client.queue("gemini-2.5-flash", _valid_layer1_response())

    agent = TemplateTextAgent(mock_client)
    inp = _make_input(force_layer2=False)

    layer2_called = []
    original_run_layer2 = agent._run_layer2

    def _spy_layer2(input, *, ctx=None):  # noqa: A002
        layer2_called.append(True)
        return original_run_layer2(input, ctx=ctx)

    agent._run_layer2 = _spy_layer2  # type: ignore[method-assign]

    result = agent.run(inp)

    # Layer-1 was used.
    assert len(mock_client.invocations) == 1
    assert layer2_called == []
    assert isinstance(result, TemplateTextOutput)
    assert result.overlays[0].sample_text == "layer1 overlay"


# ── GCS path fix tests (canary 404, 2026-05-17) ───────────────────────────────


def test_run_layer2_downloads_gcs_and_passes_local_path(monkeypatch) -> None:
    """_run_layer2 must call download_to_file with gcs_path (not file_uri) and
    pass the resulting local path to run_full_pipeline.

    Root cause of the canary 404: download_to_file received the Gemini File API
    URI (file_uri) which the GCS client URL-encoded and used as an object key,
    producing "No such object: nova-videos-dev/https://generativelanguage..."
    The fix: _run_layer2 now reads input.gcs_path, not input.file_uri.
    """
    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", True)

    mock_client = MockModelClient()
    agent = TemplateTextAgent(mock_client)
    inp = _make_input(
        file_uri="https://generativelanguage.googleapis.com/v1beta/files/cd614j1u56br",
        gcs_path="templates/fdaf3bbc/source.mp4",
        force_layer2=True,
    )

    downloaded_paths: list[str] = []
    pipeline_video_paths: list[object] = []

    def _mock_download(object_path: str, local_path: str) -> None:
        downloaded_paths.append(object_path)
        # Write a sentinel so the tempdir cleanup can find the file.
        import pathlib  # noqa: PLC0415

        pathlib.Path(local_path).write_bytes(b"")

    def _mock_run_full_pipeline(video_path, **_kwargs):
        pipeline_video_paths.append(video_path)
        return TemplateTextOutput(overlays=[])

    with (
        patch("app.storage.download_to_file", side_effect=_mock_download),
        patch(
            "app.pipeline.text_overlay_v2.pipeline.run_full_pipeline",
            side_effect=_mock_run_full_pipeline,
        ),
    ):
        result = agent.run(inp)

    # download_to_file must have been called with the GCS path, not the Gemini URI.
    assert len(downloaded_paths) == 1, "download_to_file should be called exactly once"
    assert downloaded_paths[0] == "templates/fdaf3bbc/source.mp4", (
        f"Expected gcs_path but got {downloaded_paths[0]!r}"
    )
    assert "generativelanguage.googleapis.com" not in downloaded_paths[0], (
        "Gemini File API URI must NOT be passed to download_to_file"
    )

    # run_full_pipeline must receive a local filesystem path, not the Gemini URI.
    assert len(pipeline_video_paths) == 1
    pipeline_path_str = str(pipeline_video_paths[0])
    assert "generativelanguage.googleapis.com" not in pipeline_path_str, (
        "Gemini File API URI must NOT reach run_full_pipeline"
    )

    assert isinstance(result, TemplateTextOutput)


def test_run_layer2_raises_when_gcs_path_missing(monkeypatch) -> None:
    """When gcs_path is None and Layer-2 is active, _run_layer2 must raise
    TerminalError and log template_text_layer2_missing_gcs_path.

    The bridge's existing TerminalError handler catches this and returns
    (False, 0), so recipe-agent overlays pass through unchanged — graceful
    degradation without silent fallback hiding the misconfiguration.
    """
    from app.agents._runtime import TerminalError  # noqa: PLC0415

    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", True)

    mock_client = MockModelClient()
    agent = TemplateTextAgent(mock_client)
    # gcs_path deliberately omitted (defaults to None).
    inp = _make_input(force_layer2=True)
    assert inp.gcs_path is None

    import app.agents.template_text as _mod  # noqa: PLC0415
    from app.agents._runtime import RunContext  # noqa: PLC0415

    log_events: list[str] = []

    with (
        patch.object(_mod.log, "warning", side_effect=lambda event, **kw: log_events.append(event)),
        pytest.raises(TerminalError, match="gcs_path is required"),
    ):
        agent._run_layer2(inp, ctx=RunContext(job_id="test-job"))

    assert "template_text_layer2_missing_gcs_path" in log_events, (
        f"Expected structured log event template_text_layer2_missing_gcs_path; got {log_events}"
    )


def test_run_layer2_cleans_up_tmpfile_on_success(monkeypatch) -> None:
    """The TemporaryDirectory containing the downloaded video is removed after a
    successful pipeline run (try/finally inside the `with tempfile.TemporaryDirectory`
    context manager guarantees cleanup even on success).
    """
    import pathlib  # noqa: PLC0415

    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", True)

    mock_client = MockModelClient()
    agent = TemplateTextAgent(mock_client)
    inp = _make_input(
        gcs_path="templates/abc/video.mp4",
        force_layer2=True,
    )

    created_tmpdir: list[str] = []

    def _mock_download(object_path: str, local_path: str) -> None:
        # Record the tmpdir so we can check it's gone after run().
        created_tmpdir.append(str(pathlib.Path(local_path).parent))
        pathlib.Path(local_path).write_bytes(b"sentinel")

    with (
        patch("app.storage.download_to_file", side_effect=_mock_download),
        patch(
            "app.pipeline.text_overlay_v2.pipeline.run_full_pipeline",
            return_value=TemplateTextOutput(overlays=[]),
        ),
    ):
        agent.run(inp)

    assert len(created_tmpdir) == 1
    assert not pathlib.Path(created_tmpdir[0]).exists(), (
        f"Tmpdir {created_tmpdir[0]} should have been cleaned up after successful run"
    )


def test_run_layer2_cleans_up_tmpfile_on_failure(monkeypatch) -> None:
    """The TemporaryDirectory is removed even when run_full_pipeline raises an
    exception. The `with tempfile.TemporaryDirectory(...)` block acts as the
    finally guard in this implementation.
    """
    import pathlib  # noqa: PLC0415

    from app.agents._runtime import TerminalError  # noqa: PLC0415

    monkeypatch.setattr("app.config.settings.text_overlay_v2_enabled", True)

    mock_client = MockModelClient()
    agent = TemplateTextAgent(mock_client)
    inp = _make_input(
        gcs_path="templates/abc/video.mp4",
        force_layer2=True,
    )

    created_tmpdir: list[str] = []

    def _mock_download(object_path: str, local_path: str) -> None:
        created_tmpdir.append(str(pathlib.Path(local_path).parent))
        pathlib.Path(local_path).write_bytes(b"sentinel")

    with (
        patch("app.storage.download_to_file", side_effect=_mock_download),
        patch(
            "app.pipeline.text_overlay_v2.pipeline.run_full_pipeline",
            side_effect=RuntimeError("simulated pipeline crash"),
        ),
        pytest.raises(TerminalError, match="pipeline failed"),
    ):
        agent.run(inp)

    assert len(created_tmpdir) == 1
    assert not pathlib.Path(created_tmpdir[0]).exists(), (
        f"Tmpdir {created_tmpdir[0]} should have been cleaned up even after pipeline failure"
    )
