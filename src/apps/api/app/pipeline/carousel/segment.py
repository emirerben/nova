"""Top-level entry point: render a Blossom-carousel moment to an mp4 segment."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace

from . import effects, spring
from .cards import resolve_card_media
from .choreography import FocusMoment, build_timeline, rolling_timeline
from .effects import CardGeometry
from .encode import encode_carousel_segment
from .gesture import CANONICAL_FLICK
from .renderer import FPS, render_carousel_frames, render_choreography_frames
from .spring import SpringFrame
from .video_cards import resolve_video_card

log = logging.getLogger(__name__)

# Cap the number of cards rendered per moment — bounds render cost even when a
# caller passes a larger clip pool (clip_paths beyond this are simply unused).
MAX_CARDS = 5

# Card geometry for every carousel moment. No per-effect/per-caller override
# yet — a single shared layout keeps `snap_positions`/`spring.simulate` and the
# renderer's face-cache all agreeing on the same card size.
DEFAULT_GEOMETRY = CardGeometry(card_w=540, card_h=720, gap=48, corner_radius=24)

_VIEWPORT_W = 1080

MODES = ("stills", "rolling", "focus")

# Hard cap on a `mode="focus"` moment's total rendered length, regardless of
# how many/long the requested `focus_moments` are — `choreography.
# build_timeline`'s natural length is driven entirely by its inputs (lead-in +
# per-moment flick/hold/zoom phases + one trailing flick), so an
# over-ambitious caller (many moments, long holds) is trimmed here rather
# than producing an unbounded render.
MAX_FOCUS_TOTAL_S = 15.0


@dataclass(frozen=True)
class CarouselMomentSpec:
    effect: str  # one of effects.EFFECTS
    clip_paths: tuple[str, ...]  # source clips; one card per clip
    duration_s: float = 4.0
    mode: str = "stills"  # "stills" | "rolling" | "focus"
    focus_moments: tuple[FocusMoment, ...] = ()  # only consulted when mode == "focus"
    seed: int = 0  # only consulted when mode in ("rolling", "focus")
    # Explicit user-requested cap on a `mode="focus"` moment's total length
    # (seconds), clamped by the caller to [2.0, 15.0] before it reaches here.
    # `None` (the default) preserves this dataclass's pre-existing behavior:
    # `duration_s` is ignored for focus mode and the natural choreography
    # length is hard-capped at `MAX_FOCUS_TOTAL_S` — see `_render_focus_mode`.
    # Only ever set via the carousel-editor dispatch path
    # (`_apply_moment_overrides` in generative_build.py); the auto-director
    # never sets it, so an auto-authored focus moment's length is unaffected
    # by this field existing.
    focus_duration_cap_s: float | None = None


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
    failure (callers skip the moment).

    `spec.mode` dispatches to one of three render paths, sharing `effect`/
    `clip_paths`/geometry validation across all three:
      - "stills" (default): byte-identical to the pre-V2 behavior — a still
        poster per card, `spring.simulate(CANONICAL_FLICK, ...)` fit to
        `duration_s`.
      - "rolling": every card plays its own clip continuously
        (`choreography.rolling_timeline`, `video_cards.resolve_video_card`'s
        card tier only — no fullscreen focus).
      - "focus": FOCUS CHOREOGRAPHY — `choreography.build_timeline` flicks to
        each `spec.focus_moments` card in turn, zooms it to fullscreen, holds,
        returns, and continues; `duration_s` is IGNORED except as a soft
        input to the video-card extraction window — the timeline's own
        natural length is hard-capped at `MAX_FOCUS_TOTAL_S` by trimming
        trailing frames (logged when it actually trims). Only the cards
        actually named by a focus moment get the extra full-resolution tier.
    """
    try:
        if spec.effect not in effects.EFFECTS:
            log.warning(
                "carousel_moment_invalid_effect effect=%r expected=%r",
                spec.effect,
                effects.EFFECTS,
            )
            return None

        if spec.mode not in MODES:
            log.warning("carousel_moment_invalid_mode mode=%r expected=%r", spec.mode, MODES)
            return None

        n_clips = len(spec.clip_paths)
        if not (2 <= n_clips <= 8):
            log.warning("carousel_moment_invalid_clip_count n_clips=%d (expected 2..8)", n_clips)
            return None

        clip_paths = spec.clip_paths[:MAX_CARDS]
        geo = DEFAULT_GEOMETRY
        n_cards = len(clip_paths)
        frames_dir = os.path.join(work_dir, "carousel_frames")

        if spec.mode == "stills":
            frame_paths = _render_stills_mode(spec, work_dir, clip_paths, geo, n_cards, frames_dir)
        elif spec.mode == "rolling":
            frame_paths = _render_rolling_mode(spec, work_dir, clip_paths, geo, n_cards, frames_dir)
        else:  # "focus"
            frame_paths = _render_focus_mode(spec, work_dir, clip_paths, geo, n_cards, frames_dir)

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
            "carousel_moment_render_failed effect=%r mode=%r n_clips=%d work_dir=%r",
            spec.effect,
            spec.mode,
            len(spec.clip_paths),
            work_dir,
        )
        return None


