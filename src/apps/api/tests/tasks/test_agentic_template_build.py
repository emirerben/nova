"""Tests for the parallel text_designer pool in agentic_template_build.

Phase 1 (PR replacing the sequential per-overlay loop) parallelized
`_run_text_designer_on_slots`. The sequential version explicitly admitted
~60s of single-threaded LLM wait per 20-overlay template; this test pins the
parallel contract: wall-clock drops, count is preserved, and a single
TerminalError no longer poisons the batch.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from app.tasks.agentic_template_build import (
    _TEXT_DESIGNER_PARALLEL_CAP,
    _bake_text_designer_into_overlay,
    _classify_overlay,
    _fetch_transcript_words_for_layer2,
    _run_text_designer_on_slots,
)


@dataclass
class _StubDesignerOutput:
    """Mirrors the fields _bake_text_designer_into_overlay reads."""

    text_size: str = "large"
    font_style: str = "sans-bold"
    text_color: str = "#FFFFFF"
    effect: str = "none"
    start_s: float = 0.0
    accel_at_s: float | None = None


def _make_subject_overlay(**kwargs: Any) -> dict[str, Any]:
    """Build an overlay that _classify_overlay will tag as 'subject' (label-like)."""
    base = {
        "role": "label",
        "sample_text": "{subject}",  # subject placeholder → kind="subject"
        "start_s": 0.0,
        "end_s": 2.0,
    }
    base.update(kwargs)
    return base


def _make_non_label_overlay(**kwargs: Any) -> dict[str, Any]:
    """Build an overlay that _classify_overlay returns None for (skipped)."""
    base = {
        "role": "body",
        "sample_text": "this is body copy, not a label",
        "start_s": 0.0,
        "end_s": 2.0,
    }
    base.update(kwargs)
    return base


def _slots_with_n_label_overlays(n: int) -> list[dict[str, Any]]:
    """One slot per overlay so slot_position varies and matches real templates."""
    return [
        {
            "position": i + 1,
            "slot_type": "hook" if i == 0 else "broll",
            "text_overlays": [_make_subject_overlay()],
        }
        for i in range(n)
    ]


class _SleepingAgent:
    """Stand-in for TextDesignerAgent.run that sleeps a fixed duration.

    Used to verify the pool is actually parallelizing: with N items and a
    pool of size W, total wall-clock should be ~ceil(N/W) × per_call_s,
    NOT N × per_call_s.
    """

    def __init__(self, per_call_s: float) -> None:
        self._per_call_s = per_call_s
        self.calls = 0

    def run(self, _input, ctx=None):  # noqa: ANN001 — match agent signature
        time.sleep(self._per_call_s)
        self.calls += 1
        return _StubDesignerOutput()


class _FlakeyAgent:
    """First call raises TerminalError; the rest succeed. Pins isolation."""

    def __init__(self, terminal_error_cls) -> None:  # noqa: ANN001
        self._terminal_error_cls = terminal_error_cls
        self.calls = 0
        self._first_call_seen = False

    def run(self, _input, ctx=None):  # noqa: ANN001
        self.calls += 1
        if not self._first_call_seen:
            self._first_call_seen = True
            raise self._terminal_error_cls("simulated agent failure")
        return _StubDesignerOutput()


def test_parallelizes_and_preserves_count() -> None:
    """20 overlays × 200ms/call should finish in well under sequential 4s.

    With a pool cap of 8, the lower bound is ceil(20/8) × 0.2 = 0.6s. We
    assert < 1.5s to leave generous headroom for thread startup + GIL hops
    on slow CI. The point is to fail loudly if someone accidentally reverts
    to sequential (which would be ≥ 4s).
    """
    overlays_n = 20
    per_call_s = 0.2
    sleeping = _SleepingAgent(per_call_s=per_call_s)
    slots = _slots_with_n_label_overlays(overlays_n)

    with (
        patch(
            "app.agents.text_designer.TextDesignerAgent",
            return_value=sleeping,
        ),
        patch("app.agents._model_client.default_client", return_value=None),
    ):
        t0 = time.monotonic()
        baked = _run_text_designer_on_slots(
            slots,
            copy_tone="punchy",
            creative_direction="bold and clean",
            job_id="test-parallel",
        )
        elapsed = time.monotonic() - t0

    assert baked == overlays_n, f"expected {overlays_n} overlays baked, got {baked}"
    assert sleeping.calls == overlays_n
    # Sequential bound: 20 × 0.2 = 4.0s. Parallel ceiling (cap=8): 3 × 0.2 = 0.6s.
    assert elapsed < 1.5, (
        f"_run_text_designer_on_slots looks sequential — took {elapsed:.2f}s "
        f"for {overlays_n} overlays at {per_call_s}s each (parallel cap "
        f"is {_TEXT_DESIGNER_PARALLEL_CAP})"
    )

    # Each overlay was mutated in place with the stub's fields.
    for slot in slots:
        ov = slot["text_overlays"][0]
        assert ov["text_size"] == "large"
        assert ov["font_style"] == "sans-bold"
        assert ov["text_color"] == "#FFFFFF"
        assert ov["effect"] == "none"


def test_terminal_error_isolated_to_one_overlay() -> None:
    """A single agent failure must not drop the rest of the batch.

    The failed overlay keeps whatever template_recipe set; the others get
    text_designer styling baked in. Sequential code already had this
    behavior via try/except; the parallel pool must preserve it through
    future.result()'s exception channel.
    """
    from app.agents._runtime import TerminalError

    overlays_n = 5
    flakey = _FlakeyAgent(TerminalError)
    slots = _slots_with_n_label_overlays(overlays_n)

    with (
        patch(
            "app.agents.text_designer.TextDesignerAgent",
            return_value=flakey,
        ),
        patch("app.agents._model_client.default_client", return_value=None),
    ):
        baked = _run_text_designer_on_slots(
            slots,
            copy_tone="punchy",
            creative_direction="bold and clean",
            job_id="test-isolation",
        )

    assert flakey.calls == overlays_n, "every overlay must be attempted"
    assert baked == overlays_n - 1, f"one failure → {overlays_n - 1} baked, got {baked}"
    # Exactly one overlay missing the text_designer fields; the rest have them.
    untouched = [slot for slot in slots if "text_size" not in slot["text_overlays"][0]]
    assert len(untouched) == 1


def test_skips_non_label_overlays() -> None:
    """body / non-subject overlays must NOT trigger text_designer calls."""
    sleeping = _SleepingAgent(per_call_s=0.0)
    slots = [
        {
            "position": 1,
            "slot_type": "broll",
            "text_overlays": [
                _make_subject_overlay(),  # → 1 call
                _make_non_label_overlay(),  # → skipped
                _make_non_label_overlay(),  # → skipped
            ],
        }
    ]

    with (
        patch(
            "app.agents.text_designer.TextDesignerAgent",
            return_value=sleeping,
        ),
        patch("app.agents._model_client.default_client", return_value=None),
    ):
        baked = _run_text_designer_on_slots(
            slots,
            copy_tone="",
            creative_direction="",
            job_id="test-classify",
        )

    assert baked == 1
    assert sleeping.calls == 1
    # The body overlays are untouched.
    assert "text_size" not in slots[0]["text_overlays"][1]
    assert "text_size" not in slots[0]["text_overlays"][2]


def test_empty_workload_returns_zero_without_pool() -> None:
    """No label overlays → no pool spin-up, no errors, baked=0."""
    sleeping = _SleepingAgent(per_call_s=0.0)
    slots = [
        {
            "position": 1,
            "slot_type": "broll",
            "text_overlays": [_make_non_label_overlay()],
        },
        {"position": 2, "slot_type": "broll", "text_overlays": []},
        {"position": 3, "slot_type": "broll"},  # no text_overlays key
    ]

    with (
        patch(
            "app.agents.text_designer.TextDesignerAgent",
            return_value=sleeping,
        ),
        patch("app.agents._model_client.default_client", return_value=None),
    ):
        baked = _run_text_designer_on_slots(
            slots,
            copy_tone="",
            creative_direction="",
            job_id="test-empty",
        )

    assert baked == 0
    assert sleeping.calls == 0


def test_accel_at_s_baked_when_present() -> None:
    """When the designer output carries accel_at_s, it lands on the overlay.
    When it's None, the overlay's font_cycle_accel_at_s key stays absent
    (no spurious nulls written)."""
    overlays_n = 2

    class _SwitchingAgent:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, _input, ctx=None):  # noqa: ANN001
            self.calls += 1
            # First overlay gets accel; second doesn't.
            return _StubDesignerOutput(accel_at_s=8.0 if self.calls == 1 else None)

    agent = _SwitchingAgent()
    slots = _slots_with_n_label_overlays(overlays_n)

    with (
        patch(
            "app.agents.text_designer.TextDesignerAgent",
            return_value=agent,
        ),
        patch("app.agents._model_client.default_client", return_value=None),
    ):
        baked = _run_text_designer_on_slots(
            slots,
            copy_tone="",
            creative_direction="",
            job_id="test-accel",
        )

    assert baked == 2
    # Order of `as_completed` is non-deterministic — locate by accel presence.
    accel_overlays = [
        slot["text_overlays"][0]
        for slot in slots
        if "font_cycle_accel_at_s" in slot["text_overlays"][0]
    ]
    no_accel_overlays = [
        slot["text_overlays"][0]
        for slot in slots
        if "font_cycle_accel_at_s" not in slot["text_overlays"][0]
    ]
    assert len(accel_overlays) == 1
    assert len(no_accel_overlays) == 1
    assert accel_overlays[0]["font_cycle_accel_at_s"] == pytest.approx(8.0)


# ── Regression tests: post-bake timing-inversion clamp ────────────────────────
#
# Root cause (identified after PR #200 deployed): `_bake_text_designer_into_overlay`
# overwrites `start_s` with text_designer's calibrated value (e.g. 3.0 for
# subject labels on slot 1) without checking the overlay's source-extracted
# `end_s` (e.g. 0.9). This produces `start_s=3.0, end_s=0.9` — inverted,
# post-parse, post-merge — which the renderer rejects.
#
# Prod evidence (round-1 agent, 2026-05-17):
#   - `not just luck` overlay 0 ("It's"):  stored start_s=3.0, end_s=0.9
#   - `Rich in life v2` overlay 0 ("this is what i call:"): stored start_s=2.0, end_s=0.04
#
# Fix: clamp start_s to end_s - 0.01 immediately after the overwrite.


class _InversionDesignerOutput:
    """Simulates a text_designer that returns a start_s > overlay's end_s."""

    text_size: str = "large"
    font_style: str = "sans-bold"
    text_color: str = "#F4D03F"
    effect: str = "font-cycle"
    start_s: float = 3.0  # subject-label calibration for slot 1
    accel_at_s: float | None = None

    def __init__(self, start_s: float = 3.0, accel_at_s: float | None = None) -> None:
        self.start_s = start_s
        self.accel_at_s = accel_at_s


