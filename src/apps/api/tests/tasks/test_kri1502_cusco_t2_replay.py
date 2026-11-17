"""KRI-1502 Cusco DJI T2 "list 1" (prod job 887f89d6, thread ca6a8138, 2026-11-16): replayed.

The speaker says "Kilitli sistemler", pauses about 2 s, then says "yıkılmayı zorlaştırıyor".
whisper-1 stamped "Kilitli sistemler" at 13.96-15.24 s, inside that pause; the words' voice
(12.30-13.40 s, by a voice-band read and a local whisper.cpp pass, neither of them whisper-1)
got no word. Label "2. Kilitli sistemler" and its caption then came 1.6 s late, in the silence
(KRI-1506), and Clean up speech kept that 2.05 s pause while it cut shorter ones (KRI-1507).

Real: the cleanup analysis (`analyze_speech_cleanup`: the cut planner, the KRI-1017 voice guard
and the late-word step), its preflight snapshot, and the phone Talking worker
(`_run_generative_job` -> `_run_phone_subtitled_job`) that times the list labels and captions.
Fixture (`tests/fixtures/kri1502_cusco_t2/incident.json`): transport only. The words prod's
snapshot stored, the kit clip's silence spans, and its voiced runs, from which the take's PCM
is re-synthesized (a voiced tone where the clip is voiced, room noise elsewhere). No model call,
no media. Failure modes: docs/reviews/kri-1502-late-words-failure-modes.md.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import numpy as np
import pytest

import app.pipeline.phone_subtitled_title as title_mod
import app.services.pipeline_trace as pipeline_trace
from app.config import settings
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.pipeline.silence_cut import CutPlan, Removal, remap_time
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupAnalysisResult,
    analyze_speech_cleanup,
)
from app.pipeline.speech_voice_guard import quiet_floor_db, voiced_runs
from app.services.client_protocol import clear_request_context
from app.services.clip_speech import SilenceDetectionResult
from app.tasks import generative_build as gb
from tests._prod_profile import PROD_VERIFIED_FEATURES, apply_prod_profile
from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri1502_cusco_t2" / "incident.json"
    ).read_text(encoding="utf-8")
)
DURATION_S = float(FIXTURE["duration_s"])
WORDS = FIXTURE["words"]
SILENCES = [tuple(span) for span in FIXTURE["silence_spans"]]
PROD = FIXTURE["prod"]
PROD_REMOVED = [tuple(span) for span in PROD["removed"]]
TRUTH = FIXTURE["speech_truth"]
KILITLI = tuple(TRUTH["kilitli_sistemler"])  # (12.30, 13.40): where the words are said
PAUSE = tuple(TRUTH["pause_after"])  # (13.40, 15.40): the pause after them
SR = 16_000
# The job's face band on the take (`list_label_placement.rows[1].placement.face_band`).
FACE = NormalizedBox(0.32542, 0.39875, 0.77667, 0.60148)
LABEL_TEXTS = [row["text"] for row in PROD["labels"]]
_HOP_S = 0.01  # speech_voice_guard.HOP_S


def _pcm(*, voiced: list[tuple[float, float]] | None = None, noise_spans: tuple = ()) -> np.ndarray:
    """The take re-synthesized: room noise at the clip's floor, a voiced tone in each voiced
    run at the clip's speech level. ``noise_spans`` replace their stretch with loud APERIODIC
    sound (the wrong-content control: sound that is not a voice)."""
    rng = np.random.default_rng(1502)
    n = int(DURATION_S * SR)
    floor_rms = 10 ** (FIXTURE["floor_db"] / 20)
    speech_rms = 10 ** (FIXTURE["speech_db"] / 20)
    out = rng.normal(0.0, floor_rms, n)
    t = np.arange(n) / SR
    tone = sum(np.sin(2 * np.pi * 140.0 * k * t) / k for k in (1, 2, 3, 4))
    tone *= speech_rms / np.sqrt(np.mean(tone**2))
    for lo, hi in voiced if voiced is not None else FIXTURE["voiced_runs"]:
        # A recorded run spans frame centres +- half a hop; a tone one hop shorter on each
        # side is heard as exactly that run (the transport check below pins it).
        a, b = int((lo + _HOP_S) * SR), int((hi - _HOP_S) * SR)
        out[a:b] = tone[a:b] + rng.normal(0.0, floor_rms, b - a)
    for lo, hi in noise_spans:
        a, b = int(lo * SR), int(hi * SR)
        out[a:b] = rng.normal(0.0, speech_rms, b - a)
    return out.astype(np.float32)


def _analysis(
    monkeypatch, *, late_words: bool, words=None, pcm: np.ndarray | None = None
) -> SpeechCleanupAnalysisResult:
    """The real cleanup analysis on the recorded take, as prod's preflight ran it."""
    monkeypatch.setattr(settings, "speech_cleanup_late_words_enabled", late_words)
    samples = _pcm() if pcm is None else pcm
    return analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint="c" * 64,
            local_media_path="preflight.wav",
            duration_s=DURATION_S,
            source_window_start_s=0.0,
            source_window_end_s=DURATION_S,
            mixed_gap_mode="apply",
            over_budget_policy="clamp",
            max_removal_frac_required=1.0,
            filler_check=True,
        ),
        transcribe_fn=lambda *_a, **_k: {
            "language": "tr",
            "words": copy.deepcopy(words if words is not None else WORDS),
            "low_confidence": False,
        },
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(spans=SILENCES, status="ok"),
        pcm_loader=lambda _path: samples,
    )


