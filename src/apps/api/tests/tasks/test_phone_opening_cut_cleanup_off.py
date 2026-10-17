"""KRI-704: "start when I say 'Deniz'im'" cuts the take even with speech cleanup off.

Tigre night T1: a 10 s take that is silent until "Deniz'im, neredesin?" at 4.3 s, no pauses or
fillers, so the speech check finished `checked_no_change`, no cleanup card was shown and the job
ran `off_v1`. The KRI-628 opening cut used to need a cleanup plan and said `cleanup_off`.

End to end through `_run_phone_subtitled_job`: the brief's own words -> the checked preflight
words (never a second Whisper call) -> a single keep segment [line, end) on the compiled
recipe, captions on the cut timeline, and no cleanup outcome claimed.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisInput, analyze_speech_cleanup
from app.pipeline.transcribe import Word
from app.services.clip_speech import SilenceDetectionResult
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

DURATION = 10.0
R1 = "Baştaki sessiz kısmı kes, “Deniz'im” dendiği anda başlasın"


def _t1_words() -> list[Word]:
    spoken = [
        ("Deniz'im,", 4.3, 5.0),
        ("neredesin?", 5.0, 6.0),
        ("Buradayım", 6.4, 7.2),
        ("abi.", 7.2, 7.8),
    ]
    return [Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in spoken]


def _checked_no_change_snapshot(storage_path: str) -> dict:
    fingerprint = "a" * 64
    analysis = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint=fingerprint,
            local_media_path="preflight.wav",
            duration_s=DURATION,
            source_window_start_s=0.0,
            source_window_end_s=DURATION,
            mixed_gap_mode="off",
            over_budget_policy="clamp",
        ),
        transcribe_fn=lambda *_a, **_k: SimpleNamespace(
            words=_t1_words(), language="tr", low_confidence=False
        ),
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(spans=[], status="ok"),
    )
    return {
        "schema_version": 1,
        "analysis_id": str(uuid.uuid4()),
        "engine_version": "preflight-v1-2026-09-05",
        "detector_version": analysis.detector_version,
        "source": {
            "kind": "embedded_spine",
            "media_identity": "c0",
            "storage_path": storage_path,
            "generation": "1",
            "window_start_s": 0.0,
            "window_end_s": DURATION,
            "source_policy_fingerprint": fingerprint,
        },
        "analysis": analysis.to_payload(),
    }


def _brief() -> dict:
    brief = CreativeBrief(
        version=1,
        requirements=[BriefRequirement(id="r1", kind="select", scope="global", description=R1)],
    )
    return BriefBinding.create(uuid.uuid4(), brief).model_dump(mode="json")


def _off_job(monkeypatch, *, with_snapshot: bool):
    job, snapshot, _session, binding = _setup_subtitled(monkeypatch, duration_s=DURATION)
    snapshot["speech_cleanup_contract"] = "off_v1"
    snapshot["creator_brief_binding"] = _brief()
    if with_snapshot:
        snapshot["_speech_cleanup_internal"] = {
            "preflight_snapshot": _checked_no_change_snapshot(binding.proxy_path)
        }
    for key in ("creator_brief_binding", "speech_cleanup_contract", "_speech_cleanup_internal"):
        if key in snapshot:
            job.assembly_plan[key] = copy.deepcopy(snapshot[key])
    return job, snapshot


def test_a_quoted_start_cuts_the_take_with_cleanup_off(monkeypatch):
    job, snapshot = _off_job(monkeypatch, with_snapshot=True)

    import app.pipeline.transcribe as transcribe_mod

    def _explode(*_a, **_k):
        raise AssertionError("the checked preflight words ground the cut, never a new Whisper run")

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper_cached", _explode, raising=False)

    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    cut = variant["opening_cut"]
    assert (cut["status"], cut["quote"]) == ("applied", "Deniz'im")
    # It opens a hair before the first word, never on the silent head.
    assert 4.1 <= cut["start_s"] < 4.3

    recipe = device_status(job, "subtitled").request.recipe
    video = next(t for t in recipe.tracks if t.kind == "video")
    assert video.clips[0].source_start == pytest.approx(cut["start_s"])
    assert recipe.duration == pytest.approx(DURATION - cut["start_s"], abs=0.01)
    # Captions follow the cut and keep every word (no cleanup, so no filler filtering).
    assert variant["caption_cues"][0]["start_s"] == pytest.approx(4.3 - cut["start_s"], abs=0.05)
    assert variant["caption_cues"][0]["text"].startswith("Deniz'im")
    # Only the opening cut ran: never reported as a speech-cleanup outcome.
    assert "silence_cut_outcome" not in variant
    assert "_speech_cleanup_outcome_context" not in variant
    [receipt] = build_receipts(
        [BriefRequirement(id="r1", kind="select", scope="global", description=R1)],
        plan_facts_from_phone_variant(variant),
        include_unchecked=True,
    )
    assert receipt.status == "met", receipt


def test_without_the_checked_words_the_miss_stays_honest(monkeypatch):
    job, snapshot = _off_job(monkeypatch, with_snapshot=False)

    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    cut = job.assembly_plan["variants"][0]["opening_cut"]
    assert (cut["status"], cut["reason"]) == ("not_applied", "cleanup_off")
