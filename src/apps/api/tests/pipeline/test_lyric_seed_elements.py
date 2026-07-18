"""Lyrics-as-optional-elements: pure line-scheduling + seed-element builder.

Covers `schedule_karaoke_lines` (the scheduling logic extracted from
`_inject_karaoke` so it's reusable outside the burn path) and
`build_lyric_seed_elements` (the GET .../lyric-seeds pure builder). See
CLAUDE.md "Lyrics beat-sync pipeline" / the injector module docstring for the
lines/words cache shape.
"""

from __future__ import annotations

from app.pipeline.lyric_injector import (
    _select_section_lines,
    build_lyric_seed_elements,
    schedule_karaoke_lines,
)


def _make_lyrics_cache(
    lines: list[tuple[str, float, float, list[tuple[str, float, float]]]],
    *,
    source: str = "lrclib_synced+whisper",
) -> dict:
    return {
        "source": source,
        "lines": [
            {
                "text": text,
                "start_s": start,
                "end_s": end,
                "words": [{"text": w, "start_s": ws, "end_s": we} for w, ws, we in words],
            }
            for text, start, end, words in lines
        ],
    }


# ── schedule_karaoke_lines ───────────────────────────────────────────────────


def test_schedule_karaoke_lines_matches_injector_window() -> None:
    """Section-relative start/end + word_timings mirror what `_inject_karaoke`
    would burn absent any slot-boundary clipping (same floor, same word math)."""
    cache = _make_lyrics_cache(
        [("Hello world", 0.5, 1.5, [("Hello", 0.5, 1.0), ("world", 1.0, 1.5)])]
    )
    section_lines = _select_section_lines(cache["lines"], 0.0, 10.0)
    schedule = schedule_karaoke_lines(section_lines)

    assert len(schedule) == 1
    line = schedule[0]
    assert line.lyric_line_key == "L0"
    assert line.text == "Hello world"
    assert line.start_s == 0.5
    assert line.end_s == 1.5
    assert [w["text"] for w in line.word_timings] == ["Hello", "world"]
    assert line.word_timings[0]["start_s"] == 0.0
    assert line.word_timings[1]["start_s"] == 0.5


def test_schedule_karaoke_lines_floors_short_line_duration() -> None:
    """A naturally sub-floor line still gets the same _MIN_OVERLAY_DURATION_S
    floor `_inject_karaoke` always applies, independent of slot clipping."""
    cache = _make_lyrics_cache([("Yo", 2.0, 2.05, [("Yo", 2.0, 2.05)])])
    section_lines = _select_section_lines(cache["lines"], 0.0, 10.0)
    schedule = schedule_karaoke_lines(section_lines)

    assert len(schedule) == 1
    assert schedule[0].end_s - schedule[0].start_s >= 0.18


def test_schedule_karaoke_lines_empty_words_yields_no_word_timings() -> None:
    cache = _make_lyrics_cache([("Instrumental", 1.0, 3.0, [])])
    section_lines = _select_section_lines(cache["lines"], 0.0, 10.0)
    schedule = schedule_karaoke_lines(section_lines)

    assert schedule[0].word_timings == []


# ── build_lyric_seed_elements ────────────────────────────────────────────────


def test_seed_elements_absolute_time_and_id_format() -> None:
    # Lyrics-cache timestamps are SONG-absolute; the section starts at
    # best_start_s=100.0. The recipe's slots (what actually gets burned)
    # start at video t=0 == song t=best_start_s, so the seed's "absolute
    # video time" is the SECTION-relative offset (song_time - best_start_s),
    # not the raw song timestamp.
    cache = _make_lyrics_cache(
        [
            ("Hello world", 100.5, 101.5, [("Hello", 100.5, 101.0), ("world", 101.0, 101.5)]),
            ("Goodbye now", 106.0, 107.5, [("Goodbye", 106.0, 106.8), ("now", 106.8, 107.5)]),
        ]
    )
    elements = build_lyric_seed_elements(cache, 100.0, 110.0, {"enabled": True})

    assert len(elements) == 2
    first, second = elements
    assert first["id"] == "lyr-L0"
    assert first["role"] == "lyric_line"
    assert first["start_s"] == 0.5
    assert first["end_s"] == 1.5
    assert second["id"] == "lyr-L1"
    assert second["start_s"] == 6.0


def test_seed_elements_karaoke_effect_iff_word_timings() -> None:
    cache = _make_lyrics_cache(
        [
            ("Has words", 0.0, 1.0, [("Has", 0.0, 0.5), ("words", 0.5, 1.0)]),
            ("No words", 2.0, 3.0, []),
        ]
    )
    elements = build_lyric_seed_elements(cache, 0.0, 10.0, {"enabled": True})

    by_id = {e["id"]: e for e in elements}
    assert by_id["lyr-L0"]["effect"] == "karaoke-line"
    assert by_id["lyr-L0"]["word_timings"]
    assert by_id["lyr-L0"]["highlight_color"] is not None

    assert by_id["lyr-L1"]["effect"] == "static"
    assert by_id["lyr-L1"]["word_timings"] is None
    assert by_id["lyr-L1"]["highlight_color"] is None


def test_seed_elements_styling_matches_injector_defaults() -> None:
    """Pin a few style fields against `_common_overlay_fields`' bare defaults
    (position="bottom", text_color="#FFFFFF") so a materialized element LOOKS
    like today's burned lyrics absent any style-set/config override."""
    cache = _make_lyrics_cache([("Hello", 0.0, 1.0, [("Hello", 0.0, 1.0)])])
    elements = build_lyric_seed_elements(cache, 0.0, 10.0, {"enabled": True})

    assert elements[0]["position"] == "bottom"
    assert elements[0]["color"] == "#FFFFFF"


def test_seed_elements_custom_position_from_config() -> None:
    cache = _make_lyrics_cache([("Hello", 0.0, 1.0, [("Hello", 0.0, 1.0)])])
    elements = build_lyric_seed_elements(
        cache,
        0.0,
        10.0,
        {"enabled": True, "position_x_frac": 0.5, "position_y_frac": 0.8},
    )

    assert elements[0]["position"] == "custom"
    assert elements[0]["x_frac"] == 0.5
    assert elements[0]["y_frac"] == 0.8


def test_seed_elements_no_lyrics_cache_returns_empty() -> None:
    assert build_lyric_seed_elements(None, 0.0, 10.0, {"enabled": True}) == []


def test_seed_elements_no_lines_in_section_returns_empty() -> None:
    cache = _make_lyrics_cache([("Too late", 50.0, 51.0, [("Too", 50.0, 50.5)])])
    assert build_lyric_seed_elements(cache, 0.0, 10.0, {"enabled": True}) == []


def test_seed_elements_empty_lines_list_returns_empty() -> None:
    cache = {"source": "lrclib_synced+whisper", "lines": []}
    assert build_lyric_seed_elements(cache, 0.0, 10.0, {"enabled": True}) == []
