"""Card-transform shapes and stubs for the four Blossom-carousel visual effects.

Each `*_transform` function maps a scroll position + card index to the card's
on-canvas pose for one effect style. `transform_for` dispatches by name.

Pure math only — no skia, no ffmpeg, no I/O. All arithmetic here mirrors the CSS
that the browser reference pages use (`animation-timeline: view(inline)` scroll
scrubbing), so the constants below are meant to visually match those pages, not
to be derived from first principles.

## Coordinate model

- The canvas viewport is `viewport_w` px wide (1080 in production) and
  `VIEWPORT_H` px tall (1920, hardcoded — the renderer's canvas height is fixed
  and effects only ever animate width-wise scroll, so it is not threaded through
  as a parameter). Cards are laid out in a single horizontal row with constant
  pitch `card_w + gap`, centered under a leading/trailing pad of
  `(viewport_w - card_w) / 2` so that card 0 is centered at `scroll_x = 0`.
- `CardTransform.x` is the on-screen LEFT edge of the card, BEFORE scaling. The
  renderer scales (and rotates) the card about its own center, not `x` — so `x`
  here is always the pre-scale geometric left edge, even when `scale != 1`.
- `CardTransform.y` follows the same pre-scale convention and, unless an effect
  says otherwise, is fixed so the card's top is vertically centered in the
  canvas: `y = (VIEWPORT_H - card_h) / 2`.

## View-progress model

CSS `animation-timeline: view(inline)` progress for card `i` is:

    p = (viewport_w - left(i)) / (viewport_w + card_w)

`p == 0` when the card's left edge sits at the right viewport edge (the card is
just entering from the right); `p == 1` when the card's right edge has crossed
the left viewport edge (the card has fully exited to the left); `p == 0.5` when
the card is perfectly centered. Clamped to `[0, 1]`.
"""

from __future__ import annotations

from dataclasses import dataclass

EFFECTS = ("scale_sweep", "cover_flow", "cards_stack", "flipbook")

# Canvas height. Duplicated from renderer.CANVAS_H (not imported) because
# renderer.py imports FROM this module — importing it back would be circular.
VIEWPORT_H: float = 1920.0

# --- scale_sweep (homepage "blossom" effect) ---------------------------------
# Symmetric triangle keyframe on view progress, linear timing: cards scale/fade
# up as they approach center and back down as they leave, peaking at p=0.5.
SCALE_SWEEP_MIN: float = 0.5
SCALE_SWEEP_OPACITY_MIN: float = 0.35
SCALE_SWEEP_SHADOW_MAX: float = 0.25

# --- cover_flow ----------------------------------------------------------------
# Classic Cover Flow: side cards rotate toward the viewer around a vertical
# axis and recede in Z: rotateY(...) translateZ(...) scale(...). Driven by
# view-timeline progress `p` (see `cover_flow_transform`'s docstring) — NOT a
# pitch-normalized distance, so there is no separate "range" constant; `p`'s
# own [0, 1] clamp (in `view_progress`) is the saturation point.
COVER_FLOW_MAX_DEG: float = 35.0
COVER_FLOW_DEPTH_PX: float = 200.0
COVER_FLOW_SCALE_FALLOFF: float = 0.15
COVER_FLOW_SHADOW_MAX: float = 0.35

# --- cards_stack (smart-stack) -------------------------------------------------
# `cards.html`'s actual `@keyframes stack-transform` (view-timeline progress
# `p`, 3 stops — verified against `tools/carousel_reference/out/cards_stack/
# trace.json`): 0%{translateX(24px) scale(0.94); opacity:1} 50%{translateX(0)
# scale(1); opacity:1} 100%{translateX(-38.4px) scale(0.94); opacity:0.3} —
# asymmetric (entering keeps full opacity, exiting fades), unlike
# scale_sweep's symmetric triangle.
STACK_ENTER_TRANSLATE_PX: float = 24.0
STACK_EXIT_TRANSLATE_PX: float = -38.4
STACK_SCALE_MIN: float = 0.94
STACK_EXIT_OPACITY_MIN: float = 0.3

