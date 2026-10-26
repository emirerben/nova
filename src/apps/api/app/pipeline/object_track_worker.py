"""Subprocess body of `object_track.track_object` (KRI-827).

Follows one creator-marked object (a box on one source frame) through the clip at a fixed
step, and streams one JSON line per anchor so a timeout keeps what it already found:

    {"at_s": 1.2, "box": [l, t, r, b] | null, "status": "tracked" | "lost", "score": 0.83}

then a summary line ``{"done": true, ...}``. Boxes are normalized to the decoded (upright)
frame. Run as ``python -m app.pipeline.object_track_worker <video> <json-spec>``.

How it decides: a correlation tracker (CSRT, else KRI's fallbacks KCF / MIL) proposes the
box at each anchor, and every proposal is re-checked against the creator's seed patch
(normalized cross-correlation of a small grey patch) and a size bound. A proposal that fails
the check is ``lost`` -- never reported as the object -- and the worker tries to re-acquire
the seed patch near the last good box before tracking on. Anchors before the seed time are
tracked backwards over the frames buffered on the way to the seed.
"""

from __future__ import annotations

import json
import sys
from typing import Any

# The frame is downscaled to this width before tracking (analysis proxies are ~720p; the
# object is a large thing in the frame -- a plate, a cup -- so 320 px keeps its texture).
_TRACK_WIDTH = 320
_PATCH = 24
# Appearance is compared on the box's middle (this share trimmed off each side).
_INNER_MARGIN = 0.22
# Below this similarity to the seed patch a proposed box is not the marked object.
_MIN_NCC = 0.35
# Re-acquisition by template search needs a stronger match than tracking continuity.
_REACQUIRE_NCC = 0.6
_REACQUIRE_RADIUS = 0.35
# A tracked box whose area drifts this far from the seed's is not the marked object.
_AREA_RATIO = (0.35, 2.8)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _make_tracker(cv2: Any) -> Any:
    for name in ("TrackerCSRT_create", "TrackerKCF_create", "TrackerMIL_create"):
        factory = getattr(cv2, name, None)
        if factory is not None:
            return factory()
    legacy = getattr(cv2, "legacy", None)
    if legacy is not None and hasattr(legacy, "TrackerCSRT_create"):
        return legacy.TrackerCSRT_create()
    raise RuntimeError("no OpenCV tracker available")


