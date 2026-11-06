"""Qendresa item 16: full screen in her own room, then the corner (S72-S74, S80).

Pure arithmetic over a Qendresa-shaped talk (a ~7 s intro sentence, then "number one is spec kit
..." and four more topics, ~230 wpm). Failure modes:
``docs/reviews/speaker-background-windows-failure-modes.md``.
"""

from __future__ import annotations

import random

import pytest

from app.config import settings
from app.services import phone_background_sequence as seq
from tests.tasks.test_background_follows_talk import (
    AS_SPOKEN,
    SAID,
    TAKE_S,
    WORDS,
    _contract,
    _lay,
    _rows,
)


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)


def _entries(phrases):
    return [seq.SequenceEntry(index=i, spoken_phrase=p) for i, p in enumerate(phrases)]


def _kept_words(words, keep):
    out = []
    for w in words:
        at = seq.to_output_time(w["start_s"], keep)
        if at is not None:
            out.append((at, at + (w["end_s"] - w["start_s"])))
    return out


def _inside_a_word(x, words, keep):
    return any(s + 1e-6 < x < e - 1e-6 for s, e in _kept_words(words, keep))


def _open(words=WORDS, keep=None, take_s=TAKE_S, stated=None, heard=None, title=None):
    return seq.full_screen_opening_end(
        words=words,
        keep_segments=keep,
        take_s=take_s,
        stated_s=stated,
        first_heard_s=heard,
        title_end_s=title,
    )


def test_the_opening_ends_on_the_first_recordings_word():
    heard = seq.first_heard_at(_entries(AS_SPOKEN), WORDS, None)
    assert heard == pytest.approx(SAID[0], abs=0.01)
    x, why = _open(heard=heard)
    assert why is None and x is not None
    # Her V5 switches on "spec kit": the switch is in the gap right before that word.
    assert abs(x - SAID[0]) <= 0.1 and x <= SAID[0] + 1e-6
    assert not _inside_a_word(x, WORDS, None)


def test_the_switch_never_lands_inside_a_word():
    rng = random.Random(16)
    for trial in range(40):
        t, words = 0.0, []
        for _ in range(rng.randint(30, 90)):
            dur = rng.uniform(0.1, 0.9)
            words.append({"text": "w.", "start_s": round(t, 3), "end_s": round(t + dur, 3)})
            t += dur + rng.choice([0.0, 0.0, 0.02, 0.05, 0.3, 0.9])
        take = t + 0.2
        for target in [x / 10 for x in range(10, 150, 3)]:
            x, why = _open(words=words, take_s=take, stated=target)
            if x is None:
                assert why == "take_too_short"
                continue
            assert x >= 1.0 - 1e-6 and take - x >= 1.0 - 1e-6, (trial, target, x)
            assert not _inside_a_word(x, words, None), (trial, target, x)


def test_a_stated_length_wins():
    x, _ = _open(stated=4.0, heard=SAID[0])
    assert x is not None and abs(x - 4.0) <= 1.0
    assert not _inside_a_word(x, WORDS, None)


def test_a_first_word_outside_two_to_eight_seconds_falls_back_to_five():
    x, _ = _open(heard=12.0)
    assert x is not None and abs(x - 5.0) <= 1.0
    x, _ = _open(heard=None)
    assert x is not None and abs(x - 5.0) <= 1.0


def test_the_opening_covers_the_title():
    x, _ = _open(heard=None, title=7.0)
    assert x is not None and x >= 6.7
    # A title longer than eight seconds is not honoured (it would eat the take).
    x, _ = _open(heard=None, title=9.5)
    assert x is not None and abs(x - 5.0) <= 1.0


def test_a_short_take_gets_no_opening_and_says_so():
    words = [
        {"text": "hi.", "start_s": 0.1, "end_s": 0.4},
        {"text": "there", "start_s": 0.5, "end_s": 0.9},
    ]
    assert _open(words=words, take_s=1.8) == (None, "take_too_short")
    assert _open(words=words, take_s=2.6, stated=5.0)[0] is not None
    x, _ = _open(words=words, take_s=2.6, stated=5.0)
    assert 1.0 <= x <= 1.6


