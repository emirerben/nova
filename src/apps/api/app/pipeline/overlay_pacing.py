"""Slot overlay pacing — legibility floor + redistribution within a fixed slot.

Pure dict transformations on one slot's ``text_overlays``. Shared by the
Layer-2 generation bridge (``template_text_extraction._apply_legibility_pacing``,
run after the timing-faithful merge) and the admin overlay editor
(``routes/admin.py``) so there is exactly ONE implementation of "how a slot's
reveal timing is made readable and laid out".

The three building blocks, smallest to largest:

- ``_resequence_slot_overlays`` — lay phrase blocks end-to-end so only one
  phrase is on screen at a time (ripple-forward only; preserves gaps).
- ``normalize_slot_overlay_pacing`` — the canonical pass: enforce a per-word
  legibility floor (expanding too-fast reveals), resequence, then — if the
  floored timeline overflows the slot — reclaim inter-phrase dead air and shrink
  the slow phrases' SLACK (time above their own floor) proportionally so it fits.
  This is what fixes "the work to get there." (5 words in 431 ms → readable)
  and "and good timing so..." (a 25 ms singleton → ≥ a visible window) on prod
  template 89cde014.
- ``_fit_slot_overlays_to_duration`` — thin wrapper kept for the admin
  "fit to duration" toggle; delegates to ``normalize_slot_overlay_pacing``.

Why expand-then-redistribute (rather than just compressing): nothing in the old
pipeline ever LENGTHENED a phrase whose words were below a readable floor — the
de-clusterer (`text_overlay_v2._despace_word_starts`) and the fit pass both only
ever shrank or pushed. So a phrase the OCR/transcript timed faster than the eye
can read stayed unreadable. We floor every word first; if that overflows the
slot, the deficit is funded from the SLACK of the slow phrases (high-slack
phrases lose proportionally more) and from inter-phrase dead air — never by
pushing a word back below the floor. That is exactly the user intent: "make the
slowest one faster to fund the fastest", slot total fixed.
"""

from __future__ import annotations

from app.pipeline.text_reveal import butt_join_cumulative_phrases, group_phrase_index_blocks

# On-screen minimum for one revealed word in a cumulative reveal. Sits just
# above the generation-time de-cluster step (`_MIN_WORD_REVEAL_STEP_S=0.30`) so
# it strictly dominates without a jarring jump, and below the standalone floor
# (a single revealed word carries less new information than a whole overlay).
# Sub-300 ms reads as a flicker — the exact "impossible to read" complaint.
MIN_PER_WORD_S = 0.35
# On-screen minimum for a standalone (non-cumulative) overlay. Mirrors the
# Layer-2 passthrough floor `_MIN_OVERLAY_DURATION_S=0.5`.
MIN_SINGLETON_OVERLAY_S = 0.5

# Slot reflow: adjacency tolerance. Two overlays whose windows touch
# (start == prev_end, as cumulative reveal stages do) are NOT overlapping; only
# a genuine overlap (start strictly before prev_end by more than this epsilon)
# triggers a ripple.
_REFLOW_EPS = 1e-6
_EPS = 1e-9


def _is_pct_timed(o: dict) -> bool:
    """True if the overlay is agentic pct-timed (render-time authoritative).

    Mirrors ``agentic_timing.resolve_overlay_window``: when BOTH ``start_pct``
    and ``end_pct`` are present, pct wins and the seconds fields are ignored at
    render time. Such overlays are a no-op for every seconds-math pass here and
    are passed through untouched.
    """
    return o.get("start_pct") is not None and o.get("end_pct") is not None


def _eff_start(o: dict) -> float:
    """Effective start: override wins over base (matches the render path)."""
    v = o.get("start_s_override")
    return float((v if v is not None else o.get("start_s")) or 0.0)


def _eff_end(o: dict) -> float:
    """Effective end: override wins over base (matches the render path)."""
    v = o.get("end_s_override")
    return float((v if v is not None else o.get("end_s")) or 0.0)


