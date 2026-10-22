"""Original boat request through brief -> worker -> recipe -> captions -> receipts.

Source expectations come from the recorded T1 word timings; transport is replayed.
This verifies a phone recipe, not a device export or a new live transcription.
"""

from __future__ import annotations

import copy
import uuid

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.pipeline.transcribe import Word
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_kri707_talking_spoken_text import (
    T1_REQUIREMENTS,
    _faces,
    _run,
    _t1_brief,
    _t1_job,
)
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

# T1's brief as Main Creator v70 records it (KRI-667): the phrase cut and the engine-noise
# ask carry structured facts. Prod's stored v1 rows carry none, so they authorize no cut.
_V70_FACTS = {
    "r2": {"speech_remove": {"quote": "Neredesin", "occurrence": 2}},
    "r5": {"noise_reduction": "engine"},
}


def _v70_job(monkeypatch):
    brief = _t1_brief(
        *(
            req.model_copy(update={"facts": {**req.facts, **_V70_FACTS[req.id]}})
            if req.id in _V70_FACTS
            else req
            for req in T1_REQUIREMENTS
        )
    )
    return _t1_job(monkeypatch, brief=brief)


def _assert_only_second_removed(recipe):
    video = next(t for t in recipe.tracks if t.id == "subtitled")
    keeps = [(c.source_start, c.source_start + c.source_duration) for c in video.clips]
    assert any(a <= 6.32 and b >= 7.34 for a, b in keeps), "first occurrence lost"
    assert not any(max(a, 9.22) + 1e-6 < min(b, 9.70) for a, b in keeps), "second still audible"
    assert any(a <= 8.52 and b >= 8.94 for a, b in keeps), "off-camera answer lost"
    assert any(a <= 13.06 and b >= 14.76 for a, b in keeps), "following sentence clipped"


@pytest.mark.parametrize("preflight", [True, False])
def test_original_request_removes_second_only_and_remaps_text(monkeypatch, preflight):
    _faces(monkeypatch)
    job, snapshot = _v70_job(monkeypatch)
    if not preflight:
        snapshot.pop("_speech_cleanup_internal")
        job.assembly_plan.pop("_speech_cleanup_internal")
    variant = _run(job, snapshot)
    recipe = device_status(job, "subtitled").request.recipe
    _assert_only_second_removed(recipe)
    [cut] = variant["explicit_speech_cuts"]
    assert cut["status"] == "applied"
    assert cut["source_span"] == {"start_s": 9.22, "end_s": 9.70}
    words = [w for cue in variant["caption_cues"] for w in cue.get("words", [])]
    assert sum(w["text"].casefold().strip("?.,!") == "neredesin" for w in words) == 1
    [atakan] = [r for r in variant["text_elements"] if r["text"] == "ATAKAN"]
    assert atakan["start_s"] == pytest.approx(
        13.78 - variant["opening_cut"]["start_s"] - (9.70 - 9.22), abs=0.01
    )
    brief = BriefBinding.model_validate(snapshot["creator_brief_binding"]).resolve()
    req = next(r for r in brief.requirements if r.id == "r2")
    [receipt] = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    assert receipt.status == "met"
    assert "silence_cut_outcome" not in variant
    # Wrong-content control: a first-occurrence cut must fail even though length matches.
    wrong = recipe.model_copy(deep=True)
    video = next(t for t in wrong.tracks if t.id == "subtitled")
    second_clip = video.clips[-1]
    video.clips[0] = video.clips[0].model_copy(
        update={"source_duration": 6.32 - video.clips[0].source_start}
    )
    video.clips[-1] = second_clip.model_copy(update={"source_start": 6.80})
    with pytest.raises(AssertionError):
        _assert_only_second_removed(wrong)