# ROUND 1 -> ROUND 2 (see `cards_stack_transform`'s docstring and
# agents/DECISIONS.md for the full writeup): Round 1 shipped `.card {
# position: sticky; left: 270px; right: 270px }` in `cards.html` (mirroring
# Blossom's own canonical `advanced/cards` example) plus a single best-fit
# `STACK_STICKY_PIN_X` floor constant on `x` to approximate the visual pin.
# That missed a SECOND, deeper effect: once sticky-stuck, the card's
# `animation-timeline: view(inline)` PROGRESS itself (not just its on-screen
# position) stopped being a pure function of `(scroll_x, card_index)` —
# captured-trace evidence: two different cards at nearly identical unstuck
# "flow" positions (`flow_left` within 1-2px of each other) produced
# view-timeline progress 0.209 vs 0.304, a gap that tracked unrelated scroll
# HISTORY, not current geometry (most likely Chromium's view-timeline
# caching a "stuck" progress primed from the transient pre-reset
# `scrollLeft` — see `cards.html`'s old native-resnap workaround, since
# removed, for the underlying `scroll-snap` + `position: sticky` circular-
# resolution quirk that produced that transient in the first place). No
# floor/clamp constant on `x` alone can fix a progress-level (hence
# scale/opacity-level) mismatch like that.
#
# Round 2 took the task's documented escape hatch instead of chasing that
# hysteresis: `cards.html`'s `.card` no longer sets `position: sticky` (or
# `left`/`right`) at all — it's now structurally identical to
# `flipbook.html` (plain `position: relative`, same `padding-inline: 270px`
# centering, same per-card `view(inline)` timeline), just with
# `stack-transform`'s asymmetric keyframes instead of flipbook's symmetric
# ones. With no sticky, `view_progress()`'s existing plain formula (already
# proven exact for `flipbook_transform`/`cover_flow_transform`) applies
# unchanged, and `cards_stack_transform` needs no position floor/clamp at
# all — refit against the recaptured `tools/carousel_reference/out/
# cards_stack/trace.json`, `x` matches to float noise across every frame.

# --- flipbook (spine-pivot page turn) ------------------------------------------
# Pages pivot at the viewport center-line (the spine): an entering page (ahead
# of center) and an exiting page (behind center) rotate/recede symmetrically,
# via `m = 2*view_progress - 1` (see `flipbook_transform`'s docstring) — its
# z-index peak (1000) is `_view_timeline_z_index`'s hardcoded midpoint, shared
# with `cover_flow_transform`, so there's no separate FLIPBOOK_Z_BASE constant.
FLIPBOOK_ENTER_DEG: float = 35.0
FLIPBOOK_DEPTH_PX: float = 200.0
FLIPBOOK_SHADOW_MAX: float = 0.3


@dataclass(frozen=True)
class CardGeometry:
    card_w: float
    card_h: float
    gap: float
    corner_radius: float = 24.0


@dataclass(frozen=True)
class CardTransform:
    x: float  # left edge of the card on the 1080-wide canvas, px, pre-scale
    y: float = 0.0
    scale: float = 1.0
    opacity: float = 1.0
    rotate_y_deg: float = 0.0
    translate_z_px: float = 0.0
    z_index: int = 0
    shadow_alpha: float = 0.0
    # CSS 3D transforms compose in the ORDER the functions are LISTED, applied
    # innermost-first from the *right* end of the list (`transform: A B` means
    # B applies to the point first, then A). `cover_flow`'s keyframes list
    # `rotateY(...) translateZ(...) scale(...)` -> apply order scale, then
    # translateZ, then rotateY LAST (`project_card_corners`'s default: rotate
    # the already-translated (x, z) pair as one unit). `flipbook`'s keyframes
    # list `translateZ(...) rotateY(...)` -> the OPPOSITE order: rotateY
    # applies FIRST (to (x, z=0)), then translateZ adds to z UNROTATED. This
    # flag tells `project_card_corners` which of the two to use — verified
    # against `tools/carousel_reference/out/flipbook/trace.json` frame 0's
    # card-1 `left` (822.6): the rotateY-then-translateZ order lands there;
    # the default (translateZ-then-rotateY) order lands ~98px off.
    rotate_before_translate: bool = False


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _pitch(geo: CardGeometry) -> float:
    return geo.card_w + geo.gap


def _center_left(geo: CardGeometry, viewport_w: float) -> float:
    """Left edge of card 0's static (unscrolled) content position — also the
    left edge any centered card sits at once scrolled into view."""
    return (viewport_w - geo.card_w) / 2


