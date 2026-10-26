"""KRI-898: a freeze is planned only for an app build that can play it.

Merope café M2 (KRI-775/828 fixture): "Freeze the frame for one second on Atakan's bite".
KRI-828 renders the hold as a main-track ``hold_duration`` that only app protocol 16+ decodes.
The draft (Celery), the pre-approval check and the render (Celery) used to plan the hold with
no app build in hand, so a protocol-15 phone approved a held recipe and then got 426
``client_update_required`` for the WHOLE video from ``device_render``.

This replays the approved M2 draft through the real gate, draft check, phone montage
preflight, phone worker, guided compiler, phone recipe compiler, rollout validator and the
``device_render`` status route, at the app protocol stamped when the creator sent the message
(draft) and when they approved (render).

Ways this can fail (written before the fix), each pinned below:
1. A protocol-15 approval still gets a hold recipe, so ``device_render`` 426s the whole video.
2. A protocol-15 draft still asks "which frame should hold?" for a hold it can never play.
3. A protocol-15 draft leaves the freeze off the draft check, so approval shows no "Couldn't".
4. The preflight plans a hold for protocol 15, so its verdicts judge a plan the render won't
   build.
5. A protocol-16 creator loses the freeze (stamp dropped somewhere): KRI-828 regression.
6. A missing stamp (legacy in-flight job, web approval) plans a hold or crashes; it must plan
   none and keep the "Couldn't" receipt.
7. Mixed devices: drafted on 16, approved on 15 -> no hold, "Couldn't", the video renders.
8. A held recipe opened on an older phone still 426s (that recipe really holds a frame).

Set KRI898_KEEP_DIR to keep the evidence: ``report.json`` (one row per protocol case) plus
each case's recipe JSON.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.kria.brief_checks import requirements_to_check_at_draft
from app.kria.device_render import make_device_request
from app.main import app
from app.pipeline import unified_montage
from app.routes import device_render as device_routes
from app.services.choice_questions import answered_brief
from app.services.device_render import pin_device_request
from app.services.phone_montage_preflight import montage_blockers
from app.services.phone_rollout import recipe_has_freeze_frame
from tests.kria.test_kri775_merope_m2_replay import (
    CREATOR_ID,
    RENDERS,
    gate_media,
    gate_strategy,
    m2_brief_with_moment_ask,
    m2_visual,
    prod_flags,
    run_worker,
)
from tests.kria.test_kri828_freeze_frame_m2 import (
    _phone_recipe,
    _receipt,
    _verify_freeze,
    gate_answering,
)

KEEP_DIR = os.environ.get("KRI898_KEEP_DIR")
_REPORT: dict[str, dict] = {}

OLD_APP = 15  # one below `freeze_frame_min_client_protocol`
FREEZE_APP = 16


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)
    _verify_freeze(monkeypatch)  # the device-parity gate is ON: only the app build decides


@pytest.fixture(scope="module", autouse=True)
def _write_report():  # noqa: ANN202
    yield
    if KEEP_DIR and _REPORT:
        directory = Path(KEEP_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.json").write_text(json.dumps(_REPORT, indent=2, default=str) + "\n")


def _keep(name: str, payload: object) -> None:
    if KEEP_DIR:
        directory = Path(KEEP_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps(payload, indent=2, default=str) + "\n")


def _device_status(recipe, protocol: int | None, monkeypatch: pytest.MonkeyPatch) -> int:  # noqa: ANN001
    """GET /me/jobs/{id}/device-render through the ASGI app, as the phone polls it."""
    user = SimpleNamespace(id=uuid.uuid4())
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        status="processing",
        content_plan_item_id=None,
        assembly_plan={"variants": [{"variant_id": "first", "render_generation_id": "approved"}]},
    )
    request = make_device_request(job_id=job.id, variant_id="first", revision=1, recipe=recipe)
    pin_device_request(job, request, base_generation="approved")
    db = AsyncMock()
    db.add = MagicMock()
    db.get.return_value = job
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    monkeypatch.setattr(device_routes.limiter, "enabled", False)
    monkeypatch.setattr(device_routes, "_owned_job", AsyncMock(return_value=job))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    headers = {"X-Kria-Client-Protocol": str(protocol)} if protocol is not None else {}
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                f"/me/jobs/{job.id}/device-render?variant_id=first", headers=headers
            )
    finally:
        app.dependency_overrides.clear()
    if response.status_code == 426:
        assert response.json()["detail"]["feature"] == "freeze_frame", response.json()
    return response.status_code


async def _replay(
    monkeypatch: pytest.MonkeyPatch, *, drafted_on: int | None, approved_on: int | None
):  # noqa: ANN202
    """The M2 draft sent from ``drafted_on`` and approved from ``approved_on``."""
    render = RENDERS[0]
    brief = m2_brief_with_moment_ask(render)
    result, asked = await gate_answering(
        monkeypatch, render, brief=brief, client_protocol=drafted_on
    )
    strategy, media = gate_strategy(result), gate_media(result)
    answered = answered_brief(brief, strategy)

    # The draft check the approval card is built from (`_complete_draft_turn`).
    checked = {
        req.id
        for req in requirements_to_check_at_draft(
            answered.live(),
            creator_id=CREATOR_ID,
            strategy=strategy,
            item_edit_format="montage",
            clip_paths=media.get("clip_paths") or (),
            client_protocol=drafted_on,
        )
    }

    # The pre-approval phone plan (KRI-775) and the render plan, spied on the same planner.
    calls: list[dict] = []
    real = unified_montage.plan_unified_montage

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        plan = real(*args, **kwargs)
        calls.append({"freeze_frames": kwargs.get("freeze_frames"), "plan": plan})
        return plan

    monkeypatch.setattr(unified_montage, "plan_unified_montage", spy)
    blockers = montage_blockers(
        creator_id=CREATOR_ID,
        edit_format="montage",
        brief=answered,
        strategy=strategy,
        media_snapshot=media,
        visuals=[m2_visual()],
        client_protocol=drafted_on,
    )
    assert blockers == [], [b.reason for b in blockers]
    assert len(calls) == 1, "the preflight must plan the draft"
    preflight_freeze = calls[0]["freeze_frames"]

    outcome = run_worker(
        monkeypatch,
        render,
        brief=answered,
        media=media,
        strategy=strategy,
        client_protocol=approved_on,
    )
    assert outcome.refusal is None, outcome.refusal
    assert len(calls) == 2, "the worker must plan the approved draft"
    _execution, recipe = _phone_recipe(monkeypatch, calls[1]["plan"])
    holds = [
        round(float(clip.hold_duration), 3)
        for track in recipe.tracks
        for clip in track.clips
        if track.kind == "video" and (clip.hold_duration or 0) > 1 / recipe.frame_rate + 1e-6
    ]
    frame_questions = [a for a in asked if a["question"].get("kind") == "freeze_frame"]
    return SimpleNamespace(
        asked=frame_questions,
        checked=checked,
        preflight_freeze=preflight_freeze,
        render_freeze=calls[1]["freeze_frames"],
        recipe=recipe,
        holds=holds,
        freeze=_receipt(outcome, "r3"),
    )


@pytest.mark.parametrize(
    ("drafted_on", "approved_on"),
    [(OLD_APP, OLD_APP), (None, None)],
    ids=["protocol-15", "no-stamp"],
)
async def test_an_app_that_cannot_play_a_freeze_gets_the_whole_video_and_an_honest_couldnt(
    monkeypatch: pytest.MonkeyPatch, drafted_on: int | None, approved_on: int | None
) -> None:
    case = await _replay(monkeypatch, drafted_on=drafted_on, approved_on=approved_on)

    # 2. the gate never asks which frame should hold
    assert case.asked == [], case.asked
    # 3. the freeze stays on the draft check, so its "Couldn't" is on the approval card
    assert "r3" in case.checked, case.checked
    # 4. the preflight judges the plan the render will build: no hold
    assert case.preflight_freeze is False
    # 1, 6. the render plans no hold and the recipe needs nothing the app can't decode
    assert case.render_freeze is False
    assert case.holds == []
    assert "freezeFrame" not in case.recipe.required_capabilities
    assert not recipe_has_freeze_frame(case.recipe)
    # ... the receipt is the honest KRI-777 "Couldn't" ...
    assert case.freeze["status"] == "not_possible", case.freeze
    assert "freez" in case.freeze["reason"].lower(), case.freeze
    # ... and the whole video reaches the phone that approved it.
    status = _device_status(case.recipe, approved_on, monkeypatch)
    assert status == 200

    name = "protocol-15" if drafted_on == OLD_APP else "no-stamp"
    _REPORT[name] = {
        "drafted_on": drafted_on,
        "approved_on": approved_on,
        "frame_question_asked": bool(case.asked),
        "freeze_on_draft_check": "r3" in case.checked,
        "preflight_freeze_frames": case.preflight_freeze,
        "render_freeze_frames": case.render_freeze,
        "hold_seconds": case.holds,
        "required_capabilities": sorted(case.recipe.required_capabilities),
        "r3": case.freeze,
        "device_render_status": {str(approved_on): status},
        "duration_s": case.recipe.duration,
    }
    _keep(f"{name}_recipe.json", case.recipe.model_dump(mode="json"))


async def test_an_app_that_plays_a_freeze_keeps_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    case = await _replay(monkeypatch, drafted_on=FREEZE_APP, approved_on=FREEZE_APP)

    # 5. KRI-828/900 unchanged: the frame question, a deferred check, a 1 s hold, "met"
    assert len(case.asked) == 1, case.asked
    assert "r3" not in case.checked, case.checked
    assert case.preflight_freeze is True
    assert case.render_freeze is True
    assert case.holds == [pytest.approx(1.0, abs=1 / 30 + 0.001)]
    assert "freezeFrame" in case.recipe.required_capabilities
    assert case.freeze["status"] == "met", case.freeze
    on_new = _device_status(case.recipe, FREEZE_APP, monkeypatch)
    assert on_new == 200
    # 8. the same held recipe opened on an older phone still asks for an update: it really
    # holds a frame that build cannot decode.
    on_old = _device_status(case.recipe, OLD_APP, monkeypatch)
    assert on_old == 426

    _REPORT["protocol-16"] = {
        "drafted_on": FREEZE_APP,
        "approved_on": FREEZE_APP,
        "frame_question_asked": True,
        "freeze_on_draft_check": "r3" in case.checked,
        "preflight_freeze_frames": case.preflight_freeze,
        "render_freeze_frames": case.render_freeze,
        "hold_seconds": case.holds,
        "required_capabilities": sorted(case.recipe.required_capabilities),
        "r3": case.freeze,
        "device_render_status": {str(FREEZE_APP): on_new, str(OLD_APP): on_old},
        "duration_s": case.recipe.duration,
    }
    _keep("protocol-16_recipe.json", case.recipe.model_dump(mode="json"))


async def test_drafted_on_a_new_phone_and_approved_on_an_old_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _replay(monkeypatch, drafted_on=FREEZE_APP, approved_on=OLD_APP)

    # 7. the approving app decides the render: no hold, "Couldn't", and the video renders
    assert case.render_freeze is False
    assert case.holds == []
    assert "freezeFrame" not in case.recipe.required_capabilities
    assert case.freeze["status"] == "not_possible", case.freeze
    status = _device_status(case.recipe, OLD_APP, monkeypatch)
    assert status == 200

    _REPORT["mixed-16-then-15"] = {
        "drafted_on": FREEZE_APP,
        "approved_on": OLD_APP,
        "frame_question_asked": bool(case.asked),
        "preflight_freeze_frames": case.preflight_freeze,
        "render_freeze_frames": case.render_freeze,
        "hold_seconds": case.holds,
        "required_capabilities": sorted(case.recipe.required_capabilities),
        "r3": case.freeze,
        "device_render_status": {str(OLD_APP): status},
        "duration_s": case.recipe.duration,
    }
    _keep("mixed_recipe.json", case.recipe.model_dump(mode="json"))


# -- the Job carries the approving build to the render ---------------------------------------


@pytest.mark.parametrize("approved_on", [OLD_APP, FREEZE_APP, None], ids=str)
def test_dispatch_stamps_the_approving_build_on_the_job(
    monkeypatch: pytest.MonkeyPatch, approved_on: int | None
) -> None:
    """The real phone-montage dispatch (`_dispatch_item_render`) writes the stamp the render
    reads; an unstamped approval leaves the Job's candidates exactly as before."""
    from tests.tasks.test_duplicate_upload_dedupe import (  # noqa: PLC0415
        _dispatch_strategy,
        _run_dispatch,
    )

    result, _item, built, _bind = _run_dispatch(
        monkeypatch,
        _dispatch_strategy(),
        **({"creator_client_protocol": approved_on} if approved_on is not None else {}),
    )
    assert result.outcome == "dispatched"
    candidates = built["job"].all_candidates
    if approved_on is None:
        assert "creator_client_protocol" not in candidates
    else:
        assert candidates["creator_client_protocol"] == approved_on
