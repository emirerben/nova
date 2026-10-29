"""KRI-1017 end to end: speech cleanup must not cut a quiet voice the transcript cannot hear.

A synthetic 15 s take is written to a real WAV and pushed through the REAL stages: ffmpeg
``silencedetect`` with the KRI-234 ambient fallback, the real cut planner (V1 baseline and the
V2 mixed-gap candidate), the real ffmpeg PCM decode, the real voice guard, the typed analysis
snapshot with its partition/receipt validators, and the in-render ``_silence_cut_analysis``
path. Only the speech-to-text call is replaced, by the incident's shape: a transcript with
tokens for the creator and none for the quiet guide.

The take (16 kHz mono; noise bed -40 dB; creator voice -20 dB, F0 118 Hz)::

    0.3-2.4   creator, three words
    3.0-3.8   GUIDE 1, tokenless, -36 dB (4 dB over the bed; the ambient threshold is -33 dB,
              so silencedetect calls it silence and the pause rule cuts it)
    4.6-6.0   creator, two words
    6.0-7.5   noise only: a real pause
    7.5-9.0   creator, two words
    9.4-9.8   GUIDE 2, tokenless, -33 dB (an island between two fake silences, so V2 reads
              it as a tokenless "acoustic filler")
    10.2-12.0 creator: a word, a spoken "um" (a transcribed filler, voiced by nature), a word
    12.5-12.8 a noise burst at -30 dB (a cough or click: louder than either guide, not voiced)
    13.3-14.6 creator, two words

Ways this test could mislead, written before it:

- the scenario never cut the voice in the first place, so a passing guard test proves nothing
  (the negative control: the same pipeline with the guard's audio stood down DOES cut both
  guides);
- the guard "passes" by dropping every cut (the remaining plan must still hold the real pause,
  the noise burst and the transcribed "um");
- the guard keeps the "um" because it is not voiced, not because it is exempt (checked by
  running the guard without the exemption);
- loudness is doing the work (the burst is louder than both guides and is kept; guide 1 is
  quieter than the burst and is given back);
- findings and the public receipt keep counting a cut the plan no longer makes.

Artifact: ``voice-guard-e2e-report.json`` in the pytest tmp dir (also written to
``$KRI1017_E2E_OUT`` when set), next to the WAV it describes.
"""

from __future__ import annotations

import json
import os
import shutil
import types
import wave
from pathlib import Path

import numpy as np
import pytest

import app.pipeline.speech_voice_guard as voice_guard
import app.tasks.generative_build as gb
from app.pipeline.audio_pcm import decode_pcm_f32
from app.pipeline.silence_cut import PAD_S, REASON_FILLER_LEXICAL
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupAnalysisResult,
    analyze_speech_cleanup,
)
from app.pipeline.speech_voice_guard import guard_audible_speech, voice_guard_exempt_spans
from app.services.speech_cleanup_identity import SpeechCleanupAssignment
from tests._silent_pcm import silent_pcm

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="needs ffmpeg for silencedetect and PCM decode"
)

SR = 16_000
DURATION_S = 15.0
NOISE_DB, CREATOR_DB, BURST_DB = -40.0, -20.0, -30.0
GUIDE_1 = (3.00, 3.80)
GUIDE_2 = (9.40, 9.80)
GUIDES = (GUIDE_1, GUIDE_2)
BURST = (12.50, 12.80)
REAL_PAUSE = (6.00, 7.50)
WORDS = (
    ("w00", 0.30, 0.90),
    ("w01", 1.00, 1.70),
    ("w02", 1.80, 2.40),
    ("w03", 4.60, 5.20),
    ("w04", 5.30, 6.00),
    ("w05", 7.50, 8.20),
    ("w06", 8.30, 9.00),
    ("w07", 10.20, 10.80),
    ("um", 10.90, 11.30),
    ("w08", 11.40, 12.00),
    ("w09", 13.30, 13.90),
    ("w10", 14.00, 14.60),
)
UM = (10.90, 11.30)
TOL = 0.05


def _voice(n: int, f0: float, level_db: float, phase0: float = 0.0) -> np.ndarray:
    """Harmonic stack with a slow pitch contour and 60 ms fades: periodic, like a voice."""

    t = np.arange(n) / SR
    f = f0 * (1.0 + 0.03 * np.sin(2 * np.pi * 3.0 * t))
    phase = 2 * np.pi * np.cumsum(f) / SR + phase0
    signal = sum(np.sin(k * phase) / k for k in range(1, 13))
    signal = signal * np.minimum(1.0, np.minimum(t, t[-1] - t) / 0.06)
    signal = signal / np.sqrt(np.mean(signal**2))
    return signal * 10 ** (level_db / 20)


