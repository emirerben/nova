from app.pipeline import lyric_injector


def test_line_defaults_locked() -> None:
    assert lyric_injector._LINE_POST_DWELL_S == 1.0
    assert lyric_injector._LINE_HOLD_TO_NEXT_THRESHOLD_MS == 500
    assert lyric_injector._MIN_LINE_VISIBLE_S == 0.20