def _card_left(scroll_x: float, card_index: int, geo: CardGeometry, viewport_w: float) -> float:
    """On-screen left edge of card `card_index` at `scroll_x`: L(i) - scroll_x."""
    return _center_left(geo, viewport_w) + card_index * _pitch(geo) - scroll_x


def _centered_distance(
    scroll_x: float, card_index: int, geo: CardGeometry, viewport_w: float
) -> float:
    """Signed distance from the card's center to the viewport's center. Zero
    when centered; +pitch when the next card over, -pitch when the previous."""
    left = _card_left(scroll_x, card_index, geo, viewport_w)
    center_x = left + geo.card_w / 2
    viewport_center = viewport_w / 2
    return center_x - viewport_center


def _default_y(geo: CardGeometry) -> float:
    return (VIEWPORT_H - geo.card_h) / 2


def view_progress(scroll_x: float, card_index: int, geo: CardGeometry, viewport_w: float) -> float:
    """CSS `animation-timeline: view(inline)` progress for this card, clamped to
    [0, 1]. See the module docstring for the p=0/0.5/1 landmarks."""
    left = _card_left(scroll_x, card_index, geo, viewport_w)
    p = (viewport_w - left) / (viewport_w + geo.card_w)
    return _clamp(p, 0.0, 1.0)


def _view_timeline_z_index(p: float, card_index: int) -> int:
    """z-index for a `*-z` keyframe of the shape `cover-flow.html`/
    `flipbook.html` both use:
        0%   { z-index: calc(100 - sibling-index()); }
        50%  { z-index: 1000; }
        100% { z-index: sibling-index(); }
    i.e. linearly interpolated (in `p`) between two DOM-index-based constants
    at the ends and a fixed peak at the center — NOT a distance-based value,
    unlike this module's rotate/translateZ/scale formulas. `sibling-index()`
    is 1-based per the CSS spec; this repo's cards are referenced by a
    0-based `card_index` throughout, so `card_index` is used as-is here
    rather than `card_index + 1` — the exact base only shifts every card's
    z-index by a constant 1, which never changes paint ORDER (the only thing
    z-index affects for this renderer — see `render_carousel_frames`'s
    `(z_index, index)` sort key, which already breaks ties by index).

    No captured ground truth for this one: `trace.json` has no z-index field
    (draw order isn't geometry), so unlike the rest of this module's Round 1
    fixes, this is a direct keyframe port, not independently verified against
    a browser capture.
    """
    if p <= 0.5:
        return round((100 - card_index) + (1000 - (100 - card_index)) * (p / 0.5))
    return round(1000 + (card_index - 1000) * ((p - 0.5) / 0.5))


def scale_sweep_transform(
    scroll_x: float,
    card_index: int,
    geo: CardGeometry,
    viewport_w: float,
    *,
    position_scroll_x: float | None = None,
) -> CardTransform:
    """Symmetric triangle keyframe on view progress: `t = 1 - |2p - 1|` is 0 at
    the edges of the scroll-timeline and 1 exactly centered; scale and opacity
    both ramp linearly with `t`.

    `scroll_x` drives the view-timeline PROGRESS (p/t and everything derived
    from it — scale, opacity, shadow); `position_scroll_x` (defaults to
    `scroll_x`, so single-argument callers see the old self-consistent
    behavior) drives the card's LAYOUT position (`x`). These are genuinely
    different scroll values one frame apart when driven by
    `renderer.lagged_virtual_scroll` — see that function's docstring: a
    captured browser frame's `scrollLeft` (hence layout position) is NOT
    lagged, but its `animation-timeline: view(inline)`-driven `transform`
    IS, by one frame. Verified against `tools/carousel_reference/out/
    scale_sweep/trace.json` frame 15 card 0: using frame 15's own scrollLeft
    (629) for BOTH position and progress predicts `left=-254.16`; using
    frame 15's scrollLeft for position but frame 14's (531) for progress
    predicts `left=-270.5` — the browser's exact value.
    """
    if position_scroll_x is None:
        position_scroll_x = scroll_x
    p = view_progress(scroll_x, card_index, geo, viewport_w)
    t = 1 - abs(2 * p - 1)
    scale = SCALE_SWEEP_MIN + (1 - SCALE_SWEEP_MIN) * t
    opacity = SCALE_SWEEP_OPACITY_MIN + (1 - SCALE_SWEEP_OPACITY_MIN) * t
    return CardTransform(
        x=_card_left(position_scroll_x, card_index, geo, viewport_w),
        y=_default_y(geo),
        scale=scale,
        opacity=opacity,
        rotate_y_deg=0.0,
        translate_z_px=0.0,
        z_index=0,
        shadow_alpha=SCALE_SWEEP_SHADOW_MAX * t,
    )