def _set_overlay_window(o: dict, start_s: float, end_s: float) -> None:
    """Set an overlay's effective window to ``[start_s, end_s]`` in place.

    Writes the base ``start_s``/``end_s`` always, and the override pair too if
    it was present — so the EFFECTIVE window (override-first, per ``_eff_*``)
    lands exactly on the requested values whichever representation the overlay
    carried. ``font_cycle_accel_at_s`` is clamped to stay inside the new window
    (matching the accel contract in ``_shift_overlay``).
    """
    s, e = round(start_s, 3), round(end_s, 3)
    o["start_s"] = s
    o["end_s"] = e
    if o.get("start_s_override") is not None:
        o["start_s_override"] = s
    if o.get("end_s_override") is not None:
        o["end_s_override"] = e
    accel = o.get("font_cycle_accel_at_s")
    if accel is not None:
        o["font_cycle_accel_at_s"] = round(max(s, min(float(accel), e - 1e-3)), 3)


def _shift_overlay(o: dict, delta: float) -> None:
    """Move an overlay forward in time by ``delta`` seconds in place.

    Shifts base and override timings together so the effective window moves
    correctly whichever pair is set, and carries ``font_cycle_accel_at_s`` along
    (clamped to stay inside the new window, matching Dedup 2's accel contract).
    """
    for base, ovr in (("start_s", "start_s_override"), ("end_s", "end_s_override")):
        if o.get(base) is not None:
            o[base] = round(float(o[base]) + delta, 3)
        if o.get(ovr) is not None:
            o[ovr] = round(float(o[ovr]) + delta, 3)
    accel = o.get("font_cycle_accel_at_s")
    if accel is not None:
        lo, hi = _eff_start(o), _eff_end(o)
        o["font_cycle_accel_at_s"] = round(max(lo, min(float(accel) + delta, hi - 1e-3)), 3)


def _slot_target_duration(slot: dict) -> float | None:
    """Coerce a slot's ``target_duration_s`` to float, or None if unusable."""
    raw = slot.get("target_duration_s") if isinstance(slot, dict) else None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _resequence_slot_overlays(
    overlays: list[dict], *, target_duration_s: float | None
) -> tuple[list[dict], dict]:
    """Lay phrase blocks end-to-end so no two overlays in the slot overlap.

    Groups overlays into phrase blocks (``group_phrase_index_blocks``) and
    walks them in array order — the authored reading order — ripple-forwarding
    each whole block so it starts no earlier than the previous block ended. Only
    ever moves blocks LATER (never earlier, never compressed); a block already
    clear of the previous one (a gap) is left untouched, so intentional pauses
    survive. Each phrase keeps its internal per-word pacing — only the block as a
    unit moves, which resolves interleaved phrases instead of fragmenting them.

    This is slot-wide and position-agnostic: a multi-lane (e.g. two-line) layout
    collapses to a single sequential read where only one phrase is on screen at
    a time. Phrase blocks carrying agentic ``start_pct``/``end_pct`` timing are
    skipped (a seconds-shift is a render no-op) and don't advance the cursor.

    Returns ``(new_overlays, warnings)`` where ``warnings`` reports how many
    overlays were pushed so their start exceeds ``target_duration_s`` (the
    renderer clamps end_s to the clip, so these render truncated, not dropped).
    """
    out = [dict(o) if isinstance(o, dict) else o for o in overlays]
    pushed_past_target = 0
    cursor: float | None = None
    for block in group_phrase_index_blocks(out):
        members = [out[i] for i in block if isinstance(out[i], dict)]
        if not members:
            continue
        if any(m.get("start_pct") is not None or m.get("end_pct") is not None for m in members):
            continue  # agentic-relative timing — leave as-is, don't advance cursor
        block_start = min(_eff_start(m) for m in members)
        block_end = max(_eff_end(m) for m in members)
        if cursor is not None and block_start < cursor - _REFLOW_EPS:
            delta = cursor - block_start
            for m in members:
                _shift_overlay(m, delta)
            block_start += delta
            block_end += delta
        cursor = block_end
        if target_duration_s is not None and block_start > target_duration_s:
            pushed_past_target += len(members)
    # Close any intra-phrase gaps so a cumulative reveal never blanks out
    # between words (the prod 89cde014 glitch). This only extends a member's
    # end to the next member's start within the SAME phrase block — it never
    # touches inter-phrase gaps, so the intentional pauses the ripple preserved
    # above survive.
    butt_join_cumulative_phrases(out)
    return out, {"overlays_pushed_past_target": pushed_past_target}