def test_the_first_window_starts_at_the_switch():
    heard = seq.first_heard_at(_entries(AS_SPOKEN), WORDS, None)
    x, _ = _open(heard=heard)
    on_the_word = _lay(AS_SPOKEN, opening_s=x)
    assert on_the_word.windows[0].start_s == pytest.approx(x)
    assert all(w.start_s >= x - 1e-9 for w in on_the_word.windows)
    # Rotation (no phrases): the cycle starts filling at the switch, contiguous to the end.
    rotation = seq.lay_out(
        [seq.SequenceEntry(index=i) for i in range(3)],
        cadence_s=4.0,
        take_s=TAKE_S,
        words=WORDS,
        keep_segments=None,
        opening_s=x,
    )
    assert rotation.windows[0].start_s == pytest.approx(x) and rotation.windows[0].index == 0
    assert rotation.windows[-1].end_s == pytest.approx(TAKE_S)
    # A name heard just before the switch is seated at the switch, never dropped.
    late = _lay(AS_SPOKEN, opening_s=SAID[1] - 0.2)
    assert late.windows[0].start_s == pytest.approx(SAID[1] - 0.2)
    assert late.windows[0].index == 0 and {w.index for w in late.windows} == {0, 1, 2, 3, 4}


def test_the_opening_survives_a_speech_cut():
    # A 1.5 s pause inside the intro is cut out of the kept take.
    words = []
    for w in WORDS:
        shifted = dict(w)
        if w["start_s"] >= 3.0:
            shifted["start_s"] = round(w["start_s"] + 1.5, 2)
            shifted["end_s"] = round(w["end_s"] + 1.5, 2)
        words.append(shifted)
    keep = [(0.0, 3.0), (4.5, words[-1]["end_s"] + 0.3)]
    take_s = seq.kept_duration(keep, 0.0)
    heard = seq.first_heard_at(_entries(AS_SPOKEN), words, keep)
    assert heard == pytest.approx(SAID[0], abs=0.01)  # kept time equals the uncut talk
    x, why = _open(words=words, keep=keep, take_s=take_s, heard=heard)
    assert why is None and abs(x - SAID[0]) <= 0.1
    assert not _inside_a_word(x, words, keep)
    source = seq.to_source_time(x, keep, as_end=True)
    assert not any(w["start_s"] + 1e-6 < source < w["end_s"] - 1e-6 for w in words)
    assert not (3.0 + 1e-6 < source < 4.5 - 1e-6)


def test_a_word_inside_a_removed_span_does_not_exist():
    words = [dict(w) for w in WORDS]
    keep = [(0.0, 2.0), (3.0, TAKE_S)]
    kept = _kept_words(words, keep)
    assert len(kept) < len(words)


@pytest.mark.parametrize("follows", [True, False])
def test_no_opening_is_byte_identical(monkeypatch, follows):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", follows)
    for phrases in (AS_SPOKEN, ["spec kit", "impeccable"]):
        plain = _lay(phrases)
        explicit = _lay(phrases, opening_s=None)
        assert _rows(plain) == _rows(explicit) and plain.notes == explicit.notes
        assert plain == explicit


# ---- review fixes: an opening never removes a visual, the switch waits for the title


def _intro_talk():
    """25 s take: she names both tools in her 6 s intro, then talks on."""
    words, t = [], 0.0
    for text in ["Today", "Notion", "and", "Figma"]:
        words.append({"text": text, "start_s": t, "end_s": t + 0.4})
        t += 0.6
    while t < 24.5:
        words.append({"text": "blah", "start_s": t, "end_s": t + 0.3})
        t += 0.4
    return words, 25.0


@pytest.mark.parametrize("follows", [True, False])
def test_names_heard_during_the_opening_still_show_after_it(monkeypatch, follows):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", follows)
    words, take = _intro_talk()
    layout = _lay(["Notion", "Figma"], words=words, take_s=take, opening_s=6.0)
    assert layout.windows, "the opening removed every visual"
    assert layout.windows[0].start_s == pytest.approx(6.0)
    assert all(w.start_s >= 6.0 - 1e-9 for w in layout.windows)
    assert {w.index for w in layout.windows} == {0, 1}
    # Spoken order is kept.
    assert layout.windows[0].index == 0


def test_names_from_the_opening_and_a_later_name_all_show_after_the_switch(monkeypatch):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
    words, take = _intro_talk()
    # A third tool is named late; the other two were named during the opening.
    words[-3]["text"] = "Linear"
    layout = _lay(["Notion", "Figma", "Linear"], words=words, take_s=take, opening_s=6.0)
    assert {w.index for w in layout.windows} == {0, 1, 2}
    assert all(w.start_s >= 6.0 - 1e-9 for w in layout.windows)