def _inner(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """The middle of the box: what the object itself looks like, without its outline or
    the background in its corners (a round face and a round plate share a silhouette)."""
    x, y, w, h = box
    dx, dy = round(w * _INNER_MARGIN), round(h * _INNER_MARGIN)
    return x + dx, y + dy, max(1, w - 2 * dx), max(1, h - 2 * dy)


def _patch(cv2: Any, np: Any, frame: Any, box: tuple[int, int, int, int]) -> Any | None:
    x, y, w, h = _inner(box)
    if w < 4 or h < 4:
        return None
    crop = frame[max(0, y) : y + h, max(0, x) : x + w]
    if crop.size == 0:
        return None
    grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(grey, (_PATCH, _PATCH), interpolation=cv2.INTER_AREA).astype("float32")
    small -= small.mean()
    norm = float(np.linalg.norm(small))
    return small / norm if norm > 1e-6 else None


def _ncc(np: Any, a: Any, b: Any) -> float:
    if a is None or b is None:
        return 0.0
    return float((a * b).sum())


def _colour(cv2: Any, frame: Any, box: tuple[int, int, int, int]) -> Any | None:
    x, y, w, h = _inner(box)
    crop = frame[max(0, y) : y + h, max(0, x) : x + w]
    if crop.size == 0:
        return None
    return crop.reshape(-1, 3).mean(axis=0)


def _colour_close(np: Any, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    return float(np.abs(a - b).max()) <= 40.0


def main(argv: list[str]) -> int:
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    video_path = argv[1]
    spec = json.loads(argv[2])
    seed_t = float(spec["seed_t_s"])
    seed_box = [float(v) for v in spec["box"]]
    anchors = sorted({round(float(v), 3) for v in spec["anchors_s"]})

    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        _emit({"done": True, "error": "unreadable"})
        return 0
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0) or 30.0
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    def frame_index(at_s: float) -> int:
        index = int(round(at_s * fps))
        return max(0, min(index, frame_count - 1)) if frame_count > 0 else max(0, index)

    seed_index = frame_index(seed_t)
    wanted: dict[int, list[float]] = {}
    for at_s in anchors:
        wanted.setdefault(frame_index(at_s), []).append(at_s)
    first_needed = min([seed_index, *wanted])
    last_needed = max([seed_index, *wanted])

    def small(frame: Any) -> Any:
        height, width = frame.shape[:2]
        if width <= _TRACK_WIDTH:
            return frame
        scale = _TRACK_WIDTH / width
        return cv2.resize(
            frame, (_TRACK_WIDTH, round(height * scale)), interpolation=cv2.INTER_AREA
        )

    # Decode from the first frame needed; buffer only the anchor frames BEFORE the seed
    # (tracked backwards later); frames after the seed are tracked as they stream.
    capture.set(cv2.CAP_PROP_POS_FRAMES, first_needed)
    index = first_needed
    before: dict[int, Any] = {}
    seed_frame = None
    while index <= seed_index:
        ok, frame = capture.read()
        if not ok:
            break
        if index == seed_index:
            seed_frame = small(frame)
        elif index in wanted:
            before[index] = small(frame)
        index += 1
    if seed_frame is None:
        _emit({"done": True, "error": "seed_frame_unreadable"})
        return 0
    height, width = seed_frame.shape[:2]

    def to_px(box: list[float]) -> tuple[int, int, int, int]:
        left, top = round(box[0] * width), round(box[1] * height)
        right, bottom = round(box[2] * width), round(box[3] * height)
        return left, top, max(1, right - left), max(1, bottom - top)

    def to_norm(box: tuple[float, float, float, float]) -> list[float]:
        x, y, w, h = box
        return [
            round(max(0.0, x / width), 4),
            round(max(0.0, y / height), 4),
            round(min(1.0, (x + w) / width), 4),
            round(min(1.0, (y + h) / height), 4),
        ]

    seed_px = to_px(seed_box)
    seed_patch = _patch(cv2, np, seed_frame, seed_px)
    seed_colour = _colour(cv2, seed_frame, seed_px)
    seed_area = float(seed_px[2] * seed_px[3])
    if seed_patch is None:
        _emit({"done": True, "error": "seed_box_empty"})
        return 0

    def verified(frame: Any, box: tuple[int, int, int, int]) -> float | None:
        area = float(box[2] * box[3])
        if not (_AREA_RATIO[0] <= area / seed_area <= _AREA_RATIO[1]):
            return None
        score = _ncc(np, seed_patch, _patch(cv2, np, frame, box))
        if score < _MIN_NCC or not _colour_close(np, seed_colour, _colour(cv2, frame, box)):
            return None
        return score

    def reacquire(frame: Any, near: tuple[int, int, int, int]) -> tuple | None:
        """The seed patch searched for near the last good box (never across the frame:
        a decoy elsewhere must not become the marked object)."""
        x, y, w, h = near
        pad_x, pad_y = round(_REACQUIRE_RADIUS * width), round(_REACQUIRE_RADIUS * height)
        x0, y0 = max(0, x - pad_x), max(0, y - pad_y)
        x1, y1 = min(width, x + w + pad_x), min(height, y + h + pad_y)
        region = frame[y0:y1, x0:x1]
        template = seed_frame[
            max(0, seed_px[1]) : seed_px[1] + seed_px[3],
            max(0, seed_px[0]) : seed_px[0] + seed_px[2],
        ]
        if region.shape[0] < template.shape[0] or region.shape[1] < template.shape[1]:
            return None
        result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        _min_v, max_v, _min_loc, max_loc = cv2.minMaxLoc(result)
        if max_v < _REACQUIRE_NCC:
            return None
        found = (x0 + max_loc[0], y0 + max_loc[1], template.shape[1], template.shape[0])
        return found if verified(frame, found) is not None else None

    def run(frames: Any) -> None:
        tracker = _make_tracker(cv2)
        tracker.init(seed_frame, seed_px)
        last_good = seed_px
        for frame_no, frame in frames:
            ok, proposal = tracker.update(frame)
            box = tuple(int(round(v)) for v in proposal) if ok else None
            score = verified(frame, box) if box is not None else None
            if score is None:
                found = reacquire(frame, last_good)
                if found is not None:
                    tracker = _make_tracker(cv2)
                    tracker.init(frame, found)
                    box, score = found, verified(frame, found)
            for at_s in wanted.get(frame_no, ()):
                if score is not None and box is not None:
                    _emit(
                        {
                            "at_s": at_s,
                            "box": to_norm(box),
                            "status": "tracked",
                            "score": round(score, 3),
                        }
                    )
                else:
                    _emit({"at_s": at_s, "box": None, "status": "lost"})
            if score is not None and box is not None:
                last_good = box

    for at_s in wanted.get(seed_index, ()):
        _emit({"at_s": at_s, "box": to_norm(seed_px), "status": "tracked", "score": 1.0})

    # Backwards over the buffered anchor frames, nearest to the seed first.
    run(sorted(before.items(), reverse=True))
    before.clear()

    def forward() -> Any:
        frame_no = seed_index + 1
        while frame_no <= last_needed:
            ok, frame = capture.read()
            if not ok:
                return
            # Every decoded frame feeds the tracker (continuity); only anchors are emitted.
            yield frame_no, small(frame)
            frame_no += 1

    run(forward())
    _emit({"done": True, "fps": fps, "frame_size": [width, height]})
    capture.release()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
