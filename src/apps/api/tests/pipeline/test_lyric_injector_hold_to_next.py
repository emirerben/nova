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
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)])
    assert overlays[0]["fade_out_ms"] == 250


def test_hold_to_next_threshold_is_deprecated_noop_for_next_fade_in_and_pre_roll() -> None:
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)])
    # Defaults: fade_in_ms=50, pre_roll_s=0.40 → second.start_s = 2.3 - 0.40 = 1.90
    assert overlays[1]["fade_in_ms"] == 50
    assert overlays[1]["start_s"] == pytest.approx(1.9, abs=1e-3)


def test_hold_to_next_no_longer_forces_section_end_to_next_line_start() -> None:
    # Defaults: pre_roll=0.40, fade_in=50, fade_out=250 →
    # next_visual_start = 2.3 - 0.40 = 1.90
    # overlap_budget = min(_LINE_MAX_OVERLAP_S=0.4, (50+250)/1000=0.30) = 0.30
    # first.end_s = next_visual_start + overlap_budget = 1.90 + 0.30 = 2.20
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)])
    assert overlays[0]["end_s"] == pytest.approx(2.2, abs=1e-3)


def test_hold_to_next_threshold_value_does_not_change_output() -> None:
    threshold_0 = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)], threshold_ms=0)
    threshold_500 = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)], threshold_ms=500)
    assert threshold_500 == threshold_0


def test_negative_gap_does_not_trigger_hold_to_next() -> None:
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 1.9, 3.0)])
    assert overlays[0]["fade_out_ms"] == 250
    assert overlays[1]["fade_in_ms"] == 50


def test_overlapping_lines_do_not_create_negative_duration_overlay() -> None:
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 1.9, 2.2)])
    assert all(o["end_s"] > o["start_s"] for o in overlays)


def test_min_line_visible_s_guard() -> None:
    overlays = _overlays([("Tiny", 0.05, 0.12), ("Next", 0.2, 1.0)])
    assert overlays[0]["fade_out_ms"] == 250


def test_rapid_sequence_uses_overlap_budget() -> None:
    # With defaults (fade_in=50, fade_out=250), overlap_budget caps at
    # (50+250)/1000 = 0.30s, below _LINE_MAX_OVERLAP_S=0.4. The previous
    # defaults (fade_in=150) gave a 0.40s budget that hit the cap; the
    # new fade-bound is the binding constraint.
    overlays = _overlays(
        [
            ("One", 1.0, 1.2),
            ("Two", 1.4, 1.6),
            ("Three", 1.8, 2.0),
        ]
    )
    assert overlays[0]["end_s"] == pytest.approx(overlays[1]["start_s"] + 0.3, abs=1e-3)
    assert overlays[1]["end_s"] == pytest.approx(overlays[2]["start_s"] + 0.3, abs=1e-3)