class _SafeDesignerOutput:
    """Simulates a text_designer that returns a start_s safely inside the window."""

    text_size: str = "large"
    font_style: str = "sans-bold"
    text_color: str = "#FFFFFF"
    effect: str = "none"
    start_s: float = 2.0
    accel_at_s: float | None = None

    def __init__(self, start_s: float = 2.0, accel_at_s: float | None = None) -> None:
        self.start_s = start_s
        self.accel_at_s = accel_at_s


def test_bake_clamps_inverted_start_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: designer start_s > overlay end_s must be clamped.

    Reproduces the `not just luck` prod failure: pre-bake overlay has
    start_s=0.4, end_s=0.9 (source-extracted window). Text_designer
    suggests start_s=3.0 (subject-label calibration). Without the clamp,
    stored overlay would have start_s=3.0, end_s=0.9 — inverted.

    After the fix: start_s must be clamped to end_s - 0.01 = 0.89, and a
    'text_designer_bake_start_s_clamped' log event must be emitted.
    """
    from app.tasks import agentic_template_build

    warnings: list[tuple[str, dict]] = []

    class _LogRecorder:
        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append((event, kwargs))

        def info(self, *a: object, **kw: object) -> None:
            pass

        def debug(self, *a: object, **kw: object) -> None:
            pass

    monkeypatch.setattr(agentic_template_build, "log", _LogRecorder())

    overlay: dict = {
        "role": "label",
        "sample_text": "It's",
        "start_s": 0.4,
        "end_s": 0.9,
        "bbox": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.1, "sample_frame_t": 0.5},
        "text_color": "#FFFFFF",
    }
    designer_out = _InversionDesignerOutput(start_s=3.0)

    _bake_text_designer_into_overlay(overlay, designer_out)

    assert overlay["start_s"] < overlay["end_s"], (
        f"post-bake inversion: start_s={overlay['start_s']}, end_s={overlay['end_s']}"
    )
    assert overlay["start_s"] == pytest.approx(0.9 - 0.01)
    assert overlay["end_s"] == pytest.approx(0.9)  # end_s untouched

    # Structured log event must be emitted for observability.
    clamped_events = [ev for ev, _ in warnings if ev == "text_designer_bake_start_s_clamped"]
    assert clamped_events, (
        "Expected a 'text_designer_bake_start_s_clamped' log warning but got none. "
        f"All warnings: {[ev for ev, _ in warnings]}"
    )


def test_bake_clamps_inverted_start_s_rich_in_life_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: covers the `Rich in life v2` failure shape.

    Pre-bake overlay: start_s=0.0, end_s=0.04 (very short source window).
    Designer suggests start_s=2.0. Clamped result: start_s = 0.03, end_s=0.04.
    """
    from app.tasks import agentic_template_build

    warnings: list[tuple[str, dict]] = []

    class _LogRecorder:
        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append((event, kwargs))

        def info(self, *a: object, **kw: object) -> None:
            pass

        def debug(self, *a: object, **kw: object) -> None:
            pass

    monkeypatch.setattr(agentic_template_build, "log", _LogRecorder())

    overlay: dict = {
        "role": "label",
        "sample_text": "this is what i call:",
        "start_s": 0.0,
        "end_s": 0.04,
        "bbox": {"x": 0.1, "y": 0.2, "w": 0.8, "h": 0.1, "sample_frame_t": 0.02},
        "text_color": "#FFFFFF",
    }
    designer_out = _InversionDesignerOutput(start_s=2.0)

    _bake_text_designer_into_overlay(overlay, designer_out)

    assert overlay["start_s"] < overlay["end_s"], (
        f"post-bake inversion: start_s={overlay['start_s']}, end_s={overlay['end_s']}"
    )
    assert overlay["start_s"] == pytest.approx(0.04 - 0.01)

    clamped_events = [ev for ev, _ in warnings if ev == "text_designer_bake_start_s_clamped"]
    assert clamped_events, "Expected 'text_designer_bake_start_s_clamped' log event"


