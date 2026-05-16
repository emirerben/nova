"""Resolve slot durations and overlay windows for agentic vs classic recipes.

Agentic templates emit *relative* timings (fractions of slot duration / total
recipe duration) so that recipes adapt to the user's actual clip lengths
instead of being locked to the template video's analyzed length. Classic
templates keep using *absolute* seconds — the timings written by the human
template author are the ground truth and don't scale.

Two small pure functions centralise the dispatch:

  resolve_slot_duration(slot, is_agentic, user_total_dur_s)
    Decides each slot's per-job target duration in seconds.
    Classic                                  : slot["target_duration_s"]   (frozen)
    Agentic + pct present                    : pct * user_total_dur_s
    Agentic + pct absent (legacy cached row) : slot["target_duration_s"]   (fallback)

  resolve_overlay_window(ov, slot_actual_dur, is_agentic)
    Returns (start_s_rel, end_s_rel) relative to the slot's start.
    Classic                                  : (ov.start_s, ov.end_s)      (frozen)
    Agentic + pct present                    : (start_pct * slot, end_pct * slot)
    Agentic + pct absent (legacy cached row) : (ov.start_s, ov.end_s)      (fallback)

Both helpers preserve the D1 lazy-migration story: an `is_agentic=True`
template whose `recipe_cached` predates the pct schema renders correctly
through the inner-fallback branch. Reanalysing the template populates the pct
fields and the renderer flips to the proportional path automatically.
"""

from __future__ import annotations

import math

import structlog

log = structlog.get_logger()


def resolve_slot_duration(
    slot: dict,
    *,
    is_agentic: bool,
    user_total_dur_s: float,
) -> float:
    """Return the per-job target duration in seconds for this slot.

    `slot` is the recipe's slot dict. `user_total_dur_s` is the sum of all
    user clip durations for this job (only consulted in the agentic + pct
    branch).
    """
    target_s = float(slot.get("target_duration_s", 0.0) or 0.0)

    if not is_agentic:
        # Classic path — recipe's frozen duration wins. Untouched.
        return target_s

    pct_raw = slot.get("target_duration_pct")
    if pct_raw is None:
        # Lazy-migration fallback: legacy agentic recipe lacks pct fields.
        # Keep the frozen target_duration_s so the slot renders at all.
        return target_s

    try:
        pct = float(pct_raw)
    except (TypeError, ValueError):
        log.warning(
            "agentic_slot_pct_invalid",
            slot_position=slot.get("position"),
            pct_raw=pct_raw,
        )
        return target_s

    # F3 fix: NaN/Inf slip past simple range checks because all comparisons
    # involving NaN return False. Reject them explicitly before the range
    # check so a corrupted recipe cannot poison slot_target_dur with NaN.
    if math.isnan(pct) or math.isinf(pct):
        log.warning(
            "agentic_slot_pct_non_finite",
            slot_position=slot.get("position"),
            pct=pct,
        )
        return target_s

    if pct <= 0.0 or pct > 1.0:
        log.warning(
            "agentic_slot_pct_out_of_range",
            slot_position=slot.get("position"),
            pct=pct,
        )
        return target_s

    if user_total_dur_s <= 0.0:
        # 0-second user clip set: critical-gap flagged in the plan. Logged
        # loudly so the upstream guard catches it before the assembler does.
        log.error(
            "agentic_slot_zero_user_total",
            slot_position=slot.get("position"),
            user_total_dur_s=user_total_dur_s,
        )
        return target_s  # don't multiply pct by 0 — fall back to frozen value

    return pct * user_total_dur_s


def resolve_overlay_window(
    ov: dict,
    slot_actual_dur: float,
    *,
    is_agentic: bool,
) -> tuple[float, float]:
    """Return (start, end) of the overlay relative to the slot's start, in seconds.

    Caller adds the slot's cumulative_s to convert to absolute video time.
    """
    legacy_start = float(ov.get("start_s", 0.0) or 0.0)
    legacy_end = float(ov.get("end_s", slot_actual_dur) or slot_actual_dur)

    if not is_agentic:
        # Classic path — recipe's absolute timings win. Untouched.
        return legacy_start, legacy_end

    start_pct = ov.get("start_pct")
    end_pct = ov.get("end_pct")

    if start_pct is None or end_pct is None:
        # Lazy-migration fallback: legacy agentic overlay lacks pct fields.
        # Use the absolute timings the old prompt emitted.
        return legacy_start, legacy_end

    try:
        s_pct = float(start_pct)
        e_pct = float(end_pct)
    except (TypeError, ValueError):
        log.warning(
            "agentic_overlay_pct_invalid",
            start_pct=start_pct,
            end_pct=end_pct,
        )
        return legacy_start, legacy_end

    # F3 fix: NaN/Inf must be rejected explicitly. NaN comparisons return
    # False, so `not (0.0 <= NaN < ...)` would happen to fall back here,
    # but Inf would satisfy `0.0 <= inf` and fail the `< e_pct` check —
    # making the behavior fragile to operator order. Be explicit.
    if math.isnan(s_pct) or math.isnan(e_pct) or math.isinf(s_pct) or math.isinf(e_pct):
        log.warning(
            "agentic_overlay_pct_non_finite",
            start_pct=s_pct,
            end_pct=e_pct,
        )
        return legacy_start, legacy_end

    if not (0.0 <= s_pct < e_pct <= 1.0):
        log.warning(
            "agentic_overlay_pct_out_of_range",
            start_pct=s_pct,
            end_pct=e_pct,
        )
        return legacy_start, legacy_end

    return s_pct * slot_actual_dur, e_pct * slot_actual_dur
