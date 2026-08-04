"""Port of the Blossom Carousel scroll physics, extracted directly from the
VENDORED bundle actually driving `tools/carousel_reference/` — NOT from the
`jespervos/blossom-carousel` GitHub source. Pinned version:
`@blossom-carousel/web@1.4.2`, bundling `@blossom-carousel/core@1.1.8`
(confirmed by the inlined `//#region .../@blossom-carousel+core@1.1.8/...`
comment at the top of `tools/carousel_reference/lib/blossom-vendor/dist/
blossom-carousel-web.es.js`, lines 1-441 of that file — beautified for
reading via `npx js-beautify`).

## What Round 1 found

The FRICTION/DAMPING constants and roles were NOT swapped (a live hypothesis
going into this pass): the bundle defines `var u = .72, d = .12` and uses `u`
(friction) for both the per-tick velocity decay AND as the drag-tracking damp
factor, `d` (damping) only for the released/idle damp factor toward `target`
— exactly the roles `FRICTION`/`DAMPING` already had here. The `tick()`/
`release()` math (see below) was already a correct 1:1 port.

The actual bug was in the CALLER (`effects.snap_positions`, formerly a naive
`i * (card_w + gap)` flat grid) combined with two behaviors this module
didn't model at all:

1. **Snap positions are computed from the PAINTED (scaled/rotated) card
   rect, not the flat layout rect.** The bundle's `ee()` (its snap-position
   scanner) runs once, from a `ResizeObserver` callback on the scroller's
   box — it does NOT re-run per animation frame. By the time it first fires
   (page mount, `scrollLeft=0`), each card's `animation-timeline:
   view(inline)`-driven transform has ALREADY painted at that scroll
   position, so `card.getBoundingClientRect().left` reflects the scaled/
   rotated pose, not the unscaled flex-layout position. `ee()` then combines
   that PAINTED `rect.left` with `card.clientWidth` (a layout property —
   NEVER affected by `transform`, so it's always the flat, unscaled width)
   to compute the `scroll-snap-align: center` position:
   `snap_x = paintedLeft + clientWidth/2 - scrollerWidth/2`.
   Mixing a scaled left-edge with an unscaled half-width is exactly the
   quirk that makes the canonical flick settle at `scrollLeft = 1311`, not
   `1176`, on `scale_sweep`: card index 2 sits at flat/unscaled center
   `1176`, but at `scrollLeft=0` (mount time) it's painted at `scale=0.5`
   (view-progress `t=0`, off in the wings), so its PAINTED left edge is
   shifted +135px right of its flat left edge (`card_w * (1 - 0.5) / 2 =
   135`) — `1176 + 135 = 1311`. Verified against
   `tools/carousel_reference/out/scale_sweep/trace.json`: frame 0's card-2
   `left` is `1581`, and `1581 - card_w/2(270) - viewport_w/2... ` — worked
   out precisely, `snap_x = paintedLeft(1581) + card_w/2(270) -
   viewport_w/2(540) = 1311`. Exact match. This module still has NO opinion
   on card geometry/painted poses — `effects.snap_positions` now does this
   projection (reusing `renderer.project_card_corners`, imported lazily to
   dodge the module cycle) and passes the resulting (clamped) list in here
   exactly as before; `release()`'s job is unchanged, it just receives
   truer input.

2. **The tick loop doesn't start immediately at pointerdown — it starts
   once *cumulative* pointer movement crosses `DRAG_ACTIVATION_PX` (10px in
   the bundle, `_.x >= 10`).** Before that: `pointermove` still accumulates
   into `target`/`velocity` (the bundle's `h.x`/`g.x`), but nothing calls
   `scrollTo` and velocity never decays — the whole tick machinery is
   inert. The frame that crosses the threshold runs its FIRST tick with an
   effectively-zero `frame_delta_ms` (the bundle sets its clock reference
   `V = performance.now()` synchronously inside the same handler that flips
   the "start ticking" flag, so the first `requestAnimationFrame(H)` callback
   measures ~0ms elapsed) — that first tick still decays `velocity` (the
   decay is unconditional in the bundle, before the branch on `frame_delta_ms`)
   but leaves `virtual_scroll` untouched (`damp(x, y, t, delta_ms=0) == x`).

   **The same threshold-crossing handler ALSO resets `target` to the
   scroller's current actual `scrollLeft`** (`h.x = t.scrollLeft`, bundle
   line: `r && !z.value ? (V = performance.now(), v.x && (h.x =
   t.scrollLeft), ...)` — a side effect of the SAME Proxy setter that flips
   the activation flag, itself triggered synchronously from inside the
   pointermove handler that just accumulated this frame's delta into
   `target`). This DISCARDS whatever the activating delta (and everything
   before it) had accumulated into `target` — velocity is untouched. Missing
   this reset produces a trace that's off by a near-constant ~10px through
   the whole drag+decay ramp (traced this by hand against
   `tools/carousel_reference/out/scale_sweep/trace.json`: frame 3 is
   `scrollLeft=8`; a naive no-reset model predicts `damp(0, 19, .72,
   33.33) ≈ 17.5`; with the reset, `target` at frame 3 is `9` — not `19` —
   giving `damp(0, 9, .72, 33.33) ≈ 8.29`, matching). It doesn't change the
   final settle position (the closed-form release recurrence — see
   `test_release_force_recurrence_proof_holds_numerically` — converges to
   `slide_x` regardless of `target`'s value at release, so this bug was
   invisible if you only checked the resting position).

   `simulate()` below reproduces both the zero-delay tick and the
   target-reset with `tick_active` + `total_drag_px` on `SpringState`, and
   emits a leading pointerdown frame (frame 0, no movement) so `simulate()`'s
   output frame `i` lines up 1:1 with the browser trace's `trace[i]` —
   previously frame 0 here WAS the first pointermove already fully ticked,
   one frame ahead of the browser.

3. **The picked snap position is clamped to the scroller's flat scrollable
   range** (`ne()`: `o(i.x, min(0, (scrollWidth-scrollerWidth)*dir),
   max(...))`) before being converted into a release force — added as
   `release()`'s optional `bounds` parameter (`None` skips the clamp, so
   existing hand-derived-value tests are unaffected).

## Untouched because it doesn't apply here / was already correct

- `te()` (the bundle's "should we even snap on release" gate) short-circuits
  to always-true when `scroll-snap-type` is `mandatory` — true on all four
  `tools/carousel_reference/*.html` pages — so its distance-threshold
  branch is dead code for every effect this repo renders. `release()` below
  keeps the threshold/candidate-then-fallback-to-nearest structure (it
  always resolves to "nearest of all snap_positions" regardless, so it was
  already behaviorally equivalent to the mandatory-snap case) rather than
  special-casing mandatory-vs-proximity, since we have no non-mandatory
  effect to exercise the other branch against.
- `tick()`'s formulas (velocity decay order, which branch updates `target`
  during a drag vs. after release, `damp`'s exponential-smoothing formula)
  were already a correct 1:1 port and are unchanged.
- `rubberband_offset()` (bundle's `le()`) was already a correct port and
  isn't exercised by `simulate()` (`CANONICAL_FLICK` never leaves scroll
  bounds), so it's untouched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .gesture import GestureTrace

FRICTION: float = 0.72
DAMPING: float = 0.12

# Bundle: `_.x >= 10` (Proxy setter on the drag-distance accumulator) gates
# both (a) whether the tick loop (`H()`/this module's `tick()`) runs at all
# during a drag, and (b) whether `release()`'s snap-seeking logic runs at
# pointerup (`P()`: `!(_.x <= 10) && (...)`) — below this, a gesture is a tap,
# not a flick.
DRAG_ACTIVATION_PX: float = 10.0


@dataclass(frozen=True)
class SpringState:
    virtual_scroll: float = 0.0
    target: float = 0.0
    velocity: float = 0.0
    is_dragging: bool = False
    # Cumulative |pointermove delta| since the current pointerdown (bundle's
    # `_.x`). Drives `DRAG_ACTIVATION_PX` gating in `simulate()`/`release()`.
    total_drag_px: float = 0.0
    # Whether the tick loop has been requested yet (bundle's `z.value`,
    # restricted to the "has the drag-distance latch fired" meaning it has
    # here — the bundle's `z.value` has other triggers unrelated to a
    # scripted single-drag replay, e.g. wheel/keyboard, that this port has no
    # need to model).
    tick_active: bool = False


@dataclass(frozen=True)
class SpringFrame:
    t_s: float
    virtual_scroll: float
    velocity: float
    target: float


def damp(x: float, y: float, t: float, delta_ms: float) -> float:
    """Framerate-independent exponential smoothing of `x` toward `y`.

    JS: `lerp(x, y, 1 - Math.exp(Math.log(1 - t) * (delta / (1000 / 60))))`.
    """
    factor = 1 - math.exp(math.log(1 - t) * (delta_ms / (1000 / 60)))
    return x + (y - x) * factor


def project(target: float, velocity: float, friction: float = FRICTION) -> float:
    return target + velocity / (1 - friction)


def tick(state: SpringState, frame_delta_ms: float) -> SpringState:
    """Advance the spring by one animation frame. Only meaningful once the
    tick loop is actually running (`state.tick_active`) — `simulate()` is
    responsible for not calling this before `DRAG_ACTIVATION_PX` is crossed."""
    velocity = state.velocity * FRICTION
    if not state.is_dragging:
        target = state.target + velocity
        virtual_scroll = damp(state.virtual_scroll, target, DAMPING, frame_delta_ms)
    else:
        target = state.target
        virtual_scroll = damp(state.virtual_scroll, state.target, FRICTION, frame_delta_ms)
    return replace(state, velocity=velocity, target=target, virtual_scroll=virtual_scroll)


def release(
    state: SpringState,
    snap_positions: list[float],
    snapport_width: float,
    bounds: tuple[float, float] | None = None,
) -> SpringState:
    """Called once at pointer-up: project the resting scroll position, snap to
    the nearest candidate within a proximity threshold (else the nearest snap
    position overall), clamp that choice to `bounds` (the scroller's flat
    scrollable range, if given), and convert the required correction into a
    velocity that drives the overshoot-settle animation.

    Below `DRAG_ACTIVATION_PX` of total drag, the bundle treats the gesture
    as a tap, not a flick, and skips this entirely (`P()`: `!(_.x <= 10) &&
    ...`) — just stops dragging with whatever target/velocity already stand.
    """
    if state.total_drag_px <= DRAG_ACTIVATION_PX:
        return replace(state, is_dragging=False)

    velocity = state.velocity * 2
    resting_x = project(state.target, velocity, FRICTION)
    threshold = snapport_width / 3
    candidates = [p for p in snap_positions if abs(p - resting_x) <= threshold]
    pool = candidates if candidates else snap_positions
    slide_x = min(pool, key=lambda p: abs(p - resting_x))
    if bounds is not None:
        lo, hi = bounds
        slide_x = min(hi, max(lo, slide_x))
    force = (slide_x - state.target) * (1 - FRICTION) * (1 / FRICTION)
    return replace(state, is_dragging=False, velocity=force)


def is_settled(state: SpringState) -> bool:
    return round(state.velocity, 12) == 0.0


def rubberband_offset(
    offset: float, overscroll: float, is_dragging: bool, frame_delta_ms: float
) -> float:
    t = 0.8 if is_dragging else DAMPING
    return damp(offset, overscroll * -0.2, t, frame_delta_ms)


def simulate(
    gesture: GestureTrace,
    snap_positions: list[float],
    snapport_width: float,
    start_scroll: float = 0.0,
    max_frames: int = 600,
    bounds: tuple[float, float] | None = None,
) -> list[SpringFrame]:
    """Replay a scripted drag+release gesture through the spring and return one
    SpringFrame per animation frame, from the pointerdown frame through settle
    — frame `i` of the returned list lines up 1:1 with `trace[i]` of a
    `tools/carousel_reference/capture.sh` browser capture (see this module's
    docstring, point 2: the browser's `harness.js` also captures a frame right
    after pointerdown, before any movement or ticking has happened).

    Sign convention: `gesture.drag_deltas_px` are FINGER movement per frame
    (negative = finger moves left = carousel advances). Blossom computes
    `deltaX = pointerStart.x - clientX`, i.e. scroll delta = -finger_delta.
    """
    frame_delta_ms = 1000 / gesture.fps
    state = SpringState(
        virtual_scroll=start_scroll, target=start_scroll, velocity=0.0, is_dragging=True
    )
    frames: list[SpringFrame] = []
    frame_index = 0

    def _emit() -> None:
        nonlocal frame_index
        frame_index += 1
        frames.append(
            SpringFrame(
                t_s=frame_index / gesture.fps,
                virtual_scroll=state.virtual_scroll,
                velocity=state.velocity,
                target=state.target,
            )
        )

    # Frame 0: pointerdown only — no movement, no tick. The bundle's tick
    # loop isn't requested until a pointermove's cumulative drag distance
    # crosses DRAG_ACTIVATION_PX, and pointerdown itself is not a move.
    _emit()

    for finger_delta in gesture.drag_deltas_px:
        scroll_delta = -finger_delta
        was_active = state.tick_active
        total_drag_px = state.total_drag_px + abs(scroll_delta)
        state = replace(
            state,
            target=state.target + scroll_delta,
            velocity=state.velocity + scroll_delta,
            total_drag_px=total_drag_px,
        )
        just_activated = (not was_active) and total_drag_px >= DRAG_ACTIVATION_PX
        if just_activated:
            # The activation-latching frame: the bundle resets `target` to
            # the CURRENT virtual_scroll (discarding this delta's — and
            # every prior delta's — contribution to target; velocity is
            # untouched), then runs its first tick with the clock reference
            # just set, so ~0ms elapsed — a real tick (it still decays
            # `velocity`) that leaves `virtual_scroll` in place since
            # target == virtual_scroll going in. See module docstring point 2.
            state = replace(state, tick_active=True, target=state.virtual_scroll)
            state = tick(state, 0.0)
        elif was_active:
            state = tick(state, frame_delta_ms)
        # else: still below the activation threshold this frame — no tick;
        # target/velocity keep accumulating undamped/undecayed (matches the
        # bundle: pointermove always updates its target/velocity accumulators,
        # but nothing reads or decays them until the tick loop is running).
        _emit()

    state = release(state, snap_positions, snapport_width, bounds=bounds)

    while not is_settled(state) and frame_index < max_frames:
        state = tick(state, frame_delta_ms)
        _emit()

    return frames