def test_bake_no_clamp_when_start_s_inside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: designer start_s falls inside the overlay's window — no clamp.

    Pre-bake: start_s=0.0, end_s=5.0. Designer suggests start_s=2.0 (valid).
    Post-bake: start_s=2.0 (unchanged), end_s=5.0, no log emitted.
    """
    from app.tasks import agentic_template_build

    warnings: list[tuple[str, dict]] = []

    class _LogRecorder:
        def warning(self, event: str, **kwargs: object) -> None:
            warnings.append((event, kwargs))

        def info(self, *a: object, **kw: object) -> None:
            pass

        def debug(self, *a: object, **kw: object) -> None:
            pass

    monkeypatch.setattr(agentic_template_build, "log", _LogRecorder())

    overlay: dict = {
        "role": "label",
        "sample_text": "{subject}",
        "start_s": 0.0,
        "end_s": 5.0,
        "bbox": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.1, "sample_frame_t": 2.5},
        "text_color": "#FFFFFF",
    }
    designer_out = _SafeDesignerOutput(start_s=2.0)

    _bake_text_designer_into_overlay(overlay, designer_out)

    assert overlay["start_s"] == pytest.approx(2.0), (
        f"start_s should be 2.0 (designer's value), got {overlay['start_s']}"
    )
    assert overlay["end_s"] == pytest.approx(5.0)

    clamped_events = [ev for ev, _ in warnings if ev == "text_designer_bake_start_s_clamped"]
    assert not clamped_events, (
        f"No clamp expected when start_s is inside window, but got: {clamped_events}"
    )


# ── _classify_overlay: body kind for Layer-2 roles ───────────────────────────


def test_classify_overlay_returns_body_for_hook_role() -> None:
    """Layer-2 role=hook overlays must route to kind='body', not be skipped."""
    overlay = {"role": "hook", "sample_text": "It's", "start_s": 0.0, "end_s": 0.9}
    assert _classify_overlay(overlay) == "body"


def test_classify_overlay_returns_body_for_reaction_role() -> None:
    """Layer-2 role=reaction overlays must route to kind='body', not be skipped."""
    overlay = {
        "role": "reaction",
        "sample_text": "if you / put in",
        "start_s": 1.0,
        "end_s": 3.0,
    }
    assert _classify_overlay(overlay) == "body"


def test_classify_overlay_returns_body_for_cta_role() -> None:
    """Layer-2 role=cta overlays must route to kind='body', not be skipped."""
    overlay = {"role": "cta", "sample_text": "Follow for more", "start_s": 8.0, "end_s": 10.0}
    assert _classify_overlay(overlay) == "body"


def test_classify_overlay_still_returns_label_for_label_role() -> None:
    """Backward-compat: role=label overlays still route to 'prefix', not 'body'."""
    overlay = {"role": "label", "sample_text": "Welcome to", "start_s": 0.0, "end_s": 2.0}
    result = _classify_overlay(overlay)
    assert result in {"prefix", "subject"}, (
        f"role=label must still return a label kind, got {result!r}"
    )


def test_classify_overlay_subject_placeholder_still_label() -> None:
    """Backward-compat: all-caps subject placeholders still return 'subject'."""
    overlay = {"role": "label", "sample_text": "PERU", "start_s": 0.0, "end_s": 5.0}
    assert _classify_overlay(overlay) == "subject"


def test_classify_overlay_skips_layer2_uniform_sentinel() -> None:
    """`_layer2_uniform=True` short-circuits classification to None — neither
    _BODY_CONFIG nor text_designer should touch these overlays.

    Without the gate, text_designer's unconditional ``overlay["text_size"] =
    designer_output.text_size`` write would clobber the pinned `"large"`
    and re-introduce per-overlay size variance — the bug the uniform bridge
    was added to fix. The sentinel wins over both the explicit role check
    (hook/reaction/cta → "body") and the subject-placeholder heuristic.
    """
    for role, sample_text in (
        ("hook", "It's"),
        ("reaction", "the work"),
        ("cta", "Follow for more"),
        ("label", "PERU"),
        ("label", "Welcome to"),
    ):
        overlay = {
            "role": role,
            "sample_text": sample_text,
            "start_s": 0.0,
            "end_s": 1.0,
            "_layer2_uniform": True,
        }
        assert _classify_overlay(overlay) is None, (
            f"_layer2_uniform sentinel must skip classification "
            f"(role={role!r}, text={sample_text!r})"
        )


def test_classify_overlay_returns_none_for_unknown_role() -> None:
    """Overlays with roles we don't recognize are skipped (None)."""
    overlay = {"role": "caption", "sample_text": "some caption text"}
    assert _classify_overlay(overlay) is None


