"""KRI-1512 (follow-up to KRI-1507), Cusco DJI T2 "list 1" (prod job 887f89d6): replayed.

After #2374 moved "Kilitli sistemler" onto its voice, Clean up speech still kept four pauses of
about 1 s on this take. Three were hidden by whisper-1 stretching words over them: the head of
"1." (B, 5.55-6.55 s) and of "sertlikten" (C, 9.25-10.30 s) stamped back over the pause before
their voice, and "Ve 4'te" (D, 37.40-38.50 s) stamped from the voice into the pause after it.
Those are now cut. The fourth (A, 1.35-2.10 s) holds a steady quiet hum the KRI-1017 voice guard
keeps on purpose, and stays.

Real: the cleanup analysis (`analyze_speech_cleanup`: the late-word move with straddling runs,
the stretched-head trim, the cut planner, the voice guard) and the phone Talking worker that
times the captions and list labels. Fixtures: the #2374 transport (`kri1502_cusco_t2/
incident.json`: the stored words, the clip's silence spans and voiced runs, from which the take
is re-synthesized) and this ticket's independent truth (`kri1512_pauses.json`: voice-band
pauses, voicing bounds, local whisper.cpp reads). No model call, no media. Failure modes:
docs/reviews/kri-1512-stretched-words-failure-modes.md.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.pipeline.silence_cut import remap_time
from app.pipeline.speech_voice_guard import quiet_floor_db, voiced_runs
from tests.tasks.test_kri1502_cusco_t2_replay import (
    DURATION_S,
    FIXTURE,
    WORDS,
    _analysis,
    _kept_s,
    _label,
    _pcm,
    _plan,
    _removed,
    _render,
    _source_time,
    _word,
)

TRUTH = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "kri1502_cusco_t2"
        / "kri1512_pauses.json"
    ).read_text(encoding="utf-8")
)
PAUSES = TRUTH["pauses"]
AFTER_2374 = [tuple(span) for span in TRUTH["after_2374"]["removed"]]
VOICED = [tuple(run) for run in FIXTURE["voiced_runs"]]
LABEL_TEXTS = [row["text"] for row in FIXTURE["prod"]["labels"]]


def _on(monkeypatch, **kwargs):
    return _analysis(monkeypatch, late_words=True, stretched_words=True, **kwargs)


def _off(monkeypatch, **kwargs):
    return _analysis(monkeypatch, late_words=True, stretched_words=False, **kwargs)


def _gap(name: str) -> tuple[float, float]:
    lo, hi = PAUSES[name]["voice_band"]
    return lo, hi


def _in(span: tuple[float, float], removed) -> list[tuple[float, float]]:
    lo, hi = span
    return [(a, b) for a, b in removed if a < hi and lo < b]


def _overlap(lo: float, hi: float, runs) -> float:
    return sum(max(0.0, min(hi, b) - max(lo, a)) for a, b in runs)


def _without(runs, lo: float, hi: float):
    return [run for run in runs if not (lo <= run[0] < hi)]


# ------------------------------------------------------------------ [o] the oracle


def test_o_the_stamps_hide_pauses_b_c_d_that_the_audio_hears():
    """The independent reads and whisper-1 disagree exactly as the ticket says."""
    by_text = {w["text"]: w for w in FIXTURE["words"]}
    for name, before, after in (("B", "bunlar?", "1."), ("C", "mütemadiyen", "sertlikten")):
        pause = PAUSES[name]
        lo, hi = pause["silence_span"]
        # The stamped gap is under MAX_PAUSE_S: rule 3 cannot see the pause ...
        assert by_text[after]["start_s"] - by_text[before]["end_s"] < 0.6
        # ... because the next word's stamp starts inside it, while its voice starts after it.
        assert lo <= by_text[after]["start_s"] < hi < pause["voice_after"][0] < hi + 0.2
        assert _overlap(lo, hi, VOICED) < 0.05
    d = PAUSES["D"]
    lo, hi = d["silence_span"]
    assert d["voice_before"][0] <= by_text["Ve"]["start_s"] < lo < by_text["Ve"]["end_s"] < hi
    assert lo <= by_text["4'te"]["start_s"] and by_text["4'te"]["end_s"] <= hi
    assert _overlap(lo, hi, VOICED) < 0.05


def test_o_the_resynthesized_take_is_voiced_around_each_pause_as_the_clip_is():
    """Transport check, pause by pause (the #2374 replay checks the whole take). The hum in A is
    checked in the window the voice guard reads for the cut it drops (1.505-2.035 s, plus its
    0.1 s context): its 1.72 s burst is only heard there, as on the clip."""
    samples = _pcm()
    floor = quiet_floor_db(samples)
    heard = voiced_runs(samples, 0.0, DURATION_S, floor_db=floor)
    for name in "BCD":
        lo, hi = _gap(name)
        assert _overlap(lo - 0.5, hi + 0.5, heard) == pytest.approx(
            _overlap(lo - 0.5, hi + 0.5, VOICED), abs=0.03
        ), name
    guard_window = (1.405, 2.135)
    assert _overlap(*guard_window, voiced_runs(samples, *guard_window, floor_db=floor)) == (
        pytest.approx(_overlap(*guard_window, VOICED), abs=0.03)
    )


# ------------------------------------------------------------------ the control: #2374 alone


def test_control_switch_off_is_the_late_word_plan(monkeypatch):
    result = _off(monkeypatch)
    assert _removed(result) == AFTER_2374
    assert "stretched_heads" not in result.diagnostics
    assert all("straddle" not in run for run in result.diagnostics["late_words"])
    for name in "BCD":
        assert _kept_s(*_gap(name), AFTER_2374) >= 1.0


# ------------------------------------------------------------------ the fix


def test_pauses_b_c_d_are_cut_and_no_cut_touches_a_voice(monkeypatch):
    removed = _removed(_on(monkeypatch))
    for name in "BCD":
        assert _in(_gap(name), removed), name
        assert _kept_s(*_gap(name), removed) <= 0.6, name
    # No cut holds any of the take's voice (the voice guard has nothing left to give back
    # outside pause A), and each new cut keeps a pad before the voice after it.
    for lo, hi in removed:
        assert _overlap(lo, hi, VOICED) == 0.0, (lo, hi)
    for name in "BCD":
        [(_lo, hi)] = _in(_gap(name), removed)
        assert PAUSES[name]["voice_after"][0] - hi >= 0.1, name
    # Every other cut is #2374's; the one before "Ve" now ends at its moved start's pad.
    new = {span for name in "BCD" for span in _in(_gap(name), removed)}
    rest = [span for span in removed if span not in new]
    assert [s for s in rest if s[0] != 35.965] == [s for s in AFTER_2374 if s[0] != 35.965]
    [before_ve] = [s for s in rest if s[0] == 35.965]
    assert before_ve[1] <= PAUSES["D"]["voice_before"][0] - 0.1


def test_the_words_start_on_their_voice(monkeypatch):
    result = _on(monkeypatch)
    one, sertlikten = _word(result, "1."), _word(result, "sertlikten")
    ve, dortte = _word(result, "Ve"), _word(result, "4'te")
    assert one[0] == pytest.approx(PAUSES["B"]["voice_after"][0], abs=0.05)
    assert sertlikten[0] == pytest.approx(PAUSES["C"]["voice_after"][0], abs=0.08)
    assert ve[0] == pytest.approx(PAUSES["D"]["voice_before"][0], abs=0.05)
    assert dortte[1] == pytest.approx(PAUSES["D"]["voice_before"][1], abs=0.1)
    assert ve[1] == pytest.approx(dortte[0])
    # The word before each trimmed head ends where the pause starts, not inside it.
    assert _word(result, "bunlar?")[1] == pytest.approx(PAUSES["B"]["silence_span"][0])
    assert _word(result, "mütemadiyen")[1] == pytest.approx(PAUSES["C"]["silence_span"][0])
    changed = {"bunlar?", "1.", "mütemadiyen", "sertlikten", "Ve", "4'te", "Kilitli", "sistemler"}
    for before, after in zip(FIXTURE["words"], result.timed_words, strict=True):
        assert after.text == before["text"]
        if before["text"] not in changed:
            assert (after.start_s, after.end_s) == (before["start_s"], before["end_s"])
    assert result.diagnostics["stretched_heads"] == [
        {"start_s": [6.2, 6.547], "pause_s": [5.582, 6.547], "previous_end_s": [5.64, 5.582]},
        {"start_s": [9.5, 10.243], "pause_s": [9.241, 10.243], "previous_end_s": [9.5, 9.241]},
    ]
    assert result.diagnostics["late_words"][1] == {
        "words": 2,
        "stamped_s": [37.0, 38.2],
        "moved_to_s": [36.839, 37.413],
        "pause_s": [37.413, 38.486],
        "straddle": True,
    }
    # Rule 0 no longer has a stretched head to carve at B or C.
    for adjustment in result.diagnostics.get("token_adjustments", []):
        assert not 5.0 < adjustment["original_start_s"] < 11.0


def test_a_the_hum_after_bahsettik_stays_and_the_voice_guard_is_why(monkeypatch):
    """Kept on purpose: 1.45-2.03 s holds steady quiet voicing (a hum, 103-125 Hz, lower than
    the speaker) that no other pause of the take has. The voice guard drops the cut there."""
    result = _on(monkeypatch)
    assert _kept_s(*_gap("A"), _removed(result)) == pytest.approx(0.75)
    [dropped] = result.diagnostics["voice_guard"]["dropped"]
    assert dropped["start_s"] == pytest.approx(1.505) and dropped["voiced_s"] > 0.05
    hum = PAUSES["A"]["hum"]
    assert hum["pitch_hz"][1] < PAUSES["A"]["speaker_pitch_hz"][0]
    assert hum["over_floor_db"][0] > PAUSES["A"]["other_pauses"]["mean_over_floor_db"][1]


def test_the_new_cuts_are_ordinary_pause_cuts(monkeypatch):
    result = _on(monkeypatch)
    assert {r.reason for r in result.cut_plan.removed} == {"silence"}
    assert result.public_receipt.category_counts.long_pauses == len(result.cut_plan.removed)


# ------------------------------------------------------------------ wrong-content controls


def test_control_voice_in_pauses_b_and_c_trims_nothing(monkeypatch):
    """Quiet speech the silence floor missed (voicing inside the "pause"): the stamps stay."""
    pcm = _pcm(voiced=[*VOICED, (6.0, 6.2), (9.6, 9.8)])
    on = _on(monkeypatch, pcm=pcm)
    off = _off(monkeypatch, pcm=pcm)
    assert "stretched_heads" not in on.diagnostics
    assert _word(on, "1.")[0] == pytest.approx(6.2)
    assert _word(on, "sertlikten")[0] == pytest.approx(9.5)
    for name in "BC":
        assert _in(_gap(name), _removed(on)) == _in(_gap(name), _removed(off))


def test_control_voice_running_into_the_pause_trims_nothing(monkeypatch):
    """Çiğdem S4's shape at 2.30 s: the voice before the pause runs 0.06 s into the silence span
    ("bunlar?" voiced to 5.64 s here, the span starts at 5.582 s). Read over the pause alone the
    detector hears none of it (too short inside the window); read whole, as the voice guard reads
    a cut, it is 0.058 s of voice in the pause, so nothing is trimmed."""
    voiced = [run if run[0] != 4.895 else (4.895, 5.64) for run in VOICED]
    on = _on(monkeypatch, pcm=_pcm(voiced=voiced))
    assert [head["pause_s"] for head in on.diagnostics["stretched_heads"]] == [[9.241, 10.243]]
    assert _word(on, "1.")[0] == pytest.approx(6.2)


def test_control_no_voice_after_the_pauses_trims_nothing(monkeypatch):
    """The sound after the pause is not a voice (loud aperiodic noise where "bir" and
    "sertlikten" are said): nothing proves the word's voice starts there, so nothing changes."""
    voiced = _without(_without(VOICED, 6.5, 6.8), 10.25, 10.9)
    pcm = _pcm(voiced=voiced, noise_spans=((6.56, 6.72), (10.3, 10.85)))
    on = _on(monkeypatch, pcm=pcm)
    assert "stretched_heads" not in on.diagnostics
    assert _word(on, "1.")[0] == pytest.approx(6.2)
    assert _word(on, "sertlikten")[0] == pytest.approx(9.5)


def test_control_voice_under_the_straddled_stamps_moves_nothing(monkeypatch):
    """Voicing where whisper stamped "4'te": the stamps may be right, so "Ve 4'te" stays."""
    on = _on(monkeypatch, pcm=_pcm(voiced=[*VOICED, (37.8, 38.0)]))
    assert all("straddle" not in run for run in on.diagnostics.get("late_words", []))
    assert _word(on, "Ve")[0] == pytest.approx(37.0)


def test_control_voice_running_into_the_straddle_pause_moves_nothing(monkeypatch):
    """The voice of "dörtte" runs 0.06 s into the silence span where "4'te" is stamped. Read over
    the stamps alone it is too short to be heard; read whole, it is voice under the stamps, so the
    run stays where whisper put it."""
    voiced = [run if run[0] != 37.265 else (37.265, 37.47) for run in VOICED]
    on = _on(monkeypatch, pcm=_pcm(voiced=voiced))
    assert all("straddle" not in run for run in on.diagnostics.get("late_words", []))
    assert _word(on, "Ve")[0] == pytest.approx(37.0)


def test_control_no_voice_on_the_straddle_island_moves_nothing(monkeypatch):
    """The sound before the pause is not a voice: "Ve 4'te" has nowhere proven to go."""
    voiced = _without(VOICED, 36.8, 37.4)
    on = _on(monkeypatch, pcm=_pcm(voiced=voiced, noise_spans=((36.82, 37.33),)))
    assert all("straddle" not in run for run in on.diagnostics.get("late_words", []))
    assert _word(on, "Ve")[0] == pytest.approx(37.0)


def test_a_take_that_cannot_be_decoded_changes_no_word_and_is_decoded_once(monkeypatch):
    """Failure mode 13: a decode fault moves and trims nothing, and the late-word check, the
    stretched-head check and the voice guard share one failed attempt (a hung ffmpeg times out
    once, not three times)."""
    from app.config import settings
    from app.pipeline.speech_cleanup_analysis import (
        SpeechCleanupAnalysisInput,
        analyze_speech_cleanup,
    )
    from app.services.clip_speech import SilenceDetectionResult

    monkeypatch.setattr(settings, "speech_cleanup_late_words_enabled", True)
    monkeypatch.setattr(settings, "speech_cleanup_stretched_words_enabled", True)
    attempts = []

    def failing_loader(path):
        attempts.append(path)
        raise OSError("ffmpeg timed out")

    result = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint="c" * 64,
            local_media_path="preflight.wav",
            duration_s=DURATION_S,
            source_window_start_s=0.0,
            source_window_end_s=DURATION_S,
            mixed_gap_mode="apply",
            over_budget_policy="clamp",
            max_removal_frac_required=1.0,
            filler_check=False,
        ),
        transcribe_fn=lambda *_a, **_k: {"language": "tr", "words": copy.deepcopy(WORDS)},
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(
            spans=[tuple(span) for span in FIXTURE["silence_spans"]], status="ok"
        ),
        pcm_loader=failing_loader,
    )
    assert attempts == ["preflight.wav"]
    assert [(w.start_s, w.end_s) for w in result.timed_words] == [
        (w["start_s"], w["end_s"]) for w in WORDS
    ]
    assert "late_words" not in result.diagnostics
    assert "stretched_heads" not in result.diagnostics
    assert result.diagnostics["voice_guard"]["status"] == "audio_unavailable"


