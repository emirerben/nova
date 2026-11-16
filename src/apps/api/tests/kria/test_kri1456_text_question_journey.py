"""KRI-1456: when the text composer asks, the creator is asked; a failure shows its real reason.

Original incident: prod thread 6db7ecd4 (2026-11-14), the lyrics montage in
``tests/fixtures/kria_replay/kri1445_lyrics_montage``. The composer answered with a question;
the worker turned it into a failed job and the chat said "I couldn't prepare the text ... Try
generating this edit again" while the turn's recovery was ``ask_user``. The creator never saw the
question, and the sentence offered a retry the card did not.

The real phone worker, the real ``_fail_job`` and the real chat observer run here against the
scratch Postgres; only the composer's model answer (the fixture's authored cassettes) is
replaced. The replay scorer itself runs in ``test_scorecard_is_green``.

Ways this channel could fail, written down before the change:
1. The composer's question never reaches the thread: the card shows the generic "couldn't
   prepare the text" sentence (the incident).
2. A question that only concerns layout or style blocks a video whose every typed line is on
   screen (the replay scorer's check 6 covers the worker; this file covers the thread).
3. The question is saved but the turn also offers Retry, so the creator re-runs into the same
   question.
4. Receipts judged against an empty plan ride beside the question and mark drawn text missing.
5. A failure that asks nothing leaks an exception's text ("Text composition is incomplete").
6. The sentence invites a retry while the card's recovery is ``ask_user`` (a native render with
   no program to run again).
7. A stale creator reason from an earlier failure is shown for a later failure of the same job.
Native rendering, a live composer and production rollout are separate evidence.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import sync_session
from app.models import CreatorAgentExecution, Job
from app.tasks import generative_build as gb
from app.tasks import kria_runtime as rt
from app.tasks.content_plan_build import (
    CREATOR_FAILURE_DETAIL_FIELD,
    creator_failure_detail,
    job_failure_message,
)
from tests.kria.test_render_card_kick_postgres import _failure_cards, _seed_dispatched_render

REPO = Path(__file__).resolve().parents[5]
SCORER = REPO / "scripts" / "kria" / "score_kri1445_lyrics_montage.py"


def _load_scorer():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("score_kri1445_lyrics_montage", SCORER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


score = _load_scorer()


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


def test_scorecard_is_green() -> None:
    """The offline replay of the incident and its neighbours: every check VERIFIED."""
    run = subprocess.run(  # noqa: S603
        [sys.executable, str(SCORER)],
        cwd=SCORER.parents[2] / "src" / "apps" / "api",
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
    summary = json.loads(run.stdout.strip().splitlines()[-1])
    assert summary == {"VERIFIED": summary["VERIFIED"], "NOT VERIFIED": 0, "INCONCLUSIVE": 0}


def _fail_through_the_worker(name: str, *, native_device_only: bool = False) -> tuple:
    """Run scenario ``name`` through the real worker, persist its failure the way the dispatcher
    does, and let the real observer post the card. Returns (seeded, card, job row fields)."""
    seeded = _seed_dispatched_render(job_status="processing", assembly_plan={})
    fx = score.load_fixture()
    scenario = fx["scenarios"][name]
    output = scenario.get("output") or fx["cassettes"][scenario["cassette"]]["output"]
    with score._prod_settings():
        job = score.build_job(fx, scenario["extra_requirements"], thread_id=seeded.thread_id)
        seen = score.run_worker(job, composer_raw=json.dumps(output))
    exc = seen["worker_exc"]
    failure_reason, kind = gb._classify_phone_plan_failure(exc)
    detail, creator_detail = gb._phone_failure_details(
        exc, failure_reason=failure_reason, kind=kind, decline=False
    )
    plan = {**seen["assembly_plan"], **({"native_device_only": True} if native_device_only else {})}
    with sync_session() as db:
        db.get(Job, seeded.job_id).assembly_plan = plan
        db.commit()
    assert gb._fail_job(
        str(seeded.job_id), detail, failure_reason=failure_reason, creator_detail=creator_detail
    )
    assert rt.observe_kria_job.run(str(seeded.job_id)) == {"settled": 1}
    [card] = _failure_cards(seeded.thread_id)
    with sync_session() as db:
        row = db.get(Job, seeded.job_id)
        execution = db.execute(
            select(CreatorAgentExecution).where(CreatorAgentExecution.id == seeded.execution_id)
        ).scalar_one()
        fields = SimpleNamespace(
            failure_reason=row.failure_reason,
            error_detail=row.error_detail,
            plan=dict(row.assembly_plan),
            execution_error=dict(execution.error),
            creator_detail=creator_failure_detail(row),
        )
    return card, fields, fx, scenario


def test_the_composers_question_is_what_the_creator_reads() -> None:
    card, fields, _fx, scenario = _fail_through_the_worker("asks_text_missing")
    question = scenario["output"]["reply"]

    assert card.content == question
    assert "couldn't prepare the text" not in card.content
    assert card.payload["recovery"] == "ask_user"
    assert fields.execution_error["retryable"] is False
    assert card.payload["code"] == fields.failure_reason == "phone_plan_unsupported"
    # The default receipts would mark the drawn lyric "isn't in this draft" beside the question.
    assert "requirement_receipts" not in card.payload
    assert fields.plan["request_recovery"]["message"] == question
    assert fields.error_detail == question


def test_a_rejection_with_no_question_shows_a_reason_that_offers_no_retry() -> None:
    card, fields, _fx, _scenario = _fail_through_the_worker(
        "composer_rejects", native_device_only=True
    )

    assert fields.failure_reason == "creation_text_failed"
    assert card.payload["recovery"] == "ask_user"
    assert fields.execution_error["retryable"] is False
    assert card.content.startswith("I couldn't prepare the text while keeping your approved")
    assert "Try generating" not in card.content
    assert "Text composition" not in card.content
    assert "I can't do that with the text tool" not in card.content
    assert "ask for a new version" in card.content
    # Nothing was written for the creator, so nothing is stamped and error_detail stays the
    # code's own sentence, never the exception's text.
    assert fields.creator_detail is None
    assert CREATOR_FAILURE_DETAIL_FIELD not in fields.plan
    assert "Text composition" not in fields.error_detail


def test_a_creator_reason_belongs_to_one_failure_code() -> None:
    row = SimpleNamespace(
        failure_reason="phone_plan_failed",
        assembly_plan={
            CREATOR_FAILURE_DETAIL_FIELD: {
                "failure_reason": "creation_text_failed",
                "text": "Which layout?",
            }
        },
    )
    assert creator_failure_detail(row) is None
    assert job_failure_message("phone_plan_failed", creator_failure_detail(row)).startswith(
        "Something broke while planning"
    )
    row.failure_reason = "creation_text_failed"
    assert creator_failure_detail(row) == "Which layout?"
    assert job_failure_message("creation_text_failed", creator_failure_detail(row)) == (
        "Which layout?"
    )