# ── _run_text_designer_on_slots: body overlays styled deterministically ───────


def test_run_text_designer_styles_body_overlays() -> None:
    """Layer-2 body overlays (hook/reaction) must be styled without LLM calls.

    1 label overlay -> LLM call (1 agent.run invocation).
    2 body overlays (hook + reaction) -> deterministic _BODY_CONFIG, no agent.run.
    All 3 must be styled (baked == 3).
    """
    sleeping = _SleepingAgent(per_call_s=0.0)
    slots = [
        {
            "position": 1,
            "slot_type": "hook",
            "text_overlays": [
                _make_subject_overlay(),  # label -> LLM
                {
                    "role": "hook",
                    "sample_text": "It's",
                    "start_s": 0.0,
                    "end_s": 0.9,
                    "effect": "pop-in",
                    "text_color": "#FFFFFF",
                },
                {
                    "role": "reaction",
                    "sample_text": "if you / put in",
                    "start_s": 1.0,
                    "end_s": 3.0,
                    "effect": "fade-in",
                    "text_color": "#CCCCCC",
                },
            ],
        }
    ]

    with (
        patch(
            "app.agents.text_designer.TextDesignerAgent",
            return_value=sleeping,
        ),
        patch("app.agents._model_client.default_client", return_value=None),
    ):
        baked = _run_text_designer_on_slots(
            slots,
            copy_tone="punchy",
            creative_direction="bold and clean",
            job_id="test-body-styling",
        )

    # All 3 overlays styled.
    assert baked == 3, f"expected 3 overlays styled, got {baked}"

    # Label overlay got the LLM path (sleeping agent provides _StubDesignerOutput).
    label_ov = slots[0]["text_overlays"][0]
    assert "text_size" in label_ov, "label overlay should have text_size from LLM path"

    # Hook overlay got deterministic body config (text_size set, effect preserved).
    hook_ov = slots[0]["text_overlays"][1]
    assert hook_ov.get("text_size") == "large", (
        f"hook overlay should have text_size='large' from _BODY_CONFIG, "
        f"got {hook_ov.get('text_size')!r}"
    )
    assert hook_ov.get("effect") == "pop-in", (
        "hook overlay effect must be preserved from stage F, not overridden"
    )
    assert hook_ov.get("text_color") == "#FFFFFF", (
        "hook overlay text_color must be preserved from stage F"
    )

    # Reaction overlay got deterministic body config.
    reaction_ov = slots[0]["text_overlays"][2]
    assert reaction_ov.get("text_size") == "medium", (
        f"reaction overlay should have text_size='medium', got {reaction_ov.get('text_size')!r}"
    )
    assert reaction_ov.get("effect") == "fade-in", (
        "reaction overlay effect must be preserved from stage F"
    )
    assert reaction_ov.get("text_color") == "#CCCCCC", (
        "reaction overlay text_color must be preserved from stage F"
    )

    # LLM was only called once (for the label overlay, not the 2 body overlays).
    assert sleeping.calls == 1, (
        f"LLM agent should be called exactly once (label only), got {sleeping.calls}"
    )


