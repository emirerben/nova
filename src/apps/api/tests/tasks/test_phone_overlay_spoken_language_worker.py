"""`_run_phone_subtitled_job` tells Visuals grounding the language the creator speaks.

Generated images are titled in whatever language the creator typed, so the placement agent needs
the DETECTED spoken language to match across the two. When detection fell back to the job
language, nothing is claimed ("" = unknown); it is never the job-language fallback.
"""

from __future__ import annotations

import pytest

import app.pipeline.transcribe as transcribe_mod
import app.services.phone_overlay_grounding as phone_overlay_grounding_mod
import app.services.phone_visuals as phone_visuals_mod
from app.pipeline.transcribe import Transcript
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _enable_beats,
    _grounding_mock,
    _make_fake_bind,
    _setup_subtitled,
    _words,
)


def _run(monkeypatch, language: str):
    words = _words(("Merhaba", 0.0, 0.5), ("kahve", 0.5, 1.0))
    overlays = _grounding_mock([])
    job, *_ = _setup_subtitled(monkeypatch, transcript_words=words)
    monkeypatch.setattr(
        transcribe_mod,
        "transcribe_whisper_cached",
        lambda *a, **k: Transcript(words=words, language=language),
        raising=False,
    )
    _enable_beats(monkeypatch)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    monkeypatch.setattr(phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", overlays)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    return overlays


@pytest.mark.parametrize("detected", ["tr", "en"])
def test_the_detected_spoken_language_reaches_overlay_grounding(monkeypatch, detected):
    overlays = _run(monkeypatch, detected)
    assert overlays.call_args.kwargs["spoken_language"] == detected


def test_an_undetected_language_is_passed_as_unknown_not_as_the_job_language(monkeypatch):
    # The job's own language is "en" (see `_setup_subtitled`); an empty detection falls back to it.
    overlays = _run(monkeypatch, "")
    assert overlays.call_args.kwargs["spoken_language"] == ""
