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

import json
from pathlib import Path

import pytest

from app.config import settings
from app.services import phone_background_sequence as seq
from app.services.client_protocol import clear_request_context
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
    _recipe,
)

_FIXTURE = "green3_9bad2d75_backgrounds_on_word.json"

# ------------------------------------------------------------------ the Green 3 run, verbatim

# The incident record (prod job 9bad2d75) and the kit's answer key, kept apart from this code.
GREEN3 = json.loads((Path(__file__).parents[1] / "fixtures" / _FIXTURE).read_text())
# `variants[0].overlay_transcript` (whisper words, original take time, 2 dp).
GREEN3_WORDS = [tuple(word) for word in GREEN3["words"]]
WORDS = [{"text": t, "start_s": s, "end_s": e} for t, s, e in GREEN3_WORDS]
TAKE_FULL_S = GREEN3["take_duration_s"]
# `variants[0].silence_cut.removed`: three pauses, 0.964 s.
REMOVED = [tuple(span) for span in GREEN3["silence_cut_removed"]]
KEEP = list(
    zip([0.0, *(e for _s, e in REMOVED)], [*(s for s, _e in REMOVED), TAKE_FULL_S], strict=True)
)
TAKE_S = seq.kept_duration(KEEP, TAKE_FULL_S)  # 30.259 s, the finished edit
CADENCE_S = 4.0  # "changing every 3-5 seconds" -> the midpoint

# The item's ready Visuals pool (bare pool-row ids, as `_load_ready_pool_assets` returns them).
KIT = {visual["file"]: visual for visual in GREEN3["kit_answer_key"]["visuals"]}
SUNRISE = KIT["IMG_7120"]["pool_id"]
TABLET = KIT["IMG_7122"]["pool_id"]
DRONE_PHOTO = KIT["IMG_7123"]["pool_id"]
DRONE_VIDEO = KIT["IMG_7125"]["pool_id"]  # 5 s video
COLLAB_POST = KIT["IMG_7127"]["pool_id"]
LUNCH = KIT["IMG_7129"]["pool_id"]  # the decoy
NAMES = {visual["pool_id"]: name for name, visual in KIT.items()}

# The planner's stored `background_media` (job.all_candidates.creator_strategy), verbatim: the
# five in filename order, no phrase, no cadence -- the rotation the creator did not ask for.
STORED_BACKGROUND = GREEN3["stored_background_media"]
STORED_ORDER = [entry["media_id"].removeprefix("asset-") for entry in STORED_BACKGROUND["sequence"]]
# The stored fill (one of the five photos): since S57 the layout never lets it repeat a photo.
STORED_FILL = STORED_BACKGROUND["media_id"].removeprefix("asset-")
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

# Kit answer key: the word each visual belongs on (the first time it is said), the visual that
# must follow another, the visuals never shown.
GROUND_TRUTH_WORD = {v["pool_id"]: v["on_word"] for v in KIT.values() if v.get("on_word")}
RIGHT_AFTER = {
    v["pool_id"]: KIT[v["right_after"]]["pool_id"] for v in KIT.values() if v.get("right_after")
}
NEVER = {v["pool_id"] for v in KIT.values() if v.get("never")}
WORD_TOLERANCE_S = GREEN3["kit_answer_key"]["tolerance_s"]
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