def _synthesize() -> np.ndarray:
    rng = np.random.default_rng(1017)
    x = rng.standard_normal(int(DURATION_S * SR)) * 10 ** (NOISE_DB / 20)
    for _text, start, end in WORDS:
        i, j = int(start * SR), int(end * SR)
        x[i:j] += _voice(j - i, 118.0, CREATOR_DB)
    for (start, end), level_db in zip(GUIDES, (-36.0, -33.0), strict=True):
        i, j = int(start * SR), int(end * SR)
        x[i:j] += _voice(j - i, 152.0, level_db, 0.7)
    i, j = int(BURST[0] * SR), int(BURST[1] * SR)
    x[i:j] += rng.standard_normal(j - i) * 10 ** (BURST_DB / 20)
    return np.clip(x, -1.0, 1.0).astype(np.float32)


@pytest.fixture(scope="module")
def take(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("kri1017-e2e") / "take.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes((_synthesize() * 32767).astype("<i2").tobytes())
    return path


def _transcript() -> types.SimpleNamespace:
    """What the incident's transcriber heard: the creator's words, nothing for the guide."""

    return types.SimpleNamespace(
        words=[
            types.SimpleNamespace(text=text, start_s=start, end_s=end, confidence=0.99)
            for text, start, end in WORDS
        ],
        language="en",
        low_confidence=False,
    )


def _analyze(take: Path, *, mode: str, guarded: bool) -> SpeechCleanupAnalysisResult:
    return analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint="e2e-kri1017",
            local_media_path=str(take),
            duration_s=DURATION_S,
            mixed_gap_mode=mode,
        ),
        transcribe_fn=lambda *_a, **_k: _transcript(),
        pcm_loader=None if guarded else silent_pcm,  # silent audio = the guard stood down
    )


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _cuts_over(removals, span: tuple[float, float]) -> list[tuple[float, float, str]]:
    return [
        (round(r.start_s, 3), round(r.end_s, 3), r.reason)
        for r in removals
        if _overlap((r.start_s, r.end_s), span) > 0
    ]


def _spans(result: SpeechCleanupAnalysisResult) -> list[tuple[float, float, str]]:
    return [(item.start_s, item.end_s, item.reason) for item in result.cut_plan.removed]


def _write_report(take: Path, **sections: object) -> Path:
    report = {
        "take": take.name,
        "layout": {
            "guide_1": GUIDE_1,
            "guide_2": GUIDE_2,
            "burst": BURST,
            "real_pause": REAL_PAUSE,
        },
        **sections,
    }
    text = json.dumps(report, indent=2)
    path = take.parent / "voice-guard-e2e-report.json"
    path.write_text(text)
    out_dir = os.environ.get("KRI1017_E2E_OUT")
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / path.name).write_text(text)
        shutil.copy(take, Path(out_dir) / take.name)
    return path


# -- the negative control --------------------------------------------------------------------


def test_without_the_guard_the_same_take_is_cut_through_both_quiet_voices(take):
    result = _analyze(take, mode="apply", guarded=False)

    assert result.diagnostics.get("voice_guard") is None  # nothing was judged: silent audio
    assert result.safety_signals.selected_plan == "candidate"
    over_guide_1 = _cuts_over(result.cut_plan.removed, GUIDE_1)
    over_guide_2 = _cuts_over(result.cut_plan.removed, GUIDE_2)
    assert over_guide_1 and over_guide_1[0][2] == "silence"  # a pause cut through the voice
    assert over_guide_2  # a cluster the filler check never sees (label "silence", not a filler)
    # V2 also reads guide 2 as a tokenless acoustic filler, and says so in the findings.
    assert any(
        f.reason == "filler_acoustic" and _overlap((f.start_s, f.end_s), GUIDE_2) > 0
        for f in result.findings
    )


# -- the engine ------------------------------------------------------------------------------


