"""Cross-engine OCR ground-truth scaffolding.

This package hosts the pluggable :class:`OCREngine` protocol plus the two
production adapters (pytesseract + Cloud Vision) used by the Layer-2
ground-truth autobuilder. Cross-checking two independent engines is the
trust mechanism — only fixtures where both engines agree above a
configurable Levenshtein-similarity threshold get auto-committed; the rest
land in a ``disagreements/`` folder for human review.

Why a Protocol (not an ABC): adapters in this codebase are constructed
lazily (Cloud Vision needs creds; pytesseract needs the system binary).
A Protocol keeps the contract narrow and lets tests use bare classes
without inheriting from a base — see ``tests/services/ocr/`` for the
fake-engine shape used in unit tests.

The :class:`OCREngine` adapter shape is intentionally simpler than the
pipeline's ``OcrBackend`` (in ``app/services/text_overlay_ocr.py``) — that
one returns rich polygon detections wired into Pydantic schemas for the
Layer-2 pipeline. This one only carries what cross-checking needs:
token text, normalized bbox, confidence. The two backends could
share a common interface in the future, but conflating them today would
drag the pipeline schemas into the autobuilder's dependency surface for
no upside.
"""

from app.services.ocr.engines import (
    CloudVisionEngine,
    OCREngine,
    OCRWord,
    PytesseractEngine,
)

__all__ = [
    "CloudVisionEngine",
    "OCREngine",
    "OCRWord",
    "PytesseractEngine",
]
