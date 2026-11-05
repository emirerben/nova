"""Qendresa item 19: numbered list labels on a phone Talking edit.

Failure modes: ``docs/reviews/qendresa-list-labels-failure-modes.md`` (Q2, 1-12 and 18-20).
Pure calls over her V5 labels and the recordings' phrases; no render, no network.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.recipes import Canvas
from app.kria.spoken_text_ask import SPOKEN_TEXT_CUE_KEYS
from app.pipeline.phone_list_labels import (
    HOLD_S,
    LABEL_Y_FRAC,
    LIST_LABEL_ELEMENT_PREFIX,
    LIST_LABEL_SOURCE,
    LabelAnchor,
    ListLabelAsk,
    ground_on_words,
    label_item,
    label_start,
    list_label_asks,
    list_label_rows,
    match_labels,
)
from app.pipeline.phone_subtitled_title import frame_face_box, place_talking_title
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services import phone_background_sequence as seq

_CANVAS = Canvas(width=1080, height=1920)
KEY = SPOKEN_TEXT_CUE_KEYS[0]

V5_LABELS = [
    "1. Spec-kit",
    "2. Impeccable",
    "3. TradingAgents",
    "4. Agents",
    "5. Claude code game studio",
]
V5_PHRASES = [
    "spec kit",
    "impeccable",
    "trading agents",
    "fourth one is agents",
    "cloud code game studios",
]


def _asks(literals=V5_LABELS) -> list[ListLabelAsk]:
    return [
        ListLabelAsk(f"r{n}", " ".join(text.split()), label_item(text))
        for n, text in enumerate(literals, 1)
    ]


def _anchors(times, *, window_starts=None, phrases=V5_PHRASES) -> list[LabelAnchor]:
    window_starts = window_starts or times
    return [
        LabelAnchor(index=i, spoken_phrase=p, anchor_s=t, window_start_s=w)
        for i, (p, t, w) in enumerate(zip(phrases, times, window_starts, strict=True))
    ]


def _words(script):
    """``[(start_s, "words ..."), ...]`` -> whisper-shaped words, 0.4 s each."""
    out = []
    for start, text in script:
        t = start
        for w in text.split():
            out.append({"text": w, "start_s": round(t, 2), "end_s": round(t + 0.35, 2)})
            t += 0.4
    return out


def _place(row, avoid):
    return dict(row), {"status": "preset", "chosen_y_frac": row["y_frac"], "avoid": len(avoid)}


def _avoid(window, rows):
    return []


def _rows(asks, anchors, *, words=(), duration=60.0, place=_place, avoid_for=_avoid):
    return list_label_rows(
        asks,
        anchors=anchors,
        words=list(words),
        timeline_duration_s=duration,
        canvas=_CANVAS,
        place=place,
        avoid_for=avoid_for,
    )


def _status(receipt) -> dict[str, str]:
    return {r["requirement_id"]: r["status"] for r in receipt["rows"]}


# --- 20 number formats -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("literal", "item"),
    [
        ("1. Spec-kit", "Spec-kit"),
        ("01: Life Admin Brain", "Life Admin Brain"),
        ("#1 Spec-kit", "Spec-kit"),
        ("1) X", "X"),
        ("  12.   Two   words ", "Two words"),
        ("Impeccable", "Impeccable"),
        ("1st place", "1st place"),
        ("2024 recap", "2024 recap"),
        ("3. 4. nested", "4. nested"),
    ],
)
def test_label_item_drops_one_leading_marker(literal, item):
    assert label_item(literal) == item


@pytest.mark.parametrize("literal", ["1.", "#3", "07:", "2)"])
def test_a_label_that_is_only_a_number_is_not_a_label(literal):
    assert label_item(literal) == ""
    req = BriefRequirement(id="x", kind="text", scope="global", literal=literal)
    assert list_label_asks([req]) == ()


# --- the asks ----------------------------------------------------------------------------


def _req(rid, literal, **kw):
    return BriefRequirement(id=rid, kind="text", scope="global", literal=literal, **kw)


def test_asks_are_live_global_texts_in_brief_order_without_a_spoken_trigger():
    reqs = [
        _req("a", "1. Spec-kit"),
        _req("b", "2. Impeccable", status="superseded"),
        BriefRequirement(id="c", kind="text", scope="clip:1", literal="3. Nope"),
        _req("d", ""),
        _req("e", "4. Agents", facts={KEY: "agents"}),
        _req("f", "comment REPO"),
    ]
    asks = list_label_asks(reqs)
    ids = [a.requirement_id for a in asks]
    assert ids == ["a", "f"]  # b superseded, c clip-scoped, d empty, e is KRI-707's


# --- 4/5 pairing -------------------------------------------------------------------------


def test_v5_labels_pair_one_to_one_in_order():
    matched = match_labels(_asks(), list(enumerate(V5_PHRASES)))
    assert matched == {"r1": 0, "r2": 1, "r3": 2, "r4": 3, "r5": 4}


def test_agents_never_takes_trading_agents():
    matched = match_labels(_asks(), list(enumerate(V5_PHRASES)))
    assert matched["r4"] != 2
    assert matched["r3"] == 2  # "TradingAgents" is "trading agents" (loose-equal) first


def test_label_five_pairs_with_the_sound_alike_plural_by_overlap():
    matched = match_labels(_asks()[4:], [(4, "cloud code game studios")])
    assert matched == {"r5": 4}


def test_an_entry_is_never_given_to_two_labels():
    matched = match_labels(_asks(["1. Spec-kit", "2. Spec kit"]), [(0, "spec kit")])
    assert matched == {"r1": 0}


def test_a_tie_goes_to_the_first_entry_after_the_previous_label():
    asks = _asks(["1. Impeccable", "2. Agents"])
    entries = [(0, "impeccable"), (1, "trading agents"), (2, "fourth one is agents")]
    assert match_labels(asks, entries) == {"r1": 0, "r2": 1}


def test_no_name_in_common_means_no_pairing():
    assert match_labels(_asks(["1. Spec-kit"]), [(0, "impeccable")]) == {}


def test_short_contained_names_do_not_pair():
    # "go" is under four letters: contained in "let's go play" is not a name.
    assert match_labels(_asks(["1. Go"]), [(0, "lets go play")]) == {}


# --- timing ------------------------------------------------------------------------------


def test_lead_in_window_does_not_pull_the_first_label_to_zero():
    anchors = _anchors([7.7, 20.0], window_starts=[0.0, 20.0], phrases=V5_PHRASES[:2])
    assert label_start(anchors[0]) == 7.7
    rows, receipt = _rows(_asks(V5_LABELS[:2]), anchors)
    assert rows[0]["start_s"] == 7.7
    assert _status(receipt) == {"r1": "placed", "r2": "placed"}


def test_back_to_back_names_take_the_window_start():
    anchor = LabelAnchor(index=1, spoken_phrase="impeccable", anchor_s=20.0, window_start_s=21.4)
    assert label_start(anchor) == 21.4


def test_a_window_that_starts_with_the_word_uses_the_anchor():
    anchor = LabelAnchor(index=1, spoken_phrase="impeccable", anchor_s=20.0, window_start_s=20.0)
    assert label_start(anchor) == 20.0


def test_labels_do_not_overlap_each_ends_at_the_next_start():
    anchors = _anchors([5.0, 6.5, 20.0, 30.0, 40.0])
    rows, _ = _rows(_asks(), anchors)
    assert [(r["start_s"], r["end_s"]) for r in rows] == [
        (5.0, 6.5),
        (6.5, 9.5),
        (20.0, 23.0),
        (30.0, 33.0),
        (40.0, 43.0),
    ]
    for a, b in zip(rows, rows[1:], strict=False):
        assert a["end_s"] <= b["start_s"]
        assert a["end_s"] - a["start_s"] <= HOLD_S


def test_a_label_in_the_last_second_is_a_sliver_and_says_so():
    anchors = _anchors([5.0, 10.0, 15.0, 20.0, 59.8])
    rows, receipt = _rows(_asks(), anchors, duration=60.0)
    assert _status(receipt)["r5"] == "no_time"
    assert len(rows) == 4


def test_a_label_near_the_take_end_stops_at_the_take_end():
    anchors = _anchors([5.0, 10.0, 15.0, 20.0, 58.5])
    rows, _ = _rows(_asks(), anchors, duration=60.0)
    assert rows[-1]["end_s"] == 60.0


def test_a_label_past_the_take_end_places_nothing():
    anchors = _anchors([5.0, 10.0, 15.0, 20.0, 61.0])
    _rows_, receipt = _rows(_asks(), anchors, duration=60.0)
    assert _status(receipt)["r5"] == "no_time"


def test_two_labels_at_the_same_moment_keep_the_earlier_one():
    asks = _asks(V5_LABELS[:2])
    anchors = _anchors([10.0, 10.0], phrases=V5_PHRASES[:2])
    rows, receipt = _rows(asks, anchors)
    assert _status(receipt) == {"r1": "placed", "r2": "same_moment"}
    assert [r["id"] for r in rows] == [LIST_LABEL_ELEMENT_PREFIX + "r1"]


def test_a_sliver_gap_before_the_next_label_drops_the_short_one():
    asks = _asks(V5_LABELS[:2])
    anchors = _anchors([10.0, 10.2], phrases=V5_PHRASES[:2])
    rows, receipt = _rows(asks, anchors)
    assert _status(receipt) == {"r1": "no_time", "r2": "placed"}
    assert rows[0]["start_s"] == 10.2


# --- 11 swept-in texts, 6 unanchored, 19 counts -------------------------------------------


def test_the_title_and_the_closing_text_place_nothing():
    asks = [
        ListLabelAsk(
            "title",
            "5 PROJECTS / the AI builder community",
            label_item("5 PROJECTS / the AI builder community"),
        ),
        *_asks(V5_LABELS[:2]),
        ListLabelAsk("cta", "comment REPO", "comment REPO"),
    ]
    anchors = _anchors([7.7, 20.0], phrases=V5_PHRASES[:2])
    words = _words([(1.0, "five projects the AI builder community comment repo")])
    rows, receipt = _rows(asks, anchors, words=words)
    assert _status(receipt) == {
        "title": "not_a_list_item",
        "r1": "placed",
        "r2": "placed",
        "cta": "not_a_list_item",
    }
    assert {r["id"] for r in rows} == {
        LIST_LABEL_ELEMENT_PREFIX + "r1",
        LIST_LABEL_ELEMENT_PREFIX + "r2",
    }


def test_a_list_label_nobody_introduced_says_not_heard():
    _rows_, receipt = _rows(_asks(V5_LABELS[:1]), [], words=_words([(1.0, "hello there")]))
    assert _status(receipt) == {"r1": "not_heard"}


def test_more_labels_than_recordings_place_the_matched_ones_only():
    anchors = _anchors([5.0, 12.0], phrases=V5_PHRASES[:2])
    _rows_, receipt = _rows(_asks(), anchors)
    status = _status(receipt)
    assert status["r1"] == status["r2"] == "placed"
    assert {status["r3"], status["r4"], status["r5"]} == {"not_heard"}


def test_two_labels_for_one_recording_place_one():
    asks = _asks(["1. Spec-kit", "2. Spec kit"])
    anchors = _anchors([5.0], phrases=["spec kit"])
    rows, receipt = _rows(asks, anchors)
    assert len(rows) == 1
    assert _status(receipt) == {"r1": "placed", "r2": "not_heard"}


def test_an_unmarked_text_that_names_a_recording_is_a_label():
    asks = [ListLabelAsk("a", "Impeccable", "Impeccable")]
    rows, receipt = _rows(asks, _anchors([5.0], phrases=["impeccable"]))
    assert len(rows) == 1 and _status(receipt) == {"a": "placed"}


def test_an_unmarked_text_that_only_contains_a_recordings_name_is_not_paired():
    """ "comment SPEC KIT" is a closing line that mentions the recording; only a text that IS
    the name (or carries a list marker) becomes a label."""
    asks = [ListLabelAsk("a", "comment SPEC KIT", "comment SPEC KIT")]
    assert match_labels(asks, [(0, "spec kit")]) == {}
    rows, receipt = _rows(asks, _anchors([5.0], phrases=["spec kit"]))
    assert rows == [] and _status(receipt) == {"a": "not_a_list_item"}


def test_an_unmarked_text_that_is_the_name_still_pairs():
    asks = [ListLabelAsk("a", "Spec Kit", "Spec Kit")]
    assert match_labels(asks, [(0, "spec kit")]) == {"a": 0}


# --- row look, 10 width, 8/9 placement, errors ---------------------------------------------


def test_row_is_an_editable_text_element_at_her_spot():
    rows, receipt = _rows(_asks(V5_LABELS[:1]), _anchors([5.0], phrases=V5_PHRASES[:1]))
    row = rows[0]
    assert row["id"] == "list-label-r1" and row["text"] == "1. Spec-kit"
    assert row["role"] == "generative_intro" and row["effect"] == "fade-in"
    assert row["y_frac"] == LABEL_Y_FRAC and row["x_frac"] == 0.5
    assert row["source_params"] == {"source": LIST_LABEL_SOURCE, "requirement_id": "r1"}
    assert "source_params" in row and not row["source_params"].get("read_only")


def test_a_long_label_gets_a_size_that_fits_the_width():
    short, _ = _rows(_asks(["1. Hi there"]), _anchors([5.0], phrases=["hi there"]))
    long_, _ = _rows(
        _asks(["5. Claude code game studio and its friends"]),
        _anchors([5.0], phrases=["claude code game studio and its friends"]),
    )
    assert long_[0]["size_px"] < short[0]["size_px"]


def test_avoid_is_asked_per_window_with_the_rows_so_far():
    seen = []

    def avoid_for(window, rows):
        seen.append((window, [r["id"] for r in rows]))
        return [NormalizedBox(0.1, 0.7, 0.9, 0.8)]

    def place(row, avoid):
        assert len(avoid) == 1
        return dict(row), {"status": "preset"}

    anchors = _anchors([5.0, 10.0], phrases=V5_PHRASES[:2])
    _rows(_asks(V5_LABELS[:2]), anchors, place=place, avoid_for=avoid_for)
    assert seen == [((5.0, 8.0), []), ((10.0, 13.0), ["list-label-r1"])]


def test_one_place_failure_is_that_asks_error_the_rest_continue():
    def place(row, avoid):
        if row["id"].endswith("r2"):
            raise RuntimeError("boom")
        return dict(row), {"status": "preset"}

    anchors = _anchors([5.0, 10.0, 15.0], phrases=V5_PHRASES[:3])
    rows, receipt = _rows(_asks(V5_LABELS[:3]), anchors, place=place)
    assert _status(receipt) == {"r1": "placed", "r2": "error", "r3": "placed"}
    assert len(rows) == 2
    assert "boom" in receipt["rows"][1]["error"]


def test_avoid_for_failure_is_that_asks_error():
    def avoid_for(window, rows):
        raise ValueError("no cues")

    _r, receipt = _rows(
        _asks(V5_LABELS[:1]), _anchors([5.0], phrases=V5_PHRASES[:1]), avoid_for=avoid_for
    )
    assert _status(receipt) == {"r1": "error"}


def test_receipt_shape():
    anchors = _anchors([5.0, 12.0], phrases=V5_PHRASES[:2])
    _rows_, receipt = _rows(_asks(V5_LABELS[:2]), anchors)
    assert receipt["version"] == 1
    first = receipt["rows"][0]
    assert first == {
        "requirement_id": "r1",
        "text": "1. Spec-kit",
        "item": "Spec-kit",
        "status": "placed",
        "basis": "background",
        "entry_index": 0,
        "start_s": 5.0,
        "end_s": 8.0,
        "element_id": "list-label-r1",
        "placement": {"status": "preset", "chosen_y_frac": LABEL_Y_FRAC, "avoid": 0},
        "face_collision": False,
    }


def test_a_best_effort_placement_on_the_face_is_reported():
    def place(row, avoid):
        return dict(row), {"status": "best_effort", "face_collision": True}

    _r, receipt = _rows(_asks(V5_LABELS[:1]), _anchors([5.0], phrases=V5_PHRASES[:1]), place=place)
    assert receipt["rows"][0]["face_collision"] is True


def test_a_placement_error_status_is_never_called_clear():
    def place(row, avoid):
        return dict(row), {"status": "error"}

    _r, receipt = _rows(_asks(V5_LABELS[:1]), _anchors([5.0], phrases=V5_PHRASES[:1]), place=place)
    assert receipt["rows"][0]["face_collision"] is True


# --- fallback: the take's own words --------------------------------------------------------

_FALLBACK_WORDS = _words(
    [
        (2.0, "first is spec kit"),
        (14.0, "second is impeccable"),
        (30.0, "third is trading agents"),
        (45.0, "it runs a whole trading firm as agents"),
        (70.0, "fourth one is agents"),
    ]
)


def test_fallback_grounds_in_list_order_and_skips_the_longer_name():
    asks = _asks(V5_LABELS[:4])
    grounded = ground_on_words(asks, _FALLBACK_WORDS)
    assert grounded["r1"] == pytest.approx(2.8, abs=0.5)
    assert grounded["r2"] == pytest.approx(14.8, abs=0.5)
    assert grounded["r3"] == pytest.approx(31.2, abs=0.5)
    # "agents" said right after "trading" belongs to label 3 (dropped), not label 4.
    trading_agents = (31.2, 32.0)
    assert not trading_agents[0] <= grounded["r4"] <= trading_agents[1]
    assert grounded["r4"] > grounded["r3"]


def test_fallback_honest_limit_it_takes_as_agents_before_fourth_one_is_agents():
    """Documented limit: the rule has no ordinal cue (KRI-667: no English intent words), so the
    first free "agents" after label 3 -- "...a whole trading firm as AGENTS" -- wins over the
    later "fourth one is agents". Recordings (the real path) never reach this."""
    grounded = ground_on_words(_asks(V5_LABELS[:4]), _FALLBACK_WORDS)
    as_agents = next(
        w["start_s"] for w in _FALLBACK_WORDS if w["text"] == "agents" and 45 < w["start_s"] < 50
    )
    assert grounded["r4"] == pytest.approx(as_agents)


def test_fallback_rows_use_the_words_and_say_so():
    asks = _asks(V5_LABELS[:3])
    rows, receipt = _rows(asks, [], words=_FALLBACK_WORDS, duration=100.0)
    assert [r["id"] for r in rows] == ["list-label-r1", "list-label-r2", "list-label-r3"]
    assert {r["basis"] for r in receipt["rows"]} == {"words"}


def test_fallback_starts_after_the_background_labels_before_it():
    # Labels 1-2 are on recordings; label 3 is not paired: it must be heard after label 2's time.
    anchors = _anchors([40.0, 50.0], phrases=V5_PHRASES[:2])
    words = _words([(5.0, "trading agents early"), (60.0, "trading agents again")])
    rows, receipt = _rows(_asks(V5_LABELS[:3]), anchors, words=words, duration=100.0)
    third = next(r for r in rows if r["id"].endswith("r3"))
    assert third["start_s"] >= 60.0
    assert receipt["rows"][2]["basis"] == "words"


def test_fallback_never_places_an_unmarked_text():
    asks = [ListLabelAsk("t", "trading agents", "trading agents")]
    rows, receipt = _rows(asks, [], words=_FALLBACK_WORDS, duration=100.0)
    assert rows == [] and _status(receipt) == {"t": "not_a_list_item"}


def test_fallback_hears_a_sound_alike_plural():
    words = _words([(10.0, "then cloud code game studios arrives")])
    got = ground_on_words(_asks(["5. Claude code game studio"]), words)
    assert got["r1"] == pytest.approx(10.4)


# --- the speaker frame -----------------------------------------------------------------------


def test_frame_face_box_maps_a_full_frame_face_into_the_small_speaker():
    frame = (0.42, 0.76, 0.80)  # small_bottom_right
    mapped = frame_face_box(NormalizedBox(0.35, 0.25, 0.65, 0.50), frame)
    assert (mapped.left, mapped.top, mapped.right, mapped.bottom) == pytest.approx(
        (0.697, 0.695, 0.823, 0.80), abs=1e-3
    )


def test_frame_face_box_full_width_frame_is_the_identity():
    box = NormalizedBox(0.2, 0.3, 0.6, 0.7)
    mapped = frame_face_box(box, (1.0, 0.5, 0.5))
    assert (mapped.left, mapped.top, mapped.right, mapped.bottom) == pytest.approx(
        (0.2, 0.3, 0.6, 0.7)
    )


def _title_row(y_frac):
    ask = _asks(["1. Spec-kit"])[0]
    rows, _ = _rows([ask], _anchors([5.0], phrases=["spec kit"]))
    return {**rows[0], "y_frac": y_frac}


def _sampler(region):
    def sample(path, anchors, **kwargs):
        return [region], {"attempted": len(anchors), "decoded": len(anchors), "detected": 1}

    return sample


def _place_face(row, **kw):
    face = ProtectedRegion(0.0, 10.0, NormalizedBox(0.35, 0.25, 0.65, 0.50), "face")
    return place_talking_title(
        row,
        clip_path="/tmp/speaker.mp4",
        keep_segments=[(0.0, 60.0)],
        display_width=1080,
        display_height=1920,
        canvas=_CANVAS,
        sample=_sampler(face),
        **kw,
    )


def test_the_placer_keeps_a_row_off_the_face_where_it_is_drawn():
    row = _title_row(0.74)
    plain, _ = _place_face(row)
    framed, receipt = _place_face(row, speaker_frame=(0.42, 0.76, 0.80))
    assert plain["y_frac"] == 0.74  # full-frame face is far above: nothing to avoid
    assert framed["y_frac"] != 0.74  # the drawn face sits at y 0.70-0.80: the row moves
    assert receipt["status"] in {"moved", "moved_below_face", "best_effort"}


def test_speaker_frame_none_leaves_the_placement_byte_identical():
    row = _title_row(0.74)
    assert _place_face(row, speaker_frame=None) == _place_face(row)


# --- the lay_out hook --------------------------------------------------------------------------


def test_lay_out_exposes_anchor_s_and_changes_no_window_or_note():
    words = _words([(1.0, "intro words"), (8.0, "spec kit is first"), (20.0, "then impeccable")])
    entries = [seq.SequenceEntry(0, "spec kit"), seq.SequenceEntry(1, "impeccable")]
    layout = seq.lay_out(entries, cadence_s=4.0, take_s=40.0, words=words, keep_segments=None)
    assert layout.anchor_s == {0: pytest.approx(8.0), 1: pytest.approx(20.4)}
    # Windows/notes are the same values the layout computed before the field existed.
    assert [(w.index, w.start_s) for w in layout.windows][:2] == [(0, 0.0), (1, 20.4)]
    assert layout.notes == ()
    # Not part of equality: a layout compares by windows/notes as before.
    bare = seq.SequenceLayout(
        windows=layout.windows,
        notes=layout.notes,
        cadence_s=layout.cadence_s,
        anchored=layout.anchored,
    )
    assert bare == layout and bare.anchor_s == {}


def test_lay_out_anchor_s_follows_the_talk_flag_too(monkeypatch):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
    words = _words([(8.0, "spec kit is first"), (20.0, "then impeccable")])
    entries = [seq.SequenceEntry(0, "spec kit"), seq.SequenceEntry(1, "impeccable")]
    layout = seq.lay_out(entries, cadence_s=4.0, take_s=40.0, words=words, keep_segments=None)
    assert set(layout.anchor_s) == set(layout.anchored) == {0, 1}


def test_lay_out_with_no_anchor_has_an_empty_anchor_s():
    layout = seq.lay_out(
        [seq.SequenceEntry(0, None)], cadence_s=4.0, take_s=10.0, words=[], keep_segments=None
    )
    assert layout.anchor_s == {}


# --- Q1: the rest of a spoken list (failure modes 21-27) ---------------------------------

V7_INTRO = (
    "let's build an agent that reads an app before i trust it because every app tells you "
    "what it does and none of them tell you where your data goes so the first thing i do "
    "is install ray then i give it one job for any site i send"
)
V7_LIST = (
    "tell me which companies it sends data to what it saves on my machine what it reads "
    "from my browser and what runs the moment the page loads that i never asked for and "
    "for each one where in the code i found it"
)
V7_ASK = ListLabelAsk("l1", "1. which company it sends data to?", "which company it sends data to?")


def _talk(*parts, step=0.2):
    """Words at a steady pace, one sentence after another with a 1 s breath between."""
    out, t = [], 0.0
    for part in parts:
        for w in part.split():
            out.append({"text": w, "start_s": round(t, 3), "end_s": round(t + step - 0.03, 3)})
            t += step
        t += 1.0
    return out


def _v7_rows(words, asks=(V7_ASK,), duration=60.0):
    return _rows(list(asks), [], words=words, duration=duration)


def _texts(rows):
    return [r["text"] for r in rows]


def test_tolerant_hearing_hears_company_as_companies():
    words = _talk(V7_INTRO, V7_LIST)
    assert ground_on_words([V7_ASK], words)  # the plural would not be heard before
    stream_start = next(w["start_s"] for w in words if w["text"] == "which")
    assert ground_on_words([V7_ASK], words)["l1"] == pytest.approx(stream_start)


def test_tolerant_hearing_needs_most_of_the_item():
    words = _talk("which company is that one then")
    assert ground_on_words([V7_ASK], words) == {}


def test_tolerant_hearing_never_hears_a_one_word_item():
    ask = ListLabelAsk("a", "1. Notion", "Notion")
    assert ground_on_words([ask], _talk("notions are fine")) == {}


def test_v7_spoken_list_gives_four_items_and_trims_the_and():
    words = _talk(V7_INTRO, V7_LIST)
    rows, receipt = _v7_rows(words)
    assert _texts(rows) == [
        "1. which company it sends data to?",
        "2. what it saves on my machine?",
        "3. what it reads from my browser?",
        "4. what runs the moment the page loads?",
    ]
    assert [r["id"] for r in rows] == [
        "list-label-l1",
        "list-label-l1-2",
        "list-label-l1-3",
        "list-label-l1-4",
    ]
    for row, nxt in zip(rows, rows[1:], strict=False):
        assert row["end_s"] == pytest.approx(nxt["start_s"])
    assert rows[-1]["end_s"] - rows[-1]["start_s"] == pytest.approx(HOLD_S)
    typed, *spoken = receipt["rows"]
    assert typed["list_found"] == 4 and typed["status"] == "placed"
    assert [(r["requirement_id"], r["list_item"], r["basis"]) for r in spoken] == [
        ("l1", 2, "spoken_list"),
        ("l1", 3, "spoken_list"),
        ("l1", 4, "spoken_list"),
    ]
    assert all(r["status"] == "placed" and r["element_id"] for r in spoken)


def test_the_last_item_is_cut_at_the_take_end():
    words = _talk(V7_INTRO, V7_LIST)
    last_start = next(w["start_s"] for w in words[::-1] if w["text"] == "what" and w["start_s"] > 0)
    rows, _ = _v7_rows(words, duration=last_start + 1.0)
    assert rows[-1]["end_s"] == pytest.approx(last_start + 1.0)


def test_a_pause_ends_the_last_item():
    spoken = (
        "tell me which companies it sends data to what it saves on my machine "
        "what it reads from my browser and what runs"
    )
    words = _talk(spoken, "the moment the page loads")  # a 1 s breath after "runs"
    rows, _ = _v7_rows(words)
    assert _texts(rows)[-1] == "4. what runs?"


def test_ordinary_talk_after_the_item_is_not_a_list():
    words = _talk(
        V7_INTRO,
        "which companies it sends data to is the scary part because nobody reads the policy "
        "and nobody checks the network tab so you simply never find out",
    )
    rows, receipt = _v7_rows(words)
    assert _texts(rows) == ["1. which company it sends data to?"]
    assert receipt["rows"][0]["list_found"] == 1


def test_the_intro_what_does_not_start_a_list_when_the_item_is_elsewhere():
    words = _talk(V7_INTRO, "so which companies it sends data to is the only question that matters")
    rows, receipt = _v7_rows(words)
    assert len(rows) == 1 and receipt["rows"][0]["list_found"] == 1


def test_two_items_alone_are_not_a_list():
    words = _talk("tell me which companies it sends data to what it saves on my machine ok done")
    rows, receipt = _v7_rows(words)
    assert len(rows) == 1 and receipt["rows"][0]["list_found"] == 1


def test_a_typed_list_is_never_extended():
    second = ListLabelAsk("l2", "2. what it saves on my machine?", "what it saves on my machine?")
    rows, receipt = _v7_rows(_talk(V7_INTRO, V7_LIST), asks=(V7_ASK, second))
    assert _texts(rows) == ["1. which company it sends data to?", "2. what it saves on my machine?"]
    assert all("list_item" not in r for r in receipt["rows"])


def test_only_a_label_numbered_one_starts_a_list():
    ask = ListLabelAsk(
        "l2", "2. which company it sends data to?", "which company it sends data to?"
    )
    rows, _ = _v7_rows(_talk(V7_INTRO, V7_LIST), asks=(ask,))
    assert len(rows) == 1


def test_a_bare_number_is_not_a_list_marker():
    ask = ListLabelAsk("l1", "1 which company it sends data to?", "which company it sends data to?")
    rows, _ = _v7_rows(_talk(V7_INTRO, V7_LIST), asks=(ask,))
    assert len(rows) <= 1


def test_a_closing_text_beside_the_one_marked_label_does_not_block_the_list():
    closing = ListLabelAsk("c", "comment read", "comment read")
    rows, _ = _v7_rows(_talk(V7_INTRO, V7_LIST), asks=(V7_ASK, closing))
    assert len(rows) == 4


def test_a_label_a_recording_names_is_not_extended():
    anchors = [
        LabelAnchor(index=0, spoken_phrase="which company", anchor_s=5.0, window_start_s=5.0)
    ]
    rows, _ = _rows([V7_ASK], anchors, words=_talk(V7_INTRO, V7_LIST))
    assert len(rows) == 1


def test_item_one_never_said_places_nothing():
    rows, receipt = _v7_rows(_talk(V7_INTRO, "what it saves on my machine what it reads from it"))
    assert rows == [] and receipt["rows"][0]["status"] == "not_heard"
    assert "list_found" not in receipt["rows"][0]


def test_a_turkish_list_with_the_same_opener_works_without_english_words():
    ask = ListLabelAsk("t1", "1. hangi şirkete gönderiyor?", "hangi şirkete gönderiyor?")
    words = _talk(
        "bir ajan yapıyorum",
        "bana hangi şirkete gönderiyor neyi kaydediyor neyi okuyor ve neyi çalıştırıyor",
    )
    rows, _ = _v7_rows(words, asks=(ask,))
    assert _texts(rows) == [
        "1. hangi şirkete gönderiyor?",
        "2. neyi kaydediyor?",
        "3. neyi okuyor?",
        "4. neyi çalıştırıyor?",
    ]


@pytest.mark.parametrize(
    ("example", "second"),
    [
        ("1. which a b?", "2. what x y?"),
        ("01: Which a b", "02: What x y"),
        ("#1 which a b!", "#2 what x y!"),
        ("1) Which a b.", "2) What x y."),
    ],
)
def test_marker_styles_and_case_follow_the_example(example, second):
    from app.pipeline.phone_list_labels import SpokenItem, spoken_item_text

    item = SpokenItem(1.0, ("What,", "x", "y!"))
    assert spoken_item_text(example, 2, item) == second


def test_the_list_is_capped_at_ten_items():
    ask = ListLabelAsk("l1", "1. alpha beta?", "alpha beta?")
    script = "alpha beta " + " ".join("next a b" for _ in range(14))
    rows, receipt = _v7_rows(_talk(script), asks=(ask,))
    assert len(rows) == 10
    assert receipt["rows"][0]["list_found"] == 10