def _lay_out(
    order,
    phrases=None,
    *,
    holds=None,
    words=WORDS,
    keep=KEEP,
    take_s=TAKE_S,
    fill=STORED_FILL,
    **kw,
):
    """`lay_out` exactly as the worker calls it, over ``order`` (pool ids); windows by media."""
    phrases = phrases or {}
    holds = holds or {}
    layout = seq.lay_out(
        [
            seq.SequenceEntry(
                index=i, spoken_phrase=phrases.get(media), hold_s=holds.get(media), media_key=media
            )
            for i, media in enumerate(order)
        ],
        cadence_s=kw.get("cadence_s", CADENCE_S),
        take_s=take_s,
        words=words,
        keep_segments=keep,
        fill_key=fill,
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
    """The kit's acceptance, against its answer key (never the phrases fed in):

    * the visual whose word is said first may open the video: on screen from 0 s through its word
      (something must be behind the speaker before the first mention);
    * every other word-anchored visual's first window starts within 1.5 s of its word;
    * a "right after" visual starts exactly where its leader's first window ends;
    * a "never" visual has no window.
    """
    heard_at = {
        media: seq.to_output_time(_word_source_s(word), keep) if keep else _word_source_s(word)
        for media, word in GROUND_TRUTH_WORD.items()
    }
    opener = min(heard_at, key=heard_at.get)
    for media, at in heard_at.items():
        word = GROUND_TRUTH_WORD[media]
        start = _first_window_start(windows, media)
        if media == opener and start == pytest.approx(0.0, abs=1e-6):
            covering = [w for w in windows if w[0] == media and w[1] <= at < w[2]]
            assert covering, f"{NAMES[media]} opens the video but is gone by {word!r} ({at:.2f})"
        else:
            assert abs(start - at) <= WORD_TOLERANCE_S, (
                f"{NAMES[media]} starts at {start:.2f}, its word {word!r} is at {at:.2f}"
            )
    for follower, leader in RIGHT_AFTER.items():
        lead = next(w for w in windows if w[0] == leader)
        follow = next((w for w in windows if w[0] == follower), None)
        assert follow is not None, f"{NAMES[follower]} never shows"
        assert follow[1] == pytest.approx(lead[2], abs=1e-6), (
            f"{NAMES[follower]} ({follow[1]:.2f}) does not follow {NAMES[leader]} ({lead[2]:.2f})"
        )
    shown_never = NEVER & {w[0] for w in windows}
    assert not shown_never, f"shown although never talked about: {[NAMES[m] for m in shown_never]}"


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


def test_green3_stored_strategy_shows_each_photo_once():
    """The planner's stored strategy (no phrase) is a rotation. Prod (9bad2d75) drew eight 4 s
    windows in list order, repeating three photos; since S57 (2026-11-05, S4 "fb 1": "the same
    photo never comes up twice") each photo shows once, in list order, stretched to the end."""
    layout, windows = _lay_out(STORED_ORDER)

    assert layout.notes == ({"lane": "background", "reason": "cadence_stretched"},)
    each = TAKE_S / len(STORED_ORDER)
    assert [(NAMES[m], round(s, 3), round(e, 3)) for m, s, e in windows] == [
        ("IMG_7120", 0.0, round(each, 3)),
        ("IMG_7122", round(each, 3), round(2 * each, 3)),
        ("IMG_7123", round(2 * each, 3), round(3 * each, 3)),
        ("IMG_7125", round(3 * each, 3), round(4 * each, 3)),
        ("IMG_7127", round(4 * each, 3), round(TAKE_S, 3)),
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


def test_green3_the_last_photo_holds_to_the_end():
    """S51 reversed 2026-11-05 (S57): after the last mention ("drones", 15.3 s into a 30.3 s
    edit) the visuals used to take turns again every ~4 s; now the last one stays to the end --
    a longer scene beats showing a photo twice."""
    _layout, windows = _lay_out(STORED_ORDER, PHRASES)
    visible = _visible(windows, fill=STORED_FILL)

    assert len({m for m, _s, _e in visible}) == len(visible) == len(PHRASES), _describe(visible)
    last_mention_at = max(_first_window_start(windows, m) for m in PHRASES)
    tail = [row for row in visible if row[1] >= last_mention_at - 1e-6]
    assert len(tail) == 1 and tail[0][2] == pytest.approx(TAKE_S, abs=1e-6), _describe(tail)


def test_a_long_tail_is_one_window():
    """A ten-minute take whose mentions all come in the first 16 s: one window per photo, the
    last held to the take end (it used to stretch a cycling tail to stay within 40 windows)."""
    long_take = 600.0
    layout, windows = _lay_out(STORED_ORDER, PHRASES, keep=None, take_s=long_take, cadence_s=2.0)

    _assert_phone_contract(windows, take_s=long_take)
    assert len(windows) == len(PHRASES)
    assert windows[-1][2] == pytest.approx(long_take, abs=1e-6)
    assert {"lane": "background", "reason": "cadence_stretched"} not in layout.notes
    _assert_on_their_words(windows, keep=None)


def test_an_explicit_hold_still_ends_the_window_early():
    """A creator-asked length (`hold_s`) ends the window; the fill shows until the next mention,
    unless what is left would only be a sliver, which the window absorbs. The fill here is a base
    outside the sequence (a colour): over one of the photos the hold gives way instead (S60)."""
    post_at = _word_output_s("technology")
    tablets_at = seq.to_output_time(LUMO_TABLETS_SOURCE_S, KEEP)

    _layout, held = _lay_out(STORED_ORDER, PHRASES, holds={COLLAB_POST: 1.5}, fill=None)
    post = next(w for w in held if w[0] == COLLAB_POST)
    assert post[1:] == (pytest.approx(post_at, abs=1e-6), pytest.approx(post_at + 1.5, abs=1e-6))
    assert next(w for w in held if w[0] == TABLET)[1] == pytest.approx(tablets_at, abs=1e-6)

    _layout, sliver = _lay_out(STORED_ORDER, PHRASES, holds={COLLAB_POST: 2.6}, fill=None)
    post = next(w for w in sliver if w[0] == COLLAB_POST)
    assert post[2] == pytest.approx(tablets_at, abs=1e-6), (
        f"a {tablets_at - post_at - 2.6:.2f} s fill flash was left: {_describe(sliver)}"
    )


def test_entries_sharing_a_mention_that_cannot_all_fit_drop_the_last_with_a_note():
    """Seven visuals on "technology partner" with "Lumo tablets" 2.84 s later: they share the
    span in list order, half a second at least each; the ones that cannot fit are dropped and
    each drop is on the receipt. After the last mention the tablet holds to the end (S57)."""
    shared = [f"00000000-0000-4000-8000-00000000000{i}" for i in range(7)]
    order = [*shared, TABLET]
    phrases = {**{media: "technology partner" for media in shared}, TABLET: "Lumo tablets"}
    layout, windows = _lay_out(order, phrases)

    _assert_phone_contract(windows)
    in_group = list(dict.fromkeys(m for m, _s, _e in windows if m in shared))
    assert len(in_group) >= 2, f"the visuals sharing a mention lost all but one: {windows}"
    assert in_group == shared[: len(in_group)], "the shared mention does not keep list order"
    dropped = {n["window"] for n in layout.notes if n.get("reason") == "window_too_short"}
    assert dropped == set(range(len(in_group), len(shared)))
    tablet = next(w for w in windows if w[0] == TABLET)
    assert tablet[1] == pytest.approx(seq.to_output_time(LUMO_TABLETS_SOURCE_S, KEEP), abs=1e-6)
    assert tablet[2] == pytest.approx(TAKE_S, abs=1e-6), f"the tablet does not hold: {windows}"
    assert len({m for m, _s, _e in windows}) == len(windows), f"a photo repeats: {windows}"


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


# The decoy's real analysis (the overlay matcher's input in job 9bad2d75); the five backgrounds
# are reserved from that matcher, so their analysis never matters here.
_LUNCH_ANALYSIS = {
    "subject": "Grilled chicken breast meal",
    "description": (
        "A plate featuring sliced grilled chicken breast, a dome of white rice garnished with "
        "chives, and a fresh side salad with lettuce, carrots, and a tomato slice, placed next to "
        "a glass of ice water on a wooden table."
    ),
}


def _pool_rows():
    """The item's ready pool in upload order (7122, 7127, 7123, 7125, 7120, 7129), from the kit."""
    rows = []
    for name in ("IMG_7122", "IMG_7127", "IMG_7123", "IMG_7125", "IMG_7120", "IMG_7129"):
        visual = KIT[name]
        ext = "mp4" if visual["kind"] == "video" else "jpg"
        rows.append(
            {
                "id": visual["pool_id"],
                "gcs_path": f"users/u1/plan/item/pool/{visual['pool_id']}.{ext}",
                "gcs_generation": "7",
                "kind": visual["kind"],
                "source_filename": f"{name}.{ext}",
                "duration_s": 5.0 if visual["kind"] == "video" else None,
                "aspect": 0.5625,
                "user_context": "",
                "analysis": _LUNCH_ANALYSIS if visual["pool_id"] == LUNCH else {},
                "capture": None,
            }
        )
    return rows


def _green3_job(monkeypatch, background):
    import app.services.phone_overlay_grounding as ogm
    import app.services.phone_reaction_grounding as rgm
    import app.services.phone_visuals as phone_visuals_mod
    from app.pipeline.transcribe import Word
    from app.services.phone_sources import PhoneVisualBinding
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled

    rows = {row["id"]: row for row in _pool_rows()}

    def bind(_open, *, job_id, pins, measure_text=False):  # noqa: ARG001
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