def test_a_name_heard_right_after_the_switch_crowds_out_no_intro_name(monkeypatch):
    """S83: three tools named in the 6 s opening sit at the switch; "Miro" is heard 0.6 s after
    it. Since #2118 nothing comes back later, so a visual squeezed under 0.5 s there would never
    show. Each intro visual keeps at least 0.5 s, Miro starts at most 1.5 s after its word, and
    nothing shows twice."""
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
    words, t = [], 0.0
    for text in ["Today", "Notion", "Figma", "and", "Linear"]:
        words.append({"text": text, "start_s": t, "end_s": t + 0.4})
        t += 0.6
    while t < 24.5:
        text = "Miro" if abs(t - 6.6) < 0.2 else "blah"
        words.append({"text": text, "start_s": t, "end_s": t + 0.3})
        t += 0.4
    miro_said = next(w["start_s"] for w in words if w["text"] == "Miro")
    layout = _lay(["Notion", "Figma", "Linear", "Miro"], words=words, take_s=25.0, opening_s=6.0)

    shown = [w.index for w in layout.windows]
    assert sorted(shown) == [0, 1, 2, 3], _rows(layout)
    assert len(shown) == len(set(shown)), _rows(layout)
    assert all(w.start_s >= 6.0 - 1e-9 for w in layout.windows), _rows(layout)
    assert all(w.end_s - w.start_s >= seq.MIN_WINDOW_S - 1e-6 for w in layout.windows)
    assert not [n for n in layout.notes if n.get("reason") == "window_too_short"], layout.notes
    miro = next(w for w in layout.windows if w.index == 3)
    assert miro.start_s <= miro_said + 3 * seq.MIN_WINDOW_S + 1e-6, _rows(layout)
    assert layout.windows[-1].end_s == pytest.approx(25.0)


def test_near_the_take_end_nothing_is_pushed_past_it(monkeypatch):
    """S83's limit: with no room left, a name heard right after a late switch is not pushed past
    the take end (no broken window); the intro names that cannot fit get the honest note."""
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
    words = [
        {"text": "Notion", "start_s": 1.0, "end_s": 1.4},
        {"text": "Figma", "start_s": 1.6, "end_s": 2.0},
        {"text": "Linear", "start_s": 2.2, "end_s": 2.6},
        {"text": "blah", "start_s": 5.0, "end_s": 5.3},
        {"text": "Miro", "start_s": 9.1, "end_s": 9.3},
    ]
    layout = _lay(["Notion", "Figma", "Linear", "Miro"], words=words, take_s=10.0, opening_s=8.5)
    for w in layout.windows:
        assert 8.5 - 1e-9 <= w.start_s < w.end_s <= 10.0 + 1e-9, _rows(layout)
    shown = [w.index for w in layout.windows]
    assert len(shown) == len(set(shown)) and 3 in shown, _rows(layout)
    dropped = {n["window"] for n in layout.notes if n.get("reason") == "window_too_short"}
    assert dropped == {0, 1, 2} - set(shown), (layout.notes, _rows(layout))


def test_two_later_names_in_a_saturated_tail_keep_the_lane_valid(monkeypatch):
    """S83 tail chain (Emir's review of #2111, found by reading the control flow): an 8 s take,
    a title to 3 s and an opening resolved to 6 s; Notion, Figma and Linear are said inside the
    opening (0.6 / 1.2 / 1.8 s), Miro at 6.6 s and Slack at 7.2 s. The three intro names claim
    6.0-7.5 s at the switch, Miro is pushed to 7.5 s, then the push-back stops at the take's last
    half second and Slack stayed at 7.2 s: Miro got a reversed window (7.5 -> 7.2) and Slack's
    overlapped Linear's, so the phone lane refused the whole background (corner and opening too).

    Ways it could fail (written before the fix):
    1. A later mention left before a pushed one: a reversed window, an overlap, the lane lost.
    2. A crowded later name dropped with no note (the receipt says nothing).
    3. The fix drops names that do fit, or moves a window before the switch or past the take.
    4. A visual shown twice to make room.
    5. The near-end case with ONE later name (test above) changes.
    """
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
    names = ["Notion", "Figma", "Linear", "Miro", "Slack"]
    heard = [0.6, 1.2, 1.8, 6.6, 7.2]
    words = [
        {"text": text, "start_s": at, "end_s": at + 0.2}
        for text, at in zip(names, heard, strict=True)
    ]
    words.insert(3, {"text": "blah", "start_s": 5.2, "end_s": 5.4})
    opening, why = _open(words=words, take_s=8.0, title=3.0)
    assert opening == pytest.approx(6.0), (opening, why)
    layout = _lay(names, words=words, take_s=8.0, opening_s=opening)

    _contract(layout, 8.0)
    assert all(w.start_s >= opening - 1e-9 for w in layout.windows), _rows(layout)
    shown = [w.index for w in layout.windows]
    assert len(shown) == len(set(shown)), _rows(layout)
    dropped = {n["window"] for n in layout.notes if n.get("reason") == "window_too_short"}
    assert set(shown) | dropped == {0, 1, 2, 3, 4}, (layout.notes, _rows(layout))
    assert not set(shown) & dropped, (layout.notes, _rows(layout))
    # Every name that has room shows: four half-second windows fit in the 2 s after the switch.
    assert len(shown) == 4, _rows(layout)
    assert layout.windows[-1].end_s == pytest.approx(8.0)


