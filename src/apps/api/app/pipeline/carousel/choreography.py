"""Timeline authoring for carousel V2's FOCUS CHOREOGRAPHY and ROLLING modes:
turns a list of `FocusMoment`s (or a plain duration, for rolling) into a
frame-by-frame `FrameState` timeline — scroll position plus focus/dim state —
that `renderer.render_choreography_frames` then paints.

Reuses the parity-proven spring engine (`spring.py`) unchanged: every scroll
movement here is a REAL flick run through `spring.simulate_from`, just with a
solved-for delta-scale so it lands on a specific card instead of replaying
`CANONICAL_FLICK` verbatim. See `_solve_flick_scale`'s docstring for the
numeric approach.

Coordinate model: this module always drives the spring off the FLAT
(unscaled) snap grid `i * (card_w + gap)` — NOT `effects.snap_positions`'
per-effect painted-pose grid. That grid exists to replicate a real-DOM CSS
quirk (mount-time snap targets computed from an already-painted, possibly
scaled pose — see `spring.py`'s and `effects.snap_positions`'s docstrings);
it has no bearing here because this module isn't reproducing a captured
browser trace, it's AUTHORING a new one from scratch, and the flat grid is
provably the scroll position that centers card `i` for every effect (`effects
.view_progress(i*pitch, i, ...) == 0.5` regardless of which effect transform
consumes it — see each `*_transform`'s formula: p=0.5 is effect-independent).
`build_timeline`/`rolling_timeline` therefore take `geo`/`viewport_w` but no
`effect` parameter at all.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from . import effects
from .effects import CardGeometry
from .gesture import CANONICAL_FLICK
from .spring import SpringState, simulate_from

# Fraction of the render canvas' non-focused-card dim applied at full focus
# (CSS `filter: brightness(1 - DIM_MAX)` equivalent, approximated in the
# renderer as a black overlay at this alpha — see renderer.py).
DIM_MAX: float = 0.55

# Beyond this focus_t, `renderer.render_choreography_frames` switches a
# focused card's face source from the small card-tier JPEGs to the
# full-resolution tier (see that module for why: the two tiers have
# different native aspect ratios, so the switch is a deliberate reveal, not
# a cross-fade).
FULLRES_SWITCH_T: float = 0.35

# Jitter band applied (via a seeded `random.Random`) to every hold/pad
# duration below — NOT to flick physics or zoom_s, which stay deterministic
# functions of the spring engine / the caller's FocusMoment. Keeps the
# overall timeline from feeling metronomic across multiple moments/videos
# while remaining fully reproducible for a given `seed`.
JITTER_FRAC: float = 0.10


@dataclass(frozen=True)
class FocusMoment:
    card_index: int
    hold_s: float = 2.0
    zoom_s: float = 0.6  # each direction


@dataclass(frozen=True)
class FrameState:
    t_s: float
    scroll_x: float
    focus_card: int | None = None
    focus_t: float = 0.0  # 0 in-carousel, 1 fullscreen
    dim: float = 0.0  # 0..1 dim applied to non-focused cards


def _pitch(geo: CardGeometry) -> float:
    return geo.card_w + geo.gap


def _flat_snap_positions(n_cards: int, geo: CardGeometry) -> list[float]:
    """Scroll position that centers card `i`: `i * pitch` — see the module
    docstring for why this (not `effects.snap_positions`) is the right grid
    for an authored timeline."""
    pitch = _pitch(geo)
    return [i * pitch for i in range(n_cards)]


def _nearest_snap_index(x: float, snaps: list[float]) -> int:
    return min(range(len(snaps)), key=lambda i: abs(snaps[i] - x))


def _ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _reset_for_new_gesture(state: SpringState) -> SpringState:
    """A settled state (from a prior flick's settle-loop, or the initial
    centered position) reinterpreted as the START of a brand-new drag: fresh
    `total_drag_px`/`tick_active` (a new gesture hasn't crossed
    `DRAG_ACTIVATION_PX` yet), `target` reset to the current (already
    converged) `virtual_scroll`, velocity zeroed."""
    return replace(
        state,
        is_dragging=True,
        total_drag_px=0.0,
        tick_active=False,
        velocity=0.0,
        target=state.virtual_scroll,
    )


def _landed_index(
    state: SpringState,
    k: float,
    snaps: list[float],
    snapport_width: float,
    fps: int,
    bounds: tuple[float, float] | None,
) -> int:
    deltas = tuple(d * k for d in CANONICAL_FLICK.drag_deltas_px)
    final_state, _frames = simulate_from(state, deltas, snaps, snapport_width, fps, bounds=bounds)
    return _nearest_snap_index(final_state.virtual_scroll, snaps)


def _solve_flick_scale(
    state: SpringState,
    target_index: int,
    snaps: list[float],
    snapport_width: float,
    fps: int,
    bounds: tuple[float, float] | None,
) -> float:
    """Find a scale factor `k` such that scaling `CANONICAL_FLICK.drag_deltas_px`
    by `k` and replaying it from `state` lands on `snaps[target_index]` —
    pure numeric root-find over the spring engine itself (no hand-tuned
    deltas), per the design brief: "iterate the scale factor numerically
    until release() selects the desired snap index".

    `k`'s SIGN reuses `CANONICAL_FLICK`'s own sign convention: positive `k`
    replays it as-is (finger drags left, scroll increases, carousel advances
    toward higher indices); negative `k` flips every delta's sign (drags the
    opposite way). Magnitude controls how far the flick's momentum carries.
    `landed_index(k)` (which card ends up centered after a full
    flick+release+settle at that `k`) is a monotonic, non-decreasing step
    function of `k` in practice — larger flicks travel further in the same
    direction — so a doubling-then-bisecting search converges on the
    smallest `k` (in the needed direction) whose landing lands on
    `target_index`, which by construction of the search already satisfies
    "lands on target_index" (verified directly against a real
    `simulate_from` call, not inferred).
    """
    current_index = _nearest_snap_index(state.virtual_scroll, snaps)
    if current_index == target_index:
        return 0.0

    direction = 1.0 if target_index > current_index else -1.0

    def _reached(idx: int) -> bool:
        return idx >= target_index if direction > 0 else idx <= target_index

    lo, hi = 0.0, direction * 1.0
    for _ in range(40):
        if _reached(_landed_index(state, hi, snaps, snapport_width, fps, bounds)):
            break
        hi *= 2.0
    else:
        # Never bracketed target_index (e.g. it's outside the reachable
        # `bounds`) — fall back to whatever the largest tried `hi` lands on;
        # the caller (build_timeline/rolling_timeline) still gets a legal,
        # if short, flick rather than a crash.
        return hi

    for _ in range(50):
        mid = (lo + hi) / 2.0
        if _reached(_landed_index(state, mid, snaps, snapport_width, fps, bounds)):
            hi = mid
        else:
            lo = mid

    return hi


def _run_flick(
    state: SpringState,
    target_index: int,
    snaps: list[float],
    snapport_width: float,
    fps: int,
    bounds: tuple[float, float] | None,
) -> tuple[SpringState, list[float]]:
    """Solve + replay one flick from `state` to `target_index`. Returns the
    settled end state and the per-frame `virtual_scroll` trace (empty if no
    flick was needed, i.e. `target_index` was already centered)."""
    gesture_state = _reset_for_new_gesture(state)
    k = _solve_flick_scale(gesture_state, target_index, snaps, snapport_width, fps, bounds)
    if k == 0.0:
        return replace(state, is_dragging=False), []

    deltas = tuple(d * k for d in CANONICAL_FLICK.drag_deltas_px)
    final_state, spring_frames = simulate_from(
        gesture_state, deltas, snaps, snapport_width, fps, bounds=bounds
    )
    return final_state, [sf.virtual_scroll for sf in spring_frames]


def build_timeline(
    n_cards: int,
    geo: CardGeometry,
    viewport_w: float,
    *,
    focus_moments: tuple[FocusMoment, ...] = (),
    fps: int = 30,
    lead_in_s: float = 0.4,
    settle_pad_s: float = 0.3,
    seed: int = 0,
    manual_timing: bool = False,
    move_duration_s: float | None = None,
) -> list[FrameState]:
    """Author a FOCUS CHOREOGRAPHY timeline: lead-in hold, then for each
    focus moment (sorted by `card_index`) — flick to center it, settle-pad
    hold, ease-in to fullscreen (`focus_t` 0->1, dim ramps 0->DIM_MAX), hold
    at fullscreen, ease back out (mirror), settle-pad hold — then one final
    flick onward (next card, or back one if the last moment was already the
    final card) and a trailing settle pad so the segment ends in motion-rest,
    not frozen on a focus.

    Uses `_ease_out_cubic` (not a `damp()` exponential) for the focus_t ramp:
    damp's per-frame delta is LARGEST on frame 1 (~51% of the remaining gap
    at `DAMPING*2.5`, independent of ramp length), which blows well past a
    smooth ~0.2/frame continuity budget; `_ease_out_cubic` sampled at uniform
    time steps has its largest per-frame delta at the same spot but scales
    down with more frames (~0.17 at the default `zoom_s=0.6` => 18 frames),
    and reaches exactly 1.0 (into-focus) / 0.0 (out-of-focus) on its literal
    last sample with no patch-up needed. Documented deviation from the
    brief's "pick damp for house consistency" suggestion.

    Deterministic for a given `seed`: only hold/pad durations are jittered
    (±`JITTER_FRAC`, via a seeded `random.Random`) — flick physics and
    `zoom_s` itself are untouched, so the shape of the motion never changes,
    only its breathing room.
    """
    dt = 1.0 / fps
    snaps = _flat_snap_positions(n_cards, geo)
    bounds = effects.snap_bounds(n_cards, geo, viewport_w)
    rng = random.Random(seed)

    frames: list[FrameState] = []
    t_cursor = 0.0

    def _jitter(base_s: float) -> float:
        if manual_timing:
            return base_s
        return base_s * (1.0 + rng.uniform(-JITTER_FRAC, JITTER_FRAC))

    def _hold(scroll_x: float, seconds: float) -> None:
        nonlocal t_cursor
        n = max(0, round(seconds * fps))
        for _ in range(n):
            t_cursor += dt
            frames.append(FrameState(t_s=t_cursor, scroll_x=scroll_x))

    def _append_scrolls(scrolls: list[float]) -> None:
        nonlocal t_cursor
        for sx in scrolls:
            t_cursor += dt
            frames.append(FrameState(t_s=t_cursor, scroll_x=sx))

    def _retime_scrolls(scrolls: list[float]) -> list[float]:
        if move_duration_s is None or not scrolls:
            return scrolls
        target_n = max(1, round(move_duration_s * fps))
        if target_n == 1:
            return [scrolls[-1]]
        last = len(scrolls) - 1
        return [scrolls[round(i * last / (target_n - 1))] for i in range(target_n)]

    start_scroll = snaps[0] if snaps else 0.0
    state = SpringState(
        virtual_scroll=start_scroll, target=start_scroll, velocity=0.0, is_dragging=False
    )

    _hold(state.virtual_scroll, _jitter(lead_in_s))

    ordered_moments = (
        list(focus_moments) if manual_timing else sorted(focus_moments, key=lambda m: m.card_index)
    )
    for moment in ordered_moments:
        target_index = max(0, min(n_cards - 1, moment.card_index))

        state, scrolls = _run_flick(state, target_index, snaps, viewport_w, fps, bounds)
        _append_scrolls(_retime_scrolls(scrolls))

        centered_scroll = snaps[target_index] if snaps else 0.0
        state = replace(state, virtual_scroll=centered_scroll, target=centered_scroll, velocity=0.0)

        _hold(centered_scroll, _jitter(settle_pad_s))

        n_zoom = max(2, round(moment.zoom_s * fps))
        for i in range(1, n_zoom + 1):
            t_cursor += dt
            ft = _ease_out_cubic(i / n_zoom)
            frames.append(
                FrameState(
                    t_s=t_cursor,
                    scroll_x=centered_scroll,
                    focus_card=target_index,
                    focus_t=ft,
                    dim=DIM_MAX * ft,
                )
            )

        n_hold = max(1, round(_jitter(moment.hold_s) * fps))
        for _ in range(n_hold):
            t_cursor += dt
            frames.append(
                FrameState(
                    t_s=t_cursor,
                    scroll_x=centered_scroll,
                    focus_card=target_index,
                    focus_t=1.0,
                    dim=DIM_MAX,
                )
            )

        for i in range(1, n_zoom + 1):
            t_cursor += dt
            ft = 1.0 - (i / n_zoom) ** 3  # mirror of _ease_out_cubic: 1 -> 0, decelerating into 0
            frames.append(
                FrameState(
                    t_s=t_cursor,
                    scroll_x=centered_scroll,
                    focus_card=target_index,
                    focus_t=ft,
                    dim=DIM_MAX * ft,
                )
            )

        _hold(centered_scroll, _jitter(settle_pad_s))

    if ordered_moments and not manual_timing:
        last_index = max(0, min(n_cards - 1, ordered_moments[-1].card_index))
        next_index = last_index + 1 if last_index + 1 < n_cards else max(0, last_index - 1)
        if next_index != last_index:
            state, scrolls = _run_flick(state, next_index, snaps, viewport_w, fps, bounds)
            _append_scrolls(scrolls)
        _hold(state.virtual_scroll, _jitter(settle_pad_s))

    return frames


def rolling_timeline(
    n_cards: int,
    geo: CardGeometry,
    viewport_w: float,
    *,
    duration_s: float,
    fps: int = 30,
    seed: int = 0,
    sequence: tuple[FocusMoment, ...] = (),
    move_duration_s: float | None = None,
    manual_timing: bool = False,
) -> list[FrameState]:
    """Author a ROLLING timeline: no focus, just a sequence of flicks
    advancing card by card through the whole set (seeded, slightly jittered
    hold timing between flicks), trimmed/padded to exactly
    `round(duration_s * fps)` frames — same house convention as
    `segment._fit_duration` (truncate an over-long trace; pad by repeating
    the final settled scroll position for an under-long one).
    """
    dt = 1.0 / fps
    snaps = _flat_snap_positions(n_cards, geo)
    bounds = effects.snap_bounds(n_cards, geo, viewport_w)
    rng = random.Random(seed)

    frames: list[FrameState] = []
    t_cursor = 0.0

    def _jitter(base_s: float) -> float:
        if manual_timing:
            return base_s
        return base_s * (1.0 + rng.uniform(-JITTER_FRAC, JITTER_FRAC))

    def _hold(scroll_x: float, seconds: float) -> None:
        nonlocal t_cursor
        n = max(0, round(seconds * fps))
        for _ in range(n):
            t_cursor += dt
            frames.append(FrameState(t_s=t_cursor, scroll_x=scroll_x))

    start_scroll = snaps[0] if snaps else 0.0
    state = SpringState(
        virtual_scroll=start_scroll, target=start_scroll, velocity=0.0, is_dragging=False
    )

    targets = (
        list(sequence)
        if manual_timing and sequence
        else [FocusMoment(card_index=idx, hold_s=0.3) for idx in range(1, n_cards)]
    )
    if not manual_timing:
        _hold(state.virtual_scroll, _jitter(0.3))

    for item in targets:
        if t_cursor >= duration_s:
            break
        idx = max(0, min(n_cards - 1, item.card_index))
        state, scrolls = _run_flick(state, idx, snaps, viewport_w, fps, bounds)
        if manual_timing and move_duration_s is not None and scrolls:
            target_n = max(1, round(move_duration_s * fps))
            if target_n == 1:
                scrolls = [scrolls[-1]]
            else:
                last = len(scrolls) - 1
                scrolls = [scrolls[round(i * last / (target_n - 1))] for i in range(target_n)]
        for sx in scrolls:
            t_cursor += dt
            frames.append(FrameState(t_s=t_cursor, scroll_x=sx))
        centered_scroll = snaps[idx] if snaps else 0.0
        state = replace(state, virtual_scroll=centered_scroll, target=centered_scroll, velocity=0.0)
        _hold(centered_scroll, _jitter(item.hold_s))

    target_n = max(1, round(duration_s * fps))
    if len(frames) < target_n:
        last_scroll = frames[-1].scroll_x if frames else start_scroll
        for _ in range(target_n - len(frames)):
            t_cursor += dt
            frames.append(FrameState(t_s=t_cursor, scroll_x=last_scroll))
    elif len(frames) > target_n:
        frames = frames[:target_n]

    return frames
