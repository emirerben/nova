"""KRI-1175: "behind me full screen whichever photo or video I'm talking about".

Built from the prod run "Green 3" (2026-11-03, job 9bad2d75, TestFlight 1876): the creator's real
whisper words (original take time), the real speech cut (three pauses, 0.964 s), the five real
Visuals plus the lunch decoy, and the planner's stored ``background_media``. Ground truth is the
test kit's table (``S2_brand_collab_greenscreen_short/README.md``), never the phrase a test feeds:

    IMG_7120 sunrise show      on "sunrise"
    IMG_7122 tablet            on "tablets"
    IMG_7123 drone photo       on "drones"
    IMG_7125 drone video       right after the drone photo
    IMG_7127 Instagram post    on "technology partner"
    IMG_7129 lunch (decoy)     never

The planner wrote no phrase (rotation, the bug); the fix teaches it to phrase every entry for this
ask, and lays an all-phrased sequence out "on the word". Failure modes S46-S56 in
``docs/reviews/speaker-background-windows-failure-modes.md``; every rule below is checked on the
VISIBLE background (windows plus the fill wherever no window covers the time).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import phone_background_sequence as seq
from app.services.client_protocol import clear_request_context
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
    _recipe,
)

# ------------------------------------------------------------------ the Green 3 run, verbatim

# `variants[0].overlay_transcript` of job 9bad2d75 (whisper words, original take time, 2 dp).
GREEN3_WORDS = [
    ('Okay,', 0.0, 0.3), ("we're", 0.36, 0.6), ('recording.', 0.6, 1.22), ('Curvan', 1.54, 1.72),
    ('celebrated', 1.72, 2.24), ('its', 2.24, 2.58), ('10th', 2.58, 2.98), ('year', 2.98, 3.14),
    ('with', 3.14, 3.32), ('a', 3.32, 3.56), ('Sunrise', 3.56, 3.96), ('Fashion', 3.96, 4.4),
    ('Show', 4.4, 4.7), ('at', 4.7, 4.94), ('Pamukkale.', 4.94, 5.46), ('And', 6.08, 6.3),
    ('it', 6.3, 6.54), ("wasn't", 6.54, 6.8), ('alone.', 6.8, 7.2), ('Lumo', 7.9, 7.9),
    ('was', 7.9, 8.2), ('the', 8.2, 8.4), ("show's", 8.4, 8.7), ('technology', 8.7, 9.26),
    ('partner.', 9.26, 9.6), ('The', 10.14, 10.24), ('patterns', 10.24, 10.52),
    ('were', 10.52, 10.86), ('drawn', 10.86, 11.12), ('on', 11.12, 11.54), ('Lumo', 11.54, 11.7),
    ('tablets,', 11.7, 12.18), ('and', 12.44, 12.64), ('the', 12.64, 12.74),
    ('whole', 12.74, 13.08), ('show', 13.08, 13.2), ('was', 13.2, 13.88),
    ('filmed', 13.88, 13.88), ('on', 13.88, 14.26), ('Lumo', 14.26, 14.54),
    ('phones,', 14.54, 14.98), ('mounted', 15.3, 15.44), ('on', 15.44, 15.94),
    ('drones.', 15.94, 16.28), ('So', 16.9, 17.24), ('Lumo', 17.24, 17.46),
    ('took', 17.46, 17.68), ('its', 17.68, 17.88), ('tech', 17.88, 18.12), ('from', 18.12, 18.38),
    ('cold', 18.38, 18.66), ('hard...', 18.66, 19.26), ('No,', 19.26, 19.52),
    ('again.', 19.52, 19.8), ('So', 20.26, 20.5), ('Lumo', 20.5, 20.82), ('took', 20.82, 21.02),
    ('its', 21.02, 21.22), ('tech', 21.22, 21.44), ('from', 21.44, 21.68), ('cold', 21.68, 22.0),
    ('hardware', 22.0, 22.46), ('to', 22.46, 22.9), ('a', 22.9, 23.2), ('creative', 23.2, 23.46),
    ('space', 23.46, 23.84), ('that', 23.84, 24.48), ('feeds', 24.48, 24.48),
    ('fashion.', 24.48, 24.86), ('In', 25.06, 25.58), ('short,', 25.58, 25.8),
    ("it's", 25.9, 26.16), ('a', 26.16, 26.28), ('real', 26.28, 26.5), ('1', 26.5, 26.84),
    ('plus', 26.84, 27.04), ('1', 27.04, 27.38), ('equals', 27.38, 27.7), ('3', 27.7, 28.08),
    ('story.', 28.08, 28.58), ('So', 28.58, 29.32), ('what', 29.32, 29.58), ('did', 29.58, 29.72),
    ('you', 29.72, 29.92), ('think', 29.92, 30.12), ('of', 30.12, 30.3), ('this', 30.3, 30.48),
    ('collaboration?', 30.48, 30.98),
]  # fmt: skip
WORDS = [{"text": t, "start_s": s, "end_s": e} for t, s, e in GREEN3_WORDS]
TAKE_FULL_S = 31.223333
# `variants[0].silence_cut.removed` of the same job.
REMOVED = [(5.585, 5.955), (7.325, 7.549), (16.405, 16.775)]
KEEP = [(0.0, 5.585), (5.955, 7.325), (7.549, 16.405), (16.775, TAKE_FULL_S)]
TAKE_S = seq.kept_duration(KEEP, TAKE_FULL_S)  # 30.259 s, the finished edit
CADENCE_S = 4.0  # "changing every 3-5 seconds" -> the midpoint

# The item's ready Visuals pool (bare pool-row ids, as `_load_ready_pool_assets` returns them).
SUNRISE = "7ea1e9bd-1c14-43fc-8e0a-ff9a97576f2b"  # IMG_7120
TABLET = "f4c22bc6-96f9-4b63-8673-43701f64cd03"  # IMG_7122
DRONE_PHOTO = "74c10cff-cbe5-4583-86d3-4272352a4966"  # IMG_7123
DRONE_VIDEO = "c26e289e-0041-442c-bccf-583584f03f66"  # IMG_7125 (5 s video)
COLLAB_POST = "46ad3cb7-6d2f-4685-83f2-77108c684f25"  # IMG_7127
LUNCH = "e063d6e8-282c-48f3-8814-e9344e0eb5a3"  # IMG_7129, the decoy
NAMES = {
    SUNRISE: "IMG_7120",
    TABLET: "IMG_7122",
    DRONE_PHOTO: "IMG_7123",
    DRONE_VIDEO: "IMG_7125",
    COLLAB_POST: "IMG_7127",
    LUNCH: "IMG_7129",
}

# The planner's stored `background_media` (job.all_candidates.creator_strategy), verbatim: the
# five in filename order, no phrase, no cadence -- the rotation the creator did not ask for.
STORED_ORDER = [SUNRISE, TABLET, DRONE_PHOTO, DRONE_VIDEO, COLLAB_POST]
STORED_BACKGROUND = {
    "media_id": f"asset-{SUNRISE}",
    "sequence": [{"media_id": f"asset-{media}"} for media in STORED_ORDER],
    "speaker_frame": "small_bottom_right",
}
# What the v114 windows paragraph asks the planner for on this input: every entry phrased with
# the take's own words; both drone visuals on the one mention of drones. Kept in the STORED
# (filename) order on purpose: the layout must follow the talk, not the list.
PHRASES = {
    SUNRISE: "Sunrise Fashion Show",
    TABLET: "Lumo tablets",
    DRONE_PHOTO: "drones",
    DRONE_VIDEO: "drones",
    COLLAB_POST: "technology partner",
}
ON_THE_WORD_BACKGROUND = {
    **STORED_BACKGROUND,
    "sequence": [
        {"media_id": f"asset-{media}", "spoken_phrase": PHRASES[media]} for media in STORED_ORDER
    ],
    "cadence_s": CADENCE_S,
}

# Kit ground truth: the word each visual belongs on (the first time it is said).
GROUND_TRUTH_WORD = {
    SUNRISE: "sunrise",
    COLLAB_POST: "technology",
    TABLET: "tablets",
    DRONE_PHOTO: "drones",
}
WORD_TOLERANCE_S = 1.5
# "Lumo tablets" is first heard on the "Lumo" right before "tablets," (11.54 s, original time).
LUMO_TABLETS_SOURCE_S = 11.54


def _word_source_s(word: str) -> float:
    for text, start, _end in GREEN3_WORDS:
        if text.strip(".,?!").casefold() == word:
            return start
    raise AssertionError(f"{word!r} is not in the Green 3 words")


def _word_output_s(word: str) -> float:
    at = seq.to_output_time(_word_source_s(word), KEEP)
    assert at is not None
    return at


# ------------------------------------------------------------------ layout helpers + the check


def _lay_out(order, phrases=None, *, holds=None, words=WORDS, keep=KEEP, take_s=TAKE_S, **kw):
    """`lay_out` exactly as the worker calls it, over ``order`` (pool ids); windows by media."""
    phrases = phrases or {}
    holds = holds or {}
    layout = seq.lay_out(
        [
            seq.SequenceEntry(index=i, spoken_phrase=phrases.get(media), hold_s=holds.get(media))
            for i, media in enumerate(order)
        ],
        cadence_s=kw.get("cadence_s", CADENCE_S),
        take_s=take_s,
        words=words,
        keep_segments=keep,
    )
    return layout, [(order[w.index], w.start_s, w.end_s) for w in layout.windows]


def _visible(windows, *, fill, take_s=TAKE_S):
    """What is on screen behind the speaker, start to end: each window, the fill in every gap,
    neighbours showing the same visual merged (output seconds)."""
    shown: list[list] = []

    def add(media, start, end):
        if end - start <= 1e-6:
            return
        if shown and shown[-1][0] == media and abs(shown[-1][2] - start) <= 1e-6:
            shown[-1][2] = end
        else:
            shown.append([media, start, end])

    t = 0.0
    for media, start, end in windows:
        add(fill, t, start)
        add(media, start, end)
        t = end
    add(fill, t, take_s)
    return [tuple(row) for row in shown]


def _first_window_start(windows, media):
    starts = [start for m, start, _end in windows if m == media]
    assert starts, f"{NAMES[media]} never gets a window"
    return min(starts)


def _assert_on_their_words(windows, *, keep=KEEP):
    """The kit's acceptance, against its ground truth (never the phrases fed in):

    * the opening visual is on screen from 0 s through its word (something must be behind the
      speaker before the first mention; the talk opens on it);
    * every other word-anchored visual's first window starts within 1.5 s of its word;
    * the drone video starts exactly where the drone photo's first window ends;
    * the decoy never has a window.
    """
    for media, word in GROUND_TRUTH_WORD.items():
        at = seq.to_output_time(_word_source_s(word), keep) if keep else _word_source_s(word)
        start = _first_window_start(windows, media)
        if start == pytest.approx(0.0, abs=1e-6):
            covering = [w for w in windows if w[0] == media and w[1] <= at < w[2]]
            assert covering, f"{NAMES[media]} opens the video but is gone by {word!r} ({at:.2f})"
        else:
            assert abs(start - at) <= WORD_TOLERANCE_S, (
                f"{NAMES[media]} starts at {start:.2f}, its word {word!r} is at {at:.2f}"
            )
    photo = next(w for w in windows if w[0] == DRONE_PHOTO)
    video = next((w for w in windows if w[0] == DRONE_VIDEO), None)
    assert video is not None, "the drone video never shows"
    assert video[1] == pytest.approx(photo[2], abs=1e-6), (
        f"the drone video ({video[1]:.2f}) does not follow the drone photo ({photo[2]:.2f})"
    )
    assert LUNCH not in {w[0] for w in windows}, "the lunch decoy is shown"


def _assert_phone_contract(windows, *, take_s=TAKE_S):
    """S55: what `SpeakerBackgroundLayer` / SpeakerBackground.swift accept."""
    assert 1 <= len(windows) <= seq.MAX_WINDOWS
    for _m, start, end in windows:
        assert 0.0 <= start < end <= take_s + 1e-6
        assert end - start >= seq.MIN_WINDOW_S - 1e-6, f"window {start:.2f}-{end:.2f} too short"
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert later[1] >= earlier[2] - 1e-6, "windows overlap"


def _describe(windows) -> str:
    return " | ".join(f"{NAMES.get(m, m)} {s:.2f}-{e:.2f}" for m, s, e in windows)


# ------------------------------------------------------------------ S54: nothing else changes


def test_green3_stored_strategy_still_rotates_exactly_as_before():
    """The planner's stored strategy (no phrase) is a rotation, byte-for-byte what prod drew:
    eight 4 s windows in list order, repeating (`phone_lane_receipt.background.windows == 8`)."""
    layout, windows = _lay_out(STORED_ORDER)

    assert layout.notes == ()
    assert [(NAMES[m], round(s, 3), round(e, 3)) for m, s, e in windows] == [
        ("IMG_7120", 0.0, 4.0),
        ("IMG_7122", 4.0, 8.0),
        ("IMG_7123", 8.0, 12.0),
        ("IMG_7125", 12.0, 16.0),
        ("IMG_7127", 16.0, 20.0),
        ("IMG_7120", 20.0, 24.0),
        ("IMG_7122", 24.0, 28.0),
        ("IMG_7123", 28.0, round(TAKE_S, 3)),
    ]


# ------------------------------------------------------------------ S47/S48: on the word


def test_green3_each_visual_starts_on_its_word():
    layout, windows = _lay_out(STORED_ORDER, PHRASES)

    assert not [n for n in layout.notes if n.get("reason") == "window_too_short"], layout.notes
    _assert_on_their_words(windows)
    _assert_phone_contract(windows)
    # The talk's order, not the list's: the Instagram post (listed last) is talked about second.
    first_seen = sorted(
        {m: _first_window_start(windows, m) for m in PHRASES}.items(), key=lambda kv: kv[1]
    )
    assert [NAMES[m] for m, _s in first_seen] == [
        "IMG_7120",
        "IMG_7127",
        "IMG_7122",
        "IMG_7123",
        "IMG_7125",
    ], _describe(windows)


def test_green3_windows_land_on_the_words_in_the_original_take_too():
    """The lane stores ORIGINAL take time (`remap_lanes_for_cut` moves it later): converted
    back, each anchored start is the source time of its word."""
    _layout, windows = _lay_out(STORED_ORDER, PHRASES)
    source = [
        (m, seq.to_source_time(s, KEEP), seq.to_source_time(e, KEEP, as_end=True))
        for m, s, e in windows
    ]
    _assert_on_their_words(source, keep=None)


# ------------------------------------------------------------------ S49/S50/S51: around the words


def test_green3_nothing_repeats_before_every_visual_was_shown():
    """No visual comes back (as a window or as the fill) while another still has its first
    showing ahead, and nothing on screen lasts under half a second."""
    _layout, windows = _lay_out(STORED_ORDER, PHRASES)
    visible = _visible(windows, fill=SUNRISE)

    seen: list[str] = []
    for media, _start, _end in visible:
        if len(set(seen)) == len(PHRASES):
            break
        assert media not in seen, f"{NAMES[media]} repeats too early: {_describe(visible)}"
        seen.append(media)
    assert set(seen) == set(PHRASES), f"not every visual is shown: {_describe(visible)}"
    assert all(end - start >= seq.MIN_WINDOW_S - 1e-6 for _m, start, end in visible), (
        f"a flash under 0.5 s: {_describe(visible)}"
    )


def test_the_opening_visual_covers_the_lead_in():
    """Before the first mention the first-talked-about visual is already behind the speaker:
    the fill (which repeats a visual) never shows on an all-phrased sequence without a hold."""
    _layout, windows = _lay_out(STORED_ORDER, PHRASES)
    assert windows[0][1] == pytest.approx(0.0, abs=1e-6)
    assert windows[-1][2] == pytest.approx(TAKE_S, abs=1e-6)
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert later[1] == pytest.approx(earlier[2], abs=1e-6), f"gap: {_describe(windows)}"

    # A mention 0.36 s in ("we're recording") starts at 0 too, never after a fill sliver.
    _layout, short = _lay_out(
        [TABLET, COLLAB_POST], {TABLET: "we're recording", COLLAB_POST: "technology partner"}
    )
    assert short[0] == (
        TABLET,
        pytest.approx(0.0, abs=1e-6),
        pytest.approx(_word_output_s("technology"), abs=1e-6),
    )


def test_green3_the_tail_keeps_changing_at_the_cadence():
    """After the last mention ("drones", 15.3 s into a 30.3 s edit) the visuals keep taking
    turns every ~4 s to the end, in the order they were first shown, never one held for 11 s."""
    _layout, windows = _lay_out(STORED_ORDER, PHRASES)
    visible = _visible(windows, fill=SUNRISE)

    all_shown_at = max(_first_window_start(windows, m) for m in PHRASES)
    tail = [row for row in visible if row[1] >= all_shown_at - 1e-6]
    assert tail[-1][2] == pytest.approx(TAKE_S, abs=1e-6)
    longest = CADENCE_S + max(seq.MIN_WINDOW_S, CADENCE_S / 2.0)  # cadence + an absorbed sliver
    for media, start, end in tail:
        assert end - start <= longest + 1e-6, (
            f"{NAMES[media]} held {end - start:.1f} s: {_describe(tail)}"
        )
    assert len(tail) >= 3, f"the tail does not keep changing: {_describe(tail)}"
    first_order = [
        m
        for m, _s in sorted(
            ((m, _first_window_start(windows, m)) for m in PHRASES), key=lambda kv: kv[1]
        )
    ]
    after_last = [media for media, _s, _e in tail[1:]]
    assert after_last == [first_order[i % len(first_order)] for i in range(len(after_last))], (
        f"the tail does not take turns in first-shown order: {_describe(tail)}"
    )


def test_a_long_tail_stretches_instead_of_passing_forty_windows():
    """A ten-minute take whose mentions all come in the first 16 s: the tail's cadence stretches
    so the recipe stays within 40 windows, and the last window still ends at the take end."""
    long_take = 600.0
    layout, windows = _lay_out(STORED_ORDER, PHRASES, keep=None, take_s=long_take, cadence_s=2.0)

    _assert_phone_contract(windows, take_s=long_take)
    assert windows[-1][2] == pytest.approx(long_take, abs=1e-6)
    assert {"lane": "background", "reason": "cadence_stretched"} in layout.notes
    _assert_on_their_words(windows, keep=None)


def test_an_explicit_hold_still_ends_the_window_early():
    """A creator-asked length (`hold_s`) ends the window; the fill shows until the next mention,
    unless what is left would only be a sliver, which the window absorbs."""
    post_at = _word_output_s("technology")
    tablets_at = seq.to_output_time(LUMO_TABLETS_SOURCE_S, KEEP)

    _layout, held = _lay_out(STORED_ORDER, PHRASES, holds={COLLAB_POST: 1.5})
    post = next(w for w in held if w[0] == COLLAB_POST)
    assert post[1:] == (pytest.approx(post_at, abs=1e-6), pytest.approx(post_at + 1.5, abs=1e-6))
    assert next(w for w in held if w[0] == TABLET)[1] == pytest.approx(tablets_at, abs=1e-6)

    _layout, sliver = _lay_out(STORED_ORDER, PHRASES, holds={COLLAB_POST: 2.6})
    post = next(w for w in sliver if w[0] == COLLAB_POST)
    assert post[2] == pytest.approx(tablets_at, abs=1e-6), (
        f"a {tablets_at - post_at - 2.6:.2f} s fill flash was left: {_describe(sliver)}"
    )


def test_entries_sharing_a_mention_that_cannot_all_fit_drop_the_last_with_a_note():
    """Seven visuals on "technology partner" with "Lumo tablets" 2.84 s later: they share the
    span in list order, half a second at least each; the ones that cannot fit are dropped and
    each drop is on the receipt."""
    shared = [f"00000000-0000-4000-8000-00000000000{i}" for i in range(7)]
    order = [*shared, TABLET]
    phrases = {**{media: "technology partner" for media in shared}, TABLET: "Lumo tablets"}
    layout, windows = _lay_out(order, phrases)

    _assert_phone_contract(windows)
    in_group = [m for m, _s, _e in windows if m in shared]
    assert len(in_group) >= 2, f"the visuals sharing a mention lost all but one: {windows}"
    assert in_group == shared[: len(in_group)], "the shared mention does not keep list order"
    dropped = {n["window"] for n in layout.notes if n.get("reason") == "window_too_short"}
    assert dropped == set(range(len(in_group), len(shared)))
    assert next(w for w in windows if w[0] == TABLET)[1] == pytest.approx(
        seq.to_output_time(LUMO_TABLETS_SOURCE_S, KEEP), abs=1e-6
    )


# ------------------------------------------------------------------ S52/S56: wrong content


def test_a_phrase_never_heard_is_not_cycled_in():
    """The planner wrongly adds the lunch decoy with a phrase nobody says: it never shows, the
    receipt says why, and the others stay on their words."""
    order = [*STORED_ORDER, LUNCH]
    layout, windows = _lay_out(order, {**PHRASES, LUNCH: "grilled chicken lunch"})

    assert {"lane": "background", "window": 5, "reason": "phrase_not_heard"} in layout.notes
    assert LUNCH not in {m for m, _s, _e in windows}, (
        f"the decoy is cycled in: {_describe(windows)}"
    )
    _assert_on_their_words(windows)
    _assert_phone_contract(windows)


def test_the_word_check_rejects_swapped_photos():
    """Negative control: the tablet photo on "drones" and the drone photo on "Lumo tablets" keep
    every count, length and order the check could miss -- the check must still fail."""
    swapped = {
        **PHRASES,
        TABLET: "drones",
        DRONE_PHOTO: "Lumo tablets",
        DRONE_VIDEO: "Lumo tablets",
    }
    _layout, windows = _lay_out(STORED_ORDER, swapped)
    with pytest.raises(AssertionError):
        _assert_on_their_words(windows)


# ------------------------------------------------------------------ S53/S55: the whole worker


def _pool_rows():
    def row(media, kind, filename, **extra):
        return {
            "id": media,
            "gcs_path": f"users/u1/plan/item/pool/{media}.{'mp4' if kind == 'video' else 'jpg'}",
            "gcs_generation": "7",
            "kind": kind,
            "source_filename": filename,
            "duration_s": 5.0 if kind == "video" else None,
            "aspect": 0.5625,
            "user_context": "",
            "analysis": extra.get("analysis", {}),
            "capture": None,
        }

    return [
        row(TABLET, "image", "0126B894-IMG_7122.jpeg"),
        row(COLLAB_POST, "image", "7AEE5F83-IMG_7127.png"),
        row(DRONE_PHOTO, "image", "73D5CA60-IMG_7123.jpeg"),
        row(DRONE_VIDEO, "video", "A3145EFA-IMG_7125.mp4"),
        row(SUNRISE, "image", "11376E9F-IMG_7120.jpeg"),
        # The decoy carries its real analysis (the overlay matcher's input in job 9bad2d75).
        row(
            LUNCH,
            "image",
            "2066D9C0-IMG_7129.jpeg",
            analysis={
                "subject": "Grilled chicken breast meal",
                "description": (
                    "A plate featuring sliced grilled chicken breast, a dome of white rice "
                    "garnished with chives, and a fresh side salad with lettuce, carrots, and a "
                    "tomato slice, placed next to a glass of ice water on a wooden table."
                ),
            },
        ),
    ]


def _green3_job(monkeypatch, background):
    import app.services.phone_overlay_grounding as ogm
    import app.services.phone_reaction_grounding as rgm
    import app.services.phone_visuals as phone_visuals_mod
    from app.pipeline.transcribe import Word
    from app.services.phone_sources import PhoneVisualBinding
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled

    rows = {row["id"]: row for row in _pool_rows()}

    def bind(_open, *, job_id, pins):  # noqa: ARG001
        out = []
        for media in pins:
            row = rows[media]
            video = (
                {"duration_s": 5.0, "width": 1080, "height": 1920, "orientation_degrees": 0}
                if row["kind"] == "video"
                else {}
            )
            out.append(
                PhoneVisualBinding(
                    media_id=media,
                    gcs_path=row["gcs_path"],
                    generation="7",
                    sha256="a" * 64,
                    byte_count=100,
                    kind=row["kind"],
                    **video,
                )
            )
        return tuple(out)

    job, _snapshot, _session, _binding = _setup_subtitled(
        monkeypatch,
        transcript_words=[
            Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in GREEN3_WORDS
        ],
        duration_s=TAKE_FULL_S,
    )
    _enable_beats(monkeypatch, extra_features=("visualVideos", "authoredText"))
    monkeypatch.setattr(gb.settings, "phone_subtitled_video_overlays_enabled", True)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", bind)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    monkeypatch.setattr(rgm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    clear_request_context()
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [
            *gb.settings.phone_render_verified_features,
            "speakerBackground",
            "speakerBackgroundWindows",
        ],
    )
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "background_media": background,
    }
    return job


def test_green3_worker_seats_the_backgrounds_on_their_words(monkeypatch):
    """The whole chain: approved strategy -> the item's ready pool (with the decoy) -> layout ->
    the phone recipe (validated by `SpeakerBackgroundLayer`, the parity contract). No speech cut
    here, so recipe time is the take's own time."""
    job = _green3_job(monkeypatch, ON_THE_WORD_BACKGROUND)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    recipe = _recipe(job)
    layer = recipe.speaker_background
    assert layer is not None and layer.windows, "no background windows reached the recipe"
    windows = [(w.asset_id.removeprefix("visual-"), w.start, w.end) for w in layer.windows]
    _assert_on_their_words(windows, keep=None)
    _assert_phone_contract(windows, take_s=recipe.duration)
    assert layer.speaker_frame is not None

    # The decoy is nowhere: not a window, not the fill, not a card on any track.
    assert layer.asset_id != f"visual-{LUNCH}"
    assert f"visual-{LUNCH}" not in {
        clip.source_asset_id for track in recipe.tracks for clip in track.clips
    }
    receipt = job.assembly_plan["variants"][0]["phone_lane_receipt"]
    assert "background" in receipt["applied"]
    assert not [d for d in receipt["dropped"] if d.get("lane") == "background"], receipt
