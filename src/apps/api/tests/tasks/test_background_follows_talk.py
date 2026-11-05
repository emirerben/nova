"""Qendresa item 17: background names are heard when said another way (S64-S69).

Pure ``lay_out`` calls over a synthetic five-topic screen walkthrough (~230 wpm) and the Green 3
run. Holding the last visual to the take end is #2118's rule for everyone (S57); item 17 adds the
hearing rules behind ``BACKGROUND_FOLLOWS_TALK_ENABLED`` (off in prod, so every behaviour test
turns it on); ``test_flag_off_windows_and_notes_are_byte_identical`` freezes what the code lays
out without it. Failure modes: ``docs/reviews/speaker-background-windows-failure-modes.md``.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import phone_background_sequence as seq
from tests.tasks.test_speaker_background_on_the_word import (
    CADENCE_S as G3_CADENCE_S,
)
from tests.tasks.test_speaker_background_on_the_word import (
    KEEP as G3_KEEP,
)
from tests.tasks.test_speaker_background_on_the_word import (
    PHRASES as G3_PHRASES,
)
from tests.tasks.test_speaker_background_on_the_word import (
    STORED_ORDER as G3_ORDER,
)
from tests.tasks.test_speaker_background_on_the_word import (
    TAKE_S as G3_TAKE_S,
)
from tests.tasks.test_speaker_background_on_the_word import (
    WORDS as G3_WORDS,
)

# ------------------------------------------------------------------ the five-topic talk


def _talk(script):
    """``[(start_s, "words ..."), ...]`` -> whisper-shaped words, ~0.26 s each (~230 wpm)."""
    words: list[dict] = []
    for start, text in script:
        t = start
        for w in text.split():
            words.append({"text": w, "start_s": round(t, 2), "end_s": round(t + 0.22, 2)})
            t += 0.26
    return words


_INTRO = (0.0, "here are five projects the AI builder community can not stop talking about")
_ONE = (
    6.0,
    "number one is spec kit by github it turns your idea into a spec and then into code you "
    "write the spec first and the agent builds from it which keeps everything on track",
)
_TWO = (
    26.0,
    "number two impeccable this one makes your front end look designed instead of generated "
    "it gives the agent a design vocabulary and a set of commands to polish every screen",
)
_THREE = (
    46.0,
    "number three trading agents a whole team of AI agents that research a stock debate it and "
    "then a trader agent makes the call it is open source and you can run it locally",
)
_THREE_PLAIN = (
    46.0,
    "number three trading agents a whole team of analysts that research a stock debate it and "
    "then a trader makes the call it is open source and you can run it locally",
)
_FOUR = (
    66.0,
    "number four agents this is a giant collection of ready made sub agents and skills for "
    "claude code you just install the ones you need and they plug right in",
)
_FIVE = (
    86.0,
    "number five claude code game studios it turns claude code into a full game studio with "
    "directors designers and programmers all as agents",
)
_CTA = (102.0, "comment repo and I will send you all five links")

WORDS = _talk([_INTRO, _ONE, _TWO, _THREE, _FOUR, _FIVE, _CTA])
WORDS_PLAIN = _talk([_INTRO, _ONE, _TWO, _THREE_PLAIN, _FOUR, _FIVE, _CTA])
TAKE_S = WORDS[-1]["end_s"] + 0.3

AS_SPOKEN = [
    "spec kit",
    "impeccable",
    "trading agents",
    "number four agents",
    "claude code game studios",
]
AS_SHE_TYPED = [
    "Spec-kit",
    "Impeccable",
    "TradingAgents",
    "number four Agents",
    "Claude code game studio",
]


def _at(words, tokens, after=0.0):
    """Start of the first run of ``tokens`` (lower-case words) at or after ``after``."""
    texts = [w["text"].casefold() for w in words]
    for i in range(len(texts) - len(tokens) + 1):
        if texts[i : i + len(tokens)] == tokens and words[i]["start_s"] >= after:
            return words[i]["start_s"]
    raise AssertionError(f"{tokens} is not in the talk")


# Where each topic's name is said (the key word, not "number N").
SAID = [
    _at(WORDS, ["spec", "kit"]),
    _at(WORDS, ["impeccable"]),
    _at(WORDS, ["trading", "agents"]),
    _at(WORDS, ["four", "agents"]),
    _at(WORDS, ["claude", "code", "game"], after=86.0),
]


def _lay(phrases, *, words=WORDS, take_s=TAKE_S, cadence_s=4.0, holds=None, **kw):
    holds = holds or {}
    entries = [
        seq.SequenceEntry(index=i, spoken_phrase=p, hold_s=holds.get(i))
        for i, p in enumerate(phrases)
    ]
    return seq.lay_out(
        entries, cadence_s=cadence_s, take_s=take_s, words=words, keep_segments=None, **kw
    )


def _rows(layout):
    return [(w.index, round(w.start_s, 3), round(w.end_s, 3)) for w in layout.windows]


def _notes(layout):
    return sorted(tuple(sorted(n.items())) for n in layout.notes)


def _contract(layout, take_s):
    """What the phone lane accepts: ordered, no overlap, >= 0.5 s, inside the take, <= 40."""
    assert 1 <= len(layout.windows) <= seq.MAX_WINDOWS
    t = 0.0
    for w in layout.windows:
        assert w.start_s >= t - 1e-6, f"overlap at {w}"
        assert w.end_s - w.start_s >= seq.MIN_WINDOW_S - 1e-6, f"too short: {w}"
        assert w.end_s <= take_s + 1e-6, f"past the take: {w}"
        t = w.end_s


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", False)


# ------------------------------------------------------------------ Q2 B5, as she typed it


def test_her_walkthrough_as_typed_follows_the_talk(flag_on):
    """Each recording from its name to the next one's, the last to the end (kit Q2 row B5)."""
    layout = _lay(AS_SHE_TYPED)
    _contract(layout, TAKE_S)
    assert [w.index for w in layout.windows] == [0, 1, 2, 3, 4]
    for i, w in enumerate(layout.windows):
        if i == 0:
            assert w.start_s == pytest.approx(0.0)  # the talk opens on its first visual
        else:
            assert abs(w.start_s - SAID[i]) <= 1.5, f"recording {i + 1}: {w.start_s} vs {SAID[i]}"
            assert layout.windows[i - 1].end_s == pytest.approx(w.start_s, abs=1e-6)
    assert layout.windows[-1].end_s == pytest.approx(TAKE_S)


