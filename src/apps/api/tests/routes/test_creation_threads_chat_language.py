"""A thread response writes render notes in the chat language (_job_projection_in_chat_language)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.routes.creation_threads as threads_routes
import app.routes.generative_jobs as gj

RECEIPT = {
    "version": 1,
    "matcher": "agent",
    "placed": [],
    "unplaced": [{"media_id": "u", "label": "a.jpg", "reason": "no_spoken_match"}],
}


@pytest.fixture
def job(monkeypatch):
    monkeypatch.setattr(
        gj,
        "_variants_for_response",
        lambda _job: [{"variant_id": "v1", "phone_overlay_receipt": RECEIPT}],
    )
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="ready",
        current_phase=None,
        failure_reason=None,
        error_detail=None,
        assembly_plan={},
    )


def _notes(thread, job) -> list[str]:
    projected = threads_routes._job_projection_in_chat_language(thread, job, None)
    return projected["render_notes"]


def test_a_turkish_chat_gets_turkish_render_notes(job):
    notes = _notes(SimpleNamespace(state={"reply_language": "tr"}), job)
    assert notes[0] == "1 görselden 0 tanesi kart olarak gösterildi"
    assert notes[1] == '"a.jpg" için konuşmanda uygun bir an bulamadım'


def test_an_english_or_unknown_chat_keeps_english_render_notes(job):
    for state in ({"reply_language": "en"}, {}):
        notes = _notes(SimpleNamespace(state=state), job)
        assert notes[0] == "Showed 0 of 1 Visuals as cards"


def test_no_job_is_still_no_projection():
    assert (
        threads_routes._job_projection_in_chat_language(SimpleNamespace(state={}), None, None)
        is None
    )


def test_the_language_does_not_leak_into_the_next_projection(job):
    _notes(SimpleNamespace(state={"reply_language": "tr"}), job)
    assert _notes(SimpleNamespace(state={}), job)[0] == "Showed 0 of 1 Visuals as cards"
