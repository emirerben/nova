"""pytesseract wrapper that runs on color-masked crops only.

Importable without pytesseract installed; calls raise OCRNotAvailable so
the analyzer can fall back to color-region matching when Tesseract is
absent on the host.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image


class OCRNotAvailable(RuntimeError):
    """pytesseract module or tesseract binary missing."""


def _load_pytesseract():
    try:
        import pytesseract  # type: ignore
    except ImportError as e:
        raise OCRNotAvailable(
            "pytesseract not installed. Run: pip install pytesseract && "
            "brew install tesseract  (or apt-get install tesseract-ocr)"
        ) from e
    try:
        pytesseract.get_tesseract_version()
    except Exception as e:  # noqa: BLE001 — pytesseract raises a custom EnvironmentError
        raise OCRNotAvailable(
            "tesseract binary not found on PATH. Install with: "
            "brew install tesseract  (macOS) or apt-get install tesseract-ocr"
        ) from e
    return pytesseract


@dataclass
class OCRResult:
    text: str
    confidence: float  # 0..1


def _isolate_text_crop(
    frame: np.ndarray,
    mask: np.ndarray,
    pad_px: int = 8,
) -> Image.Image:
    """Return a PIL image where the mask is white-on-black, padded.

    Tesseract works far better on high-contrast, white-on-black crops than
    on the original noisy frame. We use the mask itself as the source so
    the OCR sees just glyph shape, not the underlying video.
    """
    if not mask.any():
        return Image.new("L", (10, 10), 0)
    ys, xs = np.where(mask)
    y_min, y_max = int(ys.min()), int(ys.max())
    x_min, x_max = int(xs.min()), int(xs.max())
    h, w = mask.shape
    y_min = max(0, y_min - pad_px)
    y_max = min(h - 1, y_max + pad_px)
    x_min = max(0, x_min - pad_px)
    x_max = min(w - 1, x_max + pad_px)
    crop = mask[y_min:y_max + 1, x_min:x_max + 1].astype(np.uint8) * 255
    return Image.fromarray(crop, mode="L")


def ocr_mask_crop(
    frame: np.ndarray,
    mask: np.ndarray,
    *,
    psm: int = 7,  # single text line
    whitelist: str | None = None,
) -> OCRResult:
    """Run Tesseract on a mask-derived crop and return text + mean confidence.

    psm=7 (single line) works well for our centered overlays. Use psm=8
    (single word) for one-word overlays like "is" or "AFRICA" if needed.
    """
    pyt = _load_pytesseract()
    img = _isolate_text_crop(frame, mask)

    config_parts = [f"--psm {psm}", "--oem 1"]
    if whitelist:
        config_parts.append(f"-c tessedit_char_whitelist={whitelist}")
    config = " ".join(config_parts)

    data = pyt.image_to_data(img, config=config, output_type=pyt.Output.DICT)
    tokens = [
        (t, int(c)) for t, c in zip(data["text"], data["conf"])
        if t and t.strip() and int(c) >= 0
    ]
    if not tokens:
        return OCRResult(text="", confidence=0.0)
    text = " ".join(t for t, _ in tokens)
    mean_conf = float(np.mean([c for _, c in tokens])) / 100.0
    return OCRResult(text=text.strip(), confidence=mean_conf)


def is_ocr_available() -> bool:
    try:
        _load_pytesseract()
        return True
    except OCRNotAvailable:
        return False