# ------------------------------------------------------------------ S65


def test_her_label_spellings_are_heard(flag_on):
    layout = _lay(AS_SHE_TYPED)
    _contract(layout, TAKE_S)
    assert [n for n in layout.notes if n.get("reason") == "phrase_not_heard"] == []
    starts = {w.index: w.start_s for w in reversed(layout.windows)}
    for i in range(1, 5):
        assert abs(starts[i] - SAID[i]) <= 1.5, f"recording {i + 1}: {starts[i]} vs {SAID[i]}"


# ------------------------------------------------------------------ S67


def test_a_name_inside_another_visuals_name_is_not_its_word(flag_on):
    # "agents" is first said inside "trading agents" (same breath), next alone after "number four".
    phrases = ["spec kit", "trading agents", "agents", "claude code game studios"]
    layout = _lay(phrases, words=WORDS_PLAIN)
    _contract(layout, TAKE_S)
    trading = _at(WORDS_PLAIN, ["trading", "agents"])
    four = _at(WORDS_PLAIN, ["agents"], after=_at(WORDS_PLAIN, ["four"]))
    by = {w.index: w for w in layout.windows}
    assert by[1].start_s == pytest.approx(trading, abs=1e-6)
    assert by[2].start_s == pytest.approx(four, abs=1e-6)
    assert by[1].end_s == pytest.approx(four, abs=1e-6)  # trading agents is not cut short

    # Two visuals with the same phrase still share one mention (S48).
    shared = _lay(["spec kit", "trading agents", "trading agents", "agents"], words=WORDS_PLAIN)
    by = {w.index: w for w in shared.windows}
    assert by[1].start_s == pytest.approx(trading, abs=1e-6)
    assert by[2].start_s == pytest.approx(by[1].end_s, abs=1e-6)
    assert by[2].end_s == pytest.approx(four, abs=1e-6)
    assert by[3].start_s == pytest.approx(four, abs=1e-6)


# ------------------------------------------------------------------ S66


def _heard(phrase, spoken):
    """Anchor start of ``phrase`` in a short talk, or None (the second visual is the probe)."""
    words = _talk([(0.0, "hello and welcome"), (3.0, spoken)])
    layout = _lay(["hello", phrase], words=words, take_s=12.0)
    if any(n.get("reason") == "phrase_not_heard" and n.get("window") == 1 for n in layout.notes):
        return None
    return next(w.start_s for w in layout.windows if w.index == 1)


def test_the_loose_match_never_hears_a_different_word(flag_on):
    assert _heard("trading agent", "so here is trading agency stuff") is None
    assert _heard("game studio", "that game studied the market") is None
    assert _heard("cat", "look at the cats now") is None  # a name under four letters
    for phrase, spoken in [
        ("island", "this is land"),
        ("apart", "is a part"),
        ("Notion", "some notions"),
        ("Canva", "blank canvas"),
        ("news", "the new s class"),
    ]:
        assert _heard(phrase, f"so {spoken} here") is None, (phrase, spoken)
    for phrase, spoken in [
        ("TradingAgent", "so trading agents here"),
        ("Claude code game studio", "so claude code game studios here"),
        ("trading agents", "so TradingAgents here"),
    ]:
        assert _heard(phrase, spoken) is not None, (phrase, spoken)
    assert _heard("trading agents", "so here is TradingAgents again") == pytest.approx(
        3.26 + 0.26 * 2, abs=1e-6
    )
    # Last resort only: the exact hearing (later) wins over a loose one (earlier).
    words = _talk(
        [(0.0, "hello and welcome"), (3.0, "the game studios are big"), (8.0, "my game studio is")]
    )
    layout = _lay(["hello", "game studio"], words=words, take_s=12.0)
    second = next(w for w in layout.windows if w.index == 1)
    assert second.start_s == pytest.approx(_at(words, ["game", "studio"]), abs=1e-6)


