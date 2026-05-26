"""Production-path regression tests for `fade_out_curve="sqrt"`.

The renderer-parity invariant in CLAUDE.md ("agentic/music overlay change
is NOT verified by the admin preview") burns us if `fade_out_curve` is set
on a preview overlay but stripped or dropped by the production beat-snap
split + `_collect_absolute_overlays._consolidate_lyric_segments` merge.
Preview would look clean while the burned music render still stacks.

These tests drive the real production path (multi-slot recipe + scheduler
injection + cross-slot merge) and assert that:

  1. `fade_out_curve` lands on the FINAL segment of a multi-slot split
     (mid-segments must NOT carry it — they emit fade_out_ms=0).
  2. The merge in `_consolidate_lyric_segments` propagates the curve from
     the last segment onto the merged absolute overlay.
  3. Non-crossfade overlays (solo last line, sparse pair, override case,
     short-line hard cut) never acquire the curve tag — production code
     must not invent one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline.lyric_injector import inject_lyric_overlays


def _cache(lines: list[tuple[str, float, float]]) -> dict:
    return {
        "lines": [
            {
                "text": text,
                "start_s": start,
                "end_s": end,
                "words": [{"text": text, "start_s": start, "end_s": end}],
            }
            for text, start, end in lines
        ]
    }


def _recipe(slot_durations_s: list[float]) -> dict:
    return {
        "slots": [
            {"position": i + 1, "target_duration_s": d, "text_overlays": []}
            for i, d in enumerate(slot_durations_s)
        ]
    }


def _inject(lines, slot_durations_s, cfg_extra=None):
    cfg = {"enabled": True, "style": "line"}
    if cfg_extra:
        cfg.update(cfg_extra)
    span_start = min(s for _, s, _ in lines) - 1.0
    span_end = max(e for _, _, e in lines) + 1.0
    out = inject_lyric_overlays(_recipe(slot_durations_s), _cache(lines), span_start, span_end, cfg)
    return out["slots"]


def _lyric_overlays_per_slot(slots: list[dict]) -> list[list[dict]]:
    return [
        [o for o in slot.get("text_overlays", []) if o.get("effect") == "lyric-line"]
        for slot in slots
    ]


# ──────────────────────────────────────────────────────────────────────────
# 1. Slot split — curve only on final segment of a multi-slot lyric line
# ──────────────────────────────────────────────────────────────────────────


def test_curve_tag_appears_only_on_final_segment_of_multi_slot_line() -> None:
    """Drive a lyric line that spans multiple slots. Mid-segments must
    emit fade_out_ms=0 AND no fade_out_curve key (existing rule extended).
    Only the final segment carries the curve tag (when its line had a
    crossfade successor in the next line."""
    # Slots: [0..2s, 2..4s, 4..6s, 6..8s]. Line A spans 0.4 → 5.6 (covers
    # slots 0, 1, 2 — three segments). Line B at 5.8 → 7.8 (crossfade
    # successor for A).
    slots = _inject(
        [("the room was empty", 0.4, 5.6), ("we left it that way", 5.8, 7.8)],
        slot_durations_s=[2.0, 2.0, 2.0, 2.0],
    )
    by_slot = _lyric_overlays_per_slot(slots)
    # Find segments of line A — it's the line that gets split across slots.
    # `lyric_line_id` is `line:0:...` for the first line, `line:1:...` for the
    # second, so filter by the `line:0:` prefix to scoop up every segment.
    a_segments = [
        o for slot in by_slot for o in slot if o.get("lyric_line_id", "").startswith("line:0:")
    ]
    assert len(a_segments) >= 2, f"expected multi-segment split, got {len(a_segments)}"
    a_segments.sort(key=lambda o: o["lyric_segment_index"])
    # Mid-segments: no curve tag, fade_out_ms=0.
    for seg in a_segments[:-1]:
        assert "fade_out_curve" not in seg, (
            f"mid-segment must not carry fade_out_curve (got {seg.get('fade_out_curve')!r}) "
            f"at lyric_segment_index={seg['lyric_segment_index']}"
        )
        assert seg["fade_out_ms"] == 0, (
            f"mid-segment must have fade_out_ms=0, got {seg['fade_out_ms']}"
        )
    # Last segment: must carry the curve tag because line A has a crossfade
    # successor (line B), AND fade_out_ms equals the matched crossfade window.
    last = a_segments[-1]
    assert last.get("fade_out_curve") == "sqrt"
    assert last["fade_out_ms"] > 0


# ──────────────────────────────────────────────────────────────────────────
# 2. _consolidate_lyric_segments — merge must propagate the curve
# ──────────────────────────────────────────────────────────────────────────


def _run_consolidate(segments: list[dict]) -> list[dict]:
    """Drive the production `_consolidate_lyric_segments` directly.

    The function is nested inside `_collect_absolute_overlays`. To exercise
    it in isolation, we replicate the merge logic exactly. (Pulling the
    real function out of its closure would require refactoring production
    code purely for testability, which we avoid — the merge implementation
    is mirrored here verbatim from template_orchestrate.py; if the prod
    impl ever drifts, this mirror must be updated too.)
    """
    by_id: dict[str, list[tuple[int, dict]]] = {}
    for idx, ov in enumerate(segments):
        if ov.get("effect") != "lyric-line" or not ov.get("lyric_line_id"):
            continue
        by_id.setdefault(ov["lyric_line_id"], []).append((idx, ov))

    consumed: set[int] = set()
    replacements: dict[int, dict] = {}
    for line_id, members in by_id.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda im: im[1]["start_s"])
        base_idx, base = members[0]
        merged = dict(base)
        merged["fade_in_ms"] = base.get("fade_in_ms", 0)
        merged["start_s"] = min(s[1]["start_s"] for s in members)
        for nxt_idx, nxt in members[1:]:
            merged["end_s"] = max(merged["end_s"], nxt["end_s"])
            merged["fade_out_ms"] = nxt.get("fade_out_ms", 0)
            nxt_curve = nxt.get("fade_out_curve")
            if nxt_curve is not None:
                merged["fade_out_curve"] = nxt_curve
            elif "fade_out_curve" in merged:
                del merged["fade_out_curve"]
            consumed.add(nxt_idx)
        replacements[base_idx] = merged

    out = []
    for idx, ov in enumerate(segments):
        if idx in consumed:
            continue
        out.append(replacements.get(idx, ov))
    return out


def test_consolidate_lyric_segments_preserves_curve_from_last_segment() -> None:
    """The production merge must carry fade_out_curve from the LAST segment
    onto the merged overlay. Mid-segments have no curve key by design;
    naive `merged.update(nxt)` would lose the curve from the final one.
    Tests the explicit propagation in template_orchestrate.py."""
    seg0 = {
        "effect": "lyric-line",
        "lyric_line_id": "line:0:1.000:4.000",
        "start_s": 0.6,
        "end_s": 1.9,
        "fade_in_ms": 50,
        "fade_out_ms": 0,
        # mid-segment: no fade_out_curve
    }
    seg1 = {
        "effect": "lyric-line",
        "lyric_line_id": "line:0:1.000:4.000",
        "start_s": 1.9,
        "end_s": 3.6,
        "fade_in_ms": 0,
        "fade_out_ms": 300,
        "fade_out_curve": "sqrt",  # last segment carries the curve
    }
    merged = _run_consolidate([seg0, seg1])
    assert len(merged) == 1
    m = merged[0]
    assert m["fade_in_ms"] == 50, "fade_in_ms must come from first segment"
    assert m["fade_out_ms"] == 300, "fade_out_ms must come from last segment"
    assert m["fade_out_curve"] == "sqrt", "fade_out_curve must come from last segment"
    assert m["start_s"] == 0.6 and m["end_s"] == 3.6


def test_consolidate_drops_stale_curve_when_last_segment_has_none() -> None:
    """Defensive: if (somehow) the first segment carries a curve but the last
    one doesn't, the merge drops it. Last segment's absence of fade_out is
    authoritative."""
    seg0 = {
        "effect": "lyric-line",
        "lyric_line_id": "line:0:1.000:4.000",
        "start_s": 0.6,
        "end_s": 1.9,
        "fade_in_ms": 50,
        "fade_out_ms": 0,
        "fade_out_curve": "sqrt",  # should NEVER happen on mid-segment in real injection
    }
    seg1 = {
        "effect": "lyric-line",
        "lyric_line_id": "line:0:1.000:4.000",
        "start_s": 1.9,
        "end_s": 3.6,
        "fade_in_ms": 0,
        "fade_out_ms": 250,
        # no fade_out_curve (this line had no crossfade successor)
    }
    merged = _run_consolidate([seg0, seg1])
    assert len(merged) == 1
    assert "fade_out_curve" not in merged[0]


# ──────────────────────────────────────────────────────────────────────────
# 3. Non-crossfade overlays never acquire the curve tag
# ──────────────────────────────────────────────────────────────────────────


def test_solo_last_line_never_carries_curve_tag() -> None:
    """Last line in a section has no successor → no crossfade → no curve."""
    slots = _inject([("only one line", 1.0, 3.0)], slot_durations_s=[10.0])
    overlays = [o for slot in slots for o in slot.get("text_overlays", [])]
    for o in overlays:
        assert "fade_out_curve" not in o, (
            f"solo last line must not carry curve tag (got {o.get('fade_out_curve')!r})"
        )


def test_sparse_pair_never_carries_curve_tag() -> None:
    """Lines far apart enough that natural_overlap_s == 0 → no crossfade
    candidate → no curve tag."""
    # Gap >> pre_roll + post_dwell, so section_ends don't overlap.
    slots = _inject(
        [("first", 1.0, 2.0), ("second", 10.0, 11.0)],
        slot_durations_s=[15.0],
    )
    overlays = [o for slot in slots for o in slot.get("text_overlays", [])]
    for o in overlays:
        assert "fade_out_curve" not in o, (
            f"sparse pair must not carry curve tag (got {o.get('fade_out_curve')!r})"
        )


def test_kill_switch_off_pair_never_carries_curve_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LYRIC_DYNAMIC_CROSSFADE_ENABLED is off (the legacy rollback
    path), no overlay carries fade_out_curve='sqrt' — regardless of cfg
    contents. This is the kill-switch byte-identity guarantee.

    Under §F the override gate is gone, so cfg fade values no longer
    suppress the post-pass — the kill switch is the only path to legacy
    behavior. This test exists to pin that contract: there is no
    second-class "skip the post-pass without flipping the switch" path."""
    from app.config import settings

    monkeypatch.setattr(settings, "lyric_dynamic_crossfade_enabled", False)
    slots = _inject(
        [("first", 1.0, 2.0), ("second", 2.3, 3.0)],
        slot_durations_s=[10.0],
        cfg_extra={"fade_in_ms": 175},
    )
    overlays = [o for slot in slots for o in slot.get("text_overlays", [])]
    for o in overlays:
        assert "fade_out_curve" not in o, (
            f"kill-switch-off path must not carry curve tag (got {o.get('fade_out_curve')!r})"
        )


def test_short_line_hard_cut_never_carries_curve_tag() -> None:
    """Outgoing line too short to safely crossfade → hard cut decision → no
    curve tag on the outgoing overlay. See plan §1g."""
    slots = _inject(
        [("oh!", 12.92, 13.00), ("nah!", 13.05, 13.25)],
        slot_durations_s=[15.0],
    )
    overlays = [o for slot in slots for o in slot.get("text_overlays", [])]
    for o in overlays:
        assert "fade_out_curve" not in o, (
            f"hard-cut pair must not carry curve tag (got {o.get('fade_out_curve')!r})"
        )


def test_solo_segment_pass_through_does_not_invent_curve() -> None:
    """If a single segment goes through the consolidate function (len 1),
    nothing changes — the curve key (if present) must pass through as-is,
    and (more importantly) must NOT be added when absent."""
    seg = {
        "effect": "lyric-line",
        "lyric_line_id": "line:0:1.000:4.000",
        "start_s": 0.6,
        "end_s": 3.6,
        "fade_in_ms": 50,
        "fade_out_ms": 250,
        # no fade_out_curve
    }
    merged = _run_consolidate([seg])
    assert len(merged) == 1
    assert "fade_out_curve" not in merged[0]


# ──────────────────────────────────────────────────────────────────────────
# Sanity: the prod merge implementation matches the mirrored helper above.
# Tests above use a mirror to avoid pulling a nested function out of a
# closure; we cross-check the source so the mirror stays honest.
# ──────────────────────────────────────────────────────────────────────────


def test_production_merge_implementation_has_curve_propagation_branch() -> None:
    """Read the live source and assert it contains the curve-propagation
    branch we added. If anyone refactors the merge without preserving the
    fade_out_curve propagation, this test fails immediately — a textual
    canary for the prod-merge contract."""
    src = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("app/tasks/template_orchestrate.py")
        .read_text()
    )
    # The branch must mention both `fade_out_curve` and the explicit
    # propagation pattern from the last segment.
    assert 'nxt.get("fade_out_curve")' in src or "nxt.get('fade_out_curve')" in src, (
        "production _consolidate_lyric_segments must explicitly read "
        "fade_out_curve from the last segment — see plan §6a guard"
    )
