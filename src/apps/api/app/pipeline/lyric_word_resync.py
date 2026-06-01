"""Post-snap re-anchor for karaoke + per-word-pop lyric overlays.

Audio-visual sync model
-----------------------

A karaoke or per-word-pop overlay's `start_s` / `end_s` are SLOT-RELATIVE
floats computed at injection time against the recipe's pre-snap slot
durations. Beat-snap then re-times each slot's duration to land cleanly on a
beat boundary — shifts of up to one beat-interval (~250ms on a 2.4 BPS
track) are normal. The slot's cumulative position in the audio mix shifts
with it, which means the overlay's absolute audio anchor drifts even though
its slot-relative position is unchanged. The audio file itself is never
touched — only the slot timeline is — so the result is per-word highlights
running early or late against the actual vocal.

Fix
---

The lyric injectors (`_inject_karaoke`, `_inject_per_word_pop`) stamp the
line's SECTION-relative anchor (`section_anchor_s`, `section_end_anchor_s`)
onto every overlay they produce. Section coordinates are independent of slot
windowing — they're just "seconds after `best_start_s` in the song." This
module reads those stamps and rewrites the slot-relative `start_s` /
`end_s` so the overlay renders at the correct post-snap audio position.

Strict isolation contract
-------------------------

- Line-style overlays (`effect="lyric-line"`) do NOT carry section anchor
  stamps and are not touched by this module — Line behavior is byte-
  identical.
- Non-lyric overlays (text_overlay, font-cycle, scale-up, …) are not
  touched — they don't carry the stamps.
- The kill-switch `settings.lyric_word_resync_enabled` (defaults to True)
  reverts to pre-resync behavior byte-identically when flipped to False.
  Use ONLY as an emergency rollback if the resync ships a regression.

Out of scope (deferred)
-----------------------

**Pop-up sub-frame collision.** When two pop-up stages land within one
rendered frame (~33ms at 30fps), the viewer sees a single "double pop"
animation instead of two distinct events. Fixing this requires collapsing
adjacent OVERLAY DICTS within a slot (not word_timings within a single
overlay — pop-up emits one overlay per stage). That's a different surface
than the resync pass and is tracked as a separate follow-up. Karaoke does
NOT have this issue: its sweep animation interpolates between word
boundaries, so sub-frame collisions render as a smooth color blend rather
than a stuttering re-pop.
"""

from __future__ import annotations

import math

import structlog

log = structlog.get_logger()

# Overlay effects that carry per-word state and are eligible for re-anchor.
# Keep this set explicit: the resync pass is opt-in by effect string, so any
# future style that needs sync correction must register here.
_RESYNC_EFFECTS = frozenset({"karaoke-line", "pop-in"})

# Minimum slot-relative duration we'll let an overlay shrink to. Any
# rewritten window shorter than this is dropped rather than rendered as a
# 1-frame ghost. Matches `_MIN_OVERLAY_DURATION_S` in lyric_injector.py.
_MIN_OVERLAY_DURATION_S: float = 0.10


def resync_overlay_against_snapped_slot(
    overlay: dict,
    slot_post_snap_section_start_s: float,
    slot_post_snap_duration_s: float,
) -> bool:
    """Rewrite `overlay["start_s"]` / `overlay["end_s"]` so the rendered
    moment matches `overlay["section_anchor_s"]` against the post-snap slot.

    Mutates `overlay` in place. Returns True if rewrite happened, False if
    skipped (no stamp, wrong effect, kill-switch disabled, or rewrite would
    produce an empty window).

    The overlay's `word_timings` are NOT rewritten — they're relative to the
    overlay's OWN start, so as long as `start_s` resolves to the correct
    audio time, per-word offsets stay aligned with the vocal automatically.
    """
    if overlay.get("effect") not in _RESYNC_EFFECTS:
        return False
    anchor = overlay.get("section_anchor_s")
    if not isinstance(anchor, int | float) or not math.isfinite(float(anchor)):
        return False
    end_anchor = overlay.get("section_end_anchor_s")
    if not isinstance(end_anchor, int | float) or not math.isfinite(float(end_anchor)):
        return False

    new_start = float(anchor) - slot_post_snap_section_start_s
    new_end = float(end_anchor) - slot_post_snap_section_start_s

    # Clamp to slot. If the new window falls entirely outside the post-snap
    # slot — beat-snap shifted it past the slot boundary — drop the rewrite
    # and log so the failure is visible in the pipeline trace. The original
    # pre-snap window stays in place; downstream finalization (Dedup 1 / 2
    # in `_collect_absolute_overlays`) handles the inevitable overlap on its
    # own terms.
    if new_end <= 0 or new_start >= slot_post_snap_duration_s:
        log.warning(
            "lyric_word_resync_out_of_slot",
            effect=overlay.get("effect"),
            section_anchor_s=anchor,
            section_end_anchor_s=end_anchor,
            slot_start=slot_post_snap_section_start_s,
            slot_dur=slot_post_snap_duration_s,
        )
        return False

    new_start = max(0.0, new_start)
    new_end = min(slot_post_snap_duration_s, new_end)
    if new_end - new_start < _MIN_OVERLAY_DURATION_S:
        # Re-anchor would shrink window below render floor. Skip rewrite;
        # the original window may render at a slightly drifted position but
        # at least it WILL render. Logged for empirical follow-up.
        log.info(
            "lyric_word_resync_skipped_min_duration",
            effect=overlay.get("effect"),
            new_start=new_start,
            new_end=new_end,
        )
        return False

    overlay["start_s"] = round(new_start, 3)
    overlay["end_s"] = round(new_end, 3)
    return True


def resync_slot_overlays(
    slot_overlays: list[dict],
    slot_post_snap_section_start_s: float,
    slot_post_snap_duration_s: float,
) -> int:
    """Walk one slot's overlays, re-anchor each eligible overlay in place.

    Returns the count of overlays rewritten. Zero-cost for slots with no
    karaoke / pop overlays (the gate inside `resync_overlay_against_snapped_slot`
    is a frozenset lookup).
    """
    rewritten = 0
    for o in slot_overlays:
        if not isinstance(o, dict):
            continue
        if resync_overlay_against_snapped_slot(
            o, slot_post_snap_section_start_s, slot_post_snap_duration_s
        ):
            rewritten += 1
    return rewritten


# NOTE: sub-frame dedupe deliberately omitted from this module — see the
# "Out of scope (deferred)" section in the module docstring above. Karaoke
# does not need it (sweep animation handles sub-frame events); pop-up needs
# a multi-overlay collapse (different shape than per-word collapse) that
# belongs in a separate pass.