# ── _fetch_transcript_words_for_layer2 ───────────────────────────────────────


@dataclass
class _StubTranscript:
    """Minimal Transcript stand-in carrying the .words and .low_confidence
    attributes the helper reads. `low_confidence=False` (the default) means
    the transcribe call succeeded; `True` is the universal "I tried and
    failed" signal documented on `gemini_analyzer.transcribe`.
    """

    words: list[Any]
    low_confidence: bool = False


@dataclass
class _StubTranscriptWord:
    text: str
    start_s: float
    end_s: float


def test_fetch_transcript_skips_when_layer2_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Layer-2 is off (use_layer2=False AND setting=False), no Gemini call
    fires. The transcript would be wasted — Stage E only runs in Layer-2.
    """
    from app.tasks import agentic_template_build as mod

    monkeypatch.setattr(mod.settings, "text_overlay_v2_enabled", False)

    def _should_not_be_called(*_a, **_k):
        raise AssertionError("transcribe must not be called when Layer-2 is off")

    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer.transcribe", _should_not_be_called)
    words, failed = _fetch_transcript_words_for_layer2(
        object(), template_id="t-off", use_layer2=False
    )
    assert words == []
    # Not a failure — we just didn't try.
    assert failed is False


def test_fetch_transcript_fires_when_use_layer2_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """use_layer2=True alone (settings flag off) is enough to trigger the call.
    Admin force-routing must work even when the global flag stays default-False.
    """
    from app.tasks import agentic_template_build as mod

    monkeypatch.setattr(mod.settings, "text_overlay_v2_enabled", False)

    captured: dict[str, Any] = {}

    def _fake_transcribe(file_ref, *, job_id=None):
        captured["job_id"] = job_id
        return _StubTranscript(
            words=[
                _StubTranscriptWord("if", 3.0, 3.2),
                _StubTranscriptWord("you", 3.2, 3.5),
            ]
        )

    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer.transcribe", _fake_transcribe)

    words, failed = _fetch_transcript_words_for_layer2(
        object(), template_id="t-forced", use_layer2=True
    )
    assert words == [
        {"text": "if", "start_s": 3.0, "end_s": 3.2},
        {"text": "you", "start_s": 3.2, "end_s": 3.5},
    ]
    assert failed is False
    # job_id is namespaced for agent_run attribution.
    assert captured["job_id"] == "template:t-forced:agentic"


def test_fetch_transcript_fires_when_settings_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """settings.text_overlay_v2_enabled=True alone (use_layer2=False) also
    triggers the call — global flag takes the same effect as the admin override.
    """
    from app.tasks import agentic_template_build as mod

    monkeypatch.setattr(mod.settings, "text_overlay_v2_enabled", True)
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.transcribe",
        lambda file_ref, *, job_id=None: _StubTranscript(
            words=[_StubTranscriptWord("hello", 0.0, 0.5)]
        ),
    )

    words, failed = _fetch_transcript_words_for_layer2(
        object(), template_id="t-global", use_layer2=False
    )
    assert len(words) == 1
    assert words[0]["text"] == "hello"
    assert failed is False


def test_fetch_transcript_swallows_transcribe_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcribe exception must NOT abort the build. The helper returns
    ``([], True)`` so the caller refuses the Layer-2 merge instead of
    letting Stage E passthrough emit broken OCR.
    """
    from app.tasks import agentic_template_build as mod

    monkeypatch.setattr(mod.settings, "text_overlay_v2_enabled", True)

    def _boom(file_ref, *, job_id=None):
        raise RuntimeError("Gemini quota exceeded")

    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer.transcribe", _boom)
    words, failed = _fetch_transcript_words_for_layer2(
        object(), template_id="t-fail", use_layer2=True
    )
    assert words == []
    assert failed is True


