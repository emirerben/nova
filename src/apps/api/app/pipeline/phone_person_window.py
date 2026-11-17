"""Where a sideways montage clip with no face shows its person (KRI-1514).

"Fill the whole screen" on a 16:9 clip keeps under a third of its width. With a recurring face
the crop follows the face (`phone_speaker_framing`); without one it kept the middle, so a shot
whose subject sits off-centre (Cusco T5: a hand on a stone wall, left of centre) lost it.

When the face pass finds no face, the RVM person matte (`subject_matte.PersonMatter`) looks for
a person or any body part over the moment's own source window, on the analysis proxy, and the
moment gets ONE static 9:16 window: the one holding the most person, used only when

* a person is really there: at least `MIN_PERSON_SHARE` of the picture on average, in at
  least `MIN_PERSON_PRESENCE` of the frames (a stadium crowd's specks and matte noise are not);
* and that window holds clearly more of them than the middle (`MIN_GAIN_OVER_CENTRE`), so a
  person already in the middle keeps today's crop exactly.

Scenery, objects and paintings have no person in the matte and keep the middle: the matte is
not a saliency map, so textures never pull the crop to the edge. The thresholds come from the
real clips measured for KRI-1514 (Cusco T5, a stadium, green-screen speakers, screen
recordings; `tests/fixtures/kri1514_cusco_t5_person_fill.json`).

Never raises: anything that fails (decode, onnxruntime, the model) is "no person", and the clip
keeps the middle. The shift is the face fill's own main-track ``position_x``, clamped so no
black edge shows: no new device capability.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import structlog

from app.pipeline.phone_recipe_shared import max_cover_shift_px

log = structlog.get_logger(__name__)

# Decoding: the analysis proxy at its own width, up to 6 frames a second and at most 24 frames
# per moment; RVM runs on a quarter of that (its production input scale).
_DECODE_WIDTH = 568
_MAX_FPS = 6.0
_MAX_FRAMES = 24
_MIN_FRAMES = 3
# RVM's recurrent state needs a couple of frames to settle on a cut's first frames.
_WARMUP_FRAMES = 2
_DECODE_TIMEOUT_S = 15.0
# A frame "has a person" when the matte covers at least this share of it.
_FRAME_PRESENCE_SHARE = 0.01
# Measured (KRI-1514, the fixture): the hand covers 1-13% of each frame, 4.1% on average, in
# every frame; the stadium crowd 0.2% and no frame over 1%; scenery 0.0-0.1%.
MIN_PERSON_SHARE = 0.02
MIN_PERSON_PRESENCE = 0.6
# The window must hold this much more of the person than the middle one (share of the
# person's pixels): the hand window keeps 0.92 against the middle's 0.38; a centred speaker
# keeps 0.99 either way, so nothing moves.
MIN_GAIN_OVER_CENTRE = 0.10

PersonSampler = Callable[[str, float, float], "PersonColumns | None"]


class _Canvas(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


@dataclass(frozen=True)
class PersonColumns:
    """How much person the matte saw in each column of a moment's frames.

    ``columns`` is normalized to sum to 1 (all zeros when no person), left to right over the
    source width; ``share`` is the mean share of the picture the person covers, ``present`` the
    share of frames with a person in them."""

    frames: int
    share: float
    present: float
    columns: tuple[float, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"frames": self.frames, "share": self.share, "present": self.present}


@dataclass(frozen=True)
class PersonWindow:
    """The static 9:16 window (source-normalized) and the shift that puts it on screen."""

    left: float
    right: float
    position_x: float

    def as_dict(self) -> dict[str, Any]:
        return {"left": self.left, "right": self.right, "position_x": self.position_x}


def person_columns(masks: Sequence[np.ndarray]) -> PersonColumns | None:
    """Columns off a moment's boolean person masks (the first frames dropped as warm-up)."""
    if len(masks) > _WARMUP_FRAMES + _MIN_FRAMES:
        masks = masks[_WARMUP_FRAMES:]
    if not masks:
        return None
    shares = [float(mask.mean()) for mask in masks]
    mass = np.sum([mask.sum(axis=0) for mask in masks], axis=0).astype(float)
    total = float(mass.sum())
    columns = mass / total if total > 0 else mass
    return PersonColumns(
        frames=len(masks),
        share=round(sum(shares) / len(shares), 5),
        present=round(sum(1 for s in shares if s >= _FRAME_PRESENCE_SHARE) / len(shares), 5),
        columns=tuple(round(float(value), 6) for value in columns),
    )