def _removed(result: SpeechCleanupAnalysisResult) -> list[tuple[float, float]]:
    return [(round(r.start_s, 3), round(r.end_s, 3)) for r in result.cut_plan.removed]


def _word(result: SpeechCleanupAnalysisResult, text: str) -> tuple[float, float]:
    [word] = [w for w in result.timed_words if w.text == text]
    return word.start_s, word.end_s


def _kept_s(lo: float, hi: float, removed) -> float:
    return hi - lo - sum(max(0.0, min(hi, b) - max(lo, a)) for a, b in removed)


def _plan(result: SpeechCleanupAnalysisResult) -> CutPlan:
    return result.cut_plan.to_cut_plan()


# ------------------------------------------------------------------ [o] the oracle


def test_o_the_truth_puts_the_words_before_the_pause_whisper_put_them_in():
    """The independent read and whisper-1 disagree exactly as the ticket says: the voice is in
    whisper's wordless gap, and whisper's stamps are in the pause."""
    by_text = {w["text"]: w for w in WORDS}
    assert by_text["güçlü."]["end_s"] <= KILITLI[0] and KILITLI[1] <= by_text["Kilitli"]["start_s"]
    for text in ("Kilitli", "sistemler"):
        assert PAUSE[0] <= by_text[text]["start_s"] and by_text[text]["end_s"] <= PAUSE[1]
    # The longest pause the take holds.
    assert max(gap[2] for gap in TRUTH["voice_band_gaps"]) == pytest.approx(2.0)


def test_o_the_resynthesized_take_is_voiced_where_the_clip_is():
    """Transport check: the voicing detector hears the synthetic take as it heard the clip."""
    samples = _pcm()
    heard = voiced_runs(samples, 0.0, DURATION_S, floor_db=quiet_floor_db(samples))
    recorded = FIXTURE["voiced_runs"]

    def total(runs, lo, hi):
        return sum(max(0.0, min(hi, b) - max(lo, a)) for a, b in runs)

    # The recorded runs include a 50 ms one only the voice guard's own window hears (1.72 s).
    assert abs(len(heard) - len(recorded)) <= 1
    for lo, hi in (KILITLI, PAUSE):
        assert total(heard, lo, hi) == pytest.approx(total(recorded, lo, hi), abs=0.02)
    assert total(heard, 0.0, DURATION_S) == pytest.approx(
        total(recorded, 0.0, DURATION_S), rel=0.01
    )


# ------------------------------------------------------------------ the control: prod


def test_control_switch_off_reproduces_the_nine_cuts_prod_made(monkeypatch):
    result = _analysis(monkeypatch, late_words=False)
    assert _removed(result) == PROD_REMOVED
    assert _word(result, "Kilitli")[0] == pytest.approx(13.96)
    # The 2 s pause stays: 2.0 s of it kept.
    assert _kept_s(*PAUSE, PROD_REMOVED) == pytest.approx(2.0)


# ------------------------------------------------------------------ the fix: the analysis


