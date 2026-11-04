"""``near_dup_hash``: a 64-bit dHash on three keyframes per clip (photos: the image itself).

Only the per-clip hashes (plus a sharpness and brightness reading for the keep-one tiebreak) are
stored. Which clips are duplicates is decided per pair when a suggestion set is served
(``duplicate_groups``), so a threshold change never needs a re-measure of stored clips.

Frames are decoded upright (ffmpeg applies the display matrix, PIL applies EXIF orientation) and
shrunk to a fixed 256 px width first, so hashes and sharpness are comparable across resolutions.
A mirrored shot is deliberately NOT a duplicate (dHash is sensitive to flips), and a photo is never
paired with a video.

Calibration (``tests/signals/test_near_dup_hash.py``, data from
``tests/fixtures/clip_signals/dups``): the Hamming thresholds and the low-information rule below
come from a sweep over labeled pairs; bump ``VERSION`` when the measurement or the rule moves.
"""

from __future__ import annotations

import math
import statistics
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from app.services.clip_signals.registry import SignalInputs, SignalSpec, read_signals, register

NAME = "near_dup_hash"
VERSION = 2

FRAME_WIDTH = 256
KEYFRAME_FRACTIONS = (0.15, 0.5, 0.85)
# A clip shorter than this has one keyframe: three would collapse onto the same frame.
MIN_MULTI_FRAME_S = 1.0
FFMPEG_TIMEOUT_S = 20
FFPROBE_TIMEOUT_S = 10

# A hash is only trusted when the frame has structure: a plain sky, a blank wall or a black frame
# hashes to (nearly) all-0 or all-1 bits, and two unrelated plain frames would then "match".
MIN_POPCOUNT = 12
MAX_POPCOUNT = 52
MIN_MEAN_ABS_STEP = 1.0  # mean |left-right| difference of the 9x8 grid, 0-255 scale
# A symmetric vignette (a wall, a sky or a dark room lit from the centre) is half 1-bits and has a
# large step, yet every shot of it hashes to the same value: all 8 row-bytes are identical. Real
# scenes differ from their most common row by 14+ bits in total, vignettes by 0 (see the sweep).
MIN_ROW_VARIETY = 6

# Pair decision (see the module docstring for where the numbers come from).
MATCH_DISTANCE = 11  # Hamming distance (of 64) at or under which two keyframes are the same view
MATCH_DISTANCE_NO_TIME = 9  # stricter when a capture time is missing on either clip
MAX_CAPTURE_GAP_S = 600.0  # both clips timed and further apart than this: not the same moment

_PHOTO_MAX_PIXELS = 120_000_000


# ── measurement ──────────────────────────────────────────────────────────────────────


def _gray_array(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.float32)


def dhash(gray: np.ndarray) -> tuple[int, float]:
    """``(hash, mean_abs_step)`` of one gray frame: the 9x8 horizontal-gradient signature."""
    small = Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8)).resize(
        (9, 8), Image.Resampling.LANCZOS
    )
    grid = np.asarray(small, dtype=np.float32)
    step = grid[:, 1:] - grid[:, :-1]
    value = 0
    for bit in (step > 0).flatten():
        value = (value << 1) | int(bit)
    return value, float(np.abs(step).mean())


def row_variety(value: int) -> int:
    """Bits by which the 8 row-bytes of a hash differ from their most common row."""
    rows = [(value >> (8 * i)) & 0xFF for i in range(8)]
    mode = max(set(rows), key=rows.count)
    return sum((row ^ mode).bit_count() for row in rows)


def informative(value: int, mean_abs_step: float) -> bool:
    return (
        MIN_POPCOUNT <= value.bit_count() <= MAX_POPCOUNT
        and mean_abs_step >= MIN_MEAN_ABS_STEP
        and row_variety(value) >= MIN_ROW_VARIETY
    )


def laplacian_variance(gray: np.ndarray) -> float:
    """Sharpness: variance of the 4-neighbour Laplacian (higher is sharper)."""
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    c = gray[1:-1, 1:-1]
    lap = gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:] - 4.0 * c
    return float(lap.var())


class _Undecodable(Exception):
    """The file is not readable as media: a permanent verdict, stored so it is not retried."""


class _Transient(Exception):
    """A timeout or missing tool: not stored, so the next run measures again."""


def _probe_duration(path: str) -> float:
    args = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]
    try:
        out = subprocess.run(
            args, capture_output=True, timeout=FFPROBE_TIMEOUT_S, check=True
        ).stdout.decode()
        return max(0.0, float(out.strip()))
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise _Transient from exc
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


def _video_frame(path: str, at_s: float) -> np.ndarray | None:
    args = ["ffmpeg", "-nostdin", "-v", "error"]
    if at_s > 0:
        args += ["-ss", f"{at_s:.3f}"]  # before -i: a seek, not a decode of the whole file
    args += ["-i", path, "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", "1"]
    args += ["-vf", f"scale={FRAME_WIDTH}:-2", "-f", "image2pipe", "-vcodec", "pgm", "-"]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=FFMPEG_TIMEOUT_S)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise _Transient from exc
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return _gray_array(Image.open(BytesIO(proc.stdout)))
    except Exception:  # noqa: BLE001 - a torn frame is "no frame"
        return None


