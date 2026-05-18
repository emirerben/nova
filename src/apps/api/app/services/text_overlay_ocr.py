"""OCR backends for the Layer-2 text-overlay extraction pipeline.

Two backends, same `FrameDetection` shape:

- `CloudVisionBackend` — Google Cloud Vision DOCUMENT_TEXT_DETECTION. The
  production backend. Uses whichever service account already authenticates
  GCS (`GOOGLE_APPLICATION_CREDENTIALS` or `GOOGLE_SERVICE_ACCOUNT_JSON`).
  Requires the `google-cloud-vision` extra; absent that, instantiation
  raises ImportError lazily so non-OCR call sites stay importable.

- `AppleVisionBackend` — macOS Vision framework via pyobjc. Local dev /
  eval only; cannot run on Fly's Linux containers. Requires
  `pyobjc-framework-Vision` and `pyobjc-framework-Quartz` (declared in
  the `dev-ocr` optional dep group).

`default_backend()` picks one at runtime: Cloud Vision if installed AND
creds are present, else Apple Vision if importable, else raises with a
helpful install hint.

PR 1 (this file) ships the wrapper only — no caller is wired in yet. The
wrapper exists so subsequent PRs land without touching dependency
bookkeeping again, and so the prod Docker build is validated against the
new transitive grpc/protobuf deps independently of behavior changes.
"""

from __future__ import annotations

import os
from typing import Protocol

import structlog

from app.agents._schemas.text_overlay_ocr import FrameDetection, OcrPolygon

log = structlog.get_logger()


class OcrBackend(Protocol):
    """OCR backend interface. Implementations must be safe to instantiate
    multiple times and must not mutate global state.
    """

    name: str

    def detect(self, image_path: str, *, frame_t_s: float) -> list[FrameDetection]:
        """Run OCR on `image_path` and return per-line detections.

        `frame_t_s` is stamped onto every returned FrameDetection so callers
        don't need to track filename → timestamp mapping separately.
        """
        ...


# ── Cloud Vision (prod) ───────────────────────────────────────────────────────


class CloudVisionBackend:
    name = "cloud-vision"

    def __init__(self) -> None:
        # Import inside __init__ so unrelated callers (e.g. config loaders,
        # the runtime registry import scan) don't pay the SDK + grpc import
        # cost just because this module is on PYTHONPATH.
        from google.cloud import vision  # type: ignore[import]  # noqa: PLC0415

        self._vision = vision
        self._client = vision.ImageAnnotatorClient()

    def detect(self, image_path: str, *, frame_t_s: float) -> list[FrameDetection]:
        from PIL import Image  # noqa: PLC0415

        with open(image_path, "rb") as f:
            content = f.read()
        # Cloud Vision returns absolute pixel coordinates; we need normalized
        # ones to stay backend-agnostic. PIL is already a project dep so the
        # extra image open is free relative to the network round trip.
        with Image.open(image_path) as img:
            width, height = img.width, img.height
        if width <= 0 or height <= 0:
            log.warning("ocr_invalid_image_dims", path=image_path, w=width, h=height)
            return []

        image = self._vision.Image(content=content)
        response = self._client.document_text_detection(image=image)
        if response.error.message:
            # The API surfaces transient and permanent errors the same way.
            # Surface as an exception; the caller (pipeline orchestrator)
            # decides retry policy.
            raise RuntimeError(f"cloud_vision_error: {response.error.message}")

        detections: list[FrameDetection] = []
        for page in response.full_text_annotation.pages or []:
            for block in page.blocks:
                for paragraph in block.paragraphs:
                    text = _assemble_paragraph_text(paragraph, self._vision)
                    if not text:
                        continue
                    poly = paragraph.bounding_box
                    if len(poly.vertices) != 4:
                        log.warning(
                            "ocr_paragraph_polygon_unexpected_vertices",
                            n=len(poly.vertices),
                            text=text[:40],
                        )
                        continue
                    pts: list[tuple[float, float]] = [
                        (
                            _clamp01(v.x / width),
                            _clamp01(v.y / height),
                        )
                        for v in poly.vertices
                    ]
                    detections.append(
                        FrameDetection(
                            frame_t_s=frame_t_s,
                            text=text,
                            polygon=OcrPolygon(points=pts),
                            confidence=float(paragraph.confidence),
                        )
                    )
        return detections


def _assemble_paragraph_text(paragraph, vision_module) -> str:
    """Reconstruct a paragraph's literal text from its word/symbol tree.

    Cloud Vision returns paragraphs as words-of-symbols-with-break-hints.
    We honor SPACE / SURE_SPACE / EOL_SURE_SPACE / LINE_BREAK so the
    assembled string matches what a human reads on screen, including
    multi-line break preservation as "\n".
    """
    break_types = vision_module.TextAnnotation.DetectedBreak.BreakType
    parts: list[str] = []
    for word in paragraph.words:
        for sym in word.symbols:
            parts.append(sym.text)
            bt = sym.property.detected_break.type_
            if bt in (break_types.SPACE, break_types.SURE_SPACE):
                parts.append(" ")
            elif bt in (break_types.EOL_SURE_SPACE, break_types.LINE_BREAK):
                parts.append("\n")
    return "".join(parts).strip()