def _expand_block_legibility(
    out: list[dict], *, min_per_word_s: float, min_singleton_s: float
) -> dict:
    """Floor each reveal stage / singleton to a readable on-screen minimum.

    Walks each phrase block in array order:

    - Singleton (one-member block) → its span is floored to ``min_singleton_s``.
    - Cumulative reveal block → each stage's "newest-word" window (start_k →
      start_{k+1}, i.e. its own span) is floored to ``min_per_word_s``. Stages
      are only pushed LATER when the previous floored stage would otherwise
      overlap — a stage already clear of its predecessor keeps its start, so
      intentional holds survive (and the subsequent resequence's butt-join
      re-extends a preserved gap into the prior stage's dwell). Crammed clusters
      get spread; well-paced reveals are untouched (idempotent).

    Mutates ``out`` in place. pct-timed blocks are skipped entirely. Returns a
    ``{"stages_expanded", "singletons_expanded"}`` count.
    """
    warnings = {"stages_expanded": 0, "singletons_expanded": 0}
    for block in group_phrase_index_blocks(out):
        members = [out[i] for i in block if isinstance(out[i], dict)]
        if not members or any(_is_pct_timed(m) for m in members):
            continue
        if len(members) == 1:
            m = members[0]
            s, e = _eff_start(m), _eff_end(m)
            if (e - s) < min_singleton_s - _EPS:
                _set_overlay_window(m, s, s + min_singleton_s)
                warnings["singletons_expanded"] += 1
            continue
        prev_end: float | None = None
        for m in members:
            s = _eff_start(m)
            span = max(_eff_end(m) - _eff_start(m), min_per_word_s)
            if prev_end is not None and s < prev_end - _EPS:
                s = prev_end
            new_end = s + span
            if abs(s - _eff_start(m)) > _EPS or abs(new_end - _eff_end(m)) > _EPS:
                warnings["stages_expanded"] += 1
            _set_overlay_window(m, s, new_end)
            prev_end = new_end
    return warnings


def _compress_to_fit(
    seq: list[dict], *, slot_duration_s: float, min_per_word_s: float, min_singleton_s: float
) -> dict:
    """If the resequenced seconds timeline overflows ``slot_duration_s``, butt
    every stage edge-to-edge from the origin and — if it still overflows —
    shrink each stage's SLACK above its own floor proportionally so the total
    equals the slot duration and every stage stays >= its floor.

    This is the redistribution the user asked for: the deficit created by
    expanding too-fast reveals is funded from the slack of the slow phrases
    (the high-slack phrases lose proportionally more), not from the words that
    are already at the readable floor. Inter-phrase dead air is reclaimed first
    (the edge-to-edge butt). If even the all-floor timeline overflows, stages
    are pinned to their floor and ``slot_overflow_uncompressible`` is flagged
    (the renderer clamps the tail to the clip — truncated, not unreadable).

    Mutates ``seq`` in place. Returns ``{"slot_overflow_uncompressible": 0|1}``.
    No-op (gaps preserved) when the timeline already fits.
    """
    warnings = {"slot_overflow_uncompressible": 0}
    floor_by_id: dict[int, float] = {}
    for block in group_phrase_index_blocks(seq):
        members = [seq[i] for i in block if isinstance(seq[i], dict)]
        if not members or any(_is_pct_timed(m) for m in members):
            continue
        fl = min_singleton_s if len(members) == 1 else min_per_word_s
        for m in members:
            floor_by_id[id(m)] = fl
    timed = [o for o in seq if isinstance(o, dict) and id(o) in floor_by_id]
    if not timed:
        return warnings
    timed.sort(key=lambda o: (_eff_start(o), _eff_end(o)))
    origin = _eff_start(timed[0])
    end = max(_eff_end(o) for o in timed)
    avail = slot_duration_s - origin
    if end - origin <= slot_duration_s + _EPS or avail <= 0:
        return warnings  # already fits — leave gaps/pacing untouched

    floors = [floor_by_id[id(o)] for o in timed]
    durs = [max(_eff_end(o) - _eff_start(o), f) for o, f in zip(timed, floors, strict=True)]
    total = sum(durs)
    if total > avail + _EPS:
        excess = total - avail
        slack = sum(d - f for d, f in zip(durs, floors, strict=True))
        if slack >= excess - _EPS and slack > 0:
            durs = [d - (d - f) / slack * excess for d, f in zip(durs, floors, strict=True)]
        else:
            durs = list(floors)  # cannot fit even at the floor
            warnings["slot_overflow_uncompressible"] = 1
    # Re-lay edge-to-edge from the origin in reading order.
    t = origin
    for o, d in zip(timed, durs, strict=True):
        _set_overlay_window(o, t, t + d)
        t += d
    return warnings


