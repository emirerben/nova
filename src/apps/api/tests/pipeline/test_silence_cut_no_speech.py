"""Silence cut refuses a transcript that the audio contradicts (KRI-653).

Prod job c283f1dd: a 192.6 s talk-to-camera take recorded digital silence. Whisper
returned 11 words of 20-40 ms ("I've got to go. Bye! Bye! ...") and silence cut, with
nothing but MIN_OUTPUT_S as a rail under the required-cleanup clamp policy, kept 0.6 s
around each and shipped 3.35 s. Taste is not the lever (no removal-fraction cap comes
back): when every word sits inside detected silence, nobody spoke and the plan is refused.

Failure modes pinned here, written before the code:
  1. the incident geometry still clamps to ~3 s (regression of the bug itself);
  2. real speech with long pauses must NOT bail out (false positive blocks real cuts);
  3. a mostly-silent clip whose words sit OUTSIDE the silence must still clamp as before
     (the pinned test_min_output_is_the_only_remaining_rail behaviour);
  4. no silence spans (detector unavailable) never trips the rule;
  5. a creator's manual (forced) cut is never refused;
  6. the V1 and the V2 builders agree (a V2-only bailout would be skipped for a V1
     baseline that still carries cuts).
"""

from __future__ import annotations

import pytest

from app.pipeline.silence_cut import (
    BAILOUT_NO_AUDIBLE_SPEECH,
    BAILOUT_WORDS_IN_SILENCE,
    NO_SPEECH_BAILOUTS,
    Removal,
    build_cut_plan,
)

# The incident: 11 hallucinated words at these (start_s, end_s) over a 192.6 s take.
_INCIDENT_WORDS = [
    ("I've", 102.48, 102.52),
    ("got", 102.52, 102.54),
    ("to", 102.54, 102.56),
    ("go.", 102.56, 102.58),
    ("Bye!", 119.90, 119.94),
    ("Bye!", 125.10, 125.14),
    ("Bye!", 154.10, 154.14),
    ("Bye!", 155.30, 155.34),
    ("Bye!", 184.40, 184.44),
    ("Bye!", 185.14, 185.18),
    ("Bye!", 192.00, 192.04),
]
_DURATION_S = 192.6
_WHOLE_CLIP_SILENCE = [(0.0, _DURATION_S)]
_REQUIRED_CLAMP = {"over_budget_policy": "clamp"}


def _words(rows: list[tuple[str, float, float]]) -> list[dict]:
    return [{"text": text, "start_s": start, "end_s": end} for text, start, end in rows]


@pytest.mark.parametrize("mixed_gap_enabled", [False, True], ids=["v1", "v2"])
class TestIncidentGeometryIsRefused:
    def test_words_inside_whole_clip_silence_bail_out(self, mixed_gap_enabled: bool) -> None:
        plan = build_cut_plan(
            _words(_INCIDENT_WORDS),
            _WHOLE_CLIP_SILENCE,
            _DURATION_S,
            mixed_gap_enabled=mixed_gap_enabled,
            **_REQUIRED_CLAMP,
        )
        assert plan.bailout_reason == BAILOUT_WORDS_IN_SILENCE
        assert plan.removed == []
        assert plan.keep_segments == [(0.0, _DURATION_S)]

    def test_regression_without_the_rail_the_take_collapses(self, mixed_gap_enabled: bool) -> None:
        # Same words but silence that does NOT cover them (the detector saw sound around each
        # word): this is the shape that used to collapse and now must be judged by the audio.
        spans = [
            (0.0, 102.0),
            (103.0, 119.0),
            (120.5, 124.5),
            (125.8, 154.0),
            (154.5, 155.0),
            (155.8, 184.0),
            (184.9, 191.9),
        ]
        plan = build_cut_plan(
            _words(_INCIDENT_WORDS),
            spans,
            _DURATION_S,
            mixed_gap_enabled=mixed_gap_enabled,
            **_REQUIRED_CLAMP,
        )
        # Words are in audible islands here -> not a no-speech clip; the cut proceeds and still
        # removes the dead air between them (it does not silently do nothing).
        assert plan.bailout_reason not in NO_SPEECH_BAILOUTS
        assert plan.removed
        assert plan.time_saved_s > 0