def test_a_crowded_tail_never_breaks_the_lane(monkeypatch):
    """S83 tail chain, seeded: short takes with names said in the opening and several right after
    it. Whatever saturates, the lane gets ordered, disjoint windows of at least 0.5 s from the
    switch to the take end, no visual twice, and every name either shows or carries a note."""
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
    rng = random.Random(2108)
    names = ["Notion", "Figma", "Linear", "Miro", "Slack", "Jira"]
    for trial in range(300):
        opening = rng.choice([2.0, 3.0, 4.5, 6.0, 7.5])
        take = opening + rng.choice([0.5, 1.0, 1.5, 2.0, 2.5, 3.5])
        intro = rng.randint(0, 4)
        later = len(names) - intro
        heard = sorted(rng.uniform(0.2, opening - 0.3) for _ in range(intro))
        heard += sorted(rng.uniform(opening + 0.05, take - 0.1) for _ in range(later))
        words = [
            {"text": text, "start_s": round(at, 3), "end_s": round(at, 3) + 0.05}
            for text, at in zip(names, heard, strict=True)
        ]
        layout = _lay(names, words=words, take_s=take, opening_s=opening)
        case = (trial, opening, take, [round(h, 2) for h in heard], _rows(layout), layout.notes)
        _contract(layout, take)
        assert all(w.start_s >= opening - 1e-9 for w in layout.windows), case
        assert layout.windows[0].start_s == pytest.approx(opening), case
        assert layout.windows[-1].end_s == pytest.approx(take), case
        shown = [w.index for w in layout.windows]
        assert len(shown) == len(set(shown)), case
        noted = {n["window"] for n in layout.notes if "window" in n}
        assert set(shown) | noted == set(range(len(names))), case


@pytest.mark.parametrize("follows", [True, False])
@pytest.mark.parametrize("fill", ["first_photo", "base"])
@pytest.mark.parametrize("shape", ["on_the_word", "rotation", "mixed"])
def test_an_opening_never_shows_a_photo_twice(monkeypatch, follows, fill, shape):
    """S57 + S74 + S83: fb 1's take with a 6 s opening. Names said in the opening (three of fb 1's
    eight on the word, the stadium in the mixed ask) still show once after the switch; nothing
    returns, the last visual holds to the end, and every second after the switch is covered.
    (Contributed by the "Open PRs ready to merge" session's independent resolution of this merge.)
    """
    from tests.tasks import test_background_no_repeat as fb1

    monkeypatch.setattr(settings, "background_follows_talk_enabled", follows)
    entries = {
        "on_the_word": fb1.FB1_SHAPED,
        "rotation": [("one", None), ("two", None), ("three", None)],
        "mixed": [
            ("stadium", "Kadıköy çarşısında"),
            ("plain_one", None),
            ("coast", "Moda sahilinde"),
            ("plain_two", None),
        ],
    }[shape]
    fill_key = entries[0][0] if fill == "first_photo" else "base"
    opening = 6.0
    layout = seq.lay_out(
        [
            seq.SequenceEntry(index=i, spoken_phrase=phrase, media_key=media)
            for i, (media, phrase) in enumerate(entries)
        ],
        cadence_s=4.0,
        take_s=fb1.TAKE_S,
        words=fb1.WORDS,
        keep_segments=fb1.KEEP,
        fill_key=fill_key,
        opening_s=opening,
    )
    windows = [(entries[w.index][0], w.start_s, w.end_s) for w in layout.windows]
    described = " | ".join(f"{m} {s:.2f}-{e:.2f}" for m, s, e in windows)

    # Nothing inside the opening, the first visual right at the switch, the last to the end.
    assert windows and windows[0][1] == pytest.approx(opening), described
    assert all(start >= opening - 1e-9 for _m, start, _e in windows), described
    assert windows[-1][2] == pytest.approx(fb1.TAKE_S), described
    for _m, start, end in windows:
        assert end - start >= seq.MIN_WINDOW_S - 1e-6, described
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert later[1] >= earlier[2] - 1e-6, described

    # What is behind her after the switch: the windows, and the fill only in a gap.
    shown: list[list] = []
    t = opening
    for media, start, end in [*windows, (None, fb1.TAKE_S, fb1.TAKE_S)]:
        for name, a, b in ((fill_key, t, start), (media, start, end)):
            if name is None or b - a <= 1e-6:
                continue
            if shown and shown[-1][0] == name and abs(shown[-1][2] - a) <= 1e-6:
                shown[-1][2] = b
            else:
                shown.append([name, a, b])
        t = end
    photos = [name for name, _a, _b in shown if name != "base"]
    assert len(photos) == len(set(photos)), f"a photo shows twice: {described}"
    # Every photo of the ask shows: none is crowded out here, none is lost to the opening.
    assert set(photos) == {media for media, _p in entries}, described


