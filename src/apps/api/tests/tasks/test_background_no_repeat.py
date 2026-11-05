"""Green-screen backgrounds: no picture is shown twice.

Co-founder, on the S4 "fb 1" export: "Ayni resmi iki kere gostermedigi an 10/10".
Incident: S4 run "fb 1" (prod 2026-11-04, job c39a3210): 19 background windows from 8
pictures, each shown 2-3 times. Replaying `lay_out` with the job's stored sequence, word
timings and speech cut reproduces the 19 windows exactly; the loop is the layout's tail rule
(S51) and the rotation cycle (S39).
Failure modes: docs/reviews/background-no-repeat-failure-modes.md.

The take below is invented Turkish speech with fb 1's timing shape (14 sentences over 79 s,
the real speech-cut spans, every mention in the first ~30 s as the planner wrote it); the real
transcript stays out of git. Checked on the VISIBLE background: windows plus the fill wherever
no window covers the time.
"""

from __future__ import annotations

from collections import Counter

from app.services import phone_background_sequence as seq

TAKE_FULL_S = 82.7
# fb 1 `variants[0].silence_cut.removed` (source seconds): 10 pauses, 9.631 s.
REMOVED = [
    (0.0, 0.38),
    (4.785, 5.335),
    (10.625, 11.226),
    (29.545, 30.395),
    (36.145, 37.355),
    (54.265, 56.095),
    (59.725, 60.315),
    (64.305, 64.715),
    (72.705, 73.195),
    (79.98, 82.7),
]
KEEP = [
    (start, end)
    for start, end in zip(
        [0.0, *(e for _s, e in REMOVED)], [*(s for s, _e in REMOVED), TAKE_FULL_S], strict=True
    )
    if end - start > 1e-6
]
TAKE_S = seq.kept_duration(KEEP, TAKE_FULL_S)  # 73.07 s

# (start_s, end_s, text) in the original take; fb 1's sentence times, invented words.
SENTENCES = [
    (0.68, 4.75, "Bugün Kadıköy çarşısında yürüyerek bir yemek turu yapıyoruz."),
    (5.56, 10.55, "İlk durağımız balıkçılar sokağı, sabahın erken saatinde bile çok kalabalık."),
    (
        11.38,
        18.45,
        "Tezgâhlarda lüfer, palamut ve istavrit var, fiyatlar tahtaya tek tek yazılmış.",
    ),
    (19.58, 24.08, "Mesele sadece balık değil, burada herkes birbirini tanıyor."),
    (24.74, 29.36, "Esnafla sohbet etmeden buradan geçmek neredeyse imkânsız."),
    (30.78, 35.88, "Sonra köşedeki fırından sıcak bir simit aldık ve yürümeye devam ettik."),
    (37.52, 42.60, "Moda'ya doğru inerken yol üstündeki eski kitapçılara uğradık."),
    (42.96, 44.88, "Bir tanesinde çok eski bir harita bulduk."),
    (45.66, 54.13, "Öğle yemeği için meşhur bir esnaf lokantasında kuru fasulye ve pilav yedik."),
    (56.48, 59.60, "Tatlı olarak da kazandibi söyledik, gerçekten çok iyiydi."),
    (60.56, 64.30, "Moda sahilinde gün batımını izlemek için banklara oturduk."),
    (64.90, 72.29, "Rüzgâr biraz serindi ama manzara her şeye değdi, adalar tam karşımızdaydı."),
    (73.82, 75.50, "Sizce bir sonraki turu nerede yapalım?"),
    (75.80, 79.30, "Yorumlara yazın, en çok istenen yere gidelim."),
]


def _words() -> list[dict]:
    words: list[dict] = []
    for start, end, text in SENTENCES:
        parts = text.split()
        step = (end - start) / len(parts)
        for i, part in enumerate(parts):
            words.append(
                {
                    "text": part,
                    "start_s": round(start + i * step, 2),
                    "end_s": round(start + (i + 1) * step, 2),
                }
            )
    return words


WORDS = _words()

# fb 1's planner shape: 8 pictures, every phrase from the first five sentences (0-29 s).
FB1_SHAPED = [
    ("stadium", "Kadıköy çarşısında"),
    ("aerial", "yemek turu"),
    ("towers", "balıkçılar sokağı"),
    ("construction", "lüfer, palamut"),
    ("training_ground", "fiyatlar tahtaya"),
    ("fans_video", "Mesele sadece balık"),
    ("coast", "herkes birbirini"),
    ("fans_walking", "Esnafla sohbet"),
]


