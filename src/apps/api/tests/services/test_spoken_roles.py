"""KRI-585: what a clip's own words do in a montage that keeps the camera sound.

Prod thread 7CCBDE0A (cabin kit M1): the kitchen clip opens the tour ("welcome to our
home tour") and played 9th; the closing clip introduces Deniz ("this is Deniz") with
nothing before it. The montage now reads each clip's spoken role.

Ways this can go wrong, written down before the code:

1. A bare "hello" said by the person being filmed reads as the creator's welcome, so
   the introduced person's clip jumps to the front (the Deniz clip says "Merhaba").
2. "This is the kitchen" / "bu mutfak" reads as introducing a person.
3. An introduction is claimed on a clip with nobody on screen.
4. Turkish letters or upper case break the match ("HOŞ GELDİNİZ", "hos geldiniz").
5. A clip whose analysis says it has no speech still gets a role from a stale
   transcript.
6. The role's timing points at the wrong segment (the welcome line is the second
   segment, after "Evet arkadaşlar,").
7. A quiet window overlaps a spoken segment, or one is invented for a clip with no
   timed segments.
8. Malformed analysis (missing keys, non-numeric times) raises instead of "no role".
9. whisper's "thanks for watching" on trailing silence, which the clip's own transcript
   never heard, reads as the creator's sign-off and moves the clip to the end.
10. KRI-667: the speech is read with a regex (``test_kri667_no_new_intent_regexes``
    scans this module).
"""

from __future__ import annotations

import pytest

from app.services.spoken_quotes import SEGMENT_LEAD_S as SPEECH_LEAD_S
from app.services.spoken_quotes import SEGMENT_TAIL_S as SPEECH_TAIL_S
from app.services.spoken_roles import quiet_window, spoken_line


def _analysis(*segments, transcript: str | None = None, people: int | None = None, speech=True):
    rows = [{"text": t, "start_s": s, "end_s": e} for t, s, e in segments]
    heard = transcript if transcript is not None else " ".join(t for t, _s, _e in segments)
    return {
        # The clip analysis' own (Gemini) transcript, next to whisper's timed segments.
        "transcript": heard,
        "understanding": {
            "speech": {
                "has_speech": speech,
                "transcript": transcript
                if transcript is not None
                else " ".join(t for t, _s, _e in segments),
                "segments": rows,
            },
            "people": {"count": people},
        },
    }


WELCOME = _analysis(
    ("Evet arkadaşlar,", 0.0, 1.14), ("Tigre'de ev turumuza hoş geldiniz.", 1.94, 3.4)
)
DENIZ = _analysis(
    ("Bu da Deniz,", 0.0, 1.22),
    ("kanalımızın kurucu ekibinden.", 3.3, 5.74),
    transcript="Bu da Deniz. Merhaba. Kanalımızın kurucu ekibinden.",
    people=1,
)


def test_the_welcome_is_an_opening_timed_on_its_own_segment() -> None:
    line = spoken_line(WELCOME)
    assert line is not None
    assert line.role == "opening"
    assert (line.start_s, line.end_s) == (1.94, 3.4)


@pytest.mark.parametrize(
    "text",
    [
        "HOŞ GELDİNİZ arkadaşlar",
        "ev turumuza hos geldiniz",
        "Herkese merhaba, bugün evi geziyoruz",
        "Hey guys, welcome to the cabin",
        "Welcome back to the channel",
    ],
)
def test_welcomes_in_either_language_and_any_case_are_openings(text: str) -> None:
    line = spoken_line(_analysis((text, 0.0, 2.0)))
    assert line is not None and line.role == "opening"


@pytest.mark.parametrize(
    "text",
    [
        "İzlediğiniz için teşekkürler, görüşmek üzere!",
        "Bugünlük bu kadar.",
        "Thanks for watching, see you next time",
        "Don't forget to subscribe",
    ],
)
def test_sign_offs_are_closings(text: str) -> None:
    line = spoken_line(_analysis((text, 0.0, 2.0)))
    assert line is not None and line.role == "closing"