def test_an_anchored_entry_heard_in_the_opening_joins_the_rotation(monkeypatch):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
    words, take = _intro_talk()
    entries = [
        seq.SequenceEntry(index=0, spoken_phrase="Notion"),
        seq.SequenceEntry(index=1),
    ]
    layout = seq.lay_out(
        entries, cadence_s=4.0, take_s=take, words=words, keep_segments=None, opening_s=6.0
    )
    assert {w.index for w in layout.windows} == {0, 1}
    assert layout.windows[0].start_s == pytest.approx(6.0)
    assert all(w.start_s >= 6.0 - 1e-9 for w in layout.windows)


def test_the_switch_waits_for_the_hook_title():
    words = [
        {"text": "one.", "start_s": 0.1, "end_s": 2.2},
        {"text": "two", "start_s": 2.4, "end_s": 2.9},
        {"text": "three", "start_s": 3.2, "end_s": 3.9},
        {"text": "four", "start_s": 4.1, "end_s": 6.0},
    ]
    x, why = _open(words=words, take_s=12.0, heard=2.6, title=3.0)
    assert why is None and x is not None and x >= 3.0 - 1e-6
    assert not _inside_a_word(x, words, None)


def test_a_short_take_never_switches_before_the_title_ends():
    """Review of #2111: a 3.5 s take whose title ends at 3 s switched at 2.5 s, mid-title."""
    words = [
        {"text": "hey", "start_s": 0.1, "end_s": 0.5},
        {"text": "this", "start_s": 0.7, "end_s": 1.0},
        {"text": "is", "start_s": 1.2, "end_s": 1.4},
        {"text": "short.", "start_s": 1.6, "end_s": 2.0},
        {"text": "bye", "start_s": 3.3, "end_s": 3.4},
    ]
    # The title cannot finish and still leave a second of corner: no opening, honestly.
    assert _open(words=words, take_s=3.5, title=3.0) == (None, "take_too_short")
    assert _open(words=words, take_s=3.5, title=3.0, stated=2.0) == (None, "take_too_short")
    assert _open(words=words, take_s=3.5, title=3.0, heard=2.2) == (None, "take_too_short")
    # One second after the title is enough: the switch lands exactly when the title leaves.
    x, why = _open(words=words, take_s=4.0, title=3.0)
    assert why is None and x == pytest.approx(3.0)
    # A title past eight seconds is not honoured, so a short take keeps its opening.
    x, why = _open(words=words, take_s=3.5, title=9.0)
    assert why is None and 1.0 - 1e-6 <= x <= 2.5 + 1e-6


def test_the_opening_always_outlasts_the_title_and_leaves_a_corner():
    rng = random.Random(2111)
    for trial in range(25):
        t, words = 0.0, []
        for _ in range(rng.randint(4, 30)):
            dur = rng.uniform(0.1, 0.8)
            words.append({"text": "w.", "start_s": round(t, 3), "end_s": round(t + dur, 3)})
            t += dur + rng.choice([0.0, 0.05, 0.3, 0.8])
        for take in [x / 4 for x in range(6, 49)]:  # 1.5 s .. 12 s
            for title in [None, 1.0, 2.0, 2.5, 3.0, 3.5, 5.0, 7.5, 9.0]:
                for stated in [None, 1.0, 3.0, 6.0]:
                    x, why = _open(words=words, take_s=take, title=title, stated=stated)
                    case = (trial, take, title, stated, x)
                    if x is None:
                        assert why == "take_too_short", case
                        continue
                    assert why is None, case
                    if title is not None and title <= seq.OPENING_TITLE_MAX_S:
                        assert x >= title - 1e-6, case  # never before the title ends
                    assert x >= seq.OPENING_MIN_S - 1e-6, case
                    assert take - x >= seq.OPENING_MIN_S - 1e-6, case  # a real corner after it