def cover_flow_transform(
    scroll_x: float,
    card_index: int,
    geo: CardGeometry,
    viewport_w: float,
    *,
    position_scroll_x: float | None = None,
) -> CardTransform:
    """Side cards rotate toward the center (`rotateY(+-35deg)`), recede in Z
    (`translateZ(-200px)`), and shrink slightly, all driven by the card's
    view-timeline progress `p` — NOT the pitch-normalized distance `d /
    pitch` an earlier version of this function used (see Round 1's provenance
    note below); the two are close near the center but diverge increasingly
    approaching (and beyond) the `cover` range's edges, since `p` SATURATES
    at 0/1 (`view_progress` clamps it) while `d / pitch` keeps growing
    linearly forever.

    `m = 2p - 1` recasts `p`'s [0, 1] range as [-1, 1] centered on 0, matching
    `cover-flow.html`'s `@keyframes cover-flow-transform` 3-stop shape exactly:
    `m=-1` (p=0, entering) -> `rotateY(-35deg) translateZ(-200px) scale(0.85)`;
    `m=0` (p=0.5, centered) -> `rotateY(0) translateZ(0) scale(1)`; `m=+1`
    (p=1, exited) -> `rotateY(35deg) translateZ(-200px) scale(0.85)`.

    Round 1 provenance: the pitch-based `n = clamp(d/pitch, -2, 2)` version
    was fit against `tools/carousel_reference/out/cover_flow/trace.json`'s
    raw `scale` field and looked plausible in isolation, but that field is
    `DOMMatrixReadOnly(transform).a` — for a combined `rotateY() scale()`
    transform that's `cos(rotateY) * scale`, NOT the CSS `scale()` value
    alone (see that dir's README's `scale` caveat) — so fitting against it
    directly is fitting against the wrong quantity. Refit against the actual
    rendered bounding box (`width`/`height` via `renderer.project_card_corners`,
    which the README calls out as the reliable proxy) instead: the `m`-based
    formula below matches every frame/card to ~2e-4px (float noise); the old
    `n`-based one was off by up to 54px on this same trace.

    See `scale_sweep_transform`'s docstring for `scroll_x` vs
    `position_scroll_x` (progress vs layout-position scroll)."""
    if position_scroll_x is None:
        position_scroll_x = scroll_x
    p = view_progress(scroll_x, card_index, geo, viewport_w)
    m = 2 * p - 1
    m_abs = abs(m)
    rotate_y_deg = COVER_FLOW_MAX_DEG * m
    translate_z_px = -COVER_FLOW_DEPTH_PX * m_abs
    scale = 1 - COVER_FLOW_SCALE_FALLOFF * m_abs
    return CardTransform(
        x=_card_left(position_scroll_x, card_index, geo, viewport_w),
        y=_default_y(geo),
        scale=scale,
        opacity=1.0,
        rotate_y_deg=rotate_y_deg,
        translate_z_px=translate_z_px,
        z_index=_view_timeline_z_index(p, card_index),
        shadow_alpha=COVER_FLOW_SHADOW_MAX * m_abs,
    )