def _render_stills_mode(
    spec: CarouselMomentSpec,
    work_dir: str,
    clip_paths: tuple[str, ...],
    geo: CardGeometry,
    n_cards: int,
    frames_dir: str,
) -> list[str]:
    """Pre-V2 behavior, unchanged: one still poster per card, the canonical
    flick fit to `duration_s`."""
    cards = [
        resolve_card_media(clip_path, work_dir, index) for index, clip_path in enumerate(clip_paths)
    ]

    snaps = effects.snap_positions(spec.effect, n_cards, geo, viewport_w=_VIEWPORT_W)
    bounds = effects.snap_bounds(n_cards, geo, viewport_w=_VIEWPORT_W)
    frames = spring.simulate(CANONICAL_FLICK, snaps, snapport_width=_VIEWPORT_W, bounds=bounds)

    target_n = max(1, round(spec.duration_s * FPS))
    frames = _fit_duration(frames, target_n)

    return render_carousel_frames(spec.effect, frames, cards, geo, out_dir=frames_dir)


def _render_rolling_mode(
    spec: CarouselMomentSpec,
    work_dir: str,
    clip_paths: tuple[str, ...],
    geo: CardGeometry,
    n_cards: int,
    frames_dir: str,
) -> list[str]:
    """Every card plays its own clip continuously, no focus. Video extraction
    window matches `duration_s` exactly — `rolling_timeline` itself is
    trimmed/padded to the same `round(duration_s * FPS)` frame count, so a
    render-frame index maps 1:1 onto a card-tier video-frame index."""
    video_cards = [
        resolve_video_card(clip_path, work_dir, index, card_seconds=max(0.5, spec.duration_s))
        for index, clip_path in enumerate(clip_paths)
    ]
    frame_states = rolling_timeline(
        n_cards, geo, _VIEWPORT_W, duration_s=spec.duration_s, fps=FPS, seed=spec.seed
    )
    return render_choreography_frames(
        spec.effect, frame_states, video_cards, geo, out_dir=frames_dir
    )


def _render_focus_mode(
    spec: CarouselMomentSpec,
    work_dir: str,
    clip_paths: tuple[str, ...],
    geo: CardGeometry,
    n_cards: int,
    frames_dir: str,
) -> list[str]:
    """FOCUS CHOREOGRAPHY: flick to each requested card, zoom to fullscreen,
    hold, return, repeat, then continue. `duration_s` is ignored — the
    timeline's own natural length governs, hard-capped at
    `MAX_FOCUS_TOTAL_S`."""
    focus_moments = spec.focus_moments or (FocusMoment(card_index=min(1, n_cards - 1)),)
    focus_moments = tuple(
        replace(m, card_index=max(0, min(n_cards - 1, m.card_index))) for m in focus_moments
    )

    frame_states = build_timeline(
        n_cards, geo, _VIEWPORT_W, focus_moments=focus_moments, fps=FPS, seed=spec.seed
    )

    if spec.focus_duration_cap_s is not None:
        # Explicit user cap (the carousel editor's duration_s): FIT (trim or
        # pad, via the same helper stills mode uses) to exactly the
        # requested length, rather than only trimming when it's exceeded —
        # a cap longer than the natural settle trace pads by holding the
        # final frame, same as `_fit_duration`'s stills-mode contract.
        cap_n = max(1, round(min(spec.focus_duration_cap_s, MAX_FOCUS_TOTAL_S) * FPS))
        if len(frame_states) != cap_n:
            log.info(
                "carousel_moment_focus_duration_fit original_frames=%d target_frames=%d",
                len(frame_states),
                cap_n,
            )
        frame_states = _fit_duration(frame_states, cap_n)
    else:
        hard_cap_n = max(1, round(MAX_FOCUS_TOTAL_S * FPS))
        if len(frame_states) > hard_cap_n:
            log.info(
                "carousel_moment_focus_trimmed original_frames=%d cap_frames=%d",
                len(frame_states),
                hard_cap_n,
            )
            frame_states = frame_states[:hard_cap_n]

    total_s = (len(frame_states) / FPS) if frame_states else spec.duration_s
    focus_indices = {m.card_index for m in focus_moments}
    full_seconds = max((m.hold_s + 2 * m.zoom_s + 1.0 for m in focus_moments), default=0.0)

    video_cards = [
        resolve_video_card(
            clip_path,
            work_dir,
            index,
            card_seconds=max(0.5, total_s),
            full_seconds=full_seconds if index in focus_indices else 0.0,
        )
        for index, clip_path in enumerate(clip_paths)
    ]

    return render_choreography_frames(
        spec.effect, frame_states, video_cards, geo, out_dir=frames_dir
    )