@pytest.mark.parametrize(
    ("quote", "occurrence"),
    [
        ("hello", 3),
        ("absent", 2),
    ],
)
def test_unresolved_request_keeps_source_and_reports_miss(monkeypatch, quote, occurrence):
    words = [
        Word(text="hello", start_s=1, end_s=1.5, confidence=1),
        Word(text="hello", start_s=3, end_s=3.5, confidence=1),
    ]
    job, snapshot, _, _ = _setup_subtitled(monkeypatch, transcript_words=words)
    req = BriefRequirement(
        id="r1",
        kind="timing",
        scope="global",
        description=f'Remove occurrence {occurrence} of "{quote}"',
        facts={"speech_remove": {"quote": quote, "occurrence": occurrence}},
    )
    brief = CreativeBrief(version=1, requirements=[req])
    snapshot["creator_brief_binding"] = BriefBinding.create(uuid.uuid4(), brief).model_dump(
        mode="json"
    )
    job.assembly_plan.update(copy.deepcopy(snapshot))
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    variant = job.assembly_plan["variants"][0]
    assert variant["explicit_speech_cuts"][0]["status"] == "not_applied"
    assert device_status(job, "subtitled").request.recipe.duration == pytest.approx(10)
    [receipt] = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    assert receipt.status == "not_possible"


def test_noise_replacement_uses_same_cut_segments_and_pins_private_receipt(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from app.services import phone_noise_reduction as noise

    _faces(monkeypatch)
    job, snapshot = _v70_job(monkeypatch)
    monkeypatch.setattr(noise.settings, "phone_engine_noise_reduction_enabled", True)
    monkeypatch.setattr(noise.settings, "phone_engine_noise_model_path", "/configured/model.rnnn")
    monkeypatch.setattr(noise.settings, "phone_engine_noise_model_sha256", "a" * 64)
    monkeypatch.setattr(
        noise.settings,
        "phone_render_verified_features",
        [*noise.settings.phone_render_verified_features, "processedAudio"],
    )

    def process(_source, output, **kwargs):
        assert kwargs["expected_sha256"] == "a" * 64
        Path(output).write_bytes(b"test-audio-transport")
        return SimpleNamespace(path=Path(output), duration_s=28, method="arnndn")

    monkeypatch.setattr(noise, "reduce_stationary_noise", process)
    monkeypatch.setattr(
        noise,
        "upload_local_file_immutable",
        lambda path, *_: SimpleNamespace(generation="123", size=Path(path).stat().st_size),
    )
    variant = _run(job, snapshot)
    recipe = device_status(job, "subtitled").request.recipe
    _assert_only_second_removed(recipe)
    video = next(t for t in recipe.tracks if t.id == "subtitled")
    audio = next(t for t in recipe.tracks if t.id == "processed-source-audio")
    assert [(c.source_start, c.source_duration, c.timeline_start) for c in audio.clips] == [
        (c.source_start, c.source_duration, c.timeline_start) for c in video.clips
    ]
    assert all(c.volume == 0 for c in video.clips)
    assert all(c.volume == 1 for c in audio.clips)
    assert audio.clips[0].audio_fade_out > 0
    assert audio.clips[1].audio_fade_in > 0
    assert variant["engine_noise_reduction"]["status"] == "applied"
    assert job.assembly_plan["_phone_processed_audio_v1"]["subtitled"]
    assert "generative-jobs/" not in recipe.model_dump_json()


def test_failed_noise_processing_keeps_original_and_word_cut(monkeypatch):
    from app.services import phone_noise_reduction as noise

    _faces(monkeypatch)
    job, snapshot = _v70_job(monkeypatch)
    monkeypatch.setattr(noise, "engine_noise_reduction_available", lambda: True)

    def fail(*args, **kwargs):
        raise RuntimeError("processor failed")

    monkeypatch.setattr(noise, "reduce_stationary_noise", fail)
    variant = _run(job, snapshot)
    recipe = device_status(job, "subtitled").request.recipe
    _assert_only_second_removed(recipe)
    assert not any(t.id == "processed-source-audio" for t in recipe.tracks)
    assert variant["engine_noise_reduction"]["status"] == "not_applied"
    assert "_phone_processed_audio_v1" not in job.assembly_plan
