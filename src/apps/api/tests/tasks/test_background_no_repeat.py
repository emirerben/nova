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


def _lay_out(entries, *, cadence_s: float = 4.0):
    layout = seq.lay_out(
        [
            seq.SequenceEntry(index=i, spoken_phrase=phrase)
            for i, (_media, phrase) in enumerate(entries)
        ],
        cadence_s=cadence_s,
        take_s=TAKE_S,
        words=WORDS,
        keep_segments=KEEP,
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


def _assert_each_picture_once(visible, expected: set[str]) -> None:
    counts = Counter(media for media, _s, _e in visible)
    described = " | ".join(f"{m} {s:.1f}-{e:.1f}" for m, s, e in visible)
    repeats = {media: n for media, n in counts.items() if n > 1}
    assert not repeats, f"shown more than once {repeats}: {described}"
    assert set(counts) == expected, f"pictures missing or extra: {described}"
    assert visible[0][1] == 0.0 and abs(visible[-1][2] - TAKE_S) <= 1e-6, described


def test_fb1_shaped_plan_never_shows_a_picture_twice() -> None:
    """N1: every mention in the first 30 s of a 73 s take; today the tail loops for 44 s."""
    _layout, windows = _lay_out(FB1_SHAPED)
    visible = _visible(windows, fill=FB1_SHAPED[0][0])

    _assert_each_picture_once(visible, {media for media, _p in FB1_SHAPED})


def test_rotation_without_phrases_shows_each_picture_once() -> None:
    """N2: three unphrased pictures on a 73 s take at 4 s used to cycle six times."""
    entries = [("one", None), ("two", None), ("three", None)]
    _layout, windows = _lay_out(entries)
    visible = _visible(windows, fill="one")

    _assert_each_picture_once(visible, {"one", "two", "three"})


def test_the_same_picture_listed_twice_is_shown_once() -> None:
    """N3: the planner names the stadium for two different mentions."""
    entries = [
        ("stadium", "Kadıköy çarşısında"),
        ("aerial", "balıkçılar sokağı"),
        ("stadium", "Moda sahilinde"),
    ]
    _layout, windows = _lay_out(entries)
    visible = _visible(windows, fill="stadium")

    _assert_each_picture_once(visible, {"stadium", "aerial"})