def _lay_out(entries, *, fill: str | None, cadence_s: float = 4.0, holds=None):
    """`lay_out` as both callers use it: each entry's photo identity (`media_key`) and the
    fill's (`fill_key`, None for a plain colour) so the layout can keep every photo to once."""
    holds = holds or {}
    layout = seq.lay_out(
        [
            seq.SequenceEntry(index=i, spoken_phrase=phrase, hold_s=holds.get(i), media_key=media)
            for i, (media, phrase) in enumerate(entries)
        ],
        cadence_s=cadence_s,
        take_s=TAKE_S,
        words=WORDS,
        keep_segments=KEEP,
        fill_key=fill,
    )
    return layout, [(entries[w.index][0], w.start_s, w.end_s) for w in layout.windows]


def _visible(windows, *, fill):
    """Behind the speaker from 0 s to the end: each window, the fill in any gap, same-picture
    neighbours merged (a picture continuing is not shown twice)."""
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
    add(fill, t, TAKE_S)
    return [tuple(row) for row in shown]


def _assert_phone_limits(windows) -> None:
    """S62: what `SpeakerBackgroundLayer` / SpeakerBackground.swift accept."""
    assert 1 <= len(windows) <= seq.MAX_WINDOWS
    for _m, start, end in windows:
        assert 0.0 <= start < end <= TAKE_S + 1e-6
        assert end - start >= seq.MIN_WINDOW_S - 1e-6, f"window {start:.2f}-{end:.2f} too short"
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert later[1] >= earlier[2] - 1e-6, "windows overlap"


def _assert_each_picture_once(visible, expected: set[str], *, base: str | None = None) -> None:
    """S57: every photo at most once; ``base`` (a fill outside the sequence) may show in gaps."""
    counts = Counter(media for media, _s, _e in visible if media != base)
    described = " | ".join(f"{m} {s:.1f}-{e:.1f}" for m, s, e in visible)
    repeats = {media: n for media, n in counts.items() if n > 1}
    assert not repeats, f"shown more than once {repeats}: {described}"
    assert set(counts) == expected, f"pictures missing or extra: {described}"
    assert visible[0][1] == 0.0 and abs(visible[-1][2] - TAKE_S) <= 1e-6, described


def _heard_at(phrase_word: str) -> float:
    """Output time of the first word of a phrase (the cut applied)."""
    for word in WORDS:
        if word["text"].strip(",.?") == phrase_word:
            at = seq.to_output_time(word["start_s"], KEEP)
            assert at is not None
            return at
    raise AssertionError(phrase_word)


def test_fb1_shaped_plan_never_shows_a_picture_twice() -> None:
    """S57 / N1: every mention in the first 30 s of a 73 s take; the tail looped for 44 s."""
    _layout, windows = _lay_out(FB1_SHAPED, fill=FB1_SHAPED[0][0])
    visible = _visible(windows, fill=FB1_SHAPED[0][0])

    _assert_each_picture_once(visible, {media for media, _p in FB1_SHAPED})
    _assert_phone_limits(windows)
    # A longer scene beats a repeat: the last photo talked about stays to the end.
    assert visible[-1][0] == "fans_walking"


def test_rotation_without_phrases_shows_each_picture_once() -> None:
    """S57 / N2: three unphrased pictures on a 73 s take at 4 s used to cycle six times."""
    entries = [("one", None), ("two", None), ("three", None)]
    layout, windows = _lay_out(entries, fill="one")
    visible = _visible(windows, fill="one")

    _assert_each_picture_once(visible, {"one", "two", "three"})
    _assert_phone_limits(windows)
    assert {"lane": "background", "reason": "cadence_stretched"} in layout.notes


def test_a_long_rotation_stays_inside_the_phone_limits() -> None:
    """S62: more pictures than fit at the cadence: no repeats, at most 40 windows, to the end."""
    entries = [(f"p{i}", None) for i in range(45)]
    _layout, windows = _lay_out(entries, fill="p0")
    visible = _visible(windows, fill="p0")

    assert len({media for media, _s, _e in visible}) == len(visible)
    _assert_phone_limits(windows)
    assert abs(windows[-1][2] - TAKE_S) <= 1e-6