# ------------------------------------------------------------------ S64


def _green3(**kw):
    entries = [
        seq.SequenceEntry(index=i, spoken_phrase=G3_PHRASES.get(media))
        for i, media in enumerate(G3_ORDER)
    ]
    return seq.lay_out(
        entries,
        cadence_s=G3_CADENCE_S,
        take_s=G3_TAKE_S,
        words=G3_WORDS,
        keep_segments=G3_KEEP,
        **kw,
    )


# Frozen from the code without item 17 (flag off; #2118's no-repeat layout): rows and notes.
# As typed, "TradingAgents" and "Claude code game studio" are never heard (recordings 3 and 5).
FROZEN_FIVE_REPOS_AS_SPOKEN = (
    [
        (0, 0.0, 26.52),
        (1, 26.52, 46.52),
        (2, 46.52, 66.26),
        (3, 66.26, 86.52),
        (4, 86.52, 104.86),
    ],
    [],
)
FROZEN_FIVE_REPOS_AS_TYPED = (
    [
        (0, 0.0, 26.52),
        (1, 26.52, 66.26),
        (3, 66.26, 104.86),
    ],
    [
        (("lane", "background"), ("reason", "phrase_not_heard"), ("window", 2)),
        (("lane", "background"), ("reason", "phrase_not_heard"), ("window", 4)),
    ],
)
FROZEN_GREEN3 = (
    [
        (0, 0.0, 8.106),
        (4, 8.106, 10.946),
        (1, 10.946, 15.346),
        (2, 15.346, 19.346),
        (3, 19.346, 30.259),
    ],
    [],
)


def test_flag_off_windows_and_notes_are_byte_identical(flag_off):
    five = _lay(AS_SPOKEN)
    typed = _lay(AS_SHE_TYPED)
    g3 = _green3()
    assert (
        _rows(five) == FROZEN_FIVE_REPOS_AS_SPOKEN[0]
        and _notes(five) == FROZEN_FIVE_REPOS_AS_SPOKEN[1]
    )
    assert (
        _rows(typed) == FROZEN_FIVE_REPOS_AS_TYPED[0]
        and _notes(typed) == FROZEN_FIVE_REPOS_AS_TYPED[1]
    )
    assert _rows(g3) == FROZEN_GREEN3[0] and _notes(g3) == FROZEN_GREEN3[1]


def test_the_flag_changes_only_how_a_phrase_is_heard(monkeypatch):
    """The flag only changes WHICH word anchors a phrase: Green 3, a talk named as spoken, a
    rotation and a partly phrased request lay out exactly as without it."""

    def both(run):
        monkeypatch.setattr(settings, "background_follows_talk_enabled", False)
        off = run()
        monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
        on = run()
        assert _rows(on) == _rows(off) and _notes(on) == _notes(off)
        assert on.cadence_s == off.cadence_s and on.anchored == off.anchored
        return on

    _contract(both(_green3), G3_TAKE_S)
    _contract(both(lambda: _lay(AS_SPOKEN)), TAKE_S)
    for phrases in (
        [None] * 6,
        ["spec kit", None, "impeccable", None, "number four agents"],
    ):
        _contract(both(lambda phrases=phrases: _lay(phrases)), TAKE_S)


# ------------------------------------------------------------------ S68


def test_a_name_that_only_occurs_inside_another_name_still_shows(monkeypatch):
    def run(**kw):
        entries = [
            seq.SequenceEntry(index=2, spoken_phrase="drones"),
            seq.SequenceEntry(index=3, spoken_phrase="mounted on drones"),
        ]
        return seq.lay_out(
            [seq.SequenceEntry(index=0, spoken_phrase="Sunrise Fashion Show"), *entries],
            cadence_s=G3_CADENCE_S,
            take_s=G3_TAKE_S,
            words=G3_WORDS,
            keep_segments=G3_KEEP,
            **kw,
        )

    monkeypatch.setattr(settings, "background_follows_talk_enabled", False)
    off = run()
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
    assert {w.index for w in off.windows} >= {2, 3}
    on = run()
    assert _rows(on) == _rows(off)
    assert not [n for n in on.notes if n.get("reason") == "phrase_not_heard"]


# ------------------------------------------------------------------ S65 (with KRI-961)


def test_a_sound_alike_with_a_plural_is_still_heard(flag_on):
    for spoken in (
        "last one is cloud code game studios",
        "last one is cloud code game studio",
    ):
        assert _heard("Claude code game studio", spoken) is not None, spoken
    # A single word never toggles its plural.
    assert _heard("Notion", "some notions") is None
