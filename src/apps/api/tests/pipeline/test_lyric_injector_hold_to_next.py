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


def test_hold_to_next_suppresses_current_fade_out() -> None:
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)])
    assert overlays[0]["fade_out_ms"] == 0


def test_hold_to_next_suppresses_next_fade_in_and_pre_roll() -> None:
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)])
    assert overlays[1]["fade_in_ms"] == 0
    assert overlays[1]["start_s"] == 2.3


def test_hold_to_next_section_end_equals_next_line_start() -> None:
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 2.3, 3.0)])
    assert overlays[0]["end_s"] == 2.3


def test_hold_to_next_at_exact_threshold_does_not_fire() -> None:
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 2.5, 3.0)])
    assert overlays[0]["fade_out_ms"] == 250
    assert overlays[1]["fade_in_ms"] == 150


def test_negative_gap_does_not_trigger_hold_to_next() -> None:
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 1.9, 3.0)])
    assert overlays[0]["fade_out_ms"] == 250
    assert overlays[1]["fade_in_ms"] == 150


def test_overlapping_lines_do_not_create_negative_duration_overlay() -> None:
    overlays = _overlays([("First", 1.0, 2.0), ("Second", 1.9, 2.2)])
    assert all(o["end_s"] > o["start_s"] for o in overlays)


def test_min_line_visible_s_guard() -> None:
    overlays = _overlays([("Tiny", 0.05, 0.12), ("Next", 0.2, 1.0)])
    assert overlays[0]["fade_out_ms"] == 250


def test_rapid_sequence_has_zero_dead_frames() -> None:
    overlays = _overlays(
        [
            ("One", 1.0, 1.2),
            ("Two", 1.4, 1.6),
            ("Three", 1.8, 2.0),
        ]
    )
    assert overlays[0]["end_s"] == overlays[1]["start_s"]
    assert overlays[1]["end_s"] == overlays[2]["start_s"]