def test_engine_keeps_both_quiet_voices_and_every_cut_that_is_not_a_voice(take):
    unguarded = _analyze(take, mode="apply", guarded=False)
    guarded = _analyze(take, mode="apply", guarded=True)

    # Neither quiet voice is cut, and no finding or receipt count still claims it was.
    for guide in GUIDES:
        assert _cuts_over(guarded.cut_plan.removed, guide) == []
        assert [f for f in guarded.findings if _overlap((f.start_s, f.end_s), guide) > 0] == []

    # It gave back exactly the two cuts that held a voice (dropped, or a pause trimmed to its
    # voice-free edge) ...
    report = guarded.diagnostics["voice_guard"]
    assert report["status"] == "ok"
    judged = [*report["dropped"], *report["trimmed"]]
    assert report["dropped_total"] + report["trimmed_total"] == 2
    spans = sorted((d["start_s"], d["end_s"]) for d in judged)
    for (lo, hi), guide in zip(spans, GUIDES, strict=True):
        assert lo - TOL <= guide[0] and guide[1] <= hi + TOL
    assert all(d["voiced_s"] > 0.3 for d in judged)

    # ... and nothing else: the real pause, the noise burst (louder than either guide) and the
    # transcribed "um" are still cut. Same plan minus those two cuts, plus any voice-free edge
    # a trimmed pause kept.
    kept = {(round(a, 3), round(b, 3), r) for a, b, r in _spans(guarded)}
    expected = {
        (round(a, 3), round(b, 3), r)
        for a, b, r in _spans(unguarded)
        if all(_overlap((a, b), guide) == 0 for guide in GUIDES)
    }
    remnants = {
        (t["kept_cut_start_s"], t["kept_cut_end_s"], t["reason"]) for t in report["trimmed"]
    }
    assert kept == expected | remnants
    assert _cuts_over(guarded.cut_plan.removed, REAL_PAUSE)
    assert _cuts_over(guarded.cut_plan.removed, BURST)
    assert [c for c in _cuts_over(guarded.cut_plan.removed, UM) if c[2] == REASON_FILLER_LEXICAL]
    assert any(
        f.reason == "filler_acoustic" and _overlap((f.start_s, f.end_s), BURST) > 0
        for f in guarded.findings
    )

    # The snapshot still partitions the window exactly and its receipt matches its findings.
    restored = SpeechCleanupAnalysisResult.from_payload(guarded.to_payload())
    assert restored == guarded
    removed_s = sum(b - a for a, b, _ in _spans(guarded))
    assert guarded.cut_plan.time_saved_s == pytest.approx(removed_s)
    assert guarded.public_receipt.estimated_removed_ms == round(removed_s * 1000)
    assert guarded.public_receipt.candidate_count == len(guarded.findings)
    assert guarded.public_receipt.category_counts.filler_sounds == 2  # the "um" and the burst
    # both real pauses, plus the noise-only edge of a voiced pause the guard trimmed
    assert guarded.public_receipt.category_counts.long_pauses == 2 + len(report["trimmed"])

    _write_report(
        take,
        without_guard=[list(c) for c in _spans(unguarded)],
        with_guard=[list(c) for c in _spans(guarded)],
        dropped=report["dropped"],
        trimmed=report["trimmed"],
        findings=[[f.start_s, f.end_s, f.reason] for f in guarded.findings],
    )


def test_baseline_plan_is_guarded_too(take):
    unguarded = _analyze(take, mode="off", guarded=False)
    guarded = _analyze(take, mode="off", guarded=True)

    assert _cuts_over(unguarded.cut_plan.removed, GUIDE_1)
    assert _cuts_over(guarded.cut_plan.removed, GUIDE_1) == []
    report = guarded.diagnostics["voice_guard"]
    assert report["dropped_total"] + report["trimmed_total"] >= 1
    assert _cuts_over(guarded.cut_plan.removed, REAL_PAUSE)


def test_the_transcribed_um_is_kept_because_it_is_exempt_not_because_it_is_quiet(take):
    result = _analyze(take, mode="apply", guarded=True)
    plan = result.cut_plan.to_cut_plan()
    samples = decode_pcm_f32(str(take))
    words = _transcript().words

    exempt = voice_guard_exempt_spans(words)
    assert any(lo <= UM[0] - PAD_S + 1e-6 and UM[1] + PAD_S - 1e-6 <= hi for lo, hi in exempt)

    bare, bare_report = guard_audible_speech(plan, samples)  # no exemption: the um is voiced
    assert [d for d in bare_report["dropped"] if d["reason"] == REASON_FILLER_LEXICAL]
    assert not _cuts_over(bare.removed, UM)
    exempted, exempted_report = guard_audible_speech(plan, samples, exempt=exempt)
    assert exempted_report["dropped"] == []
    assert _cuts_over(exempted.removed, UM)