def keyframe_times(duration_s: float) -> list[float]:
    if duration_s < MIN_MULTI_FRAME_S:
        return [0.0]
    return [round(duration_s * f, 3) for f in KEYFRAME_FRACTIONS]


def _video_frames(path: str, duration_s: float | None) -> list[np.ndarray]:
    duration = duration_s if duration_s and duration_s > 0 else _probe_duration(path)
    frames = [f for t in keyframe_times(duration) if (f := _video_frame(path, t)) is not None]
    if not frames:
        # A seek past the real end (a wrong stored duration) yields nothing; the first frame is
        # there for any readable file.
        first = _video_frame(path, 0.0)
        frames = [first] if first is not None else []
    if not frames:
        raise _Undecodable("no decodable frame")
    return frames


def _heif_available() -> bool:
    try:
        import pillow_heif  # type: ignore[import-not-found]  # noqa: PLC0415

        pillow_heif.register_heif_opener()
    except ImportError:
        return False
    return True


def _looks_like_heic(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError:
        return False
    return head[4:8] == b"ftyp" and head[8:12] in {b"heic", b"heix", b"mif1", b"msf1", b"hevc"}


def _photo_frame(path: str) -> np.ndarray:
    heif = _heif_available()
    try:
        with Image.open(path) as img:
            if img.width * img.height > _PHOTO_MAX_PIXELS:
                raise _Undecodable("image too large")
            if img.format == "JPEG":
                img.draft("L", (FRAME_WIDTH * 2, FRAME_WIDTH * 2))  # decode at a fraction of size
            gray = ImageOps.exif_transpose(img).convert("L")
            height = max(1, round(gray.height * FRAME_WIDTH / gray.width))
            return _gray_array(gray.resize((FRAME_WIDTH, height), Image.Resampling.LANCZOS))
    except _Undecodable:
        raise
    except (OSError, ValueError, SyntaxError) as exc:  # includes DecompressionBombError
        if not heif and _looks_like_heic(path):
            raise _Transient from exc  # readable in prod (pillow-heif): skip, do not poison
        raise _Undecodable(type(exc).__name__) from exc


def compute(inputs: SignalInputs) -> Mapping[str, Any] | None:
    """Hashes of up to three keyframes plus sharpness and brightness.

    ``None`` only when nothing could be attempted (no local copy) or the failure may pass (a
    timeout, HEIC without a decoder); an unreadable file stores an empty result so it is not
    re-measured on every backfill run.
    """
    if not inputs.local_path:
        return None
    try:
        frames = (
            [_photo_frame(inputs.local_path)]
            if inputs.kind == "image"
            else _video_frames(inputs.local_path, inputs.duration_s)
        )
    except _Transient:
        return None
    except _Undecodable as exc:
        return {"hashes": [], "frames": 0, "error": str(exc) or "decode"}
    hashes: list[str] = []
    for gray in frames:
        value, step = dhash(gray)
        if informative(value, step):
            hashes.append(f"{value:016x}")
    return {
        "hashes": hashes,
        "frames": len(frames),
        "sharpness": round(statistics.median(laplacian_variance(g) for g in frames), 1),
        "mean_luma": round(float(np.mean([g.mean() for g in frames])), 1),
    }


SPEC = register(
    SignalSpec(
        name=NAME,
        version=VERSION,
        kinds=frozenset({"video", "image"}),
        compute=compute,
        implemented=True,
    )
)


# ── pairs (serve time, pure) ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DupClip:
    """What the pair decision needs about one clip. Build it with ``dup_clip``."""

    media_id: str
    kind: str
    hashes: tuple[int, ...]
    order: int = 0  # attachment order: the final tiebreak, earlier wins
    capture_time: datetime | None = None
    sharpness: float | None = None
    mean_luma: float | None = None
    near_black_share: float | None = None


@dataclass(frozen=True)
class DupGroup:
    keep: str
    drop: tuple[str, ...]
    # Why ``keep`` won, for the card's one-line evidence: sharper | better exposed | earlier
    reason: str
    distance: int  # the largest keyframe distance that linked a dropped clip to the group


def _number(value: object, *, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return default
    return float(value)


def dup_clip(
    media_id: str,
    kind: str,
    analysis: object,
    *,
    order: int = 0,
    capture_time: datetime | None = None,
) -> DupClip | None:
    """The clip's pair inputs from its stored analysis; None when it has no usable hashes."""
    data = read_signals(analysis).get(NAME)
    raw = data.get("hashes") if data else None
    if not isinstance(raw, Sequence) or isinstance(raw, str) or not raw:
        return None
    try:
        hashes = tuple(int(h, 16) for h in raw)
    except (TypeError, ValueError):
        return None
    luma, black = _number(data.get("mean_luma")), None
    exposure = analysis.get("exposure") if isinstance(analysis, Mapping) else None
    if isinstance(exposure, Mapping) and exposure.get("version") == 1:
        # KRI-680's measurement, when present, wins over our own reading.
        luma = _number(exposure.get("mean_luma"), default=luma)
        black = _number(exposure.get("near_black_share"))
    return DupClip(
        media_id=media_id,
        kind="image" if kind == "image" else "video",
        hashes=hashes,
        order=order,
        capture_time=capture_time,
        sharpness=_number(data.get("sharpness")),
        mean_luma=luma,
        near_black_share=black,
    )


def dup_clips_from_assignments(rows: Iterable[object]) -> list[DupClip]:
    """Pair inputs for the clips of a plan item, in attachment order."""
    from app.kria.duplicate_uploads import _capture_time  # noqa: PLC0415

    out: list[DupClip] = []
    for order, row in enumerate(rows or ()):
        if not isinstance(row, Mapping) or not row.get("media_id"):
            continue
        clip = dup_clip(
            str(row["media_id"]),
            str(row.get("kind") or "video"),
            row.get("analysis"),
            order=order,
            capture_time=_capture_time(row),
        )
        if clip is not None:
            out.append(clip)
    return out


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _apart_s(a: datetime, b: datetime) -> float:
    try:
        return abs((a - b).total_seconds())
    except TypeError:  # naive vs aware: not comparable, so never trusted as "close"
        return math.inf


def match_distance(a: DupClip, b: DupClip) -> int | None:
    """The clip-level distance when ``a`` and ``b`` show the same view, else None.

    Every keyframe of the clip with fewer informative keyframes needs a partner within the
    threshold in the other clip (any position: a retake pans at a different phase). Coverage runs
    one way on purpose: a trimmed copy of a longer take is still a copy.
    """
    if a.kind != b.kind or not a.hashes or not b.hashes:
        return None
    timed = a.capture_time is not None and b.capture_time is not None
    if timed and _apart_s(a.capture_time, b.capture_time) > MAX_CAPTURE_GAP_S:  # type: ignore[arg-type]
        return None
    limit = MATCH_DISTANCE if timed else MATCH_DISTANCE_NO_TIME
    small, large = (a, b) if len(a.hashes) <= len(b.hashes) else (b, a)
    worst = 0
    for h in small.hashes:
        best = min(hamming(h, other) for other in large.hashes)
        if best > limit:
            return None
        worst = max(worst, best)
    return worst


def _exposure_factor(clip: DupClip) -> float:
    if clip.near_black_share is not None and clip.near_black_share >= 0.5:
        return 0.25
    if clip.mean_luma is not None:
        if clip.mean_luma < 40:
            return 0.5
        if clip.mean_luma > 200:
            return 0.7
    return 1.0


def quality(clip: DupClip) -> float:
    """Higher is the better one to keep: sharpness, discounted for a clip that is dark or
    washed out. A clip with no sharpness reading scores 0 and loses to anything measured."""
    return (clip.sharpness or 0.0) * _exposure_factor(clip)


def _best(group: Sequence[DupClip]) -> tuple[DupClip, str]:
    ranked = sorted(group, key=lambda c: (-quality(c), c.order))
    top = quality(ranked[0])
    # Within 5% the clips are equally good: attachment order decides.
    level = [c for c in ranked if quality(c) >= top * 0.95]
    if len(level) > 1:
        return min(level, key=lambda c: c.order), "earlier"
    winner = ranked[0]
    rival = ranked[1]
    exposure_decided = _exposure_factor(winner) > _exposure_factor(rival)
    return winner, "better exposed" if exposure_decided else "sharper"


def duplicate_groups(clips: Iterable[DupClip]) -> list[DupGroup]:
    """Groups of near-duplicate clips, each with the one to keep. Singletons are not returned.

    A clip joins a group only if it matches EVERY member (a burst A~B~C where A and C differ must
    not chain into one group), scanning in attachment order.
    """
    groups: list[list[DupClip]] = []
    worst: list[int] = []
    for clip in sorted(clips, key=lambda c: c.order):
        for index, group in enumerate(groups):
            distances = [match_distance(clip, member) for member in group]
            if all(d is not None for d in distances):
                group.append(clip)
                worst[index] = max([worst[index], *(d for d in distances if d is not None)])
                break
        else:
            groups.append([clip])
            worst.append(0)
    out: list[DupGroup] = []
    for group, distance in zip(groups, worst, strict=True):
        if len(group) < 2:
            continue
        keep, reason = _best(group)
        dropped = tuple(c.media_id for c in group if c is not keep)
        out.append(DupGroup(keep=keep.media_id, drop=dropped, reason=reason, distance=distance))
    return out


__all__ = [
    "MATCH_DISTANCE",
    "NAME",
    "VERSION",
    "DupClip",
    "DupGroup",
    "dhash",
    "dup_clip",
    "dup_clips_from_assignments",
    "duplicate_groups",
    "hamming",
    "laplacian_variance",
    "match_distance",
    "quality",
]
