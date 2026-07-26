"""resolve_face_cascade_path(): the vendored cascade must win, and a bad path
must fail loud (cascade.empty()) instead of silently reporting zero faces.

Root-caused prod incident: cv2's wheel-bundled `cv2.data.haarcascades` dir does
not ship the cascade XML in the prod image (mediapipe pulls in
opencv-contrib-python alongside opencv-python-headless — see
app/pipeline/face_sampler_worker.py's resolve_face_cascade_path() docstring
and agents/DECISIONS.md). Face-aware caption/thumbnail placement silently fell
back to preset placement on every prod job. cv2 is a hard dependency
(pyproject.toml) so these tests don't need a skip guard, but they lazy-import
cv2 inside each test body anyway, matching the module's own lazy-import
discipline (keeps this file importable in any environment where cv2 happens
to be unavailable, same principle as the skia lazy-import rule).
"""

from __future__ import annotations

import pytest

from app.pipeline.face_sampler_worker import (
    _VENDORED_CASCADE_PATH,
    FaceCascadeLoadError,
    _load_cascade,
    resolve_face_cascade_path,
)


def test_resolve_face_cascade_path_returns_vendored_asset() -> None:
    """The bundled assets/cv/ cascade is preferred over the cv2 wheel path."""
    assert _VENDORED_CASCADE_PATH.is_file(), (
        f"vendored cascade missing at {_VENDORED_CASCADE_PATH} — "
        "did assets/cv/haarcascade_frontalface_default.xml get removed?"
    )
    assert resolve_face_cascade_path() == str(_VENDORED_CASCADE_PATH)


def test_vendored_cascade_is_a_real_cascade_not_a_stub() -> None:
    """Guard against an accidental placeholder/truncated file: a real OpenCV
    frontal-face Haar cascade is ~900KB-1MB; a stub or LFS pointer is not."""
    size = _VENDORED_CASCADE_PATH.stat().st_size
    assert size > 500_000, (
        f"cascade file suspiciously small ({size} bytes) — check it's the real XML, not a stub"
    )


def test_cascade_classifier_loads_vendored_cascade_non_empty() -> None:
    cv2 = pytest.importorskip("cv2")

    cascade = cv2.CascadeClassifier(resolve_face_cascade_path())
    assert not cascade.empty()


def test_load_cascade_returns_usable_classifier() -> None:
    pytest.importorskip("cv2")

    cascade = _load_cascade()
    assert not cascade.empty()


def test_load_cascade_raises_structured_error_on_bad_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path that resolves to a non-cascade file must raise FaceCascadeLoadError
    naming the path — not silently produce an always-empty classifier that
    reports zero faces on every frame (the opaque `rc_1` prod failure mode)."""
    pytest.importorskip("cv2")

    monkeypatch.setattr(
        "app.pipeline.face_sampler_worker.resolve_face_cascade_path",
        lambda: str(_VENDORED_CASCADE_PATH.parent / "does-not-exist.xml"),
    )
    with pytest.raises(FaceCascadeLoadError, match="does-not-exist.xml"):
        _load_cascade()