def test_exempt_spans_read_dicts_and_objects_alike_and_cover_retakes_and_forced_cuts():
    objects = _transcript().words
    dicts = [
        {"text": w.text, "start_s": w.start_s, "end_s": w.end_s, "confidence": 0.99}
        for w in objects
    ]
    forced = [{"start_s": 1.0, "end_s": 1.5, "reason": "manual_review"}]

    from_objects = voice_guard_exempt_spans(objects, [(3, 4)], forced)
    assert from_objects == voice_guard_exempt_spans(dicts, [(3, 4)], forced)
    covered = [
        (lo, hi)
        for lo, hi in from_objects
        if lo <= 4.6 - PAD_S + 1e-6 and 6.0 + PAD_S - 1e-6 <= hi  # words 3-4, padded
    ]
    assert covered
    assert any(lo <= 1.0 and 1.5 <= hi for lo, hi in from_objects)  # the forced removal
    assert voice_guard_exempt_spans(objects, [(9, 99), ("x", 1)]) == voice_guard_exempt_spans(
        objects
    )  # malformed retake spans are skipped, never raised


# -- the in-render path ----------------------------------------------------------------------


def _no_database():
    raise RuntimeError("no database in this test: the optional review state fails open")


def _in_render_plan(take: Path, *, policy: str, decode=None):
    """``_silence_cut_analysis`` on the take; ``decode`` replaces the guard's PCM decode."""

    import app.pipeline.transcribe as transcribe_mod
    import app.services.pipeline_trace as pt

    events: list[tuple[str, str, dict]] = []
    required = policy == "required_v1"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(transcribe_mod, "transcribe_whisper", lambda *_a, **_k: _transcript())
        patch.setattr(
            pt,
            "record_pipeline_event",
            lambda stage, event, data=None: events.append((stage, event, data)),
        )
        patch.setattr(pt, "record_speech_cleanup_detection", lambda _receipt: "persisted")
        patch.setattr(gb, "_sync_session", _no_database)
        patch.setattr(gb.settings, "speech_cleanup_mixed_gap_mode", "apply", raising=False)
        patch.setattr(gb.settings, "speech_cleanup_mixed_gap_rollout_percent", 100, raising=False)
        if decode is not None:
            patch.setattr(voice_guard, "decode_pcm_f32", decode)
        entry = gb._silence_cut_analysis(
            str(take),
            DURATION_S,
            job_id="00000000-0000-0000-0000-000000001017",
            cache=None,
            include_retakes=False,
            include_silence_and_fillers=True,
            analysis_policy=policy,
            render_trace_id="e" * 32 if required else None,
            speech_cleanup_assignment=(
                SpeechCleanupAssignment(
                    source_slot=0, rollout_fingerprint="a" * 64, status="assigned"
                )
                if required
                else None
            ),
        )
    return entry, events


@pytest.mark.parametrize(
    "policy",
    [
        pytest.param("legacy_auto", id="off-branch"),
        pytest.param("required_v1", id="mixed-gap-comparison-branch"),
    ],
)
def test_in_render_analysis_is_guarded_on_both_branches(take, policy):
    control, _ = _in_render_plan(take, policy=policy, decode=silent_pcm)  # guard stood down
    assert control["failed"] is False
    assert _cuts_over(control["plan"].removed, GUIDE_1)  # the negative control cuts the voice

    entry, events = _in_render_plan(take, policy=policy)

    assert entry["failed"] is False
    plan = entry["plan"]
    assert _cuts_over(plan.removed, GUIDE_1) == []
    assert _cuts_over(plan.removed, REAL_PAUSE)
    guard_events = [data for _stage, event, data in events if event == "silence_cut_voice_guard"]
    assert len(guard_events) == 1
    assert guard_events[0]["dropped_total"] + guard_events[0]["trimmed_total"] >= 1
    # The partition still holds: keep + removed cover the clip exactly.
    spans = sorted([*plan.keep_segments, *((r.start_s, r.end_s) for r in plan.removed)])
    assert spans[0][0] == pytest.approx(0.0)
    assert spans[-1][1] == pytest.approx(DURATION_S)
    assert all(b[0] == pytest.approx(a[1]) for a, b in zip(spans, spans[1:], strict=False))
    assert plan.time_saved_s == pytest.approx(sum(r.end_s - r.start_s for r in plan.removed))
    if policy == "required_v1":
        context = entry["speech_cleanup_outcome_context"]
        assert context["selected_plan"] == "candidate"
        assert context["output_removal_count"] == len(plan.removed)  # the guarded plan, not V2's


def test_an_undecodable_clip_drops_every_judged_cut_instead_of_cutting_blind(take):
    entry, events = _in_render_plan(
        take, policy="legacy_auto", decode=lambda _path: np.zeros(0, dtype=np.float32)
    )

    assert entry["failed"] is False
    assert entry["plan"].removed == []
    (guard_event,) = [data for _stage, event, data in events if event == "silence_cut_voice_guard"]
    assert guard_event["status"] == "audio_unavailable"
