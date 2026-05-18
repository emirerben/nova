"""Unit tests for the OCR-backend wrapper.

Two backends, same `FrameDetection` shape. Cloud Vision is tested with a
mocked SDK so CI doesn't need GCP credentials. Apple Vision is tested only
when `Vision` is importable (macOS local dev); CI on Linux skips.

Schema tests live alongside backend tests so coordinate / polygon math
regressions surface in one place.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.agents._schemas.text_overlay_ocr import FrameDetection, OcrPolygon

# ── Schema tests ──────────────────────────────────────────────────────────────


def test_polygon_must_have_four_points():
    with pytest.raises(ValidationError):
        OcrPolygon(points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)])


def test_polygon_aabb_collapses_to_axis_aligned_bbox():
    # Rotated quad — the bbox should encompass all four vertices.
    poly = OcrPolygon(points=[(0.2, 0.1), (0.8, 0.05), (0.85, 0.4), (0.15, 0.45)])
    x_min, y_min, x_max, y_max = poly.aabb()
    assert x_min == pytest.approx(0.15)
    assert y_min == pytest.approx(0.05)
    assert x_max == pytest.approx(0.85)
    assert y_max == pytest.approx(0.45)


def test_frame_detection_rejects_empty_text():
    with pytest.raises(ValidationError):
        FrameDetection(
            frame_t_s=1.0,
            text="",
            polygon=OcrPolygon(points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]),
            confidence=0.9,
        )


def test_frame_detection_rejects_negative_time():
    with pytest.raises(ValidationError):
        FrameDetection(
            frame_t_s=-0.1,
            text="hi",
            polygon=OcrPolygon(points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]),
            confidence=0.9,
        )


def test_frame_detection_rejects_confidence_outside_unit():
    poly = OcrPolygon(points=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
    with pytest.raises(ValidationError):
        FrameDetection(frame_t_s=0.0, text="x", polygon=poly, confidence=1.5)
    with pytest.raises(ValidationError):
        FrameDetection(frame_t_s=0.0, text="x", polygon=poly, confidence=-0.1)


# ── _clamp01 / _assemble_paragraph_text helpers ───────────────────────────────


def test_clamp01_handles_overshoot_and_undershoot():
    from app.services.text_overlay_ocr import _clamp01

    assert _clamp01(-0.01) == 0.0
    assert _clamp01(0.0) == 0.0
    assert _clamp01(0.5) == 0.5
    assert _clamp01(1.0) == 1.0
    assert _clamp01(1.0001) == 1.0


def _fake_vision_module_for_paragraph():
    """Build the smallest Cloud Vision module surface our helper touches.

    The real SDK exposes `vision.TextAnnotation.DetectedBreak.BreakType` as
    an enum with attribute access. We mimic that with plain ints — the
    helper compares with `==` against the attribute values, so identity
    doesn't matter as long as the same constants are used on both sides.
    """
    break_types = SimpleNamespace(SPACE=1, SURE_SPACE=2, EOL_SURE_SPACE=3, LINE_BREAK=4)
    detected_break = SimpleNamespace(BreakType=break_types)
    text_annotation = SimpleNamespace(DetectedBreak=detected_break)
    return SimpleNamespace(TextAnnotation=text_annotation)


def _make_symbol(ch: str, break_type: int | None) -> SimpleNamespace:
    bt = break_type if break_type is not None else 0  # 0 = no break
    return SimpleNamespace(
        text=ch,
        property=SimpleNamespace(detected_break=SimpleNamespace(type_=bt)),
    )


def test_assemble_paragraph_text_joins_words_and_preserves_line_breaks():
    from app.services.text_overlay_ocr import _assemble_paragraph_text

    vision = _fake_vision_module_for_paragraph()
    BT = vision.TextAnnotation.DetectedBreak.BreakType
    # Two words on line 1 ("It's not"), then a hard line break, then "luck"
    paragraph = SimpleNamespace(
        words=[
            SimpleNamespace(
                symbols=[
                    _make_symbol("I", None),
                    _make_symbol("t", None),
                    _make_symbol("'", None),
                    _make_symbol("s", BT.SPACE),
                ]
            ),
            SimpleNamespace(
                symbols=[
                    _make_symbol("n", None),
                    _make_symbol("o", None),
                    _make_symbol("t", BT.LINE_BREAK),
                ]
            ),
            SimpleNamespace(
                symbols=[
                    _make_symbol("l", None),
                    _make_symbol("u", None),
                    _make_symbol("c", None),
                    _make_symbol("k", None),
                ]
            ),
        ]
    )
    assert _assemble_paragraph_text(paragraph, vision) == "It's not\nluck"


def test_assemble_paragraph_text_empty_paragraph_returns_empty_string():
    from app.services.text_overlay_ocr import _assemble_paragraph_text

    vision = _fake_vision_module_for_paragraph()
    paragraph = SimpleNamespace(words=[])
    assert _assemble_paragraph_text(paragraph, vision) == ""


# ── CloudVisionBackend (mocked SDK) ───────────────────────────────────────────


@pytest.fixture
def fake_cloud_vision_module():
    """A `google.cloud.vision` stand-in that records detect() arguments.

    The backend's `__init__` does `from google.cloud import vision` which
    is awkward to patch via sys.modules (the `from X import Y` form looks
    Y up as an attribute of X, not as a sub-module). Tests using this
    fixture construct CloudVisionBackend via `_make_backend()` which
    bypasses __init__ and injects the fake module + mocked client directly.
    """
    fake = SimpleNamespace()
    fake.ImageAnnotatorClient = MagicMock()

    # Mirror the parts of the SDK our code actually uses.
    break_types = SimpleNamespace(SPACE=1, SURE_SPACE=2, EOL_SURE_SPACE=3, LINE_BREAK=4)
    fake.TextAnnotation = SimpleNamespace(DetectedBreak=SimpleNamespace(BreakType=break_types))

    def _image_ctor(content):  # noqa: ARG001 — content is what the SDK accepts
        return SimpleNamespace(content=content)

    fake.Image = _image_ctor

    return fake


def _make_backend(fake_vision):
    """Construct CloudVisionBackend with the fake SDK injected.

    Skips the real __init__ (which would try to instantiate a Cloud Vision
    client and need network/auth) and wires up the two attributes the
    detect() method reads.
    """
    from app.services.text_overlay_ocr import CloudVisionBackend

    backend = CloudVisionBackend.__new__(CloudVisionBackend)
    backend._vision = fake_vision
    backend._client = fake_vision.ImageAnnotatorClient.return_value
    return backend


def _vertex(x, y):  # noqa: D401
    return SimpleNamespace(x=x, y=y)


def _build_paragraph(text: str, vertices, confidence: float, vision_module):
    """Construct a paragraph mock as Cloud Vision shapes it.

    `text` is split on whitespace into words; spaces become SPACE breaks
    between symbols. Vertices is the 4-tuple polygon in absolute pixels.
    """
    BT = vision_module.TextAnnotation.DetectedBreak.BreakType
    words: list = []
    tokens = text.split(" ")
    for i, tok in enumerate(tokens):
        symbols = []
        for j, ch in enumerate(tok):
            is_last_sym_of_word = j == len(tok) - 1
            is_last_word_of_para = i == len(tokens) - 1
            if is_last_sym_of_word and not is_last_word_of_para:
                bt = BT.SPACE
            else:
                bt = 0
            symbols.append(
                SimpleNamespace(
                    text=ch,
                    property=SimpleNamespace(detected_break=SimpleNamespace(type_=bt)),
                )
            )
        words.append(SimpleNamespace(symbols=symbols))
    return SimpleNamespace(
        words=words,
        bounding_box=SimpleNamespace(vertices=vertices),
        confidence=confidence,
    )


def test_cloud_vision_detect_normalizes_polygon_to_unit_square(fake_cloud_vision_module, tmp_path):
    fake = fake_cloud_vision_module

    # 1080x1920 image with a paragraph at pixels (108, 192) → (972, 384)
    para = _build_paragraph(
        "It's not",
        vertices=[
            _vertex(108, 192),
            _vertex(972, 192),
            _vertex(972, 384),
            _vertex(108, 384),
        ],
        confidence=0.97,
        vision_module=fake,
    )
    fake_response = SimpleNamespace(
        error=SimpleNamespace(message=""),
        full_text_annotation=SimpleNamespace(
            pages=[
                SimpleNamespace(blocks=[SimpleNamespace(paragraphs=[para])]),
            ]
        ),
    )
    fake.ImageAnnotatorClient.return_value.document_text_detection.return_value = fake_response

    # Stub PIL.Image.open to a context manager yielding width/height.
    img_path = tmp_path / "frame.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xd9")  # tiny JPEG-ish; PIL is mocked
    fake_img = SimpleNamespace(width=1080, height=1920)

    class _FakeOpenCtx:
        def __enter__(self_inner):  # noqa: N805
            return fake_img

        def __exit__(self_inner, *a):  # noqa: N805
            return False

    with patch("PIL.Image.open", return_value=_FakeOpenCtx()):
        backend = _make_backend(fake)
        results = backend.detect(str(img_path), frame_t_s=2.5)

    assert backend.name == "cloud-vision"
    assert len(results) == 1
    det = results[0]
    assert det.text == "It's not"
    assert det.frame_t_s == 2.5
    assert det.confidence == pytest.approx(0.97)
    assert det.polygon.points == [
        (pytest.approx(0.1), pytest.approx(0.1)),
        (pytest.approx(0.9), pytest.approx(0.1)),
        (pytest.approx(0.9), pytest.approx(0.2)),
        (pytest.approx(0.1), pytest.approx(0.2)),
    ]


def test_cloud_vision_detect_raises_on_api_error(fake_cloud_vision_module, tmp_path):
    fake = fake_cloud_vision_module
    fake_response = SimpleNamespace(
        error=SimpleNamespace(message="QUOTA_EXCEEDED"),
        full_text_annotation=SimpleNamespace(pages=[]),
    )
    fake.ImageAnnotatorClient.return_value.document_text_detection.return_value = fake_response
    img_path = tmp_path / "frame.jpg"
    img_path.write_bytes(b"x")
    fake_img = SimpleNamespace(width=10, height=10)

    class _FakeOpenCtx:
        def __enter__(self_inner):  # noqa: N805
            return fake_img

        def __exit__(self_inner, *a):  # noqa: N805
            return False

    with patch("PIL.Image.open", return_value=_FakeOpenCtx()):
        backend = _make_backend(fake)
        with pytest.raises(RuntimeError, match="QUOTA_EXCEEDED"):
            backend.detect(str(img_path), frame_t_s=0.0)


def test_cloud_vision_detect_skips_paragraphs_with_non_quad_polygons(
    fake_cloud_vision_module, tmp_path
):
    fake = fake_cloud_vision_module
    # 3-vertex polygon — should be skipped, not crash.
    para = _build_paragraph(
        "weird",
        vertices=[_vertex(0, 0), _vertex(10, 0), _vertex(5, 5)],
        confidence=0.5,
        vision_module=fake,
    )
    fake_response = SimpleNamespace(
        error=SimpleNamespace(message=""),
        full_text_annotation=SimpleNamespace(
            pages=[SimpleNamespace(blocks=[SimpleNamespace(paragraphs=[para])])]
        ),
    )
    fake.ImageAnnotatorClient.return_value.document_text_detection.return_value = fake_response

    img_path = tmp_path / "frame.jpg"
    img_path.write_bytes(b"x")
    fake_img = SimpleNamespace(width=100, height=100)

    class _FakeOpenCtx:
        def __enter__(self_inner):  # noqa: N805
            return fake_img

        def __exit__(self_inner, *a):  # noqa: N805
            return False

    with patch("PIL.Image.open", return_value=_FakeOpenCtx()):
        backend = _make_backend(fake)
        results = backend.detect(str(img_path), frame_t_s=0.0)

    assert results == []


# ── AppleVisionBackend (macOS only) ───────────────────────────────────────────


@pytest.fixture
def apple_vision_or_skip():
    try:
        import Vision  # type: ignore[import]  # noqa: F401
    except ImportError:
        pytest.skip("pyobjc-framework-Vision not installed — macOS dev-only backend")


def test_apple_vision_backend_instantiable(apple_vision_or_skip):
    from app.services.text_overlay_ocr import AppleVisionBackend

    backend = AppleVisionBackend()
    assert backend.name == "apple-vision"


# ── default_backend() selection ───────────────────────────────────────────────


def test_default_backend_prefers_cloud_vision_when_creds_present(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/fake/creds.json")
    from app.services import text_overlay_ocr as mod

    fake_cv = MagicMock()
    monkeypatch.setattr(mod, "_cloud_vision_available", lambda: True)
    monkeypatch.setattr(mod, "_apple_vision_available", lambda: True)
    monkeypatch.setattr(mod, "CloudVisionBackend", fake_cv)

    backend = mod.default_backend()
    fake_cv.assert_called_once_with()
    assert backend is fake_cv.return_value


def test_default_backend_falls_through_to_apple_when_no_cloud(monkeypatch):
    from app.services import text_overlay_ocr as mod

    fake_av = MagicMock()
    monkeypatch.setattr(mod, "_cloud_vision_available", lambda: False)
    monkeypatch.setattr(mod, "_apple_vision_available", lambda: True)
    monkeypatch.setattr(mod, "AppleVisionBackend", fake_av)

    backend = mod.default_backend()
    fake_av.assert_called_once_with()
    assert backend is fake_av.return_value


def test_default_backend_raises_with_install_hint_when_neither_available(monkeypatch):
    from app.services import text_overlay_ocr as mod

    monkeypatch.setattr(mod, "_cloud_vision_available", lambda: False)
    monkeypatch.setattr(mod, "_apple_vision_available", lambda: False)

    with pytest.raises(RuntimeError, match="No OCR backend available"):
        mod.default_backend()