def _clamp01(v: float) -> float:
    # Cloud Vision occasionally returns vertices a hair outside the frame
    # (rounding from interpolation). Clamp so the schema's [0,1] constraint
    # passes.
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return float(v)


# ── Apple Vision (macOS local dev) ────────────────────────────────────────────


class AppleVisionBackend:
    name = "apple-vision"

    def __init__(self) -> None:
        # Same lazy-import rationale as CloudVisionBackend. PyObjC's
        # lazy-attribute machinery is NOT thread-safe — the first access
        # to `Quartz.CFURLCreateWithFileSystemPath`, `Quartz.kCFURLPOSIXPathStyle`,
        # `Vision.VNRequestTextRecognitionLevelAccurate`, etc. mutates an
        # internal funcmap dict. Two worker threads hitting an unresolved
        # constant simultaneously race and raise KeyError. Pre-resolve every
        # attribute `detect()` uses on the main thread so the per-frame
        # parallel calls only read already-cached instance attrs.
        import Quartz  # type: ignore[import]  # noqa: PLC0415
        import Vision  # type: ignore[import]  # noqa: PLC0415

        self._cf_url_create = Quartz.CFURLCreateWithFileSystemPath
        self._cf_url_posix = Quartz.kCFURLPOSIXPathStyle
        self._cg_image_source_create_with_url = Quartz.CGImageSourceCreateWithURL
        self._cg_image_source_create_at_index = Quartz.CGImageSourceCreateImageAtIndex
        self._vn_text_request_cls = Vision.VNRecognizeTextRequest
        self._vn_level_accurate = Vision.VNRequestTextRecognitionLevelAccurate
        self._vn_handler_cls = Vision.VNImageRequestHandler

    def detect(self, image_path: str, *, frame_t_s: float) -> list[FrameDetection]:
        url = self._cf_url_create(None, image_path, self._cf_url_posix, False)
        src = self._cg_image_source_create_with_url(url, None)
        if src is None:
            return []
        cg = self._cg_image_source_create_at_index(src, 0, None)
        if cg is None:
            return []

        request = self._vn_text_request_cls.alloc().init()
        request.setRecognitionLevel_(self._vn_level_accurate)
        request.setUsesLanguageCorrection_(True)
        request.setRecognitionLanguages_(["en-US"])

        handler = self._vn_handler_cls.alloc().initWithCGImage_options_(cg, {})
        ok, _err = handler.performRequests_error_([request], None)
        if not ok or request.results() is None:
            return []

        detections: list[FrameDetection] = []
        for obs in request.results():
            candidates = obs.topCandidates_(1)
            if not candidates:
                continue
            cand = candidates[0]
            text = str(cand.string()).strip()
            if not text:
                continue
            # Vision's boundingBox is normalized [0,1] already, with origin at
            # bottom-left (Y-up). Flip Y so we expose Y-down (top-left) like
            # Cloud Vision does. Quad order: TL, TR, BR, BL.
            r = obs.boundingBox()
            x = float(r.origin.x)
            y = float(r.origin.y)
            w = float(r.size.width)
            h = float(r.size.height)
            left = _clamp01(x)
            right = _clamp01(x + w)
            top = _clamp01(1.0 - (y + h))
            bottom = _clamp01(1.0 - y)
            pts = [(left, top), (right, top), (right, bottom), (left, bottom)]
            detections.append(
                FrameDetection(
                    frame_t_s=frame_t_s,
                    text=text,
                    polygon=OcrPolygon(points=pts),
                    confidence=float(cand.confidence()),
                )
            )
        return detections


# ── Backend selection ─────────────────────────────────────────────────────────


def default_backend() -> OcrBackend:
    """Return the best available OCR backend for this environment.

    Order:
      1. Cloud Vision — `google-cloud-vision` installed AND a GCS-style
         credential env var present.
      2. Apple Vision — `pyobjc-framework-Vision` installed (macOS only).
      3. Raise RuntimeError with an install hint.
    """
    if _cloud_vision_available():
        try:
            return CloudVisionBackend()
        except Exception as exc:  # pragma: no cover — defensive
            log.warning("cloud_vision_init_failed", err=str(exc))

    if _apple_vision_available():
        try:
            return AppleVisionBackend()
        except Exception as exc:  # pragma: no cover — defensive
            log.warning("apple_vision_init_failed", err=str(exc))

    raise RuntimeError(
        "No OCR backend available. Install `google-cloud-vision` (prod) "
        "or `pyobjc-framework-Vision` (macOS dev). See "
        "src/apps/api/pyproject.toml extras `[ocr]` and `[dev-ocr]`."
    )


def _cloud_vision_available() -> bool:
    try:
        from google.cloud import vision  # type: ignore[import]  # noqa: F401, PLC0415
    except ImportError:
        return False
    return bool(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    )


def _apple_vision_available() -> bool:
    try:
        import Vision  # type: ignore[import]  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True