def cards_stack_transform(
    scroll_x: float,
    card_index: int,
    geo: CardGeometry,
    viewport_w: float,
    *,
    position_scroll_x: float | None = None,
) -> CardTransform:
    """Cards animate through `stack-transform`'s 3-stop, view-timeline-progress
    (`p`) keyframes — see `STACK_ENTER_TRANSLATE_PX` etc.'s module-level
    comment for the exact values — same triangle-on-`p` shape as
    `scale_sweep_transform`/`cover_flow_transform`/`flipbook_transform`, just
    asymmetric (translateX/scale/opacity all differ between the entering
    [0, 0.5] and exiting [0.5, 1] halves).

    ROUND 2: `cards.html` no longer uses `position: sticky` (see the
    module-level comment above `STACK_ENTER_TRANSLATE_PX` for why Round 1's
    sticky model was abandoned rather than patched further), so `x` is
    plain flat-layout-plus-`translateX`, same pattern as every other
    effect here — no floor/clamp constant. `CardTransform.x` is documented
    as the PRE-SCALE left edge (the renderer scales about the card's own
    center using `x` as input), and CSS `transform: translateX(tx)
    scale(s)` applied to an element scales it about its own center FIRST,
    then shifts by `tx` — i.e. the renderer's own `x + card_w*(1-scale)/2`
    post-scale-left computation and this function's `tx` shift compose by
    plain addition on `x` itself (the `card_w*(1-scale)/2` term cancels
    algebraically), so `x = flat_left + tx` directly, unlike Round 1's
    version which routed through an explicit post-scale `visual_left` only
    because it needed a plain px floor to clamp against.

    See `scale_sweep_transform`'s docstring for `scroll_x` vs
    `position_scroll_x` (progress vs layout-position scroll)."""
    if position_scroll_x is None:
        position_scroll_x = scroll_x
    p = view_progress(scroll_x, card_index, geo, viewport_w)
    if p <= 0.5:
        f = p / 0.5
        tx = STACK_ENTER_TRANSLATE_PX * (1 - f)
        scale = STACK_SCALE_MIN + (1 - STACK_SCALE_MIN) * f
        opacity = 1.0
    else:
        f = (p - 0.5) / 0.5
        tx = STACK_EXIT_TRANSLATE_PX * f
        scale = 1 - (1 - STACK_SCALE_MIN) * f
        opacity = 1 - (1 - STACK_EXIT_OPACITY_MIN) * f

    flat_left = _card_left(position_scroll_x, card_index, geo, viewport_w)
    x = flat_left + tx

    return CardTransform(
        x=x,
        y=_default_y(geo),
        scale=scale,
        opacity=opacity,
        rotate_y_deg=0.0,
        translate_z_px=0.0,
        z_index=_view_timeline_z_index(p, card_index),
        shadow_alpha=0.0,
    )


def flipbook_transform(
    scroll_x: float,
    card_index: int,
    geo: CardGeometry,
    viewport_w: float,
    *,
    position_scroll_x: float | None = None,
) -> CardTransform:
    """Spine-pivot page turn, driven by view-timeline progress `p` (see
    `cover_flow_transform`'s docstring for why `p` and not a pitch-normalized
    distance — the same Round 1 refit applies here): `m = 2p - 1` is `-1` at
    p=0 (entering keyframe: `translateZ(-200px) rotateY(-35deg)`), `0` at
    p=0.5 (`none` — flat, no transform), `+1` at p=1 (exiting keyframe:
    `translateZ(-200px) rotateY(35deg)`); `|m|` alone drives depth (odd in
    `m`, even effect on translateZ, matching `flip-transform`'s symmetric
    keyframes).

    Round 1 note: verified against `tools/carousel_reference/out/flipbook/
    trace.json` via the rendered bounding box (`width`/`height`), matching to
    ~2e-4px; also fixed two other bugs this effect specifically needed
    (`rotate_before_translate=True` — flipbook's keyframe CSS lists
    `translateZ(...) rotateY(...)`, the opposite composition order from
    cover_flow's `rotateY(...) translateZ(...) scale(...)`, see
    `CardTransform.rotate_before_translate` — and the ROTATE SIGN: the
    pitch-based predecessor of this function used
    `rotate_y_deg=-FLIPBOOK_ENTER_DEG*n`; the correct sign for the `m`-based
    formula is `+FLIPBOOK_ENTER_DEG*m`, i.e. the SAME sign convention
    `cover_flow_transform` uses for its `rotate_y_deg = COVER_FLOW_MAX_DEG *
    m` — the earlier negative sign was compensating for the wrong transform
    composition order, not a genuine effect-specific difference).

    See `scale_sweep_transform`'s docstring for `scroll_x` vs
    `position_scroll_x` (progress vs layout-position scroll)."""
    if position_scroll_x is None:
        position_scroll_x = scroll_x
    p = view_progress(scroll_x, card_index, geo, viewport_w)
    m = 2 * p - 1
    m_abs = abs(m)
    return CardTransform(
        x=_card_left(position_scroll_x, card_index, geo, viewport_w),
        y=_default_y(geo),
        scale=1.0,
        opacity=1.0,
        rotate_y_deg=FLIPBOOK_ENTER_DEG * m,
        translate_z_px=-FLIPBOOK_DEPTH_PX * m_abs,
        z_index=_view_timeline_z_index(p, card_index),
        shadow_alpha=FLIPBOOK_SHADOW_MAX * m_abs,
        # flipbook.html's keyframes are `translateZ(...) rotateY(...)` — the
        # opposite composition order from cover_flow's `rotateY(...)
        # translateZ(...) scale(...)`. See CardTransform.rotate_before_translate.
        rotate_before_translate=True,
    )