def test_fetch_transcript_low_confidence_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: prod template 89cde014 on 2026-05-22 09:13 had
    ``nova.audio.transcript`` outcome=terminal_refusal, which the transcribe
    shim downgrades into ``Transcript(low_confidence=True, words=[])``.
    The helper must surface that as ``failed=True`` so the caller skips the
    Layer-2 merge — otherwise Stage E passthrough emits OCR garbage
    (`"luck\\""`, `"W"`) into recipe_cached.
    """
    from app.tasks import agentic_template_build as mod

    monkeypatch.setattr(mod.settings, "text_overlay_v2_enabled", True)
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.transcribe",
        lambda file_ref, *, job_id=None: _StubTranscript(words=[], low_confidence=True),
    )

    words, failed = _fetch_transcript_words_for_layer2(
        object(), template_id="t-lowconf", use_layer2=True
    )
    assert words == []
    assert failed is True


def test_fetch_transcript_low_confidence_with_words_still_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: ``transcribe()`` returns words AND ``low_confidence=True``
    (model transcribed but flagged itself as uncertain — possible when audio
    is unclear). Treat as failed: Stage E grounding OCR against shaky
    transcript words risks producing wrong overlays, and the user-stated
    preference is for ``template_recipe``'s clean phrases over Stage E
    output produced from low-confidence ground truth. Locks the conservative
    behavior so a future loosening is an explicit, reviewed decision.
    """
    from app.tasks import agentic_template_build as mod

    monkeypatch.setattr(mod.settings, "text_overlay_v2_enabled", True)
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.transcribe",
        lambda file_ref, *, job_id=None: _StubTranscript(
            words=[_StubTranscriptWord("uncertain", 0.0, 0.5)],
            low_confidence=True,
        ),
    )

    words, failed = _fetch_transcript_words_for_layer2(
        object(), template_id="t-shaky", use_layer2=True
    )
    # Words ARE returned (caller may inspect them) but `failed=True` tells
    # the caller to skip the Layer-2 merge regardless.
    assert len(words) == 1
    assert words[0]["text"] == "uncertain"
    assert failed is True


