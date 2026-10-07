"""A tapped choice answer on an item whose latest job FAILED must re-plan, not recover.

Prod thread 387D3D1C (2026-10-07): "Redo" after a failed render asked the order_basis
question; tapping "Use the order you added the clips" was treated as a rendered
follow-up edit, brief extraction threw, the catch swallowed it into
`request_extraction_failed`, and nothing was dispatched.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from app.config import settings
from app.kria import planner
from app.kria.brief import BriefUpdateBatchError
from app.models import Job
from app.tasks import kria_runtime
from tests.kria.test_planner_editor_target_miss import _wire_real

pytestmark = pytest.mark.asyncio

ANSWER = "Use the order you added the clips"


def _arm(monkeypatch, *, job_status, extractor_raises=False):  # noqa: ANN001, ANN202
    db, item, creator_id, runs, copilot = _wire_real(monkeypatch, render_status="ready")
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    inner = db.get.side_effect

    async def get(model, identifier, **kw):  # noqa: ANN001, ANN202
        job = await inner(model, identifier, **kw)
        if model is Job:
            job.status = job_status
        return job

    db.get.side_effect = get
    extractions: list[object] = []

    class Extractor:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, *_a, **_k):  # noqa: ANN002, ANN003
            extractions.append(1)
            if extractor_raises:
                raise BriefUpdateBatchError("change without brief version")
            return SimpleNamespace(brief_updates=[])

    monkeypatch.setattr(planner, "BriefExtractorAgent", Extractor)
    return db, item, creator_id, runs, extractions


async def _turn(db, item, creator_id, **kw):  # noqa: ANN001, ANN202
    return await planner.plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item._fields["id"],
        creator_id=creator_id,
        user_message=ANSWER,
        **kw,
    )


async def test_failed_job_item_replans_instead_of_extracting_a_followup(monkeypatch) -> None:
    db, item, creator_id, runs, extractions = _arm(
        monkeypatch, job_status="processing_failed", extractor_raises=True
    )
    result = await _turn(db, item, creator_id)
    assert extractions == []  # no follow-up extraction against a render that does not exist
    assert len(runs) == 1  # the Main Creator planned
    cov = result.brief_coverage or {}
    assert cov.get("reason") != "request_extraction_failed"


async def test_choice_answer_on_rendered_item_replans_without_extraction(monkeypatch) -> None:
    db, item, creator_id, runs, extractions = _arm(
        monkeypatch, job_status="done", extractor_raises=True
    )
    result = await _turn(db, item, creator_id, answers_clip_question=True)
    assert extractions == []
    assert len(runs) == 1
    assert (result.brief_coverage or {}).get("reason") != "request_extraction_failed"


async def test_real_followup_on_rendered_item_still_extracts(monkeypatch) -> None:
    db, item, creator_id, runs, extractions = _arm(monkeypatch, job_status="done")
    try:
        await _turn(db, item, creator_id)
    except AttributeError:
        pass  # the stubbed copilot is not under test; the extraction call is
    assert extractions  # genuine follow-up edit keeps the extract-first path


async def test_swallowed_extraction_error_is_logged_with_its_cause(monkeypatch) -> None:
    db, item, creator_id, _runs, extractions = _arm(
        monkeypatch, job_status="done", extractor_raises=True
    )
    with capture_logs() as logs:
        result = await _turn(db, item, creator_id)
    assert extractions
    assert (result.brief_coverage or {})["reason"] == "request_extraction_failed"
    cause = [e for e in logs if e["event"] == "kria_request_recovery_cause"]
    assert cause and cause[0]["error_type"] == "BriefUpdateBatchError"
    assert "change without brief version" in cause[0]["error"]


@pytest.mark.parametrize(
    ("payload", "flag", "expected"),
    [
        ({"choice_selection": {"question_id": "q", "option_key": "attachment_order"}}, True, True),
        ({"choice_selection": {"question_id": "q", "option_key": "x"}}, False, False),
        ({"clip_selection": {"a": 1}}, True, True),
        ({}, True, False),
    ],
)
def test_runtime_marks_answer_turns(monkeypatch, payload, flag, expected) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", flag)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", flag)
    assert kria_runtime._answers_clip_question(SimpleNamespace(payload=payload)) is expected
