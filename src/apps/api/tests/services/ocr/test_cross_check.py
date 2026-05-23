"""Unit tests for app.services.ocr.cross_check.

Uses fake OCREngine implementations (real classes implementing the
Protocol, not mocks) so the test surface is the same shape adapters
take in prod — if the Protocol gains a method, these fakes fail
loudly instead of silently passing through ``Mock.__getattr__``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.services.ocr.cross_check import (
    DEFAULT_AGREEMENT_THRESHOLD,
    CrossCheckResult,
    cross_check_engines,
)
from app.services.ocr.engines import OCREngine, OCRWord


@dataclass
class FakeEngine:
    """Real Protocol implementation that returns canned words.

    Records every image_path it sees so a test can assert both engines
    were consulted (rather than one short-circuiting).
    """

    name: str
    words: list[OCRWord] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def recognize(self, image_path: str) -> list[OCRWord]:
        self.calls.append(image_path)
        return list(self.words)


def _w(text: str) -> OCRWord:
    """Helper for terser test data — bbox/confidence don't affect agreement."""
    return OCRWord(text=text, bbox=(0.0, 0.0, 0.1, 0.1), confidence=1.0)


def test_protocol_is_runtime_checkable() -> None:
    """The Protocol is decorated ``@runtime_checkable`` so adapters that
    forget to set ``name`` (or rename ``recognize``) fail at instantiation
    time, not deep in the autobuilder loop."""
    eng = FakeEngine(name="x", words=[_w("hi")])
    assert isinstance(eng, OCREngine)


def test_perfect_agreement() -> None:
    a = FakeEngine(name="a", words=[_w("hello"), _w("world")])
    b = FakeEngine(name="b", words=[_w("hello"), _w("world")])

    result = cross_check_engines(a, b, "frame.png")

    assert isinstance(result, CrossCheckResult)
    assert result.status == "agreed"
    assert result.agreement == pytest.approx(1.0)
    assert result.tokens == ["hello", "world"]
    assert a.calls == ["frame.png"]
    assert b.calls == ["frame.png"]
    assert result.engine_a_name == "a"
    assert result.engine_b_name == "b"


def test_below_threshold_disagreement() -> None:
    """Wildly different token sets must land in the disagreement bucket."""
    a = FakeEngine(name="a", words=[_w("rich"), _w("in"), _w("life")])
    b = FakeEngine(
        name="b",
        words=[_w("astronaut"), _w("cosmonaut"), _w("submarine")],
    )

    result = cross_check_engines(a, b, "frame.png")

    assert result.status == "disagreed"
    assert result.agreement < DEFAULT_AGREEMENT_THRESHOLD
    assert result.tokens == []
    # Raw per-engine tokens preserved for the human reviewer.
    assert result.engine_a_tokens == ["rich", "in", "life"]
    assert result.engine_b_tokens == ["astronaut", "cosmonaut", "submarine"]


def test_borderline_above_threshold() -> None:
    """Engines that differ by a single token out of ten still agree —
    the threshold is set to tolerate per-engine OCR noise on tight crops."""
    shared = [_w(t) for t in ["this", "is", "what", "i", "call", "being", "rich", "in", "life"]]
    a = FakeEngine(name="a", words=shared + [_w("today")])
    b = FakeEngine(name="b", words=shared + [_w("todayy")])  # OCR typo

    result = cross_check_engines(a, b, "frame.png")

    assert result.status == "agreed", (
        f"expected agreement; got {result.agreement:.3f} for"
        f" {result.engine_a_tokens} vs {result.engine_b_tokens}"
    )
    assert result.agreement >= DEFAULT_AGREEMENT_THRESHOLD
    # Union semantics: the disagreeing tokens both appear in the truth.
    assert "today" in result.tokens
    assert "todayy" in result.tokens


def test_normalization_handles_case() -> None:
    """Case differences must not count as disagreement — both engines
    saw the same text, just with different shift-key luck."""
    a = FakeEngine(name="a", words=[_w("HELLO"), _w("WORLD")])
    b = FakeEngine(name="b", words=[_w("hello"), _w("world")])

    result = cross_check_engines(a, b, "frame.png")

    assert result.status == "agreed"
    assert result.agreement == pytest.approx(1.0)
    assert result.tokens == ["hello", "world"]


def test_normalization_handles_whitespace() -> None:
    """Tokens with stray internal whitespace normalize to the same key."""
    a = FakeEngine(name="a", words=[_w("hello  world")])
    b = FakeEngine(name="b", words=[_w("hello world")])

    result = cross_check_engines(a, b, "frame.png")

    assert result.status == "agreed"


def test_mutual_empty_counts_as_agreement() -> None:
    """A frame with no overlay text (background-only shot) is a
    legitimate ground-truth outcome: both engines agree on "nothing here"."""
    a = FakeEngine(name="a", words=[])
    b = FakeEngine(name="b", words=[])

    result = cross_check_engines(a, b, "frame.png")

    assert result.status == "agreed"
    assert result.tokens == []
    assert result.agreement == pytest.approx(1.0)


def test_one_empty_one_populated_disagrees() -> None:
    """One engine seeing text and the other seeing none is the worst
    kind of disagreement — kept out of ground truth on principle."""
    a = FakeEngine(name="a", words=[_w("hello"), _w("world")])
    b = FakeEngine(name="b", words=[])

    result = cross_check_engines(a, b, "frame.png")

    assert result.status == "disagreed"


def test_threshold_out_of_range_rejects() -> None:
    """A config typo that sends threshold to 1.5 must fail loudly —
    silently clamping would relax the trust gate to whatever value
    a downstream comparison accidentally tolerates."""
    a = FakeEngine(name="a")
    b = FakeEngine(name="b")
    with pytest.raises(ValueError):
        cross_check_engines(a, b, "frame.png", threshold=1.5)
    with pytest.raises(ValueError):
        cross_check_engines(a, b, "frame.png", threshold=-0.1)


def test_custom_threshold_can_be_stricter() -> None:
    """A 0.99 threshold rejects the borderline-disagreement-with-typo
    case that would otherwise count as agreed at the 0.85 default."""
    shared = [_w(t) for t in ["this", "is", "what", "i", "call", "being", "rich", "in", "life"]]
    a = FakeEngine(name="a", words=shared + [_w("today")])
    b = FakeEngine(name="b", words=shared + [_w("todayy")])

    result = cross_check_engines(a, b, "frame.png", threshold=0.99)

    assert result.status == "disagreed"
    assert result.agreement < 0.99
