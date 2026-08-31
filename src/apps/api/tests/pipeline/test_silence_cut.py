"""Unit matrix for the pure silence/filler/retake cut-plan module (plans/010 T1).

No ffmpeg, no network, no API keys — everything here is arithmetic on word
timings and silence ranges. Covers: the universal filler lexicon (incl.
Turkish characters, elongation collapse, punctuation), segment-signal guards,
acoustic-gap classification + the zero-silence calibration gate, the pause
intersection rule (D16: no silencedetect agreement ⇒ no cut), leading/trailing
trims, merge + MIN_CUT_S hygiene, every safety-rail bailout, retake-span
snapping (never mid-word), remap_words exactness, no-op identity, and a
seeded property-style sweep over random layouts.
"""

import dataclasses
import random
from types import SimpleNamespace

import pytest

import app.pipeline.silence_cut as silence_cut
from app.pipeline.silence_cut import (
    ACOUSTIC_GAP_MAX_S,
    BAILOUT_CLIP_TOO_SHORT,
    BAILOUT_MAX_REMOVAL,
    BAILOUT_NO_WORDS,
    BAILOUT_OUTPUT_TOO_SHORT,
    KEPT_GAP_S,
    MAX_REMOVAL_FRAC,
    MAX_REMOVAL_FRAC_REQUIRED,
    MAX_REMOVALS,
    MIN_CLIP_S,
    MIN_CUT_S,
    MIN_KEEP_SEGMENT_S,
    MIN_OUTPUT_S,
    PAD_ACOUSTIC_S,
    PAD_S,
    REASON_FILLER_ACOUSTIC,
    REASON_FILLER_LEXICAL,
    REASON_RETAKE,
    REASON_SILENCE,
    CutPlan,
    Removal,
    build_cut_plan,
    is_filler_token,
    no_op_plan,
    plan_event_payload,
    plan_summary,
    remap_words,
)

DUR = 20.0


def w(text: str, start: float, end: float, **extra) -> dict:
    return {"text": text, "start_s": start, "end_s": end, **extra}


def lexical_fixture(filler: str = "um", **extra) -> list[dict]:
    """so [1.0,1.3] · FILLER [2.0,2.4] · lets [3.0,3.3] — with silences=[],
    only rule 1 can fire (rule 2 is calibration-gated, rule 3 needs silence)."""
    return [w("so", 1.0, 1.3), w(filler, 2.0, 2.4, **extra), w("lets", 3.0, 3.3)]


def only_removal(plan: CutPlan) -> Removal:
    assert plan.bailout_reason is None
    assert len(plan.removed) == 1, plan.removed
    return plan.removed[0]


def assert_spans(actual: list[tuple[float, float]], expected: list[tuple[float, float]]):
    assert len(actual) == len(expected), (actual, expected)
    for (a_lo, a_hi), (e_lo, e_hi) in zip(actual, expected):
        assert a_lo == pytest.approx(e_lo, abs=1e-9)
        assert a_hi == pytest.approx(e_hi, abs=1e-9)


def assert_no_cuts(plan: CutPlan, duration: float = DUR):
    assert plan.bailout_reason is None
    assert plan.removed == []
    assert_spans(plan.keep_segments, [(0.0, duration)])
    assert plan.time_saved_s == pytest.approx(0.0, abs=1e-9)


def test_retake_only_plan_does_not_apply_silence_or_filler_rules() -> None:
    plan = build_cut_plan(
        [
            w("uh", 0.5, 0.8),
            w("restart", 0.9, 1.5),
            w("clean", 3.0, 3.5),
            w("ending", 3.6, 4.2),
        ],
        [(1.5, 3.0)],
        8.0,
        retake_spans=[(0, 1)],
        include_silence_and_fillers=False,
    )

    assert plan.bailout_reason is None
    assert plan.removed
    assert all(removal.reason == REASON_RETAKE for removal in plan.removed)


# ---------------------------------------------------------------------------------
# Filler lexicon (is_filler_token)
# ---------------------------------------------------------------------------------


class TestFillerLexicon:
    @pytest.mark.parametrize(
        "token",
        [
            # exact lexicon members
            "uh",
            "um",
            "er",
            "erm",
            "hmm",
            "mm",
            "mhm",
            "ıı",
            "ııı",
            "eee",
            "aaa",
            "ıh",
            # case + punctuation normalization
            "Um",
            "UH,",
            "uh...",
            "Erm",
            "ıh.",
            "Hmm!",
            # elongation collapse (runs cap at the lexicon form)
            "uhhh",
            "uhhhh",
            "ummm",
            "ummmm",
            "mmm",
            "hmmm",
            "mhmm",
            # Turkish elongations
            "ıııı",
            "ııııı",
            "eeee",
            "aaaa",
            # round-2 additions (user-reported escapes: "eh, ıh, o" class)
            "eh",
            "Ehh,",
            "ehhh",
            "ah",
            "ahh...",
            "oh",
            "Ohh",
            "oo",
            "ooo",
            "oooo",
            # Turkish dotless-I casing (R1): capital 'I' folds to 'ı' (never
            # 'i' — str.lower() alone would miss the ıı/ııı/ıh entries)
            "Iıı,",
            "Iıı",
            "Ih.",
            "Iı",
            "IH",
        ],
    )
    def test_filler_tokens_match(self, token):
        assert is_filler_token(token), token

    @pytest.mark.parametrize(
        "token",
        [
            # real Turkish exclamations — deliberately excluded
            "ee",
            "aa",
            # sub-lexicon fragments never match
            "e",
            "a",
            "m",
            "ı",
            "hm",
            # real words (never cut in v1)
            "like",
            "şey",
            "you",
            "know",
            "hello",
            "era",
            "umbrella",
            "hummus",
            "o",  # Turkish pronoun — bare "o" must NEVER match (only "oo"+)
            # English capital-I words — the I→ı Turkish fold must not create
            # false positives (safety pin: lexicon has no i-initial entries)
            "I",
            "It",
            "Im",
            "IT",
            # junk
            "",
            "   ",
            "123",
            "?!",
        ],
    )
    def test_non_fillers_do_not_match(self, token):
        assert not is_filler_token(token), token


# ---------------------------------------------------------------------------------
# Rule 1 — lexical filler cuts
# ---------------------------------------------------------------------------------