# ------------------------------------------------------------------ the worker: labels + captions


@pytest.fixture(scope="module")
def renders():
    """The phone Talking worker with the late-word move, without and with this step."""
    out = {}
    for stretched in (False, True):
        with pytest.MonkeyPatch.context() as patch:
            out[stretched] = _render(patch, late_words=True, stretched_words=stretched)
    return out


def test_the_four_list_labels_stay_on_their_source_moment(renders):
    off_variant, off_result = renders[False]
    on_variant, on_result = renders[True]
    for text in LABEL_TEXTS:
        off_row, on_row = _label(off_variant, text), _label(on_variant, text)
        assert on_row["status"] == "placed" and on_row["basis"] == "words"
        source_off = _source_time(off_row["start_s"], _plan(off_result))
        source_on = _source_time(on_row["start_s"], _plan(on_result))
        assert source_on == pytest.approx(source_off, abs=0.01), text


def test_the_captions_of_the_trimmed_words_start_on_their_voice(renders):
    variant, result = renders[True]
    plan = _plan(result)
    words = [w for cue in variant["caption_cues"] for w in cue.get("words") or []]
    texts = [w["text"].strip(".,?") for w in words]
    for text, voice_s in (
        ("sertlikten", PAUSES["C"]["voice_after"][0]),
        ("Ve", PAUSES["D"]["voice_before"][0]),
    ):
        said_at = remap_time(voice_s, plan)
        assert words[texts.index(text)]["start_s"] == pytest.approx(said_at, abs=0.15), text
    # The take is shorter by the three new cuts, and nothing else changed length.
    _off_variant, off_result = renders[False]
    saved = sum(b - a for a, b in _removed(result)) - sum(b - a for a, b in _removed(off_result))
    assert saved == pytest.approx(0.658 + 0.752 + 0.537 + 0.014, abs=0.01)
    assert DURATION_S - saved > 0
