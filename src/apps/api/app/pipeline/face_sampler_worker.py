"""Killable OpenCV face sampler used by render_geometry's hard timeout."""

from __future__ import annotations

import json
import os
import sys


def sample(video_path: str, anchors: list[float]) -> dict:
    import cv2

    capture = cv2.VideoCapture(video_path)
    samples: list[dict] = []
    attempted = 0
    try:
        cascade_path = os.path.join(
            cv2.data.haarcascades,
            "haarcascade_frontalface_default.xml",
        )
        cascade = cv2.CascadeClassifier(cascade_path)
        for at_s in anchors:
            attempted += 1
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, at_s) * 1000)
            ok, frame = capture.read()
            if not ok:
                continue
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
    return {"attempted": attempted, "samples": samples}


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        anchors = [float(value) for value in json.loads(sys.argv[2])]
        print(json.dumps(sample(sys.argv[1], anchors), separators=(",", ":")))
        return 0
    except Exception:
        print(json.dumps({"attempted": 0, "samples": []}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
