"""The DIRECTOR: picks which Blossom-carousel treatment a generative variant
gets — mode (rolling/focus/stills), effect (scale_sweep/cover_flow/
cards_stack/flipbook), and (for `mode="focus"`) which card(s) get a
hold-and-zoom beat — from a deterministic, seeded heuristic. No LLM call in
v1; this is the piece that makes multiple variants of the same job feel
different from each other instead of all reaching for the same effect.

## Policy (every rule lives here; nowhere else)

Mode (`_choose_mode` / `_mode_weights`):
  - "Qualifies for focus" iff there are >= 3 clips AND every clip is
    >= 2.5s long. Rationale: `mode="focus"` needs at least one clip long
    enough to hold on (see the hold-duration floor below), and reads as
    thin with only 1-2 cards.
  - Qualified clip sets: weighted seeded choice over
    {focus: 0.5, rolling: 0.35, stills: 0.15} — focus is the most
    "produced"-feeling treatment, so it's favored when the footage supports
    it.
  - Non-qualified clip sets (short and/or few clips): weighted seeded choice
    over {rolling: 0.6, stills: 0.4} — no focus candidate, and rolling reads
    better than stills on a small card count.
  - `allowed_modes` filters the candidate set before the weighted draw
    (renormalized); if the filter empties it out entirely, falls back to a
    uniform draw over `allowed_modes` itself rather than raising.

Effect (`_choose_effect` / `_effect_weights`):
  - Base weight 1.0 for all four effects in `effects.EFFECTS`, then:
    - >= 4 clips: cover_flow and flipbook weights x2 — both effects put
      multiple cards visibly mid-stack (rotated/receding) at once, which
      only reads as intentional, not sparse, once there's enough deck to
      show.
    - == 3 clips: scale_sweep weight x2 — its symmetric scale/opacity
      sweep reads cleanly with a shorter deck where cover_flow/flipbook's
      side-card rotation would have little on either side to show.
    - Durations are "homogeneous" (`_is_duration_homogeneous`, coefficient
      of variation <= 0.15): cards_stack weight x2 — a stack reads as a
      single coherent deck when every card gets roughly equal screen time;
      wildly uneven durations make the stack metaphor confusing (why does
      this card linger?).
  - `allowed_effects` filters the same way `allowed_modes` does for mode.

Focus targets (`_build_focus_moments`, only when mode == "focus"):
  - Target count: 1 focus moment when `target_duration_s <= 8`, else 2 —
    a short moment only has room to breathe on one card.
  - Candidate ranking (`_rank_focus_candidates`): by `interest` descending,
    then `duration_s` descending, then original list position (stable sort)
    as the final, fully-deterministic tiebreak.
  - Per candidate, in ranked order: draw a seeded jitter in [-0.5, 0.5],
    `hold_s = clamp(2.0 + jitter, 1.5, 3.0)`. The candidate only qualifies
    if `clip.duration_s >= hold_s + 1.5` (1.5s of headroom beyond the hold
    itself, for the zoom in/out and to avoid holding on a card for
    ~its entire runtime). Candidates that fail the floor are skipped in
    favor of the next-ranked one.
  - `card_index` on each `FocusMoment` is the candidate's position in the
    INPUT `clips` list (not its rank) — this has to line up with
    `CarouselMomentSpec.clip_paths`' order, which the renderer uses 1:1 to
    assign card indices (see `segment.py`).
  - If fewer qualifying candidates exist than the target count (including
    zero), mode falls back to "rolling" for this call and no
    `focus_moments` are emitted — a focus moment that can't fill its target
    count silently degrades rather than rendering a half-committed effect.

Shared:
  - `clip_paths` preserves the INPUT `clips` order verbatim — it has to
    match the montage's own step order, since the carousel segment shows a
    supercut of the same footage the montage uses elsewhere in the variant.
  - `duration_s` is always set to `target_duration_s`, even for
    `mode="focus"` where the engine derives actual on-screen time from the
    focus moments instead (`choreography` owns it) — it still caps how long
    the caller is willing to let the segment run, and downstream code may
    want it even when it isn't load-bearing for focus.
  - Everything is driven off `random.Random(seed)` — same `clips` + `seed`
    always produces the same `CarouselMomentSpec` (mode, effect, and, for
    focus, which card(s) and holds).

## Diversity guarantee

`diversify()` exists because a generative job renders several variants of
the same clip pool, and if every variant asked `direct_carousel_moment` for
"the" answer with the same-ish seed they'd frequently land on the same
(mode, effect) pair — which defeats the point of a carousel MOMENT being a
distinguishing beat between variants. It re-rolls with an incrementing seed
(bounded attempts) until it finds a (mode, effect) pair that isn't already
in `specs_already_used`; if the attempts are exhausted, it returns whatever
the last attempt produced rather than raising (never-block-a-render
contract, same spirit as the rest of the carousel package).

## LLM handoff

v1 is pure stdlib heuristics — no network call, so it works with the same
reliability guarantees `render_carousel_moment` already has (see
`segment.py`'s never-raise contract; nothing here should ever need to
change that). `ClipInfo` and `direct_carousel_moment`'s signature are
shaped so a future agent-authored version can slot in behind the same
call: given the same `list[ClipInfo]` (already carrying optional
`labels`/`interest` for exactly this reason) plus `seed` for reproducible
re-renders, a prompted agent could emit the same `CarouselMomentSpec`
shape (mode/effect/focus_moments) that this module builds heuristically —
callers (`generative_build.py`) would not need to change at all, only the
implementation of `direct_carousel_moment` itself.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - never executed, import-cycle/landing safe
    from .choreography import FocusMoment
    from .segment import CarouselMomentSpec

log = logging.getLogger(__name__)

# `effects.py` is a stable, already-shipped module (unlike segment/choreography,
# which a concurrent lane is still extending), so this import is safe at module
# load time.
from .effects import EFFECTS  # noqa: E402

# ── Policy constants (see the module docstring for the "why" of each) ──────

FOCUS_QUALIFY_MIN_CLIPS = 3
FOCUS_QUALIFY_MIN_DURATION_S = 2.5

MODE_WEIGHTS_FOCUS_QUALIFIED: dict[str, float] = {"focus": 0.5, "rolling": 0.35, "stills": 0.15}
MODE_WEIGHTS_FOCUS_UNQUALIFIED: dict[str, float] = {"rolling": 0.6, "stills": 0.4}

EFFECT_WEIGHT_MULTIPLIER_LARGE_DECK = 2.0  # cover_flow / flipbook, >= 4 clips
EFFECT_WEIGHT_MULTIPLIER_SMALL_DECK = 2.0  # scale_sweep, == 3 clips
EFFECT_WEIGHT_MULTIPLIER_HOMOGENEOUS = 2.0  # cards_stack, low duration variance
LARGE_DECK_MIN_CLIPS = 4
SMALL_DECK_CLIPS = 3
HOMOGENEITY_CV_THRESHOLD = 0.15  # stdev / mean of clip durations

FOCUS_TWO_TARGET_DURATION_THRESHOLD_S = 8.0
HOLD_S_BASE = 2.0
HOLD_S_JITTER = 0.5
HOLD_S_MIN = 1.5
HOLD_S_MAX = 3.0
FOCUS_DURATION_HEADROOM_S = 1.5  # clip must be >= hold_s + this

DEFAULT_TARGET_DURATION_S = 6.0
DEFAULT_ALLOWED_MODES: tuple[str, ...] = ("rolling", "focus", "stills")

MAX_DIVERSIFY_ATTEMPTS = 25


@dataclass(frozen=True)
class ClipInfo:
    """One clip in the pool the director chooses from. `labels`/`interest`
    are optional Gemini clip-analysis signal — v1 doesn't wire them in from
    `generative_build.py` (defaults are used there), but `direct_carousel_moment`
    is written to use them when present so a caller with real signal (or a
    future agent-authored director, see the module docstring's "LLM handoff")
    doesn't need a shape change."""

    path: str
    duration_s: float
    labels: tuple[str, ...] = ()
    interest: float = 0.5


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    """Deterministic (given `rng`'s state) weighted draw over `weights`
    (insertion-ordered dict -> deterministic iteration for the same input)."""
    items = list(weights.items())
    total = sum(w for _, w in items)
    if total <= 0:
        return items[0][0]
    r = rng.uniform(0.0, total)
    upto = 0.0
    for key, w in items:
        upto += w
        if r <= upto:
            return key
    return items[-1][0]  # float rounding fallback


def _restrict_weights(weights: dict[str, float], allowed: tuple[str, ...]) -> dict[str, float]:
    restricted = {k: w for k, w in weights.items() if k in allowed}
    if restricted:
        return restricted
    # The caller's `allowed_*` filter excluded every weighted candidate —
    # degrade to a uniform draw over whatever the caller DID allow, rather
    # than raising. `allowed` itself is trusted non-empty (callers pass a
    # real subset of EFFECTS/modes); if it somehow is empty there is nothing
    # sane to return, so let a later KeyError/IndexError surface that loudly.
    return {k: 1.0 for k in allowed}


def _mode_weights(clips: list[ClipInfo]) -> dict[str, float]:
    qualifies = len(clips) >= FOCUS_QUALIFY_MIN_CLIPS and all(
        c.duration_s >= FOCUS_QUALIFY_MIN_DURATION_S for c in clips
    )
    return dict(MODE_WEIGHTS_FOCUS_QUALIFIED if qualifies else MODE_WEIGHTS_FOCUS_UNQUALIFIED)


def _choose_mode(clips: list[ClipInfo], rng: random.Random, allowed_modes: tuple[str, ...]) -> str:
    weights = _restrict_weights(_mode_weights(clips), allowed_modes)
    return _weighted_choice(rng, weights)


def _is_duration_homogeneous(clips: list[ClipInfo]) -> bool:
    if len(clips) < 2:
        return True
    durations = [c.duration_s for c in clips]
    mean = sum(durations) / len(durations)
    if mean <= 0:
        return True
    variance = sum((d - mean) ** 2 for d in durations) / len(durations)
    stdev = variance**0.5
    return (stdev / mean) <= HOMOGENEITY_CV_THRESHOLD


def _effect_weights(clips: list[ClipInfo]) -> dict[str, float]:
    weights = {e: 1.0 for e in EFFECTS}
    n = len(clips)
    if n >= LARGE_DECK_MIN_CLIPS:
        weights["cover_flow"] *= EFFECT_WEIGHT_MULTIPLIER_LARGE_DECK
        weights["flipbook"] *= EFFECT_WEIGHT_MULTIPLIER_LARGE_DECK
    if n == SMALL_DECK_CLIPS:
        weights["scale_sweep"] *= EFFECT_WEIGHT_MULTIPLIER_SMALL_DECK
    if _is_duration_homogeneous(clips):
        weights["cards_stack"] *= EFFECT_WEIGHT_MULTIPLIER_HOMOGENEOUS
    return weights


def _choose_effect(
    clips: list[ClipInfo], rng: random.Random, allowed_effects: tuple[str, ...]
) -> str:
    weights = _restrict_weights(_effect_weights(clips), allowed_effects)
    return _weighted_choice(rng, weights)


def _rank_focus_candidates(clips: list[ClipInfo]) -> list[int]:
    """Indices into `clips`, best focus candidate first: interest desc, then
    duration desc, then original position (stable sort gives this for free
    since ties keep their relative input order)."""
    indices = list(range(len(clips)))
    indices.sort(key=lambda i: (-clips[i].interest, -clips[i].duration_s))
    return indices


def _focus_target_count(target_duration_s: float) -> int:
    return 2 if target_duration_s > FOCUS_TWO_TARGET_DURATION_THRESHOLD_S else 1


def _build_focus_moments(
    clips: list[ClipInfo], rng: random.Random, target_duration_s: float
) -> tuple[FocusMoment, ...] | None:
    """Returns `None` when there aren't enough qualifying candidates to fill
    the target focus count — callers must treat that as "fall back to
    rolling", per the module docstring."""
    try:
        from .choreography import FocusMoment  # noqa: PLC0415
    except ImportError:
        log.warning(
            "carousel_director_choreography_schema_missing "
            "(FocusMoment not importable yet — falling back to rolling)",
            exc_info=True,
        )
        return None

    needed = _focus_target_count(target_duration_s)
    chosen: list[FocusMoment] = []
    for idx in _rank_focus_candidates(clips):
        if len(chosen) >= needed:
            break
        clip = clips[idx]
        jitter = rng.uniform(-HOLD_S_JITTER, HOLD_S_JITTER)
        hold_s = _clamp(HOLD_S_BASE + jitter, HOLD_S_MIN, HOLD_S_MAX)
        if clip.duration_s >= hold_s + FOCUS_DURATION_HEADROOM_S:
            chosen.append(FocusMoment(card_index=idx, hold_s=round(hold_s, 3)))

    if len(chosen) < needed:
        return None
    return tuple(chosen)


def direct_carousel_moment(
    clips: list[ClipInfo],
    *,
    seed: int,
    target_duration_s: float = DEFAULT_TARGET_DURATION_S,
    allowed_modes: tuple[str, ...] = DEFAULT_ALLOWED_MODES,
    allowed_effects: tuple[str, ...] = EFFECTS,
) -> CarouselMomentSpec:
    """Deterministically pick mode/effect/focus for one carousel moment. See
    the module docstring for the full rule set. Same `clips` + `seed` (+ the
    same `allowed_*`/`target_duration_s`) always returns an equal spec."""
    from .segment import CarouselMomentSpec  # noqa: PLC0415

    rng = random.Random(seed)

    mode = _choose_mode(clips, rng, allowed_modes)
    effect = _choose_effect(clips, rng, allowed_effects)

    focus_moments: tuple[FocusMoment, ...] = ()
    if mode == "focus":
        built = _build_focus_moments(clips, rng, target_duration_s)
        if built is None:
            log.info(
                "carousel_director_focus_unqualified n_clips=%d target_duration_s=%.2f "
                "falling back to rolling",
                len(clips),
                target_duration_s,
            )
            mode = "rolling"
        else:
            focus_moments = built

    return CarouselMomentSpec(
        effect=effect,
        clip_paths=tuple(c.path for c in clips),
        duration_s=target_duration_s,
        mode=mode,
        focus_moments=focus_moments,
        seed=seed,
    )


def diversify(specs_already_used: list[CarouselMomentSpec], **kwargs: Any) -> CarouselMomentSpec:
    """Like `direct_carousel_moment(**kwargs)`, but re-rolls with an
    incrementing seed (bounded by `max_attempts`, default
    `MAX_DIVERSIFY_ATTEMPTS`) until the resulting (mode, effect) pair isn't
    already present in `specs_already_used` — this is what keeps a
    multi-variant job's carousel moments from all landing on the same
    treatment. `kwargs` must include `clips` and `seed` (forwarded to
    `direct_carousel_moment` on every attempt, with `seed` incremented each
    time); any other `direct_carousel_moment` kwarg (`target_duration_s`,
    `allowed_modes`, `allowed_effects`) is forwarded unchanged.

    If every attempt collides (a small `allowed_modes`/`allowed_effects`
    intersection can exhaust every distinct pair quickly), returns the LAST
    attempt's spec rather than raising — a repeated treatment is a worse
    render than none, but "none" isn't on the table (never-block-a-render
    contract, same spirit as the rest of the carousel package)."""
    clips = kwargs.pop("clips")
    base_seed = kwargs.pop("seed")
    max_attempts = kwargs.pop("max_attempts", MAX_DIVERSIFY_ATTEMPTS)

    used_pairs = {(s.mode, s.effect) for s in specs_already_used}

    candidate = direct_carousel_moment(clips, seed=base_seed, **kwargs)
    for attempt in range(1, max_attempts):
        if (candidate.mode, candidate.effect) not in used_pairs:
            return candidate
        candidate = direct_carousel_moment(clips, seed=base_seed + attempt, **kwargs)
    return candidate