def test_mixed_request_shows_each_photo_once() -> None:
    """S57: two photos on their words, two without a phrase: each once, every second covered."""
    entries = [
        ("stadium", "Kadıköy çarşısında"),
        ("plain_one", None),
        ("coast", "Moda sahilinde"),
        ("plain_two", None),
    ]
    _layout, windows = _lay_out(entries, fill="stadium")
    visible = _visible(windows, fill="stadium")

    _assert_each_picture_once(visible, {"stadium", "plain_one", "coast", "plain_two"})
    _assert_phone_limits(windows)


def test_the_same_picture_listed_twice_is_shown_once() -> None:
    """S59 / N3: the planner names the stadium for two different mentions."""
    entries = [
        ("stadium", "Kadıköy çarşısında"),
        ("aerial", "balıkçılar sokağı"),
        ("stadium", "Moda sahilinde"),
    ]
    layout, windows = _lay_out(entries, fill="stadium")
    visible = _visible(windows, fill="stadium")

    _assert_each_picture_once(visible, {"stadium", "aerial"})
    assert {"lane": "background", "window": 2, "reason": "duplicate_media"} in layout.notes


def test_the_opening_is_the_first_photo() -> None:
    """S58: the fill is a photo talked about later; the video still opens on the first-mentioned
    photo, and the later one shows once."""
    entries = [("stadium", "Kadıköy çarşısında"), ("aerial", "balıkçılar sokağı")]
    _layout, windows = _lay_out(entries, fill="aerial")
    visible = _visible(windows, fill="aerial")

    assert visible[0][0] == "stadium" and visible[0][1] == 0.0
    _assert_each_picture_once(visible, {"stadium", "aerial"})


def test_a_hold_gives_way_when_the_fill_is_one_of_the_photos() -> None:
    """S60: a 2 s hold on the stadium would open a gap the fill (the aerial, shown later)
    covers; no-repeat wins and the stadium stays until the next photo."""
    entries = [
        ("stadium", "Kadıköy çarşısında"),
        ("towers", "balıkçılar sokağı"),
        ("aerial", "Moda sahilinde"),
    ]
    _layout, windows = _lay_out(entries, fill="aerial", holds={0: 2.0})
    visible = _visible(windows, fill="aerial")

    _assert_each_picture_once(visible, {"stadium", "towers", "aerial"})


def test_a_separate_base_still_shows_after_a_hold() -> None:
    """S60: a fill outside the sequence (a base photo or colour) is the creator's base; it may
    show after a held photo, and no photo of the sequence repeats."""
    entries = [("stadium", "Kadıköy çarşısında"), ("towers", "balıkçılar sokağı")]
    _layout, windows = _lay_out(entries, fill="base", holds={0: 2.0})
    visible = _visible(windows, fill="base")

    # S50/S58: a held first photo over a separate base keeps the base before it too.
    assert [media for media, _s, _e in visible] == ["base", "stadium", "base", "towers"]
    _assert_each_picture_once(visible, {"stadium", "towers"}, base="base")


def test_a_phrase_never_heard_is_skipped() -> None:
    """S61: a phrase nobody says gets no window and is never cycled in."""
    entries = [
        ("stadium", "Kadıköy çarşısında"),
        ("decoy", "Galata Kulesi"),
        ("coast", "Moda sahilinde"),
    ]
    layout, windows = _lay_out(entries, fill="stadium")
    visible = _visible(windows, fill="stadium")

    _assert_each_picture_once(visible, {"stadium", "coast"})
    assert {"lane": "background", "window": 1, "reason": "phrase_not_heard"} in layout.notes


def test_two_photos_on_one_mention_share_it() -> None:
    """S61: two photos of one thing play back to back from its word, each once."""
    entries = [
        ("stadium", "Kadıköy çarşısında"),
        ("drone_photo", "balıkçılar sokağı"),
        ("drone_video", "balıkçılar sokağı"),
        ("coast", "Moda sahilinde"),
    ]
    _layout, windows = _lay_out(entries, fill="stadium")
    visible = _visible(windows, fill="stadium")

    _assert_each_picture_once(visible, {"stadium", "drone_photo", "drone_video", "coast"})
    order = [media for media, _s, _e in visible]
    assert order.index("drone_video") == order.index("drone_photo") + 1
    photo = next(row for row in visible if row[0] == "drone_photo")
    assert abs(photo[1] - _heard_at("balıkçılar")) <= 1.5
