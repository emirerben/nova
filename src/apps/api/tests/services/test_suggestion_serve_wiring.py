"""The serve-side filler guard: no card on a row the preflight scheduler is about to redo.

``suggestion_serve.scheduler_will_redo`` is the read-only twin of
``speech_cleanup_preflight._redo_for_misheard_language`` (which resets the row it judges, so the
serve path cannot call it). Two copies of one rule drift, so this runs both over the same rows and
requires the same answer. The DB-backed fixtures for the whole serve wiring are in
``test_suggestion_serve_wiring_postgres.py``.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models import PlanItem, SpeechCleanupAnalysis
from app.services import speech_cleanup_preflight as preflight
from app.services import suggestion_serve as serve
from app.tasks import autoplace

PATH = "uploads/u1/clip.mp4"
ENGLISH = "so today we are going to make the best matcha latte you have ever had"
TURKISH = "bugün dünyanın en iyi matcha latte tarifini birlikte yapacağız hadi başlayalım"


def _item(transcript: str | None) -> PlanItem:
    analysis: dict = {"analysis_version": autoplace.ANALYSIS_VERSION}
    if transcript is not None:
        analysis["understanding"] = {"subject": "a cafe", "speech": {"transcript": transcript}}
    return PlanItem(
        id=uuid.uuid4(),
        clip_assignments=[
            {
                "media_id": "m1",
                "kind": "video",
                "gcs_path": PATH,
                "generation": "1",
                "storage_generation": "1",
                "analysis": analysis,
            }
        ],
    )


def _row(status, decision, payload, **extra) -> SpeechCleanupAnalysis:  # noqa: ANN003
    return SpeechCleanupAnalysis(
        id=uuid.uuid4(),
        status=status,
        decision=decision,
        analysis_payload=payload,
        engine_version=preflight.SPEECH_CLEANUP_ENGINE_VERSION,
        **extra,
    )


def _source() -> SimpleNamespace:
    return SimpleNamespace(
        source_kind="clip",
        media_id="m1",
        storage_path=PATH,
        generation="1",
        window_start_s=0.0,
        window_end_s=8.0,
        source_policy_fingerprint="fp",
    )


STATUSES = ("ready", "no_findings", "queued", "failed")
DECISIONS = (None, "clean")
PAYLOADS = (
    {"language": "en", "diagnostics": {}},
    {"language": "tr", "diagnostics": {}},
    {"language": "tr", "diagnostics": {"language_crosscheck": {"kept": "tr"}}},
    {"language": "tr"},
    None,
    {"diagnostics": {}},
)
TRANSCRIPTS = (ENGLISH, TURKISH, None)


@pytest.mark.parametrize(
    ("status", "decision", "payload", "transcript"),
    list(itertools.product(STATUSES, DECISIONS, PAYLOADS, TRANSCRIPTS)),
)
def test_the_read_only_check_agrees_with_the_scheduler_on_every_row(
    status, decision, payload, transcript
):
    item, row, source = _item(transcript), _row(status, decision, payload), _source()
    predicted = serve.scheduler_will_redo(row, item, source)
    # The scheduler's own check mutates the row and the item when it fires.
    actual = preflight._redo_for_misheard_language(row, item, source, now=datetime.now(UTC))
    assert predicted is actual


def test_a_row_the_scheduler_never_revokes_is_left_alone_even_when_the_language_disagrees():
    row = _row("ready", "keep_original", {"language": "tr", "diagnostics": {}})
    assert serve.scheduler_will_redo(row, _item(ENGLISH), _source()) is False


def test_the_check_is_not_vacuous():
    row = _row("ready", None, {"language": "tr", "diagnostics": {}})
    assert serve.scheduler_will_redo(row, _item(ENGLISH), _source()) is True


def test_a_row_from_another_engine_version_is_the_schedulers_to_replace():
    row = _row("ready", None, {"language": "en", "diagnostics": {}})
    row.engine_version = "preflight-v0-old"
    assert serve.scheduler_will_redo(row, _item(ENGLISH), _source()) is True