_TRANSFORM_BY_EFFECT = {
    "scale_sweep": scale_sweep_transform,
    "cover_flow": cover_flow_transform,
    "cards_stack": cards_stack_transform,
    "flipbook": flipbook_transform,
}


def transform_for(
    effect: str,
    scroll_x: float,
    card_index: int,
    geo: CardGeometry,
    viewport_w: float,
    *,
    position_scroll_x: float | None = None,
) -> CardTransform:
    try:
        fn = _TRANSFORM_BY_EFFECT[effect]
    except KeyError:
        raise ValueError(f"Unknown carousel effect {effect!r}; expected one of {EFFECTS}") from None
    return fn(scroll_x, card_index, geo, viewport_w, position_scroll_x=position_scroll_x)


def snap_positions(effect: str, n_cards: int, geo: CardGeometry, viewport_w: float) -> list[float]:
    """Scroll positions that center each card in turn, replicating the
    vendored bundle's actual `scroll-snap-align: center` math rather than a
    naive flat `i * pitch` grid.

    The bundle computes each card's snap position from
    `card.getBoundingClientRect()` — WHATEVER pose is currently painted,
    including any `animation-timeline: view(inline)`-driven scale/rotation
    the effect has already applied at mount time (snap targets are
    (re)computed once, from a `ResizeObserver` callback on the scroller's
    own box, not per animation frame) — combined with `card.clientWidth`
    (the flat, UNSCALED layout width; `clientWidth` never reflects a CSS
    `transform`). Mixing a scaled/rotated rect.left with an unscaled
    half-width is exactly the quirk that makes `scale_sweep`'s canonical
    flick settle at `scrollLeft=1311` (card index 2's *painted*, scale=0.5
    left edge, shifted +135px right of its flat/unscaled center of 1176)
    rather than 1176 — see `spring.py`'s module docstring for the full
    derivation, verified against `tools/carousel_reference/out/scale_sweep/
    trace.json`.

    Replicated here by evaluating `transform_for(effect, scroll_x=0, ...)`
    for each card (the pose painted at mount, before any gesture), taking
    the AABB left edge of that projected pose via the SAME math the renderer
    uses (`renderer.project_card_corners` — imported lazily inside this
    function to dodge the module cycle: `renderer.py` imports FROM this
    module at load time, so a top-level import here would be circular; by
    call time both modules are already fully loaded), adding back HALF THE
    FLAT card width (not the scaled/projected width — mirrors `clientWidth`),
    and finally clamping to the scroller's flat scrollable range (layout
    `scrollWidth` is unaffected by transforms, so this bound is NOT scaled)
    — mirroring the bundle's own clamp in `ne()` before it turns a snap
    choice into a release force (see `spring.release`'s `bounds` param).
    """
    from .renderer import project_card_corners  # noqa: PLC0415 — see docstring

    flat_content_w = (
        2 * _center_left(geo, viewport_w) + n_cards * geo.card_w + max(0, n_cards - 1) * geo.gap
    )
    bound_max = max(0.0, flat_content_w - viewport_w)

    positions: list[float] = []
    for i in range(n_cards):
        t = transform_for(effect, 0.0, i, geo, viewport_w)
        corners = project_card_corners(t, geo)
        visual_left = min(x for x, _y in corners)
        snap_x = visual_left + geo.card_w / 2.0 - viewport_w / 2.0
        positions.append(_clamp(snap_x, 0.0, bound_max))
    return positions


def snap_bounds(n_cards: int, geo: CardGeometry, viewport_w: float) -> tuple[float, float]:
    """The scroller's flat (unscaled) scrollable range `[0, scrollWidth -
    scrollerWidth]`, for `spring.release`'s `bounds` clamp. Layout
    `scrollWidth` is unaffected by CSS `transform`, so this is NOT
    effect-dependent (unlike `snap_positions`, which mixes in each effect's
    painted-at-mount pose)."""
    flat_content_w = (
        2 * _center_left(geo, viewport_w) + n_cards * geo.card_w + max(0, n_cards - 1) * geo.gap
    )
    return (0.0, max(0.0, flat_content_w - viewport_w))