def normalize_slot_overlay_pacing(
    overlays: list[dict],
    *,
    slot_duration_s: float | None,
    compress: bool = True,
    min_per_word_s: float = MIN_PER_WORD_S,
    min_singleton_s: float = MIN_SINGLETON_OVERLAY_S,
) -> tuple[list[dict], dict]:
    """Canonical pacing pass: enforce a legibility floor, then redistribute
    within the slot's FIXED duration.

    Steps:
      1. Expand every reveal stage / singleton below the floor (gap-preserving;
         see ``_expand_block_legibility``).
      2. Re-sequence phrase blocks end-to-end (``_resequence_slot_overlays``).
      3. If ``compress`` and ``slot_duration_s`` is usable and the floored
         timeline overflows it, ``_compress_to_fit``: reclaim inter-phrase dead
         air and shrink the slow phrases' slack so the total fits, keeping every
         stage at/above its floor. ``compress=False`` (the retime path) stops
         after step 2 — editing one phrase never speeds up its neighbours.

    pct-timed overlays are passed through untouched throughout. With no usable
    ``slot_duration_s`` this degrades to expand + resequence (no compression).

    Returns ``(new_overlays, warnings)``. ``warnings`` carries
    ``overlays_pushed_past_target`` (so admin callers reading that key keep
    working) plus ``stages_expanded`` / ``singletons_expanded`` /
    ``slot_overflow_uncompressible``.
    """
    out = [dict(o) if isinstance(o, dict) else o for o in overlays]
    warnings = {
        "overlays_pushed_past_target": 0,
        "stages_expanded": 0,
        "singletons_expanded": 0,
        "slot_overflow_uncompressible": 0,
    }
    if not any(isinstance(o, dict) and not _is_pct_timed(o) for o in out):
        return out, warnings

    warnings.update(
        _expand_block_legibility(
            out, min_per_word_s=min_per_word_s, min_singleton_s=min_singleton_s
        )
    )

    seq, rwarns = _resequence_slot_overlays(out, target_duration_s=slot_duration_s)
    warnings["overlays_pushed_past_target"] = rwarns["overlays_pushed_past_target"]

    if compress and slot_duration_s is not None and slot_duration_s > 0:
        cwarns = _compress_to_fit(
            seq,
            slot_duration_s=slot_duration_s,
            min_per_word_s=min_per_word_s,
            min_singleton_s=min_singleton_s,
        )
        warnings["slot_overflow_uncompressible"] = cwarns["slot_overflow_uncompressible"]
        # Recount overflow after compression (no-op shift; recomputes warnings).
        seq, rwarns = _resequence_slot_overlays(seq, target_duration_s=slot_duration_s)
        warnings["overlays_pushed_past_target"] = rwarns["overlays_pushed_past_target"]
    return seq, warnings


def _fit_slot_overlays_to_duration(
    overlays: list[dict], *, target_duration_s: float | None
) -> tuple[list[dict], dict]:
    """Admin "fit to duration" pass — re-sequence phrase blocks end-to-end and
    fit them into ``target_duration_s``, enforcing the per-word legibility floor
    (expanding too-fast reveals, then reclaiming dead air + slow-phrase slack to
    fit — see ``_compress_to_fit``).

    Thin wrapper over ``normalize_slot_overlay_pacing`` so the admin editor and
    the generation bridge share one implementation. No phrase is reworded,
    reordered, or dropped.
    """
    return normalize_slot_overlay_pacing(overlays, slot_duration_s=target_duration_s)
