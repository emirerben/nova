"""OCR engine adapters used by the ground-truth autobuilder.

Two concrete engines:

* :class:`PytesseractEngine` — local, no auth, runs against the tesseract
  binary already declared in the ``[analysis]`` extra. Free, fast, weaker
  on stylized fonts.
* :class:`CloudVisionEngine` — Google Cloud Vision document-text detection.
  Reuses the GCP credential chain wired in ``app/storage.get_gcp_credentials``
  so the autobuilder authenticates the same way the production OCR path
  does (``app/services/text_overlay_ocr.py``). Costs ~$1.50 per 1k frames.

Both adapters return :class:`OCRWord` records with bboxes normalized to
``[0, 1]`` so cross-checking is engine-agnostic. Imports are deferred to
the engines' ``__init__`` methods so importing this module never triggers
the SDK / grpc / system-binary load — that matters because the autobuilder
script imports the package eagerly to register engine subclasses, and CI
hosts (and unit tests) shouldn't have to satisfy the runtime deps just to
introspect the protocol.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class OCRWord:
    """One token-level OCR detection with normalized bbox.

    ``bbox`` is ``(x, y, w, h)`` in fractions of the source image. The
    upper-left of the image is ``(0, 0)``. ``confidence`` is per-engine —
    pytesseract reports 0-100 which we rescale to 0-1; Cloud Vision
    already reports 0-1.
    """

    text: str
    bbox: tuple[float, float, float, float]
    confidence: float


@runtime_checkable
class OCREngine(Protocol):
    """Minimal cross-engine OCR contract.

    Implementations MUST set ``name`` to a short identifier — it appears
    in the cross-check disagreement report so the operator can tell
    which engine produced which token set.
    """

    name: str

    def recognize(self, image_path: str) -> list[OCRWord]:
        """Run OCR on a single image and return token-level detections."""
        ...


# ── Pytesseract ──────────────────────────────────────────────────────────────


class PytesseractEngine:
    """Local OCR via ``pytesseract.image_to_data``.

    The tesseract binary must be installed on the host
    (``brew install tesseract`` / ``apt-get install tesseract-ocr``).
    Confidences below ``min_confidence`` are filtered as junk; the
    default mirrors the threshold the existing analyzer scripts use.
    """

    name = "pytesseract"

    def __init__(self, *, min_confidence: int = 30) -> None:
        # Lazy import so this module stays cheap to import on hosts that
        # don't have pytesseract installed.
        try:
            import pytesseract  # noqa: PLC0415
            from PIL import Image  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — env diagnostic
            raise RuntimeError(
                "PytesseractEngine requires `pytesseract` and `Pillow`. "
                "Install with: pip install -e '.[analysis]' "
                f"(missing: {exc.name})"
            ) from exc
        self._pytesseract = pytesseract
        self._Image = Image
        self._min_confidence = min_confidence

    def recognize(self, image_path: str) -> list[OCRWord]:
        img = self._Image.open(image_path)
        w, h = img.size
        if w <= 0 or h <= 0:
            return []
        data = self._pytesseract.image_to_data(img, output_type=self._pytesseract.Output.DICT)
        words: list[OCRWord] = []
        n = len(data["text"])
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                conf = int(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1
            if conf < self._min_confidence:
                continue
            x = int(data["left"][i])
            y = int(data["top"][i])
            ww = int(data["width"][i])
            hh = int(data["height"][i])
            words.append(
                OCRWord(
                    text=text,
                    bbox=(x / w, y / h, ww / w, hh / h),
                    confidence=conf / 100.0,
                )
            )
        return words


# ── Cloud Vision ─────────────────────────────────────────────────────────────


class CloudVisionEngine:
    """Google Cloud Vision document-text detection adapter.

    Auth: reuses ``app.storage.get_gcp_credentials`` — same service-account
    chain the production OCR backend uses. Raises a clear error if neither
    ``GOOGLE_APPLICATION_CREDENTIALS`` nor ``GOOGLE_SERVICE_ACCOUNT_JSON``
    is set. The SDK + grpc import (~150ms cold) is deferred to ``__init__``
    so importing this module is cheap.

    Coordinates returned by Cloud Vision are absolute pixels; we read
    width / height via Pillow (already a project dep) to normalize
    consistently with :class:`PytesseractEngine`. The axis-aligned bbox
    is computed as the convex-hull of the paragraph polygon's four
    vertices — Cloud Vision occasionally returns slightly skewed quads
    on warped overlays.
    """

    name = "cloud_vision"
    _VISION_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

    def __init__(self) -> None:
        if not (
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        ):
            raise RuntimeError(
                "CloudVisionEngine requires a GCP credential env var. "
                "Set GOOGLE_APPLICATION_CREDENTIALS (file path) or "
                "GOOGLE_SERVICE_ACCOUNT_JSON (inline JSON, e.g. Fly.io secret)."
            )
        try:
            from google.cloud import vision  # type: ignore[import]  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — env diagnostic
            raise RuntimeError(
                "CloudVisionEngine requires `google-cloud-vision`. "
                "It is already declared in pyproject.toml; reinstall with: "
                "pip install -e ."
            ) from exc
        from app.storage import get_gcp_credentials  # noqa: PLC0415

        try:
            from PIL import Image  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — env diagnostic
            raise RuntimeError("CloudVisionEngine requires `Pillow` to normalize bboxes.") from exc

        creds = get_gcp_credentials(scopes=self._VISION_SCOPES)
        self._client = vision.ImageAnnotatorClient(credentials=creds)
        self._vision = vision
        self._Image = Image

    def recognize(self, image_path: str) -> list[OCRWord]:
        with open(image_path, "rb") as f:
            content = f.read()
        with self._Image.open(image_path) as img:
            width, height = img.width, img.height
        if width <= 0 or height <= 0:
            return []
        image = self._vision.Image(content=content)
        response = self._client.document_text_detection(image=image)
        if response.error.message:
            raise RuntimeError(f"cloud_vision_error: {response.error.message}")

        words: list[OCRWord] = []
        for page in response.full_text_annotation.pages or []:
            for block in page.blocks:
                for paragraph in block.paragraphs:
                    for word in paragraph.words:
                        text = "".join(s.text for s in word.symbols).strip()
                        if not text:
                            continue
                        poly = word.bounding_box
                        if not poly.vertices:
                            continue
                        xs = [v.x for v in poly.vertices]
                        ys = [v.y for v in poly.vertices]
                        x0 = max(0, min(xs))
                        y0 = max(0, min(ys))
                        x1 = max(xs)
                        y1 = max(ys)
                        words.append(
                            OCRWord(
                                text=text,
                                bbox=(
                                    x0 / width,
                                    y0 / height,
                                    (x1 - x0) / width,
                                    (y1 - y0) / height,
                                ),
                                confidence=float(word.confidence or 0.0),
                            )
                        )
        return words
