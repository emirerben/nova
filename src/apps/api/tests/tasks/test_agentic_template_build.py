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