def test_kilitli_sistemler_moves_onto_its_voice(monkeypatch):
    result = _analysis(monkeypatch, late_words=True)
    kilitli, sistemler = _word(result, "Kilitli"), _word(result, "sistemler")
    assert kilitli[0] == pytest.approx(KILITLI[0], abs=0.06)
    assert sistemler[1] == pytest.approx(KILITLI[1], abs=0.06)
    assert kilitli[1] == pytest.approx(sistemler[0])  # order and contiguity kept
    # No other word moved, and the order is the transcript's.
    moved = {"Kilitli", "sistemler"}
    for before, after in zip(WORDS, result.timed_words, strict=True):
        assert after.text == before["text"]
        if before["text"] not in moved:
            assert (after.start_s, after.end_s) == (before["start_s"], before["end_s"])
    assert result.diagnostics["late_words"] == [
        {
            "words": 2,
            "stamped_s": [13.96, 15.24],
            "moved_to_s": [12.306, 13.384],
            "pause_s": [13.384, 15.288],
        }
    ]


def test_the_two_second_pause_is_cut_and_every_other_cut_stays(monkeypatch):
    removed = _removed(_analysis(monkeypatch, late_words=True))
    # KRI-1507: the take's longest pause is now cut down like the others.
    assert _kept_s(*PAUSE, removed) <= 0.5
    # Nothing is cut out of the words themselves or the speech after the pause.
    for lo, hi in removed:
        assert hi <= KILITLI[0] or lo >= KILITLI[1]
        assert hi <= PAUSE[1] or lo >= TRUTH["yikilmayi_onward"][1]
    # The seven cuts away from these words are prod's, unchanged.
    far = [span for span in PROD_REMOVED if not 11.0 <= span[0] <= 16.0]
    assert [span for span in removed if not 11.0 <= span[0] <= 16.0] == far
    # The pause before the words is still cut (to the words' own onset, not past it).
    [before] = [span for span in removed if 11.0 <= span[0] <= 12.3]
    assert before[0] == pytest.approx(11.565) and before[1] <= KILITLI[0] - 0.1
    # No pause the cleanup kept is as long as a pause it cut.
    kept = [_kept_s(lo, hi, removed) for lo, hi, _len in TRUTH["voice_band_gaps"]]
    assert max(kept) < 1.2


def test_the_wordless_voice_is_no_longer_a_filler_candidate(monkeypatch):
    """Before the fix rule 2 proposed the real words as an acoustic filler; only the voice guard
    kept them. With the words on their voice there is nothing for it to propose."""
    off = _analysis(monkeypatch, late_words=False)
    on = _analysis(monkeypatch, late_words=True)
    assert any(
        c["start_s"] < KILITLI[1] and KILITLI[0] < c["end_s"]
        for c in off.diagnostics["acoustic_candidates"]
    )
    assert not any(
        c["start_s"] < KILITLI[1] and KILITLI[0] < c["end_s"]
        for c in on.diagnostics.get("acoustic_candidates", [])
    )
    assert "voice_guard" not in on.diagnostics or all(
        not (row["start_s"] < KILITLI[1] and KILITLI[0] < row["end_s"])
        for row in on.diagnostics["voice_guard"]["trimmed"]
    )


# ------------------------------------------------------------------ wrong-content controls


def test_control_no_voice_in_the_gap_moves_nothing(monkeypatch):
    """The same geometry, but the sound before the pause is not a voice (loud aperiodic noise
    where she said the words): the words stay where whisper stamped them."""
    voiced = [run for run in FIXTURE["voiced_runs"] if not (KILITLI[0] <= run[0] < KILITLI[1])]
    pcm = _pcm(voiced=voiced, noise_spans=(KILITLI,))
    on = _analysis(monkeypatch, late_words=True, pcm=pcm)
    off = _analysis(monkeypatch, late_words=False, pcm=pcm)
    assert "late_words" not in on.diagnostics
    assert _word(on, "Kilitli")[0] == pytest.approx(13.96)
    assert _removed(on) == _removed(off)


def test_control_voice_under_the_stamps_moves_nothing(monkeypatch):
    """Quiet speech the silence spans missed (voicing under the stamps): never moved."""
    pcm = _pcm(voiced=[*FIXTURE["voiced_runs"], [14.0, 14.5]])
    on = _analysis(monkeypatch, late_words=True, pcm=pcm)
    assert "late_words" not in on.diagnostics
    assert _word(on, "Kilitli")[0] == pytest.approx(13.96)


def test_control_a_word_already_on_the_voice_moves_nothing(monkeypatch):
    """When an earlier word's stamp already reaches into the voice, that voice may be that
    word's: nothing moves."""
    words = copy.deepcopy(WORDS)
    [guclu] = [w for w in words if w["text"] == "güçlü."]
    guclu["end_s"] = 12.6
    on = _analysis(monkeypatch, late_words=True, words=words)
    assert "late_words" not in on.diagnostics
    assert _word(on, "Kilitli")[0] == pytest.approx(13.96)


