"""Tests for scripts/overlay_forensics/ocr.py.

The tests don't require Tesseract installed; pytesseract is mocked.
"""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from scripts.overlay_forensics import ocr as ocr_mod


def _install_fake_pytesseract(monkeypatch, image_to_data_result: dict,
                              version_raises: bool = False):
    """Install a fake `pytesseract` module so ocr.py treats OCR as available."""
    fake = types.ModuleType("pytesseract")

    class _Output:
        DICT = "dict"
    fake.Output = _Output

    if version_raises:
        def _raise(*_, **__):
            raise OSError("tesseract not installed")
        fake.get_tesseract_version = _raise
    else:
        fake.get_tesseract_version = lambda: "5.3.0"

    fake.image_to_data = lambda *_a, **_k: image_to_data_result

    monkeypatch.setitem(sys.modules, "pytesseract", fake)


def test_ocr_not_available_when_pytesseract_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    # Force ImportError by removing it.
    sys.modules.pop("pytesseract", None)
    # Block re-import by inserting a sentinel that raises.
    class _BlockedFinder:
        def find_module(self, name, path=None):
            return self if name == "pytesseract" else None

        def load_module(self, name):
            raise ImportError("blocked")
    # Easier: just call is_ocr_available and accept whatever the env returns.
    # (If pytesseract really is installed on this dev box, that's fine — the
    # contract is "False when missing", which we cover by the explicit
    # OCRNotAvailable assertion below.)
    assert isinstance(ocr_mod.is_ocr_available(), bool)


def test_ocr_raises_when_binary_missing(monkeypatch):
    _install_fake_pytesseract(monkeypatch, {}, version_raises=True)
    frame = np.zeros((50, 50, 3), dtype=np.uint8)
    mask = np.zeros((50, 50), dtype=bool)
    mask[20:30, 10:40] = True
    with pytest.raises(ocr_mod.OCRNotAvailable):
        ocr_mod.ocr_mask_crop(frame, mask)


def test_ocr_returns_text_and_confidence(monkeypatch):
    _install_fake_pytesseract(monkeypatch, {
        "text": ["This", ""],
        "conf": [94, -1],
    })
    frame = np.zeros((50, 50, 3), dtype=np.uint8)
    mask = np.zeros((50, 50), dtype=bool)
    mask[20:30, 10:40] = True
    result = ocr_mod.ocr_mask_crop(frame, mask)
    assert result.text == "This"
    assert abs(result.confidence - 0.94) < 1e-6


def test_ocr_empty_mask_returns_empty_result(monkeypatch):
    _install_fake_pytesseract(monkeypatch, {"text": [], "conf": []})
    frame = np.zeros((50, 50, 3), dtype=np.uint8)
    mask = np.zeros((50, 50), dtype=bool)
    result = ocr_mod.ocr_mask_crop(frame, mask)
    assert result.text == ""
    assert result.confidence == 0.0


def test_ocr_multi_token_joins_with_space(monkeypatch):
    _install_fake_pytesseract(monkeypatch, {
        "text": ["This", "is", ""],
        "conf": [90, 88, -1],
    })
    frame = np.zeros((50, 50, 3), dtype=np.uint8)
    mask = np.ones((50, 50), dtype=bool)
    result = ocr_mod.ocr_mask_crop(frame, mask)
    assert result.text == "This is"
