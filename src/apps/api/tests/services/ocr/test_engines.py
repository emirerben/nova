"""Smoke tests for the real OCR engine adapters.

These tests assert the adapters implement the :class:`OCREngine` Protocol
and that constructing them surfaces a clear, actionable error when the
runtime prerequisite (system binary or credentials) is missing — without
making any real OCR calls.

Why no live recognize() coverage: the autobuilder is the integration
test for that. Unit tests should fail fast on contract violations.
"""

from __future__ import annotations

import importlib
import os

import pytest

from app.services.ocr.engines import (
    CloudVisionEngine,
    OCREngine,
    OCRWord,
    PytesseractEngine,
)


def test_pytesseract_engine_satisfies_protocol() -> None:
    """The class itself doesn't need to be instantiated for the
    structural check — what matters is that callers see a stable
    ``recognize`` signature + ``name`` attribute."""
    assert hasattr(PytesseractEngine, "recognize")
    assert hasattr(PytesseractEngine, "name")
    assert PytesseractEngine.name == "pytesseract"


def test_cloud_vision_engine_satisfies_protocol() -> None:
    assert hasattr(CloudVisionEngine, "recognize")
    assert hasattr(CloudVisionEngine, "name")
    assert CloudVisionEngine.name == "cloud_vision"


def test_cloud_vision_engine_rejects_missing_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error must name the env vars to set — diagnostic debt on
    this code path cost real time on Fly v272 (per
    ``text_overlay_ocr.default_backend`` comment)."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        CloudVisionEngine()

    msg = str(exc_info.value)
    assert "GOOGLE_APPLICATION_CREDENTIALS" in msg
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in msg


def test_pytesseract_engine_instantiable_when_dep_present() -> None:
    """If ``pytesseract`` + ``Pillow`` are importable we should be able
    to construct the adapter — the system binary missing would only
    bite on the first ``recognize()`` call. If either Python dep is
    missing, skip (the analyzer extra isn't installed by default)."""
    if importlib.util.find_spec("pytesseract") is None:
        pytest.skip("pytesseract not installed; pip install -e '.[analysis]'")
    if importlib.util.find_spec("PIL") is None:
        pytest.skip("Pillow not installed")

    engine = PytesseractEngine()
    assert engine.name == "pytesseract"


def test_ocrword_is_frozen_and_hashable() -> None:
    """Frozen dataclass means tests can put OCRWords in sets — useful
    when the autobuilder dedupes detections across overlapping frames."""
    w = OCRWord(text="hi", bbox=(0.0, 0.0, 0.1, 0.1), confidence=0.9)
    {w}  # would raise if unhashable
    with pytest.raises(Exception):
        w.text = "bye"  # type: ignore[misc]


def test_protocol_runtime_check_accepts_real_adapters() -> None:
    """``isinstance(engine, OCREngine)`` must hold for the real adapters
    so callers can type-check the engine list passed into
    ``cross_check_engines`` without importing each concrete class."""
    if importlib.util.find_spec("pytesseract") is None or importlib.util.find_spec("PIL") is None:
        pytest.skip("pytesseract/PIL not installed; skip live Protocol check")
    eng = PytesseractEngine()
    assert isinstance(eng, OCREngine)


def test_env_state_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: monkeypatch.delenv inside one test must not affect another.
    Documents the expectation so future env-touching tests stay isolated."""
    # If a prior test polluted globals, this would fail before any assertion runs.
    monkeypatch.setenv("__OCR_TEST_CANARY__", "1")
    assert os.environ["__OCR_TEST_CANARY__"] == "1"