def test_fetch_transcript_music_only_template_still_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuine no-speech templates (music-only, caption-only) succeed with
    ``words=[]`` AND ``low_confidence=False``. The helper must NOT mark
    these as failed — Stage E's empty-transcript passthrough is the
    correct behavior, the agent built it for exactly this case.
    """
    from app.tasks import agentic_template_build as mod

    monkeypatch.setattr(mod.settings, "text_overlay_v2_enabled", True)
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.transcribe",
        lambda file_ref, *, job_id=None: _StubTranscript(words=[], low_confidence=False),
    )

    words, failed = _fetch_transcript_words_for_layer2(
        object(), template_id="t-music-only", use_layer2=True
    )
    assert words == []
    assert failed is False


@pytest.mark.parametrize("exc_class", [TypeError, AttributeError, NameError])
def test_fetch_transcript_reraises_programming_errors(
    monkeypatch: pytest.MonkeyPatch, exc_class
) -> None:
    """Programming errors must propagate. A refactor that breaks the transcribe()
    signature (TypeError) or accesses a missing attribute (AttributeError) would
    silently degrade every Layer-2 build to empty-transcript passthrough if
    swallowed broadly. Re-raising lets these failures surface in CI or as task
    failures in prod rather than rotting silently.
    """
    from app.tasks import agentic_template_build as mod

    monkeypatch.setattr(mod.settings, "text_overlay_v2_enabled", True)

    def _broken(file_ref, *, job_id=None):
        raise exc_class("programming error")

    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer.transcribe", _broken)
    with pytest.raises(exc_class):
        _fetch_transcript_words_for_layer2(object(), template_id="t-prog", use_layer2=True)