@pytest.mark.parametrize("mixed_gap_enabled", [False, True], ids=["v1", "v2"])
class TestRealSpeechIsNotRefused:
    def test_speech_with_long_pauses_still_cuts(self, mixed_gap_enabled: bool) -> None:
        # 24 s talk, three bursts of speech separated by 3 s pauses. The pauses are the
        # silence; the words are audible. This is the feature working, not a no-speech clip.
        rows = [(f"w{i}", 0.5 + i * 0.4, 0.85 + i * 0.4) for i in range(8)]  # 0.5 - 3.65
        rows += [(f"x{i}", 7.0 + i * 0.4, 7.35 + i * 0.4) for i in range(8)]  # 7.0 - 10.15
        rows += [(f"y{i}", 13.5 + i * 0.4, 13.85 + i * 0.4) for i in range(8)]  # 13.5 - 16.65
        spans = [(0.0, 0.4), (3.8, 6.9), (10.3, 13.4), (16.8, 24.0)]
        plan = build_cut_plan(
            _words(rows),
            spans,
            24.0,
            mixed_gap_enabled=mixed_gap_enabled,
            **_REQUIRED_CLAMP,
        )
        assert plan.bailout_reason not in NO_SPEECH_BAILOUTS
        assert plan.removed, "the long pauses should still be cut"

    def test_no_silence_spans_never_trips_the_rule(self, mixed_gap_enabled: bool) -> None:
        # The silence detector being unavailable (no spans) must not look like "no speech".
        plan = build_cut_plan(
            _words(_INCIDENT_WORDS),
            [],
            _DURATION_S,
            mixed_gap_enabled=mixed_gap_enabled,
            **_REQUIRED_CLAMP,
        )
        assert plan.bailout_reason not in NO_SPEECH_BAILOUTS

    def test_two_words_do_not_make_a_verdict(self, mixed_gap_enabled: bool) -> None:
        # The pinned shape of test_min_output_is_the_only_remaining_rail: 2 words, 20 s clip,
        # silence between them. Fewer than NO_SPEECH_MIN_WORDS words, and 1.1 s is audible.
        plan = build_cut_plan(
            _words([("hi", 0.2, 0.5), ("bye", 19.6, 19.9)]),
            [(0.6, 19.5)],
            20.0,
            mixed_gap_enabled=mixed_gap_enabled,
            **_REQUIRED_CLAMP,
        )
        assert plan.bailout_reason not in NO_SPEECH_BAILOUTS
        assert plan.clamped is True
        assert plan.keep_segments, "the clamp still leaves the MIN_OUTPUT_S floor"

    def test_manual_cuts_are_never_refused(self, mixed_gap_enabled: bool) -> None:
        # A creator's own forced removal on a clip the detector thinks is silent is their
        # call, not ours: the no-speech rail steps aside.
        plan = build_cut_plan(
            _words(_INCIDENT_WORDS),
            _WHOLE_CLIP_SILENCE,
            _DURATION_S,
            forced_removals=[Removal(start_s=10.0, end_s=20.0, reason="manual")],
            mixed_gap_enabled=mixed_gap_enabled,
            **_REQUIRED_CLAMP,
        )
        assert plan.bailout_reason not in NO_SPEECH_BAILOUTS


@pytest.mark.parametrize("mixed_gap_enabled", [False, True], ids=["v1", "v2"])
def test_nothing_audible_at_all_bails_out_even_without_words_in_silence(
    mixed_gap_enabled: bool,
) -> None:
    # Words exist but the transcript is too thin for the word rule (2 words) while the clip is a
    # real-length take with ~0.3 s of sound: still not a talking take.
    plan = build_cut_plan(
        _words([("hello", 5.0, 5.3), ("there", 40.0, 40.2)]),
        [(0.0, 4.9), (5.2, 40.0), (40.1, 60.0)],
        60.0,
        mixed_gap_enabled=mixed_gap_enabled,
        **_REQUIRED_CLAMP,
    )
    assert plan.bailout_reason == BAILOUT_NO_AUDIBLE_SPEECH
