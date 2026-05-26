import pytest

from app.pipeline.lyric_injector import inject_lyric_overlays


def _recipe() -> dict:
    return {"slots": [{"position": 1, "target_duration_s": 10.0, "text_overlays": []}]}


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


def _overlays(lines: list[tuple[str, float, float]], threshold_ms: int = 500) -> list[dict]:
    out = inject_lyric_overlays(
        _recipe(),
        _cache(lines),
        0.0,
        10.0,
        {
            "enabled": True,
            "style": "line",
            "hold_to_next_threshold_ms": threshold_ms,
        },
    )
    return out["slots"][0]["text_overlays"]


def test_hold_to_next_threshold_is_deprecated_noop_for_current_fade_out() -> None:
    # Numeric assertions on fade durations pinned the pre-fix solo-default
    # geometry. After the dynamic-crossfade post-pass (plan §1), back-to-back
    # lines get MATCHED durations: outgoing.fade_out_ms == incoming.fade_in_ms
    # == crossfade window. The load-bearing invariant is in
    # tests/pipeline/test_lyric_injector_no_stacking.py
    # (Level 1 / Level 2 / unit-partition). This test now pins the
    # matched-duration contract, not the solo default.
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)])
    assert overlays[0]["fade_out_ms"] == overlays[1]["fade_in_ms"]
    assert overlays[0]["fade_out_ms"] == 300  # natural_overlap = pre_roll(0.4) - gap(0.1) = 0.3 s


def test_hold_to_next_threshold_is_deprecated_noop_for_next_fade_in_and_pre_roll() -> None:
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)])
    # Defaults: pre_roll_s=0.40 → second.section_start = 2.3 - 0.40 = 1.90
    # (full pre-roll preserved because the §1d apply step only RAISES
    # section_start when shrinking the crossfade window forces it later).
    # second.fade_in_ms is now MATCHED to first.fade_out_ms (300) by the
    # dynamic-crossfade post-pass — not the 50 ms solo default.
    assert overlays[1]["fade_in_ms"] == 300
    assert overlays[1]["start_s"] == pytest.approx(1.9, abs=1e-3)


def test_hold_to_next_no_longer_forces_section_end_to_next_line_start() -> None:
    # The geometry of section_end is UNCHANGED by the dynamic-crossfade
    # post-pass (it only adjusts fade durations and section_start of the
    # NEXT line). With defaults pre_roll=0.40, next_visual_start = 1.90,
    # max_overlap_s = 0.40, gap_cap = 2.20, line_end = 2.0:
    #   overlap_cap = 1.90 + 0.40 = 2.30 (flag ON; was 1.90 + 0.30 = 2.20 with
    #     the legacy additive `min(max_overlap, fade_in + fade_out)` cap)
    #   gap_cap = 2.20
    #   section_end = min(natural=3.0, overlap_cap=2.30, gap_cap=2.20) = 2.20
    #   max(2.20, line_end=2.0) = 2.20
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)])
    assert overlays[0]["end_s"] == pytest.approx(2.2, abs=1e-3)


def test_hold_to_next_threshold_value_does_not_change_output() -> None:
    threshold_0 = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)], threshold_ms=0)
    threshold_500 = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)], threshold_ms=500)
    assert threshold_500 == threshold_0


def test_negative_gap_matched_durations_max_clamp() -> None:
    # Negative gap (overlapping vocals). natural_overlap_s = pre_roll - gap
    # = 0.40 - (-0.1) = 0.50 s. raw_crossfade_ms = 500, clamped to
    # _LINE_CROSSFADE_MAX_MS = 400 (ceiling — protects against fading into
    # most of the line's own audio). L1.audible = 1.0 s, max_safe = 900 ms
    # (not binding). Final: matched durations at 400 ms with sqrt curve.
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 1.9, 3.0)])
    assert overlays[0]["fade_out_ms"] == 400
    assert overlays[1]["fade_in_ms"] == 400
    assert overlays[0]["fade_out_curve"] == "sqrt"


def test_overlapping_lines_do_not_create_negative_duration_overlay() -> None:
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 1.9, 2.2)])
    assert all(o["end_s"] > o["start_s"] for o in overlays)


def test_min_line_visible_s_guard() -> None:
    overlays = _overlays([("Tiny", 0.05, 0.12), ("Next", 0.2, 1.0)])
    assert overlays[0]["fade_out_ms"] == 250


def test_rapid_sequence_actual_overlap_equals_matched_fade_duration() -> None:
    # In rapid sequences, the §1d apply step re-anchors nxt.section_start so
    # the actual emitted overlap equals the matched fade duration exactly —
    # that's the geometric invariant that makes unit-partition hold for the
    # mirror-symmetric curves. The OLD assertion (`+ 0.30`) hardcoded the
    # legacy additive-cap overlap; the new identity is `+ crossfade_ms/1000`.
    overlays = _overlays(
        [
            ("One", 1.0, 1.2),
            ("Two", 1.4, 1.6),
            ("Three", 1.8, 2.0),
        ]
    )
    fo_0 = overlays[0]["fade_out_ms"] / 1000.0
    fo_1 = overlays[1]["fade_out_ms"] / 1000.0
    assert overlays[0]["end_s"] == pytest.approx(overlays[1]["start_s"] + fo_0, abs=1e-3)
    assert overlays[1]["end_s"] == pytest.approx(overlays[2]["start_s"] + fo_1, abs=1e-3)
    # And matched durations on every consecutive pair (mirror invariant).
    assert overlays[0]["fade_out_ms"] == overlays[1]["fade_in_ms"]
    assert overlays[1]["fade_out_ms"] == overlays[2]["fade_in_ms"]