def test_introducing_someone_on_screen_is_an_introduction_not_a_welcome() -> None:
    line = spoken_line(DENIZ)
    assert line is not None
    assert line.role == "introduction"
    assert line.text == "Bu da Deniz,"
    assert (line.start_s, line.end_s) == (0.0, 1.22)


@pytest.mark.parametrize("text", ["This is Deniz.", "Meet Deniz!", "İşte Deniz, kurucumuz"])
def test_introductions_in_either_language(text: str) -> None:
    line = spoken_line(_analysis((text, 0.0, 1.5), people=1))
    assert line is not None and line.role == "introduction"


@pytest.mark.parametrize(
    ("text", "people"),
    [
        ("Bu da Deniz.", None),  # nobody on screen
        ("Bu da Deniz.", 0),
        ("Bu mutfak.", 1),  # a room, not a name
        ("This is the kitchen.", 1),
        ("Bu da ben, sayın seyirciler.", 1),  # the filmer, not someone introduced
        ("Merhaba.", 1),  # a bare hello is nobody's welcome
        ("Mutfağını görüyorsunuz.", 1),
    ],
)
def test_things_that_are_not_an_introduction_or_a_welcome(text: str, people: int | None) -> None:
    assert spoken_line(_analysis((text, 0.0, 1.5), people=people)) is None


def test_whispers_invented_sign_off_on_silence_is_not_a_closing() -> None:
    analysis = _analysis(("Mutfağı görüyorsunuz.", 0.0, 1.5), ("Thanks for watching!", 4.0, 5.0))
    analysis["transcript"] = "Mutfağı görüyorsunuz."
    assert spoken_line(analysis) is None


def test_a_clip_without_speech_has_no_role_even_with_a_stale_transcript() -> None:
    assert spoken_line(_analysis(("hoş geldiniz", 0.0, 1.0), speech=False)) is None


def test_untimed_speech_keeps_its_role_without_a_span() -> None:
    analysis = {"understanding": {"speech": {"has_speech": True, "transcript": "Hoş geldiniz!"}}}
    line = spoken_line(analysis)
    assert line is not None and line.role == "opening"
    assert line.start_s is None and line.end_s is None


@pytest.mark.parametrize(
    "analysis",
    [
        None,
        {},
        {"understanding": None},
        {"understanding": {"speech": "yes"}},
        {"understanding": {"speech": {"has_speech": True, "segments": [{"text": 3}]}}},
        {
            "understanding": {
                "speech": {
                    "has_speech": True,
                    "segments": [{"text": "hoş geldiniz", "start_s": "x", "end_s": None}],
                }
            }
        },
    ],
)
def test_malformed_analysis_means_no_role_and_no_quiet_window(analysis) -> None:
    assert spoken_line(analysis) is None or spoken_line(analysis).start_s is None
    assert quiet_window(analysis, 6.0, min_s=1.2) is None


def test_the_quiet_window_is_the_longest_gap_between_the_words() -> None:
    window = quiet_window(DENIZ, 6.0, min_s=1.2)
    assert window == pytest.approx((1.22 + SPEECH_TAIL_S, 3.3 - SPEECH_LEAD_S))


def test_no_quiet_window_when_the_gap_is_shorter_than_asked() -> None:
    assert quiet_window(DENIZ, 6.0, min_s=1.7) is None


def test_quiet_time_before_and_after_the_words_counts() -> None:
    analysis = _analysis(("Bu da Deniz.", 2.5, 3.5), people=1)
    assert quiet_window(analysis, 4.0, min_s=1.2) == pytest.approx((0.0, 2.5 - SPEECH_LEAD_S))
    late = _analysis(("Bu da Deniz.", 0.2, 1.0), people=1)
    assert quiet_window(late, 4.0, min_s=1.2) == pytest.approx((1.0 + SPEECH_TAIL_S, 4.0))


def test_no_quiet_window_without_timed_words() -> None:
    analysis = {"understanding": {"speech": {"has_speech": True, "transcript": "Bu da Deniz."}}}
    assert quiet_window(analysis, 6.0, min_s=1.2) is None
