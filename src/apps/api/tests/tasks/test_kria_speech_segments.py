"""KRI-282: timed speech segments are attached to analysed talking clips (best effort)."""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import speech_segments
from app.tasks import kria_clip_understanding as task

WORDS = [
    {"text": w, "start_s": i * 0.4, "end_s": i * 0.4 + 0.3}
    for i, w in enumerate("never rush a good espresso. It changed my whole morning.".split())
]


def _entry(**speech) -> dict:
    return {
        "gcs_path": "users/u/analysis-proxy-ios-1.mp4",
        "media_id": "m1",
        "kind": "video",
        "analysis": {"understanding": {"summary": "me", "speech": speech}},
    }


@pytest.fixture
def transcribed(monkeypatch):
    calls: list[str] = []

    def fake(path: str, **_kw):
        calls.append(path)
        return WORDS, "en"

    monkeypatch.setattr(speech_segments, "transcribe_stored_clip", fake)
    return calls


def test_talking_clip_gets_segments_and_language(transcribed) -> None:
    entry = _entry(has_speech=True, to_camera=True, transcript="never rush")
    out = task._with_speech_segments(entry)
    speech = out["analysis"]["understanding"]["speech"]
    assert [s["text"] for s in speech["segments"]] == [
        "never rush a good espresso.",
        "It changed my whole morning.",
    ]
    assert speech["language"] == "en" and speech["transcript"] == "never rush"
    assert transcribed == ["users/u/analysis-proxy-ios-1.mp4"]
    assert "segments" not in entry["analysis"]["understanding"]["speech"]  # input untouched


def test_clip_without_speech_images_and_already_segmented_clips_are_skipped(transcribed) -> None:
    quiet = _entry(has_speech=False)
    assert task._with_speech_segments(quiet) is quiet
    image = {**_entry(has_speech=True), "kind": "image"}
    assert task._with_speech_segments(image) is image
    done = _entry(has_speech=True, segments=[{"start_s": 0, "end_s": 2, "text": "hi there"}])
    assert task._with_speech_segments(done) is done
    assert transcribed == []


def test_kill_switch_off_transcribes_nothing(transcribed, monkeypatch) -> None:
    monkeypatch.setattr(settings, "speech_excerpt_montage_enabled", False)
    entry = _entry(has_speech=True)
    assert task._with_speech_segments(entry) is entry
    assert transcribed == []


def test_transcription_failure_never_fails_the_analysis(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("whisper down")

    monkeypatch.setattr(speech_segments, "transcribe_stored_clip", boom)
    entry = _entry(has_speech=True)
    assert task._with_speech_segments(entry) is entry
