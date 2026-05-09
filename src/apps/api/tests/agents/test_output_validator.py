"""Tests for nova.qa.output_validator.

The validator is rule_based — every assertion below is deterministic.
"""

from __future__ import annotations

import pytest

from app.agents._model_client import ModelDispatcher
from app.agents.output_validator import (
    CopySummary,
    OutputValidatorAgent,
    OutputValidatorInput,
    ProbeData,
    TextOverlaySummary,
)


def _ok_input() -> OutputValidatorInput:
    return OutputValidatorInput(
        probe=ProbeData(
            width=1080, height=1920, duration_s=15.0,
            video_codec="h264", audio_codec="aac", fps=30.0,
        ),
        hook_text="You won't believe what happens next",
        platform_copy=CopySummary(
            tiktok_caption="Wait for the drop",
            instagram_caption="Tag a friend",
            youtube_description="Subscribe for more",
            tiktok_hook="Wait for it",
        ),
        text_overlays=[
            TextOverlaySummary(sample_text="Hello", role="hook", start_s=0.5, end_s=2.0)
        ],
    )


@pytest.fixture
def agent() -> OutputValidatorAgent:
    # ModelDispatcher is unused for rule_based — pass it for runtime construction only.
    return OutputValidatorAgent(ModelDispatcher())


# ── Happy path ────────────────────────────────────────────────────────────────


def test_passes_clean_clip(agent: OutputValidatorAgent) -> None:
    out = agent.run(_ok_input())
    assert out.pass_ is True
    assert out.error_count == 0
    assert out.warning_count == 0


# ── Aspect ratio ──────────────────────────────────────────────────────────────


def test_rejects_landscape_aspect(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.probe.width = 1920
    inp.probe.height = 1080
    out = agent.run(inp)
    assert out.pass_ is False
    assert any(i.code == "aspect_ratio" for i in out.issues)


def test_accepts_aspect_within_tolerance(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.probe.width = 1078  # very slight off-9:16
    inp.probe.height = 1920
    out = agent.run(inp)
    # aspect 1078/1920 = 0.5614 vs 0.5625 target = delta 0.0011, within 0.02 tolerance
    assert all(i.code != "aspect_ratio" for i in out.issues)


# ── Duration ──────────────────────────────────────────────────────────────────


def test_rejects_too_short(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.probe.duration_s = 1.5
    out = agent.run(inp)
    assert out.pass_ is False
    assert any(i.code == "duration_too_short" for i in out.issues)


def test_rejects_too_long(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.probe.duration_s = 120.0
    out = agent.run(inp)
    assert out.pass_ is False
    assert any(i.code == "duration_too_long" for i in out.issues)


# ── Codecs ────────────────────────────────────────────────────────────────────


def test_rejects_non_h264_video(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.probe.video_codec = "vp9"
    out = agent.run(inp)
    assert out.pass_ is False
    assert any(i.code == "video_codec" for i in out.issues)


def test_warns_on_unexpected_audio_codec(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.probe.audio_codec = "opus"
    out = agent.run(inp)
    # warning, not error — pass_ should still be True
    assert out.pass_ is True
    assert any(i.code == "audio_codec" and i.severity == "warning" for i in out.issues)


def test_accepts_no_audio_track(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.probe.audio_codec = ""
    out = agent.run(inp)
    assert out.pass_ is True


# ── Hook text ─────────────────────────────────────────────────────────────────


def test_rejects_empty_hook(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.hook_text = ""
    out = agent.run(inp)
    assert out.pass_ is False
    assert any(i.code == "empty_hook" for i in out.issues)


def test_rejects_placeholder_hook(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.hook_text = "Watch this"  # _template_copy fallback hook
    out = agent.run(inp)
    assert out.pass_ is False
    assert any(i.code == "placeholder_hook" for i in out.issues)


# ── Copy placeholders ─────────────────────────────────────────────────────────


def test_rejects_auto_copy_failed_placeholder(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.platform_copy.instagram_caption = "Auto-copy failed — edit before posting"
    out = agent.run(inp)
    assert out.pass_ is False
    assert any(i.code == "placeholder_copy" for i in out.issues)


# ── Lazy duplicate ────────────────────────────────────────────────────────────


def test_warns_on_hook_equals_caption(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.platform_copy.tiktok_caption = inp.hook_text
    out = agent.run(inp)
    # warning, not error
    assert out.pass_ is True
    assert any(i.code == "hook_equals_caption" for i in out.issues)


# ── Empty overlay sample_text ─────────────────────────────────────────────────


def test_warns_on_empty_overlay_text(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.text_overlays = [
        TextOverlaySummary(sample_text="", role="label", start_s=0.0, end_s=2.0)
    ]
    out = agent.run(inp)
    assert out.pass_ is True  # warning only
    assert any(i.code == "empty_overlay_text" for i in out.issues)


# ── Multi-issue clip ──────────────────────────────────────────────────────────


def test_aggregates_multiple_errors(agent: OutputValidatorAgent) -> None:
    inp = _ok_input()
    inp.probe.width = 1920
    inp.probe.height = 1080  # aspect_ratio error
    inp.probe.duration_s = 1.0  # duration_too_short error
    inp.hook_text = ""  # empty_hook error
    out = agent.run(inp)
    assert out.pass_ is False
    assert out.error_count >= 3
    codes = {i.code for i in out.issues}
    assert {"aspect_ratio", "duration_too_short", "empty_hook"}.issubset(codes)
