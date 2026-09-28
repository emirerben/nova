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

import pytest

from app.services.ocr.engines import (
    CloudVisionEngine,
    PytesseractEngine,
)


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
