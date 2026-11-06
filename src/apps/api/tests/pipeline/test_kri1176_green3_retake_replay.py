"""KRI-1176: "Green 3" restarted sentence, replayed offline through the real cleanup engine.

Prod chat "Green 3" (job 9bad2d75, 2026-11-03, 31.2 s English take): the creator said "Cut the
pauses and the sentence I restarted". Consented speech cleanup (`required_v1`) cut 3 pauses and
left the restart ("So Lumo took its tech from cold hard... No, again.") because retake detection
is off in prod. The plan is to switch on the existing consented retake path: the paid Gemini
`retake_detector` answers with word indexes and `retake_detect_fn` feeds them into
`analyze_speech_cleanup`. This module is the free evidence that, IF the detector returns the right
word span, the cut removes exactly the restart and never a real word. The detector itself is
injected (no model call); the 88 prod words (`kri1176_green3_restart.json`, whisper-1) and the
real `detect_silences_with_status` spans of the same take are replayed through the REAL
`analyze_speech_cleanup` (V2 mixed-gap candidate + the V1 baseline), the voice guard, the typed
snapshot validators and `remap_words`. Settings are the production ones: `mixed_gap_mode` and the
over-budget policy from `speech_cleanup_preflight`, filler check on, no fraction cap.

How it can fail (written before the code):

F1  The retake removal for the detector answer (44, 53) reaches into word 54 (the kept
    re-delivery, starts 20.26 s) or word 43 ("drones.", ends 16.28 s).
F2  Any word outside 44..53 overlaps a removed interval (retake or pause): checked for each of
    the other 78 words, in both detector modes. "Okay, we're recording." (words 0-2) is not a
    restart and nobody asked to cut it here, so it must stay too.
F3  The restart words 44..53 are not fully inside removed intervals (the restart survives), here
    or after `remap_words`, the call the renderer makes.
F4  No usable detector answer still cuts a "retake": None injected, an empty answer, a detector
    that raises, an out-of-range span reaching the last word ((80, 87), (44, 87)), a reversed
    span, or a valid span next to an invalid one.
F5  The cut plan's removed duration is inconsistent (plan, receipt and snapshot disagree), a
    clamp or bailout hides a no-op, or the output falls under the engine's minimum.
F6  Index space drift: the detector indexes words differently from the cut planner (a reordered
    or dropped word shifts 44..53 onto real speech).
F7  The voice guard gives the retake back. The restart is voiced speech and the guard returns
    any silence/filler cut that holds a voice. Two things keep a retake out of its reach (its
    removal reason "retake" is not judged, and the retake's words are exempt spans); either
    could regress. The suite's default `silent_pcm` has no voice to judge, so this one uses a
    synthetic voiced take, with a control that the same voice DOES void a pause cut.
F8  Switching the detector on changes the base pause cuts. Without a detector the plan must be
    the 3 prod silences; with it, every pause is a prod pause or sits inside the retake.
F9  The replay is not free: a second transcription (a paid whisper-1 call in prod) or a filler
    re-listen runs. This take has no fillers, so there must be exactly one transcription.
F10 The checks above pass vacuously. Red control: a one-word-too-wide, one-word-too-early and
    one-word-too-narrow detector answer must each be caught by the same helpers.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import numpy as np
import pytest

from app.pipeline.silence_cut import MIN_OUTPUT_S, CutPlan, Removal, remap_words
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupAnalysisResult,
    analyze_speech_cleanup,
)
from app.pipeline.speech_voice_guard import guard_audible_speech, quiet_floor_db, voiced_runs
from app.pipeline.transcribe import Transcript, Word
from app.services.speech_cleanup_preflight import (
    SPEECH_CLEANUP_MIXED_GAP_MODE,
    SPEECH_CLEANUP_OVER_BUDGET_POLICY,
)
from tests._silent_pcm import silent_pcm

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri1176_green3_restart.json").read_text()
)
WORDS = FIXTURE["words_source_time"]
DURATION_S = float(FIXTURE["take"]["duration_s"])
LANGUAGE = FIXTURE["take"]["language"]
FIRST, LAST = FIXTURE["restart"]["word_span"]
PREVIOUS = FIXTURE["restart"]["previous_word"]  # "drones."
KEPT = FIXTURE["restart"]["kept_redelivery_first_word"]  # the second, kept "So"
RESTART = range(FIRST, LAST + 1)
OUTSIDE = [index for index in range(len(WORDS)) if index not in RESTART]
ANSWER = ((FIRST, LAST),)
PROD_PAUSES = [(row["start_s"], row["end_s"]) for row in FIXTURE["variant_silence_cut"]["removed"]]
SILENCES = tuple((start, end) for start, end in FIXTURE["silences_ambient_adaptive"]["spans"])
MODES = [SPEECH_CLEANUP_MIXED_GAP_MODE, "off"]
EPS = 1e-6
SR = 16_000


# -- replay ------------------------------------------------------------------------------------


class Run(NamedTuple):
    result: SpeechCleanupAnalysisResult
    transcriptions: list[str | None]  # the `language` of every transcribe_fn call
    detector_saw: list[tuple[list[dict], str]]  # every (words, language) the detector was given


def _transcript() -> Transcript:
    words = [Word(row["text"], row["start_s"], row["end_s"], 0.99) for row in WORDS]
    return Transcript(words=words, full_text=" ".join(w.text for w in words), language=LANGUAGE)


def _analyze(
    detector=None,
    *,
    mode: str = SPEECH_CLEANUP_MIXED_GAP_MODE,
    filler_check: bool = True,
    pcm_loader=silent_pcm,
) -> Run:
    transcriptions: list[str | None] = []
    detector_saw: list[tuple[list[dict], str]] = []

    def transcribe(_path: str, *, language: str | None, verbatim_prompt: str | None):
        transcriptions.append(language)
        return _transcript()

    def retake_detect_fn(words, language):
        detector_saw.append(
            (
                [{"text": w.text, "start_s": w.start_s, "end_s": w.end_s} for w in words],
                language,
            )
        )
        return detector(words, language)

    result = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint="a" * 64,
            local_media_path="take.wav",  # never decoded: pcm_loader stands in for the audio
            duration_s=DURATION_S,
            mixed_gap_mode=mode,
            include_silence_and_fillers=True,
            over_budget_policy=SPEECH_CLEANUP_OVER_BUDGET_POLICY,
            max_removal_frac_required=1.0,  # config default: MIN_OUTPUT_S is the only rail
            filler_check=filler_check,  # config default True; a no-op on this filler-free take
        ),
        transcribe_fn=transcribe,
        silence_detect_fn=lambda *_args, **_kwargs: SimpleNamespace(spans=SILENCES, status="ok"),
        retake_detect_fn=retake_detect_fn if detector is not None else None,
        pcm_loader=pcm_loader,
    )
    return Run(result, transcriptions, detector_saw)


def _answer(spans):
    return lambda _words, _language: spans


def _raises(error: Exception):
    def detector(_words, _language):
        raise error

    return detector


# -- the checks (also run against wrong answers in the red control) ----------------------------


def _overlaps(word: dict, start_s: float, end_s: float) -> bool:
    start, end = word["start_s"], word["end_s"]
    if end - start <= EPS:  # a zero-length stamp is a point: cut only when strictly inside
        return start_s < start < end_s
    return start < end_s - EPS and start_s < end - EPS


def _touched(result: SpeechCleanupAnalysisResult, indexes) -> list[int]:
    """Indexes in ``indexes`` whose word overlaps any removed interval of the final plan."""

    return [
        index
        for index in indexes
        if any(_overlaps(WORDS[index], r.start_s, r.end_s) for r in result.cut_plan.removed)
    ]


def _merged_removals(result: SpeechCleanupAnalysisResult) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for removal in sorted(result.cut_plan.removed, key=lambda r: (r.start_s, r.end_s)):
        if merged and removal.start_s <= merged[-1][1] + EPS:
            merged[-1][1] = max(merged[-1][1], removal.end_s)
        else:
            merged.append([removal.start_s, removal.end_s])
    return [(start, end) for start, end in merged]


def _not_removed(result: SpeechCleanupAnalysisResult, indexes) -> list[int]:
    """Indexes in ``indexes`` whose word is NOT fully inside one removed interval."""

    merged = _merged_removals(result)
    return [
        index
        for index in indexes
        if not any(
            start - EPS <= WORDS[index]["start_s"] and WORDS[index]["end_s"] <= end + EPS
            for start, end in merged
        )
    ]


def _retake_removals(result: SpeechCleanupAnalysisResult):
    return [r for r in result.cut_plan.removed if r.reason == "retake"]


def _flat(spans) -> list[float]:
    return [edge for span in spans for edge in span]


def _removed_flat(result: SpeechCleanupAnalysisResult) -> list[float]:
    return _flat((r.start_s, r.end_s) for r in result.cut_plan.removed)


def _is_prod_pause(start_s: float, end_s: float) -> bool:
    return any(
        abs(start_s - prod_start) < 1e-3 and abs(end_s - prod_end) < 1e-3
        for prod_start, prod_end in PROD_PAUSES
    )


# -- the fixture itself ------------------------------------------------------------------------


def test_fixture_indexes_are_the_restart_the_creator_described() -> None:
    assert [row["i"] for row in WORDS] == list(range(len(WORDS)))
    assert len(OUTSIDE) == len(WORDS) - (LAST - FIRST + 1) == 78
    assert " ".join(WORDS[i]["text"] for i in RESTART) == FIXTURE["restart"]["text"]
    assert (PREVIOUS, KEPT) == (FIRST - 1, LAST + 1)
    # The restart is bracketed by real speech: the sentence before it, and its re-delivery.
    assert WORDS[PREVIOUS]["text"] == "drones."
    assert WORDS[KEPT]["text"] == WORDS[FIRST]["text"] == "So"
    assert WORDS[PREVIOUS]["end_s"] < WORDS[FIRST]["start_s"] < WORDS[KEPT]["start_s"]


# -- F6 / F9: the replay is faithful and free --------------------------------------------------


def test_detector_gets_the_engine_words_in_fixture_index_order() -> None:
    run = _analyze(_answer(ANSWER))

    ((words, language),) = run.detector_saw
    assert language == LANGUAGE
    assert [w["text"] for w in words] == [row["text"] for row in WORDS]
    assert [(w["start_s"], w["end_s"]) for w in words] == [
        (row["start_s"], row["end_s"]) for row in WORDS
    ]
    assert words[FIRST]["text"] == "So" and words[LAST]["text"] == "again."
    assert [w.text for w in run.result.timed_words] == [row["text"] for row in WORDS]


@pytest.mark.parametrize("detector", [None, _answer(ANSWER)], ids=["no_detector", "detector"])
def test_replay_is_free_one_transcription_and_no_filler_relisten(detector) -> None:
    """Production runs the filler check; on a filler-free take it must cost nothing."""

    checked = _analyze(detector, filler_check=True)
    unchecked = _analyze(detector, filler_check=False)

    assert checked.transcriptions == [None]  # a second call would be a paid whisper-1 pass
    filler_check = checked.result.diagnostics["filler_check"]
    assert filler_check["aligned"] is False and filler_check["reason"] == "no_fillers"
    assert filler_check["relisten"]["checked"] == 0
    assert checked.result.cut_plan == unchecked.result.cut_plan


# -- F8: the base pause cuts ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_without_a_detector_the_plan_is_the_three_production_pauses(mode: str) -> None:
    result = _analyze(None, mode=mode).result

    assert _removed_flat(result) == pytest.approx(_flat(PROD_PAUSES), abs=1e-3)
    assert {r.reason for r in result.cut_plan.removed} == {"silence"}
    assert result.cut_plan.time_saved_s == pytest.approx(
        FIXTURE["variant_silence_cut"]["time_saved_s"], abs=1e-3
    )
    assert _touched(result, range(len(WORDS))) == []  # pauses alone never take a word


@pytest.mark.parametrize("mode", MODES)
def test_pause_cuts_with_the_detector_are_prod_pauses_or_absorbed_by_the_retake(
    mode: str,
) -> None:
    result = _analyze(_answer(ANSWER), mode=mode).result

    (retake,) = _retake_removals(result)
    for removal in result.cut_plan.removed:
        if removal is retake:
            continue
        assert _is_prod_pause(removal.start_s, removal.end_s), removal  # nothing new appears
    kept_pauses = [(r.start_s, r.end_s) for r in result.cut_plan.removed if r.reason == "silence"]
    for start_s, end_s in PROD_PAUSES:  # nothing prod cut is lost, except inside the retake
        in_retake = retake.start_s - EPS <= start_s and end_s <= retake.end_s + EPS
        assert in_retake or any(
            abs(start_s - s) < 1e-3 and abs(end_s - e) < 1e-3 for s, e in kept_pauses
        ), (start_s, end_s)
    assert result.public_receipt.category_counts.retakes == 1


# -- F1: the retake stops at both neighbours -----------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_retake_stops_short_of_the_kept_redelivery_and_the_word_before(mode: str) -> None:
    result = _analyze(_answer(ANSWER), mode=mode).result

    (retake,) = _retake_removals(result)
    assert retake.start_s >= WORDS[PREVIOUS]["end_s"] - EPS, "reaches into 'drones.'"
    assert retake.end_s <= WORDS[KEPT]["start_s"] + EPS, "reaches into the kept 'So'"
    findings = [f for f in result.findings if f.category == "retake"]
    assert len(findings) == 1
    assert findings[0].start_s >= WORDS[PREVIOUS]["end_s"] - EPS
    assert findings[0].end_s <= WORDS[KEPT]["start_s"] + EPS
    # No removal of any kind reaches either neighbour (the 16.4-16.8 s pause joins the retake).
    assert _touched(result, [PREVIOUS, KEPT]) == []


# -- F2: no other word is touched ----------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_no_word_outside_the_restart_overlaps_any_removal(mode: str) -> None:
    result = _analyze(_answer(ANSWER), mode=mode).result

    assert len(OUTSIDE) == 78
    assert _touched(result, OUTSIDE) == [], {i: WORDS[i]["text"] for i in _touched(result, OUTSIDE)}
    # Spelled out for the three words nobody asked to cut and the two that bracket the restart.
    assert _touched(result, [0, 1, 2, PREVIOUS, KEPT]) == []
    # After the render's own remap, every outside word is still there, in order.
    remapped = remap_words(result.timed_words, result.cut_plan.to_cut_plan())
    survivors = [w["text"] for w in remapped]
    assert survivors == [WORDS[i]["text"] for i in OUTSIDE]
    assert survivors[:3] == ["Okay,", "we're", "recording."]


# -- F3: the whole restart goes ------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_the_whole_restart_is_inside_removed_intervals(mode: str) -> None:
    result = _analyze(_answer(ANSWER), mode=mode).result

    assert _not_removed(result, RESTART) == []
    (retake,) = _retake_removals(result)
    assert retake.start_s <= WORDS[FIRST]["start_s"] and retake.end_s >= WORDS[LAST]["end_s"]
    remapped = remap_words(result.timed_words, result.cut_plan.to_cut_plan())
    assert "hard..." not in [w["text"] for w in remapped]
    # What is left reads "...mounted on drones. So Lumo took its tech from cold hardware...".
    texts = [w["text"] for w in remapped]
    at = texts.index("drones.")
    assert texts[at : at + 4] == ["drones.", "So", "Lumo", "took"]
    assert texts[at + 1 : at + 8] == [WORDS[i]["text"] for i in range(KEPT, KEPT + 7)]


# -- F4: no usable answer, no retake -------------------------------------------------------------


@pytest.mark.parametrize(
    "detector",
    [
        None,
        _answer(()),
        _raises(RuntimeError("detector unavailable")),
        _raises(TimeoutError("detector timed out")),
        _answer(((80, 87),)),  # reaches the last word (87): a kept re-delivery must follow
        _answer(((FIRST, 87),)),
        _answer(((LAST, FIRST),)),  # reversed
        _answer(((FIRST, LAST), (80, 87))),  # one valid span beside an invalid one: none cut
    ],
    ids=[
        "no_detector",
        "empty_answer",
        "detector_raises",
        "detector_times_out",
        "span_reaches_last_word",
        "span_from_restart_to_last_word",
        "reversed_span",
        "valid_beside_invalid",
    ],
)
@pytest.mark.parametrize("mode", MODES)
def test_no_usable_detector_answer_means_no_retake_at_all(detector, mode: str) -> None:
    result = _analyze(detector, mode=mode).result

    assert result.public_receipt.category_counts.retakes == 0
    assert all(f.category != "retake" for f in result.findings)
    assert _retake_removals(result) == []
    # Fail open to exactly what prod cut: the three pauses, no word touched.
    assert _removed_flat(result) == pytest.approx(_flat(PROD_PAUSES), abs=1e-3)
    assert _touched(result, range(len(WORDS))) == []
    assert _not_removed(result, RESTART) == list(RESTART)  # and the restart stays, as in prod


# -- F5: the plan adds up ------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_cut_plan_is_consistent_and_stays_above_the_minimum(mode: str) -> None:
    result = _analyze(_answer(ANSWER), mode=mode).result
    plan = result.cut_plan

    removed_s = sum(r.end_s - r.start_s for r in plan.removed)
    kept_s = sum(k.end_s - k.start_s for k in plan.keep_segments)
    assert removed_s == pytest.approx(plan.time_saved_s, abs=1e-6)
    assert kept_s + removed_s == pytest.approx(DURATION_S, abs=1e-6)
    assert kept_s >= MIN_OUTPUT_S
    assert kept_s == pytest.approx(DURATION_S - removed_s, abs=1e-6)
    assert plan.bailout_reason is None and plan.clamped is False
    assert result.safety_signals.bailout_reason is None and result.safety_signals.clamped is False
    assert result.safety_signals.silence_detection_status == "ok"
    # The restart is a few seconds of a 31 s take: the creator never loses a third of it.
    assert 0 < removed_s < DURATION_S / 3

    receipt = result.public_receipt
    assert receipt.source_duration_ms == round(DURATION_S * 1000)
    assert receipt.estimated_removed_ms == round(removed_s * 1000)
    assert receipt.result_duration_ms == receipt.source_duration_ms - receipt.estimated_removed_ms
    counts = receipt.category_counts
    assert (counts.retakes, counts.long_pauses, counts.filler_sounds) == (1, 2, 0)
    assert receipt.candidate_count == len(result.findings) == 3

    # The persisted snapshot revalidates and hydrates the same executable plan.
    restored = SpeechCleanupAnalysisResult.from_payload(json.loads(json.dumps(result.to_payload())))
    assert restored.cut_plan == result.cut_plan
    assert restored.cut_plan.to_cut_plan().keep_segments == plan.to_cut_plan().keep_segments


# -- F7: the voice guard leaves the consented retake alone ---------------------------------------


def _voiced_take(_path: str, *_args: object, **_kwargs: object) -> np.ndarray:
    """16 kHz audio with a periodic, voice-like tone over every word and a quiet noise bed."""

    rng = np.random.default_rng(1176)
    x = rng.standard_normal(int(DURATION_S * SR)) * 10 ** (-45 / 20)
    t = np.arange(len(x)) / SR
    voice = sum(np.sin(2 * np.pi * 130.0 * k * t) / k for k in range(1, 9))
    voice = voice / np.sqrt(np.mean(voice**2)) * 10 ** (-20 / 20)
    for row in WORDS:
        first, last = int(row["start_s"] * SR), int(row["end_s"] * SR)
        x[first:last] += voice[first:last]
    return x.astype(np.float32)


def test_the_retake_survives_the_voice_guard_when_the_restart_is_voiced() -> None:
    samples = _voiced_take("take.wav")
    start_s, end_s = WORDS[FIRST]["start_s"], WORDS[LAST]["end_s"]
    runs = voiced_runs(samples, start_s, end_s, floor_db=quiet_floor_db(samples))
    # Control: the guard really hears a voice where the restart is, so a pause-reason cut over
    # it is given back whole. What keeps the retake is its reason and its exempt words.
    assert sum(b - a for a, b in runs) > 0.5 * (end_s - start_s)
    pause_over_restart = CutPlan(
        keep_segments=[(0.0, start_s), (end_s, DURATION_S)],
        removed=[Removal(start_s=start_s, end_s=end_s, reason="silence")],
        time_saved_s=end_s - start_s,
    )
    judged, report = guard_audible_speech(pause_over_restart, samples)
    assert report["dropped_total"] == 1 and judged.removed == []

    for mode in MODES:
        result = _analyze(_answer(ANSWER), mode=mode, pcm_loader=_voiced_take).result
        assert _not_removed(result, RESTART) == [], mode
        assert _touched(result, OUTSIDE) == [], mode
        (retake,) = _retake_removals(result)
        assert retake.end_s <= WORDS[KEPT]["start_s"] + EPS
        guard = result.diagnostics.get("voice_guard", {})
        assert not any(
            item["start_s"] < retake.end_s and item["end_s"] > retake.start_s
            for item in guard.get("dropped", [])
        ), (mode, guard)


# -- F10: red control ----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("answer", "check", "expected"),
    [
        ((FIRST, LAST + 1), "touched", [KEPT]),  # one word too wide: eats the kept "So"
        ((FIRST - 1, LAST), "touched", [PREVIOUS]),  # one word too early: eats "drones."
        ((FIRST + 1, LAST), "not_removed", [FIRST]),  # one word too narrow: "So" survives
    ],
    ids=["too_wide", "too_early", "too_narrow"],
)
def test_checks_catch_a_detector_answer_that_is_off_by_one_word(
    answer, check: str, expected: list[int], mode: str
) -> None:
    result = _analyze(_answer((answer,)), mode=mode).result

    if check == "touched":
        assert _touched(result, OUTSIDE) == expected
        assert _not_removed(result, RESTART) == []
    else:
        assert _not_removed(result, RESTART) == expected
        assert _touched(result, OUTSIDE) == []