# ------------------------------------------------------------------ the worker: label + caption

PROD_ON = (
    "spoken_list_labels_enabled",
    "phone_subtitled_spoken_text_enabled",
    "phone_subtitled_title_enabled",
    "phone_text_styling_enabled",
    "phone_subtitled_rendering_enabled",
)


def _brief() -> CreativeBrief:
    """The brief as the thread filed it (`creator_brief_binding` of the job)."""
    requirements = [
        {
            "id": f"r{i}",
            "kind": "text",
            "facts": {},
            "scope": "per_clip",
            "status": "open",
            "literal": text,
            "description": description,
        }
        for i, (text, description) in enumerate(
            zip(
                LABEL_TEXTS,
                (
                    "Show this label when the first lesson starts, as the first of four "
                    "successive lesson labels.",
                    "Show this label when the second lesson starts, replacing the preceding "
                    "lesson label.",
                    "Show this label when the third lesson starts, replacing the preceding "
                    "lesson label.",
                    "Show this label when the fourth lesson starts, replacing the preceding "
                    "lesson label.",
                ),
                strict=True,
            ),
            start=1,
        )
    ]
    requirements.append(
        {
            "id": "r5",
            "kind": "style",
            "facts": {"captions": "on", "caption_language": "tr"},
            "scope": "global",
            "status": "open",
            "literal": None,
            "description": "Turkish captions.",
        }
    )
    return CreativeBrief.model_validate({"version": 1, "requirements": requirements})


def _snapshot_payload(result: SpeechCleanupAnalysisResult, *, storage_path: str) -> dict:
    return {
        "schema_version": 1,
        "analysis_id": "dc2d7928-a48a-43b0-9108-332a274d4102",
        "engine_version": "preflight-v1-2026-09-05",
        "detector_version": result.detector_version,
        "source": {
            "kind": "embedded_spine",
            "media_identity": "c0",
            "storage_path": storage_path,
            "generation": "1",
            "window_start_s": 0.0,
            "window_end_s": DURATION_S,
            "source_policy_fingerprint": "c" * 64,
        },
        "analysis": result.to_payload(),
    }


def _faces(_path, anchors, **_kwargs):
    regions = [ProtectedRegion(max(0.0, at - 0.5), at + 0.5, FACE, "face") for at in anchors]
    return regions, {"attempted": len(anchors), "decoded": len(anchors), "detected": len(regions)}


def _render(
    monkeypatch, *, late_words: bool, caption_request: str = "tr", hinted=None
) -> tuple[dict, SpeechCleanupAnalysisResult]:
    """The phone Talking worker on the cleaned take, as the approved draft ran it.

    ``caption_request`` other than the spoken "tr" makes the worker transcribe the take again
    with that hint; ``hinted`` is what that pass hears."""
    apply_prod_profile(monkeypatch)
    clear_request_context()
    result = _analysis(monkeypatch, late_words=late_words)
    job, snapshot, _session, binding = _setup_subtitled(
        monkeypatch, duration_s=DURATION_S, transcript_words=hinted
    )
    _enable_beats(monkeypatch)  # the media lanes the list labels ride (on in prod)
    for name in PROD_ON:
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    monkeypatch.setattr(settings, "subtitled_caption_correction_enabled", False)
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", lambda *a, **k: None)
    monkeypatch.setattr(title_mod, "sample_face_regions", _faces)
    job.all_candidates.update(
        {
            "language": "tr",
            "creator_request": (
                "I list four lessons in this video. Add a label when each one starts: "
                '"1. Esneklik", "2. Kilitli sistemler", "3. Form", "4. Doğayı biliyordu". '
                "Turkish captions."
            ),
            "creator_strategy": {"edit_format": "subtitled", "audio_strategy": "original_audio"},
            "caption_language_request": caption_request,
            "landscape_fit": "fit",
        }
    )
    snapshot["speech_cleanup_contract"] = "required_v1"
    snapshot["_speech_cleanup_internal"] = {
        "preflight_snapshot": _snapshot_payload(result, storage_path=binding.proxy_path)
    }
    brief_binding = BriefBinding.create(uuid.uuid4(), _brief()).model_dump(mode="json")
    snapshot["creator_brief_binding"] = brief_binding
    job.assembly_plan["creator_brief_binding"] = copy.deepcopy(brief_binding)
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    [variant] = job.assembly_plan["variants"]
    assert variant["render_status"] == "awaiting_device", variant
    return variant, result