def person_window(
    found: PersonColumns,
    *,
    display_width: float,
    display_height: float,
    canvas: _Canvas,
) -> tuple[PersonWindow | None, dict[str, Any]]:
    """The window that keeps the person, or None (and why) to keep the middle."""
    evidence: dict[str, Any] = {"sampling": "ok", **found.as_dict()}
    columns = np.asarray(found.columns, dtype=float)
    if (
        found.share < MIN_PERSON_SHARE
        or found.present < MIN_PERSON_PRESENCE
        or columns.size == 0
        or columns.sum() <= 0
        or display_width <= 0
        or display_height <= 0
    ):
        return None, {**evidence, "decision": "no_person"}
    cover = max(canvas.width / display_width, canvas.height / display_height)
    shown_w = display_width * cover
    window = min(1.0, canvas.width / shown_w)
    n = columns.size
    span = max(1, min(n, round(window * n)))
    cumulative = np.concatenate([[0.0], np.cumsum(columns)])
    sums = cumulative[span:] - cumulative[:-span]
    centre = (n - span) / 2
    middle = int(round(centre))
    best_value = float(sums.max())
    # Among equally good windows, the one nearest the middle.
    best = min(
        (i for i, value in enumerate(sums) if value >= best_value - 1e-9),
        key=lambda i: abs(i - centre),
    )
    kept, centre_kept = round(float(sums[best]), 4), round(float(sums[middle]), 4)
    evidence.update(kept=kept, centre_kept=centre_kept)
    if kept < centre_kept + MIN_GAIN_OVER_CENTRE:
        return None, {**evidence, "decision": "centre_holds"}
    left = min(max(best / n, 0.0), 1.0 - window)
    limit = max_cover_shift_px(display_width, display_height, canvas)
    position_x = max(-limit, min(limit, (0.5 - (left + window / 2)) * shown_w))
    placed = PersonWindow(
        left=round(left, 5), right=round(left + window, 5), position_x=round(position_x, 2) + 0.0
    )
    return placed, {**evidence, "decision": "follow", "window": placed.as_dict()}


def _decode(clip_path: str, start_s: float, end_s: float) -> list[np.ndarray]:
    """RGB frames of ``[start_s, end_s]`` at the proxy's width (empty on any failure)."""
    duration = max(0.0, end_s - start_s)
    if duration <= 0:
        return []
    fps = min(_MAX_FPS, _MAX_FRAMES / duration)
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=width,height:stream_side_data=rotation", "-of", "default=nw=1", clip_path,
        ],
        capture_output=True, text=True, timeout=_DECODE_TIMEOUT_S, check=True,
    ).stdout  # fmt: skip
    values = dict(line.split("=", 1) for line in probe.split() if "=" in line)
    width, height = int(values["width"]), int(values["height"])
    if values.get("rotation", "0").lstrip("-") in {"90", "270"}:
        width, height = height, width
    out_h = max(2, round(_DECODE_WIDTH * height / width / 2) * 2)
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{start_s:.3f}", "-t", f"{duration:.3f}",
            "-i", clip_path, "-vf", f"fps={fps:.4f},scale={_DECODE_WIDTH}:{out_h}:flags=area",
            "-frames:v", str(_MAX_FRAMES), "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True, timeout=_DECODE_TIMEOUT_S, check=True,
    ).stdout  # fmt: skip
    size = _DECODE_WIDTH * out_h * 3
    count = len(raw) // size
    if count < _MIN_FRAMES:
        return []
    frames = np.frombuffer(raw[: count * size], np.uint8).reshape(count, out_h, _DECODE_WIDTH, 3)
    return list(frames)


class RvmPersonSampler:
    """The production `PersonSampler`: one RVM session for a whole montage, opened on first
    use and closed by the caller. ``None`` when the matte is unavailable or the moment has
    too few frames; a decode or inference error raises (the caller records "failed" and the
    clip keeps the middle)."""

    def __init__(self) -> None:
        self._matter: Any = None
        self._opened = False

    def __call__(
        self, clip_path: str, source_start_s: float, source_end_s: float
    ) -> PersonColumns | None:
        if not self._opened:
            from app.pipeline.subject_matte import PersonMatter  # noqa: PLC0415

            self._opened = True
            self._matter = PersonMatter.open()
        if self._matter is None:
            return None
        frames = _decode(clip_path, float(source_start_s), float(source_end_s))
        if not frames:
            return None
        return person_columns(self._matter.masks(frames))

    def close(self) -> None:
        if self._matter is not None:
            self._matter.close()
            self._matter = None


__all__ = [
    "MIN_GAIN_OVER_CENTRE",
    "MIN_PERSON_PRESENCE",
    "MIN_PERSON_SHARE",
    "PersonColumns",
    "PersonSampler",
    "PersonWindow",
    "RvmPersonSampler",
    "person_columns",
    "person_window",
]
