"""Golden: whisper-1 word timestamps are not speech truth (prod 2026-09-08).

Two production clips (jobs a75981f8 / d0e284cb, both ``required_v1`` with the
``mixed-gap-v1`` candidate applied) still rendered an audible hesitation:

* **TR** — the real ``ııı`` is voiced at 7.406–7.978 s, but whisper stamped the
  ``ııı,`` token at 8.04–8.26 s, entirely inside FFmpeg silence 7.978–8.293 s.
  The lexical cut removed silence, and the acoustic island was rejected as
  ``right_silence_too_short`` because the flank was measured on the silence
  clipped at the mis-stamped token start (62 ms instead of 315 ms).
* **EN** — the ``a...`` token spans 5.48–7.20 s and contains a 1.4 s silence.
  Every rule windows only BETWEEN tokens, so the pause was invisible.

Fixtures are the EXACT persisted ``timed_words`` of the two
``speech_cleanup_analyses`` rows plus ``silencedetect`` (noise=-30dB:d=0.1)
over the task's 16 kHz PCM extraction of the same sources, and one fresh
whisper-1 layout of the EN clip where the pause straddles two tokens.  V1
must stay byte-identical to the plans production produced; the V2 candidate
must cut the missed audio.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.pipeline.silence_cut as silence_cut
from app.pipeline.silence_cut import (
    PAD_S,
    TOKEN_SILENCE_MIN_OVERLAP_S,
    TOKEN_SILENCE_MIN_S,
    Removal,
    _CutWord,
    _normalize_words,
    _reconcile_words_with_silence,
    build_cut_plan,
    build_cut_plan_comparison,
    remap_words,
)
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupAnalysisResult,
    analyze_speech_cleanup,
)
from app.services.speech_cleanup_selection import DETECTOR_VERSION


def w(text: str, start: float, end: float) -> dict:
    return {"text": text, "start_s": start, "end_s": end}


def _rounded(plan) -> list[tuple[float, float, str]]:
    return [(round(r.start_s, 3), round(r.end_s, 3), r.reason) for r in plan.removed]


def _covered(plan, lo: float, hi: float) -> float:
    return sum(max(0.0, min(hi, r.end_s) - max(lo, r.start_s)) for r in plan.removed)


def _assert_partition(plan, duration: float) -> None:
    cursor = 0.0
    intervals = sorted(
        [
            *(("keep", a, b) for a, b in plan.keep_segments),
            *(("cut", r.start_s, r.end_s) for r in plan.removed),
        ],
        key=lambda item: item[1],
    )
    for _kind, lo, hi in intervals:
        assert abs(lo - cursor) < 1e-6, intervals
        assert hi > lo
        cursor = hi
    assert abs(cursor - duration) < 1e-6
    assert abs(sum(r.end_s - r.start_s for r in plan.removed) - plan.time_saved_s) < 1e-6


# --- TR clip (job a75981f8, analysis 7ed6b28e, plan-021 incident source) ----------

TR_DURATION_S = 10.0
TR_WORDS = [
    w("Iıı,", 1.18, 2.04),
    w("deneme.", 2.04, 2.4),
    w("Şimdi", 3.26, 3.42),
    w("deneme", 3.42, 3.8),
    w("yapıyorum.", 3.8, 4.3),
    w("Iıı,", 5.4, 6.26),
    w("ııı,", 8.04, 8.26),  # stamped INSIDE silence 7.978–8.293; voice is 7.406–7.978
    w("şimdi", 8.3, 8.52),
    w("şöyle,", 8.52, 9.02),
    w("ıııı.", 9.18, 9.66),
]
TR_SILENCES = [
    (0.0, 1.214687),
    (1.757563, 1.875063),
    (2.529063, 3.255063),
    (4.358937, 5.778875),
    (6.209688, 7.406125),
    (7.977875, 8.293375),
    (8.526312, 8.642188),
    (9.67775, 10.0),
]
TR_MISSED = (7.406125, 7.977875)
# V1 baseline for this exact source. The shape production shipped (job d2d20bd2)
# is what the restored 0.55 cap still produces; the default budget, bound only
# by MIN_OUTPUT_S since 2026-09-08, lets V1 keep the two pause cuts the cap used
# to trim away. Neither number may move without a deliberate decision.
TR_V1_BASELINE = [
    (0.0, 2.04, "silence"),
    (2.529, 3.135, "silence"),
    (4.425, 7.406, "silence"),
    (7.92, 8.3, "filler_lexical"),
    (9.06, 10.0, "filler_lexical"),
]
TR_V1_BASELINE_CAPPED_055 = [
    (0.0, 2.04, "silence"),
    (4.656, 7.175, "silence"),
    (9.06, 10.0, "filler_lexical"),
]
# The pre-fix V2 candidate persisted on analysis 7ed6b28e: the island survived.
TR_PRE_FIX_V2 = [
    (1.06, 2.04, "filler_lexical"),
    (4.641, 7.406, "silence"),
    (7.92, 8.3, "filler_lexical"),
    (9.06, 10.0, "filler_lexical"),
]

# --- EN clip (job d0e284cb, analysis d94fbfc5) --------------------------------------

EN_DURATION_S = 10.014082
EN_WORDS = [
    w("Um,", 0.0, 1.84),  # voice starts at 1.14; the head is lead silence
    w("hello.", 2.42, 2.74),
    w("Uh-huh.", 3.96, 4.2),
    w("This", 4.9, 5.14),
    w("is", 5.14, 5.48),
    w("a...", 5.48, 7.2),  # contains the 1.405 s silence 5.661–7.066
    w("English", 7.2, 7.44),
    w("test.", 7.44, 8.2),
]
EN_SILENCES = [
    (0.0, 1.140625),
    (1.879375, 2.371125),
    (2.820438, 3.82675),
    (4.11975, 4.928),
    (5.660875, 7.066313),
    (7.473125, 7.802625),
    (8.0545, 10.014062),
]
EN_MISSED = (5.660875, 7.066313)
EN_V1_BASELINE = [
    (0.0, 1.96, "filler_lexical"),
    (2.865, 3.827, "silence"),
    (4.325, 4.775, "silence"),
    (8.7, 10.014, "silence"),
]
# Fresh whisper-1 run of the same audio (3/3 identical): the pause now straddles
# ``a`` (its last 160 ms) and ``English`` (its first 1.25 s).
EN_FRESH_WORDS = [
    w("Um,", 0.0, 1.84),
    w("hello,", 2.42, 2.74),
    w("aha,", 3.8, 4.22),
    w("this", 4.9, 5.16),
    w("is", 5.16, 5.48),
    w("a", 5.48, 5.82),
    w("English", 5.82, 7.44),
    w("test.", 7.44, 8.2),
]


def _comparison(words, silences, duration):
    return build_cut_plan_comparison(words, silences, duration, over_budget_policy="clamp")


class TestTurkishMistimedFillerToken:
    def test_pre_fix_shape_is_the_incident(self):
        """Documents the bug: with clipped flanks the island was rejected."""
        clipped = silence_cut._interior_soundful_islands(6.26, 8.04, TR_SILENCES)
        island = next(d for d in clipped if abs(d.island_start_s - TR_MISSED[0]) < 1e-6)
        # Default (a REAL word bounds the window): the flank stays clipped at
        # the token start, so a late-stamped real word keeps its protection.
        assert island.right_silence_s == pytest.approx(0.0621250, abs=1e-6)
        assert island.detection == "rejected"
        assert island.reason == "right_silence_too_short"
        # The token that clips it here is a removable filler, so the flank is
        # measured on the FULL span 7.978–8.293 (0.3155 s) instead.
        # Both boundary tokens here are fillers ("Iıı," and the mis-stamped
        # "ııı,"), which is exactly what _acoustic_removals_v2 passes.
        decisions = silence_cut._interior_soundful_islands(
            6.26, 8.04, TR_SILENCES, filler_lo_boundary=True, filler_hi_boundary=True
        )
        island = next(d for d in decisions if abs(d.island_start_s - TR_MISSED[0]) < 1e-6)
        assert island.left_silence_s == pytest.approx(1.196437, abs=1e-6)
        assert island.right_silence_s == pytest.approx(0.3155, abs=1e-6)
        assert island.detection == "eligible"
        assert island.reason == "bilateral_silence"

    def test_late_stamped_real_word_keeps_its_vocalization(self):
        """The full-span flank must not turn a real word into an acoustic filler."""
        words = [w("Hello", 1.20, 1.50), w("world", 3.00, 3.40), w("again", 6.0, 6.5)]
        # "Hello" is voiced 0.60-1.15 and its token was stamped 50 ms late.
        silences = [(0.0, 0.60), (1.15, 3.00), (3.40, 6.0), (6.5, 8.0)]
        plan = build_cut_plan(
            words, silences, 8.0, mixed_gap_enabled=True, over_budget_policy="clamp"
        )
        assert not any(
            removal.start_s < 1.15 - 1e-6 and removal.end_s > 0.60 + 1e-6
            for removal in plan.removed
        ), _rounded(plan)

    def test_island_is_cut_and_v1_is_byte_identical(self):
        comparison = _comparison(TR_WORDS, TR_SILENCES, TR_DURATION_S)
        assert comparison.candidate_status == "ready"
        candidate = comparison.candidate
        assert candidate is not None
        assert candidate.bailout_reason is None
        _assert_partition(candidate, TR_DURATION_S)
        assert _covered(candidate, *TR_MISSED) == pytest.approx(
            TR_MISSED[1] - TR_MISSED[0], abs=1e-6
        )
        assert _rounded(candidate) != TR_PRE_FIX_V2
        assert _rounded(comparison.baseline) == TR_V1_BASELINE
        # The pre-2026-09-08 budget is still reachable through the operator
        # switch, and reproduces exactly what production shipped.
        capped = build_cut_plan_comparison(
            TR_WORDS,
            TR_SILENCES,
            TR_DURATION_S,
            over_budget_policy="clamp",
            max_removal_frac_required=0.55,
        )
        assert _rounded(capped.baseline) == TR_V1_BASELINE_CAPPED_055
        assert capped.candidate is not None
        assert _covered(capped.candidate, *TR_MISSED) == pytest.approx(
            TR_MISSED[1] - TR_MISSED[0], abs=1e-6
        )
        diagnostics = candidate.diagnostics
        assert diagnostics is not None
        assert any(
            record.atom_kind == "filler_acoustic"
            and abs(record.atom_start_s - TR_MISSED[0]) < 1e-6
            and record.disposition == "selected_full"
            for record in diagnostics.atomic_dispositions
        )
        # Rule 0 never touches the mis-stamped token itself: it sits in a 0.3 s
        # span, below TOKEN_SILENCE_MIN_S, and is handled by the flank rule.
        assert not any(
            abs(item.original_start_s - 8.04) < 1e-6 for item in diagnostics.token_adjustments
        )
        # No kept word is ever cut into (validator contract on the V2 words).
        for removal in candidate.removed:
            for word in _normalize_words(TR_WORDS):
                if word.text in {"Iıı,", "ııı,", "ıııı."}:
                    continue
                assert not (
                    removal.start_s < word.end - 1e-6 and removal.end_s > word.start + 1e-6
                ), (
                    removal,
                    word,
                )

    def test_short_flank_rule_still_rejects_genuinely_short_silence(self):
        # Full-span measurement only helps when the SPAN is long; a real 50 ms
        # silence next to an island keeps rejecting it (kept from the #960 suite).
        decisions = silence_cut._interior_soundful_islands(
            1.0, 4.0, [(1.0, 2.0), (3.0, 3.05), (3.05, 3.05)]
        )
        assert [d.detection for d in decisions] == ["rejected", "rejected"]
        assert decisions[0].reason == "right_silence_too_short"


class TestEnglishStretchedToken:
    def test_persisted_layout_splits_token_and_cuts_pause(self):
        comparison = _comparison(EN_WORDS, EN_SILENCES, EN_DURATION_S)
        assert comparison.candidate_status == "ready"
        candidate = comparison.candidate
        assert candidate is not None
        _assert_partition(candidate, EN_DURATION_S)
        assert _rounded(comparison.baseline) == EN_V1_BASELINE
        diagnostics = candidate.diagnostics
        assert diagnostics is not None
        split = next(
            item
            for item in diagnostics.token_adjustments
            if abs(item.original_start_s - 5.48) < 1e-6
        )
        assert split.kind == "split"
        assert [v for piece in split.pieces for v in piece] == pytest.approx(
            [5.48, 5.660875, 7.066313, 7.2], abs=1e-6
        )
        assert split.carved_s == pytest.approx(1.405438, abs=1e-6)
        assert diagnostics.token_carved_s >= split.carved_s
        # Rule 3 keeps KEPT_GAP_S/2 of breathing room on each side of the carve.
        expected = (5.660875 + silence_cut.KEPT_GAP_S / 2, 7.066313 - silence_cut.KEPT_GAP_S / 2)
        assert any(
            abs(r.start_s - expected[0]) < 1e-6
            and abs(r.end_s - expected[1]) < 1e-6
            and r.reason == "silence"
            for r in candidate.removed
        ), _rounded(candidate)
        assert _covered(candidate, *EN_MISSED) == pytest.approx(expected[1] - expected[0], abs=1e-6)

    def test_remap_shrinks_the_stretched_token_instead_of_dropping_it(self):
        candidate = _comparison(EN_WORDS, EN_SILENCES, EN_DURATION_S).candidate
        assert candidate is not None
        remapped = {item["text"]: item for item in remap_words(EN_WORDS, candidate)}
        stretched = remapped["a..."]
        # 1.72 s token minus the 1.155 s cut through its carve.
        assert stretched["end_s"] - stretched["start_s"] == pytest.approx(
            1.72 - (7.066313 - 5.660875 - silence_cut.KEPT_GAP_S), abs=1e-6
        )
        assert remapped["English"]["start_s"] >= stretched["end_s"] - 1e-6
        starts = [item["start_s"] for item in remap_words(EN_WORDS, candidate)]
        assert starts == sorted(starts)

    def test_fresh_whisper_layout_is_a_known_miss_not_a_speech_risk(self):
        """Same audio, a different ASR roll — and the pause survives. Documented.

        Here whisper splits the hole across two CONTIGUOUS tokens (``a``
        5.48-5.82 taking its first 0.16 s, ``English`` 5.82-7.44 the rest), so
        no single token contains it and the window between them is zero-width.
        Rule 0 refuses both: 0.16 s is under the overlap floor, and the
        ``English`` carve sits at that token's HEAD, which is exactly the shape
        that is indistinguishable from a quiet onset. Cutting it would need an
        edge carve, and edge carves were measured destroying real speech on
        soft-spoken takes. Leaving a pause in is the correct trade against
        deleting a word, so this asserts the miss deliberately: the fix depends
        on how the ASR happens to stamp the hole, and the persisted layout
        (what production actually produced, covered above) is cut.
        """
        comparison = _comparison(EN_FRESH_WORDS, EN_SILENCES, EN_DURATION_S)
        assert comparison.candidate_status == "ready"
        candidate = comparison.candidate
        assert candidate is not None
        _assert_partition(candidate, EN_DURATION_S)
        diagnostics = candidate.diagnostics
        assert diagnostics is not None
        assert diagnostics.token_adjustments == ()
        assert _covered(candidate, *EN_MISSED) == pytest.approx(0.0, abs=1e-6)
        # No speech is harmed by the refusal: every cut stays clear of the
        # tokens that straddle the hole.
        for removal in candidate.removed:
            assert not (removal.start_s < 7.44 - 1e-6 and removal.end_s > 5.48 + 1e-6), _rounded(
                candidate
            )

    def test_rule_zero_is_v2_only(self):
        # Default entry point = V1 + bailout policy: 4.69 s proposed on a 10 s
        # clip trips the 40 % rail exactly as before (legacy behaviour pinned).
        legacy_bailout = build_cut_plan(EN_WORDS, EN_SILENCES, EN_DURATION_S)
        assert legacy_bailout.removed == []
        assert legacy_bailout.bailout_reason == silence_cut.BAILOUT_MAX_REMOVAL
        legacy = build_cut_plan(EN_WORDS, EN_SILENCES, EN_DURATION_S, over_budget_policy="clamp")
        assert _rounded(legacy) == EN_V1_BASELINE
        assert _covered(legacy, *EN_MISSED) == 0.0
        legacy_clamp = build_cut_plan(
            EN_WORDS, EN_SILENCES, EN_DURATION_S, over_budget_policy="clamp"
        )
        assert _covered(legacy_clamp, *EN_MISSED) == 0.0

    def test_retake_spans_still_index_the_original_transcript(self):
        # Index 5 is ``a...`` in the ORIGINAL list; rule 0 splits it into two V2
        # words, and rule 4 must still resolve the span on the original token.
        comparison = build_cut_plan_comparison(
            EN_WORDS,
            EN_SILENCES,
            EN_DURATION_S,
            retake_spans=[(5, 5)],
            over_budget_policy="clamp",
        )
        assert comparison.candidate_status == "ready", comparison.candidate_error_class
        candidate = comparison.candidate
        assert candidate is not None
        retake = next(
            r for r in candidate.removed if r.start_s <= 5.48 + 1e-6 and r.end_s >= 7.2 - 1e-6
        )
        assert retake.reason in {"retake", "silence"}
        assert _covered(candidate, 5.48, 7.2) == pytest.approx(7.2 - 5.48, abs=1e-6)


class TestRuleZeroReconciliation:
    @staticmethod
    def words(*items: tuple[str, float, float]) -> list[_CutWord]:
        return _normalize_words([w(*item) for item in items])

    def test_short_silence_and_small_overlap_are_left_alone(self):
        original = self.words(("support", 4.0, 4.6), ("word", 5.0, 5.4))
        # Interior silence shorter than TOKEN_SILENCE_MIN_S: untouched.
        reconciled, adjustments = _reconcile_words_with_silence(
            original, [(4.2, 4.2 + TOKEN_SILENCE_MIN_S - 0.05)]
        )
        assert reconciled == original and adjustments == []
        # Long silence overlapping the token edge by less than the overlap floor
        # (quiet onset under the -30 dB floor): untouched.
        reconciled, adjustments = _reconcile_words_with_silence(
            original, [(3.0, 4.0 + TOKEN_SILENCE_MIN_OVERLAP_S - 0.05)]
        )
        assert reconciled == original and adjustments == []

    def test_token_entirely_inside_long_silence_is_never_dropped(self):
        original = self.words(("ghost", 3.0, 3.4), ("real", 6.0, 6.5))
        reconciled, adjustments = _reconcile_words_with_silence(original, [(2.0, 5.0)])
        assert reconciled == original and adjustments == []

    def test_only_a_dominated_interior_split_is_carved(self):
        original = self.words(
            ("Um,", 0.0, 1.84),  # head inside lead silence -> NOT carved (edge)
            ("videos,", 3.0, 4.5),  # tail inside a long silence -> NOT carved (edge)
            ("a...", 5.48, 7.2),  # one interior silence, sliver remnants -> split
            ("mumbled", 8.0, 10.2),  # interior silence but 0.6s of voice each side
        )
        silences = [(0.0, 1.14), (3.9, 4.8), (5.66, 7.07), (8.6, 9.6)]
        reconciled, adjustments = _reconcile_words_with_silence(original, silences)
        assert [item.original_start_s for item in adjustments] == [5.48]
        split = adjustments[0]
        assert split.kind == "split"
        assert [v for piece in split.pieces for v in piece] == pytest.approx(
            [5.48, 5.66, 7.07, 7.2], abs=1e-9
        )
        assert split.carved_s == pytest.approx(7.07 - 5.66, abs=1e-9)
        assert [item.text for item in reconciled] == [
            "Um,",
            "videos,",
            "a...",
            "a...",
            "mumbled",
        ]
        assert [item.start for item in reconciled] == sorted(item.start for item in reconciled)

    def test_edge_carves_and_multi_carves_are_refused(self):
        # Each of these is indistinguishable from a quiet onset, a trailing-off
        # word, or a mumbled one, so the token is returned untouched.
        head = self.words(("word", 1.0, 3.0))
        assert _reconcile_words_with_silence(head, [(0.0, 2.0)]) == (head, [])
        tail = self.words(("word", 1.0, 3.0))
        assert _reconcile_words_with_silence(tail, [(2.0, 4.0)]) == (tail, [])
        both = self.words(("word", 1.0, 5.0))
        assert _reconcile_words_with_silence(both, [(0.0, 2.0), (4.0, 6.0)]) == (both, [])
        multi = self.words(("long", 1.0, 5.9))
        assert _reconcile_words_with_silence(multi, [(1.4, 2.4), (2.8, 3.8), (4.2, 5.2)]) == (
            multi,
            [],
        )

    def test_a_substantial_remnant_refuses_the_split(self):
        # 2.2s word, 1.0s under-read middle: 0.6s of voice survives each side,
        # over TOKEN_SPLIT_PIECE_MAX_S, so it is a word the mic under-read.
        word = self.words(("mumbled", 1.0, 3.2))
        assert _reconcile_words_with_silence(word, [(1.6, 2.6)]) == (word, [])
        # Shrink the remnants under the sliver ceiling and it splits.
        stretched = self.words(("a...", 1.0, 3.2))
        _reconciled, adjustments = _reconcile_words_with_silence(stretched, [(1.3, 2.9)])
        assert [item.kind for item in adjustments] == ["split"]

    def test_carve_can_only_ever_remove_silence(self):
        # Whatever rule 0 carves, rule 3 intersects with silencedetect, so a cut
        # through a token never contains audio FFmpeg called soundful.
        words = [w("one", 1.0, 2.0), w("stretched", 2.0, 5.0), w("two", 5.0, 6.0)]
        silences = [(0.0, 1.0), (2.3, 4.7), (6.0, 8.0)]
        plan = build_cut_plan(
            words, silences, 8.0, mixed_gap_enabled=True, over_budget_policy="clamp"
        )
        inside = [r for r in plan.removed if r.end_s > 2.0 + 1e-6 and r.start_s < 5.0 - 1e-6]
        assert inside, _rounded(plan)
        for removal in inside:
            assert removal.start_s >= 2.3 + silence_cut.KEPT_GAP_S / 2 - 1e-6, removal
            assert removal.end_s <= 4.7 - silence_cut.KEPT_GAP_S / 2 + 1e-6, removal
        assert PAD_S > 0  # imported for the flank arithmetic documented above


def test_analysis_payload_carries_token_adjustments_and_round_trips():
    transcript = SimpleNamespace(
        words=[
            SimpleNamespace(text=item["text"], start_s=item["start_s"], end_s=item["end_s"])
            for item in EN_WORDS
        ],
        language="en",
        low_confidence=False,
    )
    silence = SimpleNamespace(spans=tuple(EN_SILENCES), status="ok")
    result = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint="golden-en",
            local_media_path="/dev/null",
            duration_s=EN_DURATION_S,
            source_window_end_s=EN_DURATION_S,
            mixed_gap_mode="apply",
        ),
        transcribe_fn=lambda *_args, **_kwargs: transcript,
        silence_detect_fn=lambda *_args, **_kwargs: silence,
    )
    assert result.detector_version == DETECTOR_VERSION
    assert result.safety_signals.selected_plan == "candidate"
    adjustments = result.diagnostics["token_adjustments"]
    assert any(item["kind"] == "split" and item["original_start_s"] == 5.48 for item in adjustments)
    assert result.diagnostics["token_adjustments_total"] == len(adjustments)
    assert result.diagnostics["token_carved_s"] > 1.4
    # Persisted words stay the ORIGINAL whisper tokens; only the plan changed.
    assert [word.text for word in result.timed_words] == [item["text"] for item in EN_WORDS]
    restored = SpeechCleanupAnalysisResult.from_payload(result.to_payload())
    assert restored == result
    assert any(
        abs(finding.start_s - (5.660875 + silence_cut.KEPT_GAP_S / 2)) < 1e-6
        for finding in restored.findings
    )


def test_detector_version_was_bumped_with_the_detector():
    # Snapshot reuse and the render-side cut cache key on this string; an
    # un-bumped detector change would keep serving pre-fix plans forever.
    assert DETECTOR_VERSION == "mixed-gap-v2"
    assert Removal(0.0, 1.0, "silence").reason == "silence"  # import kept intentional


class TestRuleZeroCannotCutRealSpeech:
    """The 55% cap used to evict rule-0 carves; with it gone the detector must
    protect quiet speech on its own.

    ``silencedetect`` uses an absolute -30 dBFS floor, so a soft-spoken or
    lapel-mic take reports quiet SPEECH as silence — the pipeline says so
    itself (``generative_build``: "silencedetect undercounts quiet/lapel
    speech"). Rule 0 carving such a span makes it cuttable, and the validator
    cannot object: ``word_intrusion`` is checked against the RECONCILED words,
    so a cut inside the ORIGINAL token is invisible to it. The split-dominance
    rule is the only thing standing between a mumbled word and a chopped one.
    """

    @staticmethod
    def _soft_spoken_clip():
        """10 real 2.2 s words, each with a 1.0 s quiet middle, on a 52.5 s take."""
        words: list[dict] = []
        silences: list[tuple[float, float]] = [(0.0, 0.5)]
        start = 0.5
        for index in range(10):
            words.append(w(f"word{index}", start, start + 2.2))
            silences.append((start + 0.6, start + 1.6))  # under-read middle
            silences.append((start + 2.2, start + 5.0))  # the real pause after it
            start += 5.0
        return words, silences, 52.5

    def test_mumbled_words_are_never_split(self):
        words, silences, duration = self._soft_spoken_clip()
        plan = build_cut_plan(
            words, silences, duration, mixed_gap_enabled=True, over_budget_policy="clamp"
        )
        inside_words = sum(
            max(0.0, min(removal.end_s, word["end_s"]) - max(removal.start_s, word["start_s"]))
            for removal in plan.removed
            for word in words
        )
        assert inside_words == pytest.approx(0.0, abs=1e-6), _rounded(plan)
        assert plan.diagnostics is not None
        assert plan.diagnostics.token_adjustments_total == 0
        # The real pauses between the words are still cut.
        assert plan.time_saved_s > 20.0

    def test_the_guard_does_not_depend_on_the_removed_fraction_cap(self):
        # The old 0.55 rail hid this by evicting the carves; the outcome must
        # now be identical with and without it.
        words, silences, duration = self._soft_spoken_clip()
        uncapped = build_cut_plan(
            words, silences, duration, mixed_gap_enabled=True, over_budget_policy="clamp"
        )
        capped = build_cut_plan(
            words,
            silences,
            duration,
            mixed_gap_enabled=True,
            over_budget_policy="clamp",
            max_removal_frac_required=0.55,
        )
        assert _rounded(uncapped) == _rounded(capped)

    def test_a_token_stretched_over_a_pause_is_still_split(self):
        # The EN incident shape must survive the guard: 1.72 s token holding a
        # 1.405 s hole keeps only 0.31 s of voice, so the carve dominates.
        comparison = _comparison(EN_WORDS, EN_SILENCES, EN_DURATION_S)
        assert comparison.candidate is not None
        diagnostics = comparison.candidate.diagnostics
        assert diagnostics is not None
        assert any(
            item.kind == "split" and abs(item.original_start_s - 5.48) < 1e-6
            for item in diagnostics.token_adjustments
        )

    def test_surviving_speech_fragments_clear_the_shot_floor(self):
        # A kept fragment shorter than MIN_KEEP_SPEECH_SEGMENT_S reads as a
        # stutter between two jump cuts; hygiene widens it by retreating a
        # neighbouring silence carrier rather than absorbing the audio.
        comparison = _comparison(EN_WORDS, EN_SILENCES, EN_DURATION_S)
        candidate = comparison.candidate
        assert candidate is not None
        for start_s, end_s in candidate.keep_segments:
            if not silence_cut._has_word_overlap(start_s, end_s, _normalize_words(EN_WORDS)):
                continue
            assert end_s - start_s >= silence_cut.MIN_KEEP_SPEECH_SEGMENT_S - 1e-6, (
                (start_s, end_s),
                candidate.keep_segments,
            )
