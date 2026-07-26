"""Killable OpenCV face sampler used by render_geometry's hard timeout."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Cascade vendored into the prod image (assets/cv/) — see resolve_face_cascade_path()
# for why this is required rather than relying on cv2's wheel-bundled data dir.
_VENDORED_CASCADE_PATH = (
    Path(__file__).resolve().parents[2] / "assets" / "cv" / "haarcascade_frontalface_default.xml"
)


class FaceCascadeLoadError(RuntimeError):
    """Raised when cv2.CascadeClassifier fails to load the resolved cascade path.

    Distinguishes a broken/missing cascade file from a real zero-face frame —
    without this, cv2.CascadeClassifier(bad_path) silently returns an "empty"
    classifier that always reports zero faces, and the failure only surfaces
    later as an opaque `rc_1` in the sampler's worker_error receipt.
    """


def resolve_face_cascade_path() -> str:
    """Return a filesystem path to the frontal-face Haar cascade XML.

    Prefers the cascade vendored into the prod image at assets/cv/ — the
    wheel-bundled `cv2.data.haarcascades` directory is NOT reliable in the prod
    image: when mediapipe's opencv-contrib-python wheel wins dependency
    resolution over opencv-python-headless (pyproject.toml collision, see
    agents/DECISIONS.md), the winning cv2 package's `cv2.data.haarcascades` dir
    does not ship the cascade XML at all, and every prod job silently loses
    face-aware caption/thumbnail placement.

    Falls back to `cv2.data.haarcascades` (wheel path) for environments where
    the vendored asset is unexpectedly missing (e.g. a stale/shallow checkout).
    """
    if _VENDORED_CASCADE_PATH.is_file():
        return str(_VENDORED_CASCADE_PATH)
    import cv2  # local import: keep this module importable without cv2 installed

    return os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")


def _load_cascade():
    import cv2

    cascade_path = resolve_face_cascade_path()
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty():
        raise FaceCascadeLoadError(
            f"cv2.CascadeClassifier failed to load cascade at {cascade_path!r} "
            "(file missing, unreadable, or not a valid Haar cascade XML)"
        )
    return cascade


def sample(video_path: str, anchors: list[float]) -> dict:
    import cv2

    capture = cv2.VideoCapture(video_path)
    samples: list[dict] = []
    attempted = 0
    decoded = 0
    try:
        cascade = _load_cascade()
        for at_s in anchors:
            attempted += 1
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, at_s) * 1000)
            ok, frame = capture.read()
            if not ok:
                # Seek landed past a real EOF (e.g. a silence-cut base) or hit a
                # corrupt frame — NOT a decodable anchor. Reporting decoded lets
                # the placement chooser use it as the coverage denominator instead
                # of attempted (plan 011 Feature C — silence-cut safe).
                continue
            decoded += 1
            height, width = frame.shape[:2]
            scale = min(1.0, 480.0 / max(width, height))
            small = cv2.resize(frame, None, fx=scale, fy=scale)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4)
            if len(faces) == 0:
                continue
            x, y, face_w, face_h = max(faces, key=lambda face: face[2] * face[3])
            sw, sh = small.shape[1], small.shape[0]
            samples.append(
                {
                    "at_s": at_s,
                    "box": {
                        "left": x / sw,
                        "top": y / sh,
                        "right": (x + face_w) / sw,
                        "bottom": (y + face_h) / sh,
                    },
                }
            )
    finally:
        capture.release()
    return {"attempted": attempted, "decoded": decoded, "samples": samples}


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        anchors = [float(value) for value in json.loads(sys.argv[2])]
        print(json.dumps(sample(sys.argv[1], anchors), separators=(",", ":")))
        return 0
    except Exception as exc:
        # Surface the reason to stderr — render_geometry.sample_face_regions
        # folds stderr into the `worker_error` receipt (rc_<code>:<stderr>), so
        # a structured message here is what turns an opaque failure into a
        # diagnosable one in /admin/jobs.
        print(str(exc), file=sys.stderr)
        print(json.dumps({"attempted": 0, "samples": []}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
