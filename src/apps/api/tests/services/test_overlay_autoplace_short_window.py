"""A second Visual on a mention whose window is too short to split (Q1 run 4, Recording 5).

Prod 2026-11-17: with the card strip on, four recordings shared their screenshot's window
(first half screenshot, second half recording). The fifth mention was 2.72 s: two halves of
1.36 s are under the 1.5 s minimum, so the recording was dropped as "overlap" although the
agent had proposed it until 44.92 s and nothing else was on screen after the screenshot.

Ways the fix could go wrong (written before the code):
1. Strip off (no `shareable` windows): the short-window rule must not fire; byte-identical.
2. The first card loses time: the screenshot must keep its WHOLE window; the recording starts
   where it ends.
3. The recording starts earlier than the screenshot ends (two cards at once), or runs into
   another occupied window (the closing text, the next mention).
4. The recording gets under 1.5 s on screen (too little proposed window left): stays "overlap".
5. A recording on a window that is not a strip card's, or a second recording on a window that
   was already shared, takes the tail: only ONE share per window.
6. A card that starts after the midpoint (it belongs to what she says next) takes the tail.
7. The two-halves rule (windows >= 3.0 s) changes: it still wins when it fits.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.overlay_autoplace import build_suggestions


def _asset(asset_id: str) -> dict:
    return {
        "id": asset_id,
        "kind": "video",
        "gcs_path": f"x/{asset_id}.mp4",
        "aspect": 1.7778,
        "duration_s": 18.0,
    }


def _raw(asset_id: str, start: float, end: float) -> SimpleNamespace:
    return SimpleNamespace(
        asset_id=asset_id,
        start_s=start,
        end_s=end,
        slot="top",
        reason="x",
        transcript_anchor=None,
        sfx_intent="pop_in",
        confidence_tier="confident",
    )


WORDS = [{"word": f"w{i}", "start_s": i * 0.5, "end_s": i * 0.5 + 0.4} for i in range(97)]
# Run 4's beat windows on the take's clock and its closing text.
SHORT = (39.22, 41.94)
LONG = (15.22, 18.88)
CLOSING = (45.8, 48.8)


def _run(raw, *, occupied, shareable):
    shared: dict = {}
    out = build_suggestions(
        raw,
        assets_by_id={a: _asset(a) for a in ("a", "b")},
        words=WORDS,
        duration_s=48.8,
        occupied=occupied,
        glossary=[],
        hook_any_tier=True,
        **({"shareable": shareable, "shared_out": shared} if shareable else {}),
    )
    return out, shared


def _windows(out):
    return {s["asset_id"]: (s["overlay"]["start_s"], s["overlay"]["end_s"]) for s in out}


def test_short_window_second_visual_starts_where_the_first_ends():
    out, shared = _run([_raw("a", 39.32, 44.92)], occupied=[SHORT, CLOSING], shareable=[SHORT])
    assert _windows(out) == {"a": (41.94, 44.92)}
    first_start, first_end, new_end_of_first = next(iter(shared.values()))
    # the screenshot keeps its whole window
    assert (first_start, first_end) == SHORT and new_end_of_first == first_end


def test_strip_off_is_unchanged():
    out, shared = _run([_raw("a", 39.32, 44.92)], occupied=[SHORT, CLOSING], shareable=[])
    assert out == [] and shared == {}


def test_not_enough_proposed_window_left_stays_overlap():
    out, _ = _run([_raw("a", 39.32, 43.0)], occupied=[SHORT, CLOSING], shareable=[SHORT])
    assert out == []  # 43.0 - 41.94 = 1.06 s < 1.5 s


def test_the_tail_never_runs_into_another_occupied_window():
    out, _ = _run([_raw("a", 39.32, 44.92)], occupied=[SHORT, (43.0, 48.8)], shareable=[SHORT])
    assert out == []


def test_a_card_starting_after_the_midpoint_does_not_take_the_tail():
    out, _ = _run([_raw("a", 41.0, 44.92)], occupied=[SHORT, CLOSING], shareable=[SHORT])
    assert out == []


def test_only_one_visual_per_window():
    out, _ = _run(
        [_raw("a", 39.32, 44.92), _raw("b", 39.4, 44.9)],
        occupied=[SHORT, CLOSING],
        shareable=[SHORT],
    )
    assert len(out) == 1


def test_a_long_window_still_splits_in_two_halves():
    out, shared = _run([_raw("a", 15.32, 20.98)], occupied=[LONG], shareable=[LONG])
    assert _windows(out) == {"a": (17.05, 18.88)}
    assert next(iter(shared.values())) == (15.22, 18.88, 17.05)


def test_a_window_that_is_not_a_strip_cards_is_never_taken():
    out, _ = _run([_raw("a", 39.32, 44.92)], occupied=[SHORT, CLOSING], shareable=[LONG])
    assert out == []
