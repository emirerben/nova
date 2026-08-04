"""Top-level entry point: render a Blossom-carousel moment to an mp4 segment."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace

from . import effects, spring
from .cards import resolve_card_media
from .effects import CardGeometry
from .encode import encode_carousel_segment
from .gesture import CANONICAL_FLICK
from .renderer import FPS, render_carousel_frames
from .spring import SpringFrame

log = logging.getLogger(__name__)

# Cap the number of cards rendered per moment — bounds render cost even when a
# caller passes a larger clip pool (clip_paths beyond this are simply unused).
MAX_CARDS = 5

# Card geometry for every carousel moment. No per-effect/per-caller override
# yet — a single shared layout keeps `snap_positions`/`spring.simulate` and the
# renderer's face-cache all agreeing on the same card size.
DEFAULT_GEOMETRY = CardGeometry(card_w=540, card_h=720, gap=48, corner_radius=24)

_VIEWPORT_W = 1080


@dataclass(frozen=True)
class CarouselMomentSpec:
    effect: str  # one of effects.EFFECTS
    clip_paths: tuple[str, ...]  # source clips; one card per clip
    duration_s: float = 4.0


def _fit_duration(frames: list[SpringFrame], target_n: int) -> list[SpringFrame]:
    """Fit `frames` to exactly `target_n` entries.

    `spring.simulate`'s canonical flick trace settles at a fixed physical
    duration (~3.7s for a 2-card carousel at 30fps) that has nothing to do
    with the moment's requested `duration_s` — so the frame count almost
    always needs adjusting:

      - Too many frames (the common case: the settle trace outlasts the
        requested duration): truncate to the first `target_n` frames. This
        keeps the drag+flick+overshoot opening of the gesture and drops the
        tail of the settle, which is the visually inert part anyway.
      - Too few frames (a very long `duration_s` relative to the trace):
        pad by repeating the final (already-settled, ~zero-velocity) frame,
        advancing `t_s` by one more frame period each time so the timeline
        stays monotonic for anything downstream that reads it.
    """
    if not frames:
        return frames
    if len(frames) >= target_n:
        return frames[:target_n]

    last = frames[-1]
    padded = list(frames)
    for i in range(target_n - len(frames)):
        padded.append(replace(last, t_s=last.t_s + (i + 1) / FPS))
    return padded


def render_carousel_moment(spec: CarouselMomentSpec, work_dir: str) -> str | None:
    """Render a carousel moment to an mp4. NEVER raises — returns None on any
    failure (callers skip the moment)."""
    try:
        if spec.effect not in effects.EFFECTS:
            log.warning(
                "carousel_moment_invalid_effect effect=%r expected=%r",
                spec.effect,
                effects.EFFECTS,
            )
            return None

        n_clips = len(spec.clip_paths)
        if not (2 <= n_clips <= 8):
            log.warning("carousel_moment_invalid_clip_count n_clips=%d (expected 2..8)", n_clips)
            return None

        clip_paths = spec.clip_paths[:MAX_CARDS]
        cards = [
            resolve_card_media(clip_path, work_dir, index)
            for index, clip_path in enumerate(clip_paths)
        ]

        geo = DEFAULT_GEOMETRY
        n_cards = len(cards)
        snaps = effects.snap_positions(spec.effect, n_cards, geo, viewport_w=_VIEWPORT_W)
        bounds = effects.snap_bounds(n_cards, geo, viewport_w=_VIEWPORT_W)
        frames = spring.simulate(CANONICAL_FLICK, snaps, snapport_width=_VIEWPORT_W, bounds=bounds)

        target_n = max(1, round(spec.duration_s * FPS))
        frames = _fit_duration(frames, target_n)

        frames_dir = os.path.join(work_dir, "carousel_frames")
        frame_paths = render_carousel_frames(spec.effect, frames, cards, geo, out_dir=frames_dir)

        output_path = os.path.join(work_dir, f"carousel_moment_{spec.effect}.mp4")
        encode_carousel_segment(
            png_dir=frames_dir,
            pattern="frame_%04d.png",
            n_frames=len(frame_paths),
            fps=FPS,
            output_path=output_path,
        )
        return output_path
    except Exception:  # noqa: BLE001 — never-raise contract; callers skip on any failure
        log.exception(
            "carousel_moment_render_failed effect=%r n_clips=%d work_dir=%r",
            spec.effect,
            len(spec.clip_paths),
            work_dir,
        )
        return None