def _label(variant: dict, text: str) -> dict:
    [row] = [r for r in variant["list_label_placement"]["rows"] if r["text"] == text]
    return row


def _cue_with(variant: dict, word: str) -> dict:
    return next(cue for cue in variant["caption_cues"] if cue["text"].startswith(word))


@pytest.fixture(scope="module")
def renders():
    """Both worker runs, once per module."""
    out = {}
    for late_words in (False, True):
        with pytest.MonkeyPatch.context() as patch:
            out[late_words] = _render(patch, late_words=late_words)
    return out


def test_control_switch_off_the_worker_puts_label_two_where_prod_did(renders):
    variant, result = renders[False]
    row = _label(variant, "2. Kilitli sistemler")
    assert row["basis"] == "words"
    assert row["start_s"] == pytest.approx(PROD["label_r2"]["start_s"], abs=0.01)
    assert _cue_with(variant, "Kilitli")["start_s"] == pytest.approx(
        PROD["caption_cue"]["start_s"], abs=0.01
    )
    # 1.6 s after the words start, in the cut take's time.
    said_at = remap_time(KILITLI[0], _plan(result))
    assert row["start_s"] - said_at > 1.0


def test_label_two_and_its_caption_start_when_kilitli_is_said(renders):
    variant, result = renders[True]
    plan = _plan(result)
    said_at = remap_time(KILITLI[0], plan)  # the voice onset on the cut timeline
    row = _label(variant, "2. Kilitli sistemler")
    assert row["status"] == "placed" and row["basis"] == "words"
    assert row["start_s"] == pytest.approx(said_at, abs=0.15)
    assert _cue_with(variant, "Kilitli")["start_s"] == pytest.approx(said_at, abs=0.15)


def test_the_other_three_labels_stay_on_their_words(renders):
    """Labels 1, 3 and 4 sit on the same source moment as in prod; only the cut moved them."""
    off_variant, off_result = renders[False]
    on_variant, on_result = renders[True]
    for text in ("1. Esneklik", "3. Form", "4. Doğayı biliyordu"):
        off_row, on_row = _label(off_variant, text), _label(on_variant, text)
        assert on_row["status"] == "placed"
        source_off = _source_time(off_row["start_s"], _plan(off_result))
        source_on = _source_time(on_row["start_s"], _plan(on_result))
        assert source_on == pytest.approx(source_off, abs=0.01)


def _source_time(t: float, plan: CutPlan) -> float:
    """Inverse of `remap_time` on a keep-segment plan (cut time -> source time)."""
    elapsed = 0.0
    for lo, hi in plan.keep_segments:
        if t <= elapsed + (hi - lo) + 1e-9:
            return lo + (t - elapsed)
        elapsed += hi - lo
    raise AssertionError(t)


def test_removal_type_is_unchanged_for_the_new_cut(monkeypatch):
    """The new cut is an ordinary silence cut (receipts count it as a long pause)."""
    result = _analysis(monkeypatch, late_words=True)
    [new] = [r for r in result.cut_plan.removed if PAUSE[0] <= r.start_s < PAUSE[1]]
    assert new.reason == Removal(0.0, 1.0, "silence").reason
    assert result.public_receipt.category_counts.long_pauses == len(result.cut_plan.removed)


# ------------------------------------------------------------------ a second caption pass


def test_a_late_stamped_second_caption_pass_keeps_its_words_on_the_voice(monkeypatch):
    """Review of #2374: captions asked in another language than the one spoken come from a second
    whisper pass of the take. When that pass stamps "Kilitli sistemler" into the pause again, the
    pause the plan now cuts must not take those words out of the captions: they follow the
    analysis's move onto the voice. (The translation step is the provider's: replayed as is.)"""
    from types import SimpleNamespace

    from app.pipeline.transcribe import Word

    hinted = [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0) for w in WORDS
    ]
    monkeypatch.setattr(
        gb,
        "_ensure_requested_caption_language",
        lambda words, *, requested, **_k: SimpleNamespace(words=words, language=requested),
    )
    variant, result = _render(monkeypatch, late_words=True, caption_request="en", hinted=hinted)
    words = [w for cue in variant["caption_cues"] for w in cue.get("words") or []]
    texts = [w["text"].strip(".,") for w in words]
    assert "Kilitli" in texts and "sistemler" in texts
    said_at = remap_time(KILITLI[0], _plan(result))
    assert words[texts.index("Kilitli")]["start_s"] == pytest.approx(said_at, abs=0.15)