class TestLexicalRule:
    def test_filler_cut_span_reason_and_keep(self):
        plan = build_cut_plan(lexical_fixture(), [], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_FILLER_LEXICAL
        assert removal.start_s == pytest.approx(2.0 - PAD_S, abs=1e-9)
        assert removal.end_s == pytest.approx(2.4 + PAD_S, abs=1e-9)
        assert_spans(plan.keep_segments, [(0.0, 1.88), (2.52, DUR)])
        assert plan.time_saved_s == pytest.approx(0.64, abs=1e-9)
        assert plan.version == 1

    @pytest.mark.parametrize("filler", ["uhhh", "ıııı", "Um,", "eee"])
    def test_variant_forms_cut(self, filler):
        plan = build_cut_plan(lexical_fixture(filler), [], DUR)
        assert only_removal(plan).reason == REASON_FILLER_LEXICAL

    @pytest.mark.parametrize("word", ["like", "şey", "ee", "aa", "hello"])
    def test_real_words_and_exclamations_not_cut(self, word):
        assert_no_cuts(build_cut_plan(lexical_fixture(word), [], DUR))

    def test_removal_never_eats_adjacent_words(self):
        words = [w("so", 1.0, 1.95), w("um", 2.0, 2.4), w("lets", 2.45, 2.9)]
        removal = only_removal(build_cut_plan(words, [], DUR))
        # pad would reach 1.88 / 2.52, but neighbors clamp it
        assert removal.start_s == pytest.approx(1.95, abs=1e-9)
        assert removal.end_s == pytest.approx(2.45, abs=1e-9)

    def test_filler_first_word_clamps_to_clip_start(self):
        words = [w("um", 0.05, 0.4), w("go", 1.0, 1.4)]
        removal = only_removal(build_cut_plan(words, [], DUR))
        assert removal.start_s == pytest.approx(0.0, abs=1e-9)
        assert removal.end_s == pytest.approx(0.4 + PAD_S, abs=1e-9)

    def test_accepts_object_shaped_words(self):
        words = [
            SimpleNamespace(text="so", start_s=1.0, end_s=1.3),
            SimpleNamespace(text="um", start_s=2.0, end_s=2.4),
            SimpleNamespace(text="lets", start_s=3.0, end_s=3.3),
        ]
        removal = only_removal(build_cut_plan(words, [], DUR))
        assert removal.reason == REASON_FILLER_LEXICAL


class TestSegmentSignalGuard:
    @pytest.mark.parametrize(
        ("extra", "expect_cut"),
        [
            ({}, True),  # no signals present → None never blocks
            ({"segment_avg_logprob": -1.5}, False),  # below AVG_LOGPROB_MIN
            ({"segment_avg_logprob": -1.0}, True),  # at threshold → allowed
            ({"segment_avg_logprob": -0.2}, True),
            ({"segment_no_speech_prob": 0.8}, False),  # above NO_SPEECH_PROB_MAX
            ({"segment_no_speech_prob": 0.5}, True),  # at threshold → allowed
            ({"segment_no_speech_prob": 0.1}, True),
            # NO word-confidence floor: fillers naturally score low ASR
            # confidence, so a floor blocks exactly what this rule cuts
            # (local test 2026-07-09: conf 0.03/0.46 real "um"s escaped).
            ({"confidence": 0.03}, True),
            ({"confidence": 0.4}, True),
            ({"confidence": 1.0}, True),  # whisper-1 hardcodes 1.0 → not a signal
            ({"segment_avg_logprob": -0.3, "segment_no_speech_prob": 0.9}, False),
        ],
    )
    def test_guard_blocks_or_allows_lexical_cut(self, extra, expect_cut):
        plan = build_cut_plan(lexical_fixture("um", **extra), [], DUR)
        if expect_cut:
            assert only_removal(plan).reason == REASON_FILLER_LEXICAL
        else:
            assert_no_cuts(plan)


# ---------------------------------------------------------------------------------
# Rule 2 — acoustic fillers
# ---------------------------------------------------------------------------------

# A tiny silence INSIDE a word keeps the silence list non-empty (calibration
# gate open) without ever intersecting an inter-word gap.
INERT_SILENCE = [(1.05, 1.1)]


class TestAcousticRule:
    def test_soundful_gap_cut_with_acoustic_pads(self):
        words = [w("one", 1.0, 1.5), w("two", 2.3, 2.8), w("three", 2.9, 3.4)]
        removal = only_removal(build_cut_plan(words, INERT_SILENCE, DUR))
        assert removal.reason == REASON_FILLER_ACOUSTIC
        assert removal.start_s == pytest.approx(1.5 + PAD_ACOUSTIC_S, abs=1e-9)
        assert removal.end_s == pytest.approx(2.3 - PAD_ACOUSTIC_S, abs=1e-9)

    def test_gap_at_max_bound_still_cut(self):
        words = [w("one", 1.0, 1.5), w("two", 1.5 + ACOUSTIC_GAP_MAX_S, 3.2)]
        removal = only_removal(build_cut_plan(words, INERT_SILENCE, DUR))
        assert removal.reason == REASON_FILLER_ACOUSTIC

    def test_gap_longer_than_max_left_alone(self):
        # laughter/singing/action noise — soundful but > ACOUSTIC_GAP_MAX_S
        words = [w("one", 1.0, 1.5), w("two", 2.8, 3.3)]
        assert_no_cuts(build_cut_plan(words, INERT_SILENCE, DUR))

    def test_gap_below_min_no_cut(self):
        words = [w("one", 1.0, 1.5), w("two", 1.6, 2.1)]
        assert_no_cuts(build_cut_plan(words, INERT_SILENCE, DUR))

    def test_silence_covered_gap_attributed_to_pause_rule(self):
        words = [w("one", 1.0, 1.5), w("two", 2.3, 2.8)]
        plan = build_cut_plan(words, [(1.5, 2.3)], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_SILENCE  # rule 3, not rule 2
        assert removal.start_s == pytest.approx(1.5 + KEPT_GAP_S / 2, abs=1e-9)
        assert removal.end_s == pytest.approx(2.3 - KEPT_GAP_S / 2, abs=1e-9)

    def test_partial_silence_overlap_blocks_acoustic(self):
        # any silence overlap disqualifies rule 2; the rule-3 intersection here
        # is a sub-MIN_CUT_S sliver, so the net result is no cut at all
        words = [w("one", 1.0, 1.5), w("two", 2.3, 2.8)]
        assert_no_cuts(build_cut_plan(words, [(1.5, 1.7)], DUR))

    def test_calibration_gate_empty_silences_disables_rule_2_only(self):
        words = [
            w("so", 1.0, 1.5),
            w("two", 2.3, 2.8),  # 0.8s soundful gap — rule 2 shaped
            w("um", 3.5, 3.9),  # lexical filler — rule 1 must still fire
            w("end", 4.5, 4.9),
        ]
        plan = build_cut_plan(words, [], DUR)
        assert [r.reason for r in plan.removed] == [REASON_FILLER_LEXICAL]


# ---------------------------------------------------------------------------------
# Rule 3 — pause tightening (dual-signal intersection)
# ---------------------------------------------------------------------------------


class TestPauseRule:
    def test_silent_pause_tightened_kept_gap_arithmetic(self):
        words = [w("one", 1.0, 1.5), w("two", 3.0, 3.5)]
        plan = build_cut_plan(words, [(1.5, 3.0)], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_SILENCE
        assert removal.start_s == pytest.approx(1.5 + KEPT_GAP_S / 2, abs=1e-9)
        assert removal.end_s == pytest.approx(3.0 - KEPT_GAP_S / 2, abs=1e-9)
        assert_spans(plan.keep_segments, [(0.0, 1.625), (2.875, DUR)])
        assert plan.time_saved_s == pytest.approx(1.25, abs=1e-9)

    def test_gap_without_silence_agreement_never_cut(self):
        # D16: whisper end times drift — word-gap arithmetic alone never cuts.
        # Gap is 1.5s (> ACOUSTIC_GAP_MAX_S, so rule 2 is out too).
        words = [w("one", 1.0, 1.5), w("two", 3.0, 3.5)]
        assert_no_cuts(build_cut_plan(words, INERT_SILENCE, DUR))

    def test_gap_below_max_pause_not_tightened(self):
        # fully silent but only 0.5s — under MAX_PAUSE_S, leave it be
        words = [w("one", 1.0, 1.5), w("two", 2.0, 2.5)]
        assert_no_cuts(build_cut_plan(words, [(1.5, 2.0)], DUR))

    def test_pause_removal_clipped_to_silence_pieces(self):
        words = [w("one", 1.0, 1.5), w("two", 4.5, 5.0)]
        plan = build_cut_plan(words, [(1.6, 2.2), (3.0, 4.0)], DUR)
        assert [r.reason for r in plan.removed] == [REASON_SILENCE, REASON_SILENCE]
        assert_spans(
            [(r.start_s, r.end_s) for r in plan.removed],
            [(1.625, 2.2), (3.0, 4.0)],  # window (1.625, 4.375) ∩ silences
        )
        assert_spans(plan.keep_segments, [(0.0, 1.625), (2.2, 3.0), (4.0, DUR)])

    def test_leading_silence_trimmed_to_lead_keep(self):
        words = [w("a", 2.0, 2.5), w("b", 2.6, 3.1)]
        plan = build_cut_plan(words, [(0.0, 2.0)], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_SILENCE
        assert_spans([(removal.start_s, removal.end_s)], [(0.0, 1.7)])
        assert_spans(plan.keep_segments, [(1.7, DUR)])

    def test_leading_within_lead_keep_untouched(self):
        words = [w("a", 0.25, 0.7), w("b", 0.8, 1.3)]
        assert_no_cuts(build_cut_plan(words, [(0.0, 0.25)], DUR))

    def test_trailing_silence_trimmed_to_trail_keep(self):
        words = [w("a", 14.0, 14.5), w("b", 14.6, 15.0)]
        plan = build_cut_plan(words, [(15.0, 20.0)], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_SILENCE
        assert_spans([(removal.start_s, removal.end_s)], [(15.5, 20.0)])
        assert_spans(plan.keep_segments, [(0.0, 15.5)])

    def test_trailing_within_trail_keep_untouched(self):
        words = [w("a", 19.0, 19.3), w("b", 19.4, 19.6)]
        assert_no_cuts(build_cut_plan(words, [(19.6, 20.0)], DUR))


# ---------------------------------------------------------------------------------
# Rule 4 hygiene — MIN_CUT_S drop + merge
# ---------------------------------------------------------------------------------


class TestMergeHygiene:
    def test_sub_min_cut_removals_dropped(self):
        words = [w("one", 1.0, 1.5), w("two", 3.5, 4.0)]
        # silence sliver → 0.1s intersection < MIN_CUT_S → dropped
        assert_no_cuts(build_cut_plan(words, [(2.0, 2.1)], DUR))

    def test_overlapping_removals_merge(self):
        words = [w("so", 1.0, 1.3), w("um", 2.0, 2.3), w("uh", 2.4, 2.7), w("go", 3.4, 3.7)]
        plan = build_cut_plan(words, [], DUR)
        removal = only_removal(plan)
        assert removal.reason == REASON_FILLER_LEXICAL
        assert_spans([(removal.start_s, removal.end_s)], [(1.88, 2.82)])
        assert plan.time_saved_s == pytest.approx(0.94, abs=1e-9)

    def test_adjacent_removals_merge_keeping_first_reason(self):
        # retake removal (1.62, 2.88) touches the lexical removal (2.88, 3.52)
        # exactly → one merged removal wearing the FIRST-by-time reason.
        words = [w("start", 1.0, 1.5), w("take", 2.0, 2.5), w("um", 3.0, 3.4), w("final", 4.0, 4.5)]
        plan = build_cut_plan(words, [], 10.0, retake_spans=[(1, 1)])
        removal = only_removal(plan)
        assert removal.reason == REASON_RETAKE
        assert_spans([(removal.start_s, removal.end_s)], [(1.62, 3.52)])
        assert_spans(plan.keep_segments, [(0.0, 1.62), (3.52, 10.0)])
        assert plan.time_saved_s == pytest.approx(1.9, abs=1e-9)


# ---------------------------------------------------------------------------------
# Safety rails — every bailout_reason
# ---------------------------------------------------------------------------------


def assert_noop_bailout(plan: CutPlan, reason: str, duration: float):
    assert plan.bailout_reason == reason
    assert plan.removed == []
    assert_spans(plan.keep_segments, [(0.0, duration)])
    assert plan.time_saved_s == pytest.approx(0.0, abs=1e-9)


class TestSafetyRails:
    @pytest.mark.parametrize(
        "words",
        [
            [],
            None,
            [{"text": "hi"}],  # missing timestamps
            [{"text": "", "start_s": 1.0, "end_s": 1.5}],  # empty text
        ],
    )
    def test_no_words_bailout(self, words):
        assert_noop_bailout(build_cut_plan(words, [], DUR), BAILOUT_NO_WORDS, DUR)

    def test_clip_too_short_bailout(self):
        plan = build_cut_plan(lexical_fixture(), [], MIN_CLIP_S - 0.1)
        assert_noop_bailout(plan, BAILOUT_CLIP_TOO_SHORT, MIN_CLIP_S - 0.1)

    def test_max_removal_exceeded_bailout(self):
        words = [w("a", 0.5, 1.0), w("b", 9.5, 9.9)]
        plan = build_cut_plan(words, [(1.0, 9.5)], 10.0)  # would remove 8.25s of 10s
        assert_noop_bailout(plan, BAILOUT_MAX_REMOVAL, 10.0)

    def test_retake_spans_share_max_removal_frac(self):
        words = [w("a", 0.5, 1.0), w("b", 2.0, 2.5), w("c", 3.0, 3.5), w("d", 9.0, 9.5)]
        plan = build_cut_plan(words, [], 10.0, retake_spans=[(1, 2)])
        assert_noop_bailout(plan, BAILOUT_MAX_REMOVAL, 10.0)

    def test_reviewed_forced_removals_share_max_removal_frac(self):
        words = [w("a", 0.5, 1.0), w("b", 9.0, 9.5)]
        plan = build_cut_plan(
            words,
            [],
            10.0,
            forced_removals=[{"start_s": 1.0, "end_s": 9.0, "reason": "retake_review"}],
        )
        assert_noop_bailout(plan, BAILOUT_MAX_REMOVAL, 10.0)

    def test_output_too_short_bailout_defense_in_depth(self, monkeypatch):
        # Unreachable with the shipped constants (see the invariant test below),
        # so widen MAX_REMOVAL_FRAC to prove the rail still holds the line.
        monkeypatch.setattr(silence_cut, "MAX_REMOVAL_FRAC", 0.9)
        words = [w("a", 0.5, 1.0), w("b", 5.3, 5.7)]
        plan = build_cut_plan(words, [(1.0, 5.3)], 6.0)  # removes 4.05s of 6s
        assert_noop_bailout(plan, BAILOUT_OUTPUT_TOO_SHORT, 6.0)

    def test_shipped_constants_guarantee_min_output(self):
        # A plan passing both the clip-length and removal-fraction rails always
        # retains at least MIN_OUTPUT_S — the output rail is defense in depth.
        assert (1 - MAX_REMOVAL_FRAC) * MIN_CLIP_S >= MIN_OUTPUT_S


# ---------------------------------------------------------------------------------
# Explicit-consent budget clamp (over_budget_policy="clamp")
# ---------------------------------------------------------------------------------


def assert_partition(plan: CutPlan, duration: float):
    """keep_segments + removed must exactly tile [0, duration]."""
    intervals = sorted(
        [(lo, hi, "keep") for lo, hi in plan.keep_segments]
        + [(r.start_s, r.end_s, "cut") for r in plan.removed]
    )
    cursor = 0.0
    for lo, hi, _kind in intervals:
        assert lo == pytest.approx(cursor, abs=1e-6), intervals
        assert hi > lo
        cursor = hi
    assert cursor == pytest.approx(duration, abs=1e-6)


def clamp_budget(duration: float) -> float:
    return (
        min(MAX_REMOVAL_FRAC_REQUIRED * duration, duration - MIN_OUTPUT_S)
        - silence_cut.CLAMP_BUDGET_SLACK_S
    )


class TestOverBudgetClamp:
    """required_v1 contract: an over-budget removal set is clamped to the
    explicit-consent budget instead of tripping the auto-path bailout — the
    2026-08-25/31 prod incidents (three deterministic `unsafe_plan` render
    failures on one filler-heavy talking-to-camera clip) are the regression
    this class pins. Default policy stays byte-identical (TestSafetyRails)."""

    def test_over_budget_clamps_instead_of_bailing(self):
        # Same geometry as test_max_removal_exceeded_bailout: one 8.25s pause
        # removal on a 10s clip. Clamp trims it symmetrically to the 5.499s
        # budget (5.5 − slack) instead of returning the no-op bailout plan.
        words = [w("a", 0.5, 1.0), w("b", 9.5, 9.9)]
        plan = build_cut_plan(words, [(1.0, 9.5)], 10.0, over_budget_policy="clamp")

        assert plan.bailout_reason is None
        assert plan.clamped is True
        assert plan.clamp_budget_s == pytest.approx(clamp_budget(10.0))
        assert plan.proposed_removed_s == pytest.approx(8.25)
        assert plan.time_saved_s == pytest.approx(clamp_budget(10.0))
        assert_spans([(r.start_s, r.end_s) for r in plan.removed], [(2.5005, 7.9995)])
        assert_spans(plan.keep_segments, [(0.0, 2.5005), (7.9995, 10.0)])
        assert_partition(plan, 10.0)
        assert 10.0 - plan.time_saved_s >= MIN_OUTPUT_S

    def test_bailout_policy_is_the_default_and_unchanged(self):
        words = [w("a", 0.5, 1.0), w("b", 9.5, 9.9)]
        implicit = build_cut_plan(words, [(1.0, 9.5)], 10.0)
        explicit = build_cut_plan(words, [(1.0, 9.5)], 10.0, over_budget_policy="bailout")
        for plan in (implicit, explicit):
            assert plan.bailout_reason == BAILOUT_MAX_REMOVAL
            assert plan.clamped is False
            assert plan.proposed_removed_s is None

    # ── direct unit coverage of the greedy/trim/anchor mechanics ─────────────
    # (via _clamp_removals_to_budget: build_cut_plan treats every
    # forced_removals entry as protected, so raw geometry fixtures go straight
    # to the helper the way build_cut_plan calls it for DETECTED removals)

    def test_greedy_keeps_largest_removals_and_drops_the_rest(self):
        removals = [
            Removal(start_s=1.0, end_s=7.0, reason="silence"),
            Removal(start_s=8.0, end_s=13.0, reason="silence"),
            Removal(start_s=14.0, end_s=17.0, reason="silence"),
            Removal(start_s=18.0, end_s=18.5, reason="silence"),
        ]
        kept = silence_cut._clamp_removals_to_budget(removals, 11.0, 20.0, words=[])
        assert_spans([(r.start_s, r.end_s) for r in kept], [(1.0, 7.0), (8.0, 13.0)])

    def test_trim_is_symmetric_for_mid_clip_removals(self):
        removals = [Removal(start_s=1.0, end_s=9.0, reason="silence")]
        kept = silence_cut._clamp_removals_to_budget(removals, 5.5, 10.0, words=[])
        assert_spans([(r.start_s, r.end_s) for r in kept], [(2.25, 7.75)])

    def test_trim_anchors_trailing_removal_at_clip_end(self):
        # A mid-clip symmetric shrink here would strand dead air AFTER the
        # cut, at the very end of the video.
        removals = [Removal(start_s=3.0, end_s=10.0, reason="silence")]
        kept = silence_cut._clamp_removals_to_budget(removals, 5.5, 10.0, words=[])
        assert_spans([(r.start_s, r.end_s) for r in kept], [(4.5, 10.0)])

    def test_trim_anchors_leading_removal_at_zero(self):
        removals = [Removal(start_s=0.0, end_s=7.0, reason="silence")]
        kept = silence_cut._clamp_removals_to_budget(removals, 5.5, 10.0, words=[])
        assert_spans([(r.start_s, r.end_s) for r in kept], [(0.0, 5.5)])

    def test_trim_boundary_snaps_out_of_word_interiors(self):
        # The symmetric trim of (1.0, 9.0) to 5.5s would start at 2.25 —
        # strictly inside "ummm" (2.2–2.6). The boundary must snap past the
        # word (+PAD_S) so no partial word is resurrected (remap_words
        # precondition: removals never intrude into kept words).
        removals = [Removal(start_s=1.0, end_s=9.0, reason="silence")]
        words = silence_cut._normalize_words([w("ummm", 2.2, 2.6)])
        kept = silence_cut._clamp_removals_to_budget(removals, 5.5, 10.0, words=words)
        assert_spans([(r.start_s, r.end_s) for r in kept], [(2.6 + silence_cut.PAD_S, 7.75)])

    def test_protected_spans_survive_the_clamp_whole(self):
        # `_validate_speech_cut_publication` demands every forced interval stay
        # covered — the protected removal is charged first and kept WHOLE even
        # though largest-first ordering preferred the 6s block; the detected
        # block then trims into the 0.5s leftover budget.
        removals = [
            Removal(start_s=1.0, end_s=7.0, reason="silence"),
            Removal(start_s=8.0, end_s=12.5, reason="retake_review"),
        ]
        kept = silence_cut._clamp_removals_to_budget(
            removals, 5.0, 20.0, words=[], protected_spans=[(8.0, 12.5)]
        )
        assert_spans([(r.start_s, r.end_s) for r in kept], [(3.75, 4.25), (8.0, 12.5)])

    def test_sub_min_cut_leftover_budget_skips_instead_of_micro_trimming(self):
        # After keeping the 6s block only 0.05s of budget remains — below
        # MIN_CUT_S, so the next removal is SKIPPED whole (a 50ms jump cut is
        # not worth it), never trimmed into a micro-cut.
        removals = [
            Removal(start_s=1.0, end_s=7.0, reason="silence"),
            Removal(start_s=8.0, end_s=8.3, reason="silence"),
        ]
        kept = silence_cut._clamp_removals_to_budget(removals, 6.05, 20.0, words=[])
        assert_spans([(r.start_s, r.end_s) for r in kept], [(1.0, 7.0)])

    def test_snap_cascade_collapsing_below_min_cut_drops_the_trimmed_removal(self):
        # Leading-anchored trim of (0, 8) to a 0.5s budget puts the end
        # boundary at 0.5 — inside "word" (0.4–0.62), so it snaps LEFT to
        # 0.4 − PAD_S = 0.28 and the 0.28s remainder is still kept.
        removals = [Removal(start_s=0.0, end_s=8.0, reason="silence")]
        one_word = silence_cut._normalize_words([w("word", 0.4, 0.62)])
        kept = silence_cut._clamp_removals_to_budget(removals, 0.5, 10.0, words=one_word)
        assert_spans([(r.start_s, r.end_s) for r in kept], [(0.0, 0.4 - PAD_S)])

        # With a neighboring word under the snapped landing spot the fixpoint
        # loop cascades (0.28 is inside "early" 0.05–0.33 → 0.05 − PAD_S < 0)
        # and the collapsed remainder falls below MIN_CUT_S — the removal must
        # be DROPPED entirely, never emitted as a degenerate/negative span.
        two_words = silence_cut._normalize_words([w("early", 0.05, 0.33), w("word", 0.4, 0.62)])
        kept = silence_cut._clamp_removals_to_budget(removals, 0.5, 10.0, words=two_words)
        assert kept == []

    # ── build_cut_plan-level behavior ────────────────────────────────────────

    def test_forced_removals_survive_clamp_via_build_cut_plan(self):
        # Detected pause removals compete for the budget; the user-approved
        # forced cut must come through untouched (publication validation).
        words = [
            w("k1", 0.2, 0.8),
            w("k2", 7.5, 8.0),
            w("k3", 14.6, 15.2),
            w("k4", 17.2, 17.8),
            w("k5", 18.7, 19.3),
        ]
        silences = [(1.1, 7.2), (8.3, 14.3)]
        plan = build_cut_plan(
            words,
            silences,
            20.0,
            forced_removals=[{"start_s": 18.0, "end_s": 18.5, "reason": "retake_review"}],
            over_budget_policy="clamp",
        )

        assert plan.bailout_reason is None
        assert plan.clamped is True
        assert any(r.start_s <= 18.0 + 1e-6 and r.end_s >= 18.5 - 1e-6 for r in plan.removed), (
            plan.removed
        )
        assert plan.time_saved_s <= clamp_budget(20.0) + 1e-6
        assert_partition(plan, 20.0)

    def test_protected_forced_overload_still_bails_output_too_short(self):
        # The one rail the clamp deliberately cannot lift: forced/manual
        # removals are budget-exempt (protected), so a forced set that leaves
        # <MIN_OUTPUT_S of clip must still bail with output_too_short rather
        # than ship a 1.1s video (the REACHABLE defense-in-depth branch).
        plan = build_cut_plan(
            [w("hi", 0.15, 0.45), w("bye", 9.6, 9.85)],
            [],
            10.0,
            forced_removals=[{"start_s": 0.6, "end_s": 9.5, "reason": "manual_review"}],
            include_silence_and_fillers=False,
            over_budget_policy="clamp",
        )

        assert plan.bailout_reason == BAILOUT_OUTPUT_TOO_SHORT
        assert plan.removed == []
        assert plan.clamped is False  # bailout plan carries no clamp metadata
        assert_spans(plan.keep_segments, [(0.0, 10.0)])

    def test_lead_cut_survives_budget_competition(self):
        # Adversarial regression (2026-08-31): edge cuts are charged FIRST —
        # a duration-ordered greedy spent the budget on mid/trail blocks and
        # dropped the lead cut, shipping a ~2.7s dead-air OPENING in exactly
        # the filler-heavy clips this feature targets (hook-window rule).
        words = [w("hey", 2.8, 3.4), w("ok", 9.0, 9.6)]
        silences = [(0.0, 2.7), (3.7, 8.9), (9.7, 15.0)]
        plan = build_cut_plan(words, silences, 15.0, over_budget_policy="clamp")

        assert plan.bailout_reason is None
        assert any(r.start_s <= 1e-6 for r in plan.removed), plan.removed  # lead cut kept
        assert any(r.end_s >= 15.0 - 1e-6 for r in plan.removed), plan.removed  # trail too
        assert plan.time_saved_s <= clamp_budget(15.0) + 1e-6
        assert_partition(plan, 15.0)

    def test_far_apart_forced_spans_protect_intersections_not_hull(self):
        # Adversarial regression (2026-08-31): two tiny forced cuts at
        # opposite ends of one merged dead-air carrier must protect ONLY the
        # forced intersections — a hull would drag the whole dead gap along,
        # blowing the budget or resurrecting the output rail's unsafe_plan.
        words = [w("k1", 0.2, 0.8), w("k2", 9.0, 9.6)]
        silences = [(1.1, 8.7)]
        forced = [
            {"start_s": 1.5, "end_s": 1.75, "reason": "retake_review"},
            {"start_s": 8.0, "end_s": 8.25, "reason": "retake_review"},
        ]
        plan = build_cut_plan(
            words, silences, 10.0, forced_removals=forced, over_budget_policy="clamp"
        )

        assert plan.bailout_reason is None
        for lo, hi in [(1.5, 1.75), (8.0, 8.25)]:
            assert any(r.start_s <= lo + 1e-6 and r.end_s >= hi - 1e-6 for r in plan.removed), (
                lo,
                hi,
                plan.removed,
            )
        assert plan.time_saved_s == pytest.approx(0.5)  # intersections only, no hull
        assert_partition(plan, 10.0)

    def test_trim_boundary_keeps_pad_clearance_to_resurrected_words(self):
        # Adversarial regression (2026-08-31): a trim boundary landing just
        # AFTER a formerly-removed word resurrected it with sub-PAD_S
        # clearance to the jump cut (a surviving "um" 40ms before the cut).
        # The snap guard zone includes the word's PAD_S flank on the cut side.
        removals = [Removal(start_s=2.0, end_s=10.0, reason="silence")]
        words = silence_cut._normalize_words([w("um", 4.3, 4.46)])
        kept = silence_cut._clamp_removals_to_budget(removals, 5.5, 10.0, words=words)
        assert_spans([(r.start_s, r.end_s) for r in kept], [(4.46 + PAD_S, 10.0)])

    def test_merged_forced_carrier_shrinks_to_envelope_not_whole(self):
        # Red-team regression (2026-08-31): a tiny forced cut that MERGES with
        # a huge detected silence block must not protect the whole 17.6s
        # carrier — that would blow the budget or resurrect the output rail's
        # unsafe_plan. The carrier shrinks to the forced ENVELOPE: coverage
        # preserved, no bailout, nothing ships over budget.
        words = [w("k1", 0.2, 0.8), w("k2", 18.7, 19.3)]
        silences = [(1.1, 18.4)]
        forced = [{"start_s": 18.4, "end_s": 18.7, "reason": "retake_review"}]

        plan = build_cut_plan(
            words, silences, 20.0, forced_removals=forced, over_budget_policy="clamp"
        )
        assert plan.bailout_reason is None
        assert plan.clamped is True
        assert any(r.start_s <= 18.4 + 1e-6 and r.end_s >= 18.7 - 1e-6 for r in plan.removed), (
            plan.removed
        )
        assert plan.time_saved_s <= clamp_budget(20.0) + 1e-6
        assert_partition(plan, 20.0)

        # Same clip without the forced cut clamps normally to the full budget.
        no_forced = build_cut_plan(words, silences, 20.0, over_budget_policy="clamp")
        assert no_forced.bailout_reason is None
        assert no_forced.time_saved_s == pytest.approx(clamp_budget(20.0))

    def test_short_clip_float_band_never_resurrects_output_too_short(self):
        # Adversarial-review critical repro: on 5.0–6.67s clips the
        # duration−MIN_OUTPUT_S budget leg binds and 1 ulp of trim rounding
        # used to trip the epsilon-free output rail, resurrecting the strict
        # unsafe_plan failure. The budget slack must make this impossible.
        words = [w("hi", 0.1, 0.638), w("ok", 5.821, 6.288)]
        plan = build_cut_plan(words, [(0.638, 5.821)], 6.338, over_budget_policy="clamp")

        assert plan.bailout_reason is None
        assert plan.clamped is True
        assert 6.338 - plan.time_saved_s >= MIN_OUTPUT_S
        assert_partition(plan, 6.338)

    def test_clamp_budget_respects_min_output_floor(self):
        # At MIN_CLIP_S the frac budget (2.75s) exceeds what MIN_OUTPUT_S
        # permits (2.0s − slack) — the floor must win and the output stays
        # above MIN_OUTPUT_S with no output_too_short bailout.
        words = [w("hi", 0.1, 0.4), w("yo", 4.7, 4.95)]
        plan = build_cut_plan(words, [(0.5, 4.6)], MIN_CLIP_S, over_budget_policy="clamp")

        assert plan.bailout_reason is None
        assert plan.clamped is True
        assert plan.clamp_budget_s == pytest.approx(clamp_budget(MIN_CLIP_S))
        assert plan.time_saved_s == pytest.approx(clamp_budget(MIN_CLIP_S))
        assert MIN_CLIP_S - plan.time_saved_s >= MIN_OUTPUT_S

    def test_between_auto_rail_and_budget_renders_unclamped(self):
        # The 40–55% band: over the auto rail but under the consent budget —
        # the clamp policy must pass the FULL plan through untouched while the
        # default policy bails (pins the band where the two policies differ).
        words = [w("hi", 0.3, 0.8), w("there", 9.7, 10.2)]
        forced = [{"start_s": 1.0, "end_s": 9.5, "reason": "retake_review"}]

        default_plan = build_cut_plan(
            words, [], 20.0, forced_removals=forced, include_silence_and_fillers=False
        )
        assert default_plan.bailout_reason == BAILOUT_MAX_REMOVAL

        plan = build_cut_plan(
            words,
            [],
            20.0,
            forced_removals=forced,
            include_silence_and_fillers=False,
            over_budget_policy="clamp",
        )
        assert plan.bailout_reason is None
        assert plan.clamped is False
        assert plan.time_saved_s == pytest.approx(8.5)

    def test_clamp_under_budget_plan_is_untouched(self):
        plan = build_cut_plan(lexical_fixture(), [], DUR, over_budget_policy="clamp")
        baseline = build_cut_plan(lexical_fixture(), [], DUR)

        assert plan.clamped is False
        assert plan.proposed_removed_s is None
        assert plan.removed == baseline.removed
        assert plan.keep_segments == baseline.keep_segments
        assert plan.time_saved_s == pytest.approx(baseline.time_saved_s)

    def test_incident_clip_shape_default_bails_clamp_renders(self):
        # Sanitized geometry of the 2026-08-31 prod incident (job ca380890):
        # a 10.0s talking-to-camera clip with three short speech bursts,
        # filler tokens, and ~6.1s of pauses. ~6.4s proposed removal trips the
        # 40% auto rail; under explicit consent the clamp must deliver a real
        # cleaned plan instead of the deterministic unsafe_plan dead end.
        words = [
            w("um", 0.2, 0.5),
            w("deneme", 0.6, 1.3),
            w("simdi", 2.2, 2.5),
            w("deneme", 2.6, 3.0),
            w("yapiyorum", 3.1, 3.5),
            w("um", 6.8, 7.0),
            w("um", 7.1, 7.3),
            w("simdi", 7.4, 7.7),
            w("soyle", 7.8, 8.1),
        ]
        silences = [(1.3, 2.2), (3.5, 6.8), (8.1, 10.0)]

        default_plan = build_cut_plan(words, silences, 10.0)
        assert default_plan.bailout_reason == BAILOUT_MAX_REMOVAL

        clamped_plan = build_cut_plan(words, silences, 10.0, over_budget_policy="clamp")
        assert clamped_plan.bailout_reason is None
        assert clamped_plan.clamped is True
        assert clamped_plan.removed  # a real cut set survives
        assert clamped_plan.time_saved_s <= clamp_budget(10.0) + 1e-6
        assert 10.0 - clamped_plan.time_saved_s >= MIN_OUTPUT_S
        assert_partition(clamped_plan, 10.0)

    def test_clamp_metadata_gated_out_of_legacy_summary_and_payload(self):
        # Un-clamped plans must serialize byte-identically to the pre-clamp
        # shape (admin strip contract + finalize whitelist).
        plain = build_cut_plan(lexical_fixture(), [], DUR)
        summary = plan_summary(plain, original_duration_s=DUR)
        assert set(summary) == {"removed", "time_saved_s", "version", "original_duration_s"}
        payload = plan_event_payload(plain, variant_id="v", retake_spans=0, applied=True)
        assert "clamped" not in payload

        words = [w("a", 0.5, 1.0), w("b", 9.5, 9.9)]
        clamped = build_cut_plan(words, [(1.0, 9.5)], 10.0, over_budget_policy="clamp")
        summary = plan_summary(clamped, original_duration_s=10.0)
        assert summary["clamped"] is True
        assert summary["proposed_removed_s"] == pytest.approx(8.25)
        assert summary["clamp_budget_s"] == pytest.approx(clamp_budget(10.0))
        payload = plan_event_payload(clamped, variant_id="v", retake_spans=0, applied=True)
        assert payload["clamped"] is True
        assert payload["proposed_removed_s"] == pytest.approx(8.25)


# ---------------------------------------------------------------------------------
# Retake spans
# ---------------------------------------------------------------------------------


class TestRetakeSpans:
    def test_span_snaps_outward_to_padded_word_boundaries(self):
        words = [w("a", 1.0, 1.5), w("b", 2.0, 2.5), w("c", 3.0, 3.5), w("d", 4.0, 4.5)]
        plan = build_cut_plan(words, [], 10.0, retake_spans=[(1, 2)])
        removal = only_removal(plan)
        assert removal.reason == REASON_RETAKE
        assert_spans([(removal.start_s, removal.end_s)], [(1.5 + PAD_S, 4.0 - PAD_S)])
        # covers the abandoned take fully, outward only
        assert removal.start_s <= 2.0 and removal.end_s >= 3.5
        # never mid-word: boundaries sit strictly outside the surviving words
        for surviving in (words[0], words[3]):
            for boundary in (removal.start_s, removal.end_s):
                assert not (surviving["start_s"] < boundary < surviving["end_s"])

    def test_boundary_never_mid_word_when_gap_thinner_than_pad(self):
        # gap a→b is 0.05s < PAD_S: the padded boundary (2.12) would land INSIDE
        # the removed word b — the boundary must clamp to b's own start instead.
        words = [w("a", 1.0, 2.0), w("b", 2.05, 3.0), w("c", 4.0, 4.5)]
        plan = build_cut_plan(words, [], 12.0, retake_spans=[(1, 1)])
        removal = only_removal(plan)
        assert removal.start_s == pytest.approx(2.05, abs=1e-9)
        assert removal.end_s == pytest.approx(4.0 - PAD_S, abs=1e-9)
        assert not (1.0 < removal.start_s < 2.0)  # not inside the kept word a

    def test_span_at_first_word_extends_to_clip_start(self):
        words = [w("a", 0.8, 1.3), w("b", 2.0, 2.5), w("c", 3.0, 3.5)]
        plan = build_cut_plan(words, [], 10.0, retake_spans=[(0, 1)])
        removal = only_removal(plan)
        assert_spans([(removal.start_s, removal.end_s)], [(0.0, 3.0 - PAD_S)])
        assert_spans(plan.keep_segments, [(2.88, 10.0)])

    def test_span_at_last_word_extends_to_duration(self):
        words = [w("a", 1.0, 1.5), w("b", 16.0, 16.5), w("c", 17.0, 17.5)]
        plan = build_cut_plan(words, [], DUR, retake_spans=[(2, 2)])
        removal = only_removal(plan)
        assert_spans([(removal.start_s, removal.end_s)], [(16.5 + PAD_S, DUR)])

    def test_invalid_spans_skipped_defensively(self):
        words = [w("a", 1.0, 1.5), w("b", 2.0, 2.5), w("c", 3.0, 3.5)]
        spans = [(5, 9), (2, 1), (-1, 0), (None, 1), ("x", "y")]
        assert_no_cuts(build_cut_plan(words, [], DUR, retake_spans=spans))


# ---------------------------------------------------------------------------------
# remap_words
# ---------------------------------------------------------------------------------


def plan_with_removals(removals: list[tuple[float, float]], duration: float) -> CutPlan:
    removed = [Removal(start_s=lo, end_s=hi, reason=REASON_SILENCE) for lo, hi in removals]
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for lo, hi in removals:
        if lo > cursor:
            keep.append((cursor, lo))
        cursor = hi
    if cursor < duration:
        keep.append((cursor, duration))
    saved = sum(hi - lo for lo, hi in removals)
    return CutPlan(keep_segments=keep, removed=removed, time_saved_s=saved)


class TestRemapWords:
    def test_drops_removed_words_and_shifts_survivors(self):
        plan = plan_with_removals([(2.0, 3.0)], 10.0)
        words = [w("a", 1.0, 1.5), w("gone", 2.2, 2.8), w("b", 4.0, 4.5)]
        remapped = remap_words(words, plan)
        assert [entry["text"] for entry in remapped] == ["a", "b"]
        assert remapped[0]["start_s"] == pytest.approx(1.0, abs=1e-9)
        assert remapped[0]["end_s"] == pytest.approx(1.5, abs=1e-9)
        assert remapped[1]["start_s"] == pytest.approx(3.0, abs=1e-9)
        assert remapped[1]["end_s"] == pytest.approx(3.5, abs=1e-9)

    def test_cumulative_shift_across_multiple_removals(self):
        plan = plan_with_removals([(1.0, 2.0), (5.0, 6.0)], 10.0)
        words = [w("a", 0.5, 0.9), w("b", 3.0, 3.5), w("c", 7.0, 7.5)]
        remapped = remap_words(words, plan)
        assert_spans(
            [(entry["start_s"], entry["end_s"]) for entry in remapped],
            [(0.5, 0.9), (2.0, 2.5), (5.0, 5.5)],
        )

    def test_word_starting_exactly_at_removal_end(self):
        plan = plan_with_removals([(1.0, 2.0)], 10.0)
        remapped = remap_words([w("a", 2.0, 2.5)], plan)
        assert_spans([(remapped[0]["start_s"], remapped[0]["end_s"])], [(1.0, 1.5)])

    def test_word_ending_exactly_at_removal_start_unshifted(self):
        plan = plan_with_removals([(2.0, 3.0)], 10.0)
        remapped = remap_words([w("a", 1.5, 2.0)], plan)
        assert_spans([(remapped[0]["start_s"], remapped[0]["end_s"])], [(1.5, 2.0)])

    def test_word_coinciding_with_removal_dropped(self):
        plan = plan_with_removals([(2.0, 2.5)], 10.0)
        assert remap_words([w("gone", 2.0, 2.5)], plan) == []

    def test_accepts_object_shaped_words(self):
        plan = plan_with_removals([(2.0, 3.0)], 10.0)
        remapped = remap_words([SimpleNamespace(text="hi", start_s=4.0, end_s=4.5)], plan)
        assert_spans([(remapped[0]["start_s"], remapped[0]["end_s"])], [(3.0, 3.5)])

    def test_noop_plan_is_identity(self):
        words = [w("a", 1.0, 1.5), w("b", 2.0, 2.5)]
        remapped = remap_words(words, no_op_plan(DUR))
        assert remapped == [
            {"text": "a", "start_s": 1.0, "end_s": 1.5},
            {"text": "b", "start_s": 2.0, "end_s": 2.5},
        ]

    def test_end_to_end_lexical_cut_then_remap(self):
        words = lexical_fixture()
        plan = build_cut_plan(words, [], DUR)
        remapped = remap_words(words, plan)
        assert [entry["text"] for entry in remapped] == ["so", "lets"]
        assert_spans(
            [(entry["start_s"], entry["end_s"]) for entry in remapped],
            [(1.0, 1.3), (3.0 - 0.64, 3.3 - 0.64)],
        )
        # kept spans keep their exact durations
        assert remapped[1]["end_s"] - remapped[1]["start_s"] == pytest.approx(0.3, abs=1e-9)

    def test_output_is_monotonic(self):
        plan = plan_with_removals([(1.0, 2.0), (4.0, 5.5)], 12.0)
        words = [w(f"w{i}", 2.0 + i, 2.4 + i) for i in range(6) if not (2.0 <= 2.0 + i < 5.5)]
        remapped = remap_words(words, plan)
        starts = [entry["start_s"] for entry in remapped]
        assert starts == sorted(starts)


# ---------------------------------------------------------------------------------
# no_op_plan + plan types
# ---------------------------------------------------------------------------------


class TestNoOpPlan:
    def test_identity_shape(self):
        plan = no_op_plan(12.5)
        assert plan.keep_segments == [(0.0, 12.5)]
        assert plan.removed == []
        assert plan.time_saved_s == 0.0
        assert plan.version == 1
        assert plan.bailout_reason is None

    def test_bailout_reason_passthrough(self):
        assert no_op_plan(8.0, bailout_reason=BAILOUT_NO_WORDS).bailout_reason == BAILOUT_NO_WORDS

    def test_removal_is_frozen(self):
        removal = Removal(start_s=1.0, end_s=2.0, reason=REASON_SILENCE)
        with pytest.raises(dataclasses.FrozenInstanceError):
            removal.start_s = 0.0  # type: ignore[misc]


# ---------------------------------------------------------------------------------
# Property-style sweep (seeded — deterministic, xdist-safe)
# ---------------------------------------------------------------------------------


def _fully_removed(start: float, end: float, plan: CutPlan) -> bool:
    return any(start >= r.start_s - 1e-9 and end <= r.end_s + 1e-9 for r in plan.removed)


def _covered_by_keep(start: float, end: float, plan: CutPlan) -> bool:
    return any(lo - 1e-6 <= start and end <= hi + 1e-6 for lo, hi in plan.keep_segments)


def test_property_random_layouts_hold_plan_invariants():
    rng = random.Random(20260709)  # fixed literal seed — NEVER randomize collection
    for _ in range(40):
        duration = rng.uniform(8.0, 60.0)
        words: list[dict] = []
        t = rng.uniform(0.0, 1.5)
        while t < duration - 1.0 and len(words) < 60:
            end = min(t + rng.uniform(0.08, 0.6), duration)
            text = rng.choice(
                ["hello", "world", "um", "uh", "ııı", "take", "go", "eee", "ee", "aa"]
            )
            extra = {}
            if rng.random() < 0.3:
                extra["segment_avg_logprob"] = rng.uniform(-2.0, 0.0)
                extra["segment_no_speech_prob"] = rng.uniform(0.0, 1.0)
            words.append(w(text, t, end, **extra))
            t = end + rng.uniform(0.02, 2.0)

        silences: list[tuple[float, float]] = []
        if rng.random() < 0.5 and words[0]["start_s"] > 0.05:
            silences.append((0.0, words[0]["start_s"] * rng.uniform(0.5, 1.0)))
        for prev, nxt in zip(words, words[1:]):
            gap_lo, gap_hi = prev["end_s"], nxt["start_s"]
            if gap_hi - gap_lo > 0.05 and rng.random() < 0.6:
                lo = rng.uniform(gap_lo, gap_hi)
                hi = rng.uniform(lo, gap_hi)
                if hi - lo > 0.01:
                    silences.append((lo, hi))
        if rng.random() < 0.5 and words[-1]["end_s"] < duration - 0.05:
            silences.append((rng.uniform(words[-1]["end_s"], duration), duration))

        retake_spans = None
        if len(words) >= 4 and rng.random() < 0.3:
            i = rng.randrange(0, len(words) - 1)
            retake_spans = [(i, min(len(words) - 1, i + rng.randrange(0, 3)))]

        plan = build_cut_plan(words, silences, duration, retake_spans=retake_spans)

        if plan.bailout_reason is not None:
            assert plan.removed == []
            assert plan.keep_segments == [(0.0, duration)]
            assert plan.time_saved_s == 0.0
            continue

        # keep_segments: sorted, non-overlapping, inside [0, duration]
        for lo, hi in plan.keep_segments:
            assert 0.0 <= lo < hi <= duration + 1e-9
        for (_, prev_hi), (nxt_lo, _) in zip(plan.keep_segments, plan.keep_segments[1:]):
            assert nxt_lo > prev_hi
        # removals: sorted, non-overlapping, each >= MIN_CUT_S, capped total
        for removal in plan.removed:
            assert 0.0 <= removal.start_s < removal.end_s <= duration + 1e-9
            assert removal.end_s - removal.start_s >= MIN_CUT_S - 1e-9
        for r_prev, r_next in zip(plan.removed, plan.removed[1:]):
            assert r_next.start_s > r_prev.end_s
        total_removed = sum(r.end_s - r.start_s for r in plan.removed)
        assert total_removed <= MAX_REMOVAL_FRAC * duration + 1e-9
        assert duration - total_removed >= MIN_OUTPUT_S - 1e-9
        assert plan.time_saved_s == pytest.approx(total_removed, abs=1e-9)
        # keep + removed exactly partitions [0, duration]
        kept_total = sum(hi - lo for lo, hi in plan.keep_segments)
        assert kept_total + total_removed == pytest.approx(duration, abs=1e-6)

        # every surviving word's span sits inside one keep segment
        survivors = [
            word for word in words if not _fully_removed(word["start_s"], word["end_s"], plan)
        ]
        for word in survivors:
            assert _covered_by_keep(word["start_s"], word["end_s"], plan), (word, plan)

        # remap: one entry per survivor, monotonic, durations preserved
        remapped = remap_words(words, plan)
        assert len(remapped) == len(survivors)
        starts = [entry["start_s"] for entry in remapped]
        assert starts == sorted(starts)
        for original, entry in zip(survivors, remapped):
            original_len = original["end_s"] - original["start_s"]
            assert entry["end_s"] - entry["start_s"] == pytest.approx(original_len, abs=1e-6)


# ---------------------------------------------------------------------------------
# Micro-fragment absorption (round 2 — MIN_KEEP_SEGMENT_S glitch hygiene)
# ---------------------------------------------------------------------------------


class TestMicroFragmentAbsorb:
    """Word-free keep fragments < MIN_KEEP_SEGMENT_S never survive.

    Found in local testing 2026-07-09: keep (16.60, 16.71) flashed 110ms of
    video (3 frames) between an "um" cut and a pause cut — reads as a glitch.
    """

    def test_fragment_between_two_removals_is_absorbed(self):
        # Words at 0.5-1.5 and 5.0-6.0; "um" filler at 2.0-2.3 and a long
        # silent pause 2.5-4.5 leave a ~0.1s word-free fragment between the
        # filler cut's padded end and the pause cut's start.
        words = [
            {"text": "hello", "start_s": 0.5, "end_s": 1.5},
            {"text": "um", "start_s": 2.0, "end_s": 2.3},
            {"text": "world", "start_s": 5.0, "end_s": 6.0},
        ]
        silences = [(2.45, 4.85)]
        plan = build_cut_plan(words, silences, 8.0)
        assert plan.bailout_reason is None
        for lo, hi in plan.keep_segments:
            assert hi - lo >= MIN_KEEP_SEGMENT_S - 1e-9, plan.keep_segments

    def test_fragment_carrying_a_word_is_never_absorbed(self):
        # A short word sits between two removals — the fragment holding it
        # must survive even though it's shorter than MIN_KEEP_SEGMENT_S.
        words = [
            {"text": "hello", "start_s": 0.5, "end_s": 1.5},
            {"text": "um", "start_s": 2.0, "end_s": 2.2},
            {"text": "no", "start_s": 2.42, "end_s": 2.55},  # kept word
            {"text": "um", "start_s": 2.75, "end_s": 2.95},
            {"text": "world", "start_s": 5.0, "end_s": 6.0},
        ]
        plan = build_cut_plan(words, [(3.2, 4.8)], 8.0)
        kept_word_covered = any(lo <= 2.42 and hi >= 2.55 for lo, hi in plan.keep_segments)
        assert kept_word_covered, plan.keep_segments

    def test_trailing_sliver_after_last_removal_is_absorbed(self):
        # Trailing filler cut ends 0.19s before clip end (word-free tail).
        words = [
            {"text": "hello", "start_s": 0.5, "end_s": 1.5},
            {"text": "world", "start_s": 2.0, "end_s": 3.0},
            {"text": "um", "start_s": 20.5, "end_s": 22.5},
        ]
        silences = [(3.1, 20.4)]
        plan = build_cut_plan(words, silences, 22.7)
        # last keep segment must not be a dangling word-free sliver
        last_lo, last_hi = plan.keep_segments[-1]
        assert last_hi - last_lo >= MIN_KEEP_SEGMENT_S - 1e-9 or last_hi == pytest.approx(22.7)
        for lo, hi in plan.keep_segments:
            assert hi - lo >= MIN_KEEP_SEGMENT_S - 1e-9, plan.keep_segments


# ---------------------------------------------------------------------------------
# Removal-count cap (MAX_REMOVALS — filter-graph arg-length defense)
# ---------------------------------------------------------------------------------


def stutter_fixture(n: int, period: float = 2.0) -> tuple[list[dict], float]:
    """``n`` isolated lexical-filler removals with strictly increasing sizes.

    Unit i: real word [t, t+0.5], filler [t+1.0, t+1.0+d_i] with
    d_i = 0.15 + 0.002*i, silences=[] (rules 2+3 off) — each filler yields
    exactly one PAD_S-flanked removal of size d_i + 2*PAD_S, never clamped or
    merged (all inter-cut gaps stay well above PAD_S and MIN_KEEP_SEGMENT_S).
    """
    words: list[dict] = []
    for i in range(n):
        t = i * period
        words.append(w(f"word{i}", t, t + 0.5))
        words.append(w("um", t + 1.0, t + 1.0 + 0.15 + 0.002 * i))
    duration = n * period + 1.0
    words.append(w("tail", duration - 0.8, duration - 0.4))
    return words, duration


class TestRemovalCountCap:
    def test_150_removals_capped_to_100_largest(self):
        words, duration = stutter_fixture(150)
        plan = build_cut_plan(words, [], duration)
        assert plan.bailout_reason is None
        assert len(plan.removed) == MAX_REMOVALS == 100

        # largest survive: every kept removal is at least as long as the
        # 100th-largest synthetic cut (i=50 → 0.15 + 0.1 + 2*PAD_S)
        smallest_kept = min(r.end_s - r.start_s for r in plan.removed)
        assert smallest_kept >= 0.25 + 2 * PAD_S - 1e-9
        # ...and the single largest cut (i=149) is among the survivors
        largest = max(r.end_s - r.start_s for r in plan.removed)
        assert largest == pytest.approx(0.15 + 0.002 * 149 + 2 * PAD_S, abs=1e-9)

        # re-sorted by time after the cap
        starts = [r.start_s for r in plan.removed]
        assert starts == sorted(starts)
        for r_prev, r_next in zip(plan.removed, plan.removed[1:]):
            assert r_next.start_s > r_prev.end_s

        # plan validity: keep + removed exactly partitions [0, duration],
        # time_saved recomputed from the CAPPED set
        total_removed = sum(r.end_s - r.start_s for r in plan.removed)
        kept_total = sum(hi - lo for lo, hi in plan.keep_segments)
        assert kept_total + total_removed == pytest.approx(duration, abs=1e-6)
        assert plan.time_saved_s == pytest.approx(total_removed, abs=1e-9)
        assert total_removed <= MAX_REMOVAL_FRAC * duration + 1e-9
        for (_, prev_hi), (nxt_lo, _) in zip(plan.keep_segments, plan.keep_segments[1:]):
            assert nxt_lo > prev_hi

    def test_at_cap_no_removal_dropped(self, monkeypatch):
        monkeypatch.setattr(silence_cut, "MAX_REMOVALS", 10)
        words, duration = stutter_fixture(10)
        plan = build_cut_plan(words, [], duration)
        assert plan.bailout_reason is None
        assert len(plan.removed) == 10  # exactly at the cap — untouched

    def test_tiebreak_equal_durations_keeps_earliest(self, monkeypatch):
        monkeypatch.setattr(silence_cut, "MAX_REMOVALS", 3)
        # Five BITWISE-identical-size filler cuts — every boundary clamps to a
        # neighboring word edge on an exact binary fraction (multiples of
        # 1/16), so each removal is exactly (t+1.0, t+1.5625): duration
        # 0.5625 with no per-unit float drift. Tiebreak must keep the
        # EARLIEST three by start_s.
        words: list[dict] = []
        for i in range(5):
            t = i * 4.0
            words.append(w(f"word{i}a", t, t + 1.0))  # ends AT the filler start
            words.append(w("um", t + 1.0, t + 1.5))
            words.append(w(f"word{i}b", t + 1.5625, t + 2.0))  # gap 0.0625 < PAD_S
        plan = build_cut_plan(words, [], 21.0)
        assert plan.bailout_reason is None
        assert len(plan.removed) == 3
        assert_spans(
            [(r.start_s, r.end_s) for r in plan.removed],
            [(t + 1.0, t + 1.5625) for t in (0.0, 4.0, 8.0)],  # earliest three
        )


# ---------------------------------------------------------------------------------
# Task-layer serialization helpers (plan_summary / plan_event_payload)
# ---------------------------------------------------------------------------------


def summary_fixture_plan() -> CutPlan:
    return CutPlan(
        keep_segments=[(0.0, 1.234567), (1.9876543, 3.0), (3.5, 10.0)],
        removed=[
            Removal(start_s=1.234567, end_s=1.9876543, reason=REASON_SILENCE),
            Removal(start_s=3.0, end_s=3.5, reason=REASON_FILLER_LEXICAL),
        ],
        time_saved_s=1.2530873,
    )


class TestPlanSummary:
    def test_shape_and_rounding(self):
        summary = plan_summary(summary_fixture_plan(), original_duration_s=10.0000456)
        assert summary == {
            "removed": [
                {"start_s": 1.235, "end_s": 1.988, "reason": REASON_SILENCE},
                {"start_s": 3.0, "end_s": 3.5, "reason": REASON_FILLER_LEXICAL},
            ],
            "time_saved_s": 1.253,
            "version": 1,
            "original_duration_s": 10.0,
        }

    def test_original_duration_defaults_to_none(self):
        summary = plan_summary(no_op_plan(5.0))
        assert summary == {
            "removed": [],
            "time_saved_s": 0.0,
            "version": 1,
            "original_duration_s": None,
        }


class TestPlanEventPayload:
    def test_shape_and_reasons_histogram(self):
        plan = CutPlan(
            keep_segments=[(0.0, 20.0)],
            removed=[
                Removal(start_s=1.0, end_s=1.5, reason=REASON_SILENCE),
                Removal(start_s=2.0, end_s=2.5, reason=REASON_FILLER_LEXICAL),
                Removal(start_s=3.0, end_s=3.5, reason=REASON_SILENCE),
                Removal(start_s=4.0, end_s=4.5, reason=REASON_RETAKE),
            ],
            time_saved_s=2.0004567,
        )
        payload = plan_event_payload(plan, variant_id="song_text", retake_spans=2, applied=True)
        assert payload == {
            "variant_id": "song_text",
            "removed_count": 4,
            "time_saved_s": 2.0,
            "reasons": {REASON_SILENCE: 2, REASON_FILLER_LEXICAL: 1, REASON_RETAKE: 1},
            "retake_spans": 2,
            "applied": True,
            "cut_reused": False,  # default
        }

    def test_noop_plan_empty_histogram(self):
        payload = plan_event_payload(
            no_op_plan(8.0), variant_id="original_text", retake_spans=0, applied=False
        )
        assert payload["removed_count"] == 0
        assert payload["reasons"] == {}
        assert payload["time_saved_s"] == 0.0
        assert payload["applied"] is False

    def test_extra_merges_and_overrides_last(self):
        payload = plan_event_payload(
            no_op_plan(8.0),
            variant_id="v1",
            retake_spans=0,
            applied=False,
            cut_reused=True,
            extra={"bailout_reason": "no_words", "applied": True},
        )
        assert payload["cut_reused"] is True
        assert payload["bailout_reason"] == "no_words"  # extra key merged in
        assert payload["applied"] is True  # extra wins over the base field
