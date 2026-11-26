"""CA-7: overlay generation on the chat thread, through the real routes on real Postgres.

Failure catalogue (written before the code; each row is a test below):

  F1  flag off: no route answers (404), nothing is written, the capability is false.
  F2  an old build: a write is refused (426), and the events a newer build caused are not in its
      thread read or its delta read, so an old app never decodes a card it cannot draw.
  F3  events replay in order: ask, the compose answer, one event per state change, a done receipt
      listing the ready overlay ids; the newest event of a set equals the stored state.
  F4  a duplicate is one set and one charge: a second ask, a second identical compose answer, a
      second plan (sequential or racing it) and a second generate each make nothing new; a
      different compose answer after the first is refused.
  F5  a failed overlay retries alone: the others are not generated again, the retry is a new
      attempt id (the operation of a failed attempt is never reused), a ready overlay and an
      unknown outcome (the provider may have been paid) are never retried.
  F6  resume after a restart equals before: state read by fresh sessions is identical, an overlay
      still making when the process died finishes by repeating its own request, with no second id.
  F7  a creator's next turn is not rejected because overlay progress moved the revision.
  F8  another creator's thread is a 404, and a retry or generate for an overlay that is not in the
      set is a 404.

The paid calls are recorders (`generate_overlay_plan`, `generate_image_for_card`): the planner and
the operation store have their own suites. Everything between the request and the thread event is
the product's.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

import app.kria.overlay_set as overlay_set
import app.routes.creation_threads as threads_routes
import app.routes.overlay_generation as routes
from app.auth import get_current_user
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.runtime import read_delta
from app.main import app
from app.models import CreationThread, CreationThreadEvent, User
from app.services import generated_media
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.creation_thread_titles import (
    conversation_revision_matches,
    conversation_revision_matches_sync,
)
from app.services.generated_media import CardGeneration
from app.services.overlay_generation import OverlayPlanRequest, OverlayPlanResponse, PlannedOverlay
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

PROTOCOL = settings.overlay_set_min_client_protocol
GATE = {
    "kria_suggestion_generated_overlay_enabled": True,
    "ios_native_device_only_enabled": True,
    "ai_cost_control_enabled": True,
    "generated_images_enabled": True,
    "generated_media_live_approved": True,
    "generated_media_billing_organization": "org-offline",
    "openai_api_key": "offline",
    "generated_media_max_request_usd": 2.0,
    "ai_openai_monthly_budget_usd": 100.0,
    "ai_openai_image_budget_usd": 50.0,
    "phone_render_verified_features": ["visualBlocks"],
}


@pytest.fixture(autouse=True)
def _gate(monkeypatch):
    for name, value in GATE.items():
        monkeypatch.setattr(settings, name, value)
    yield
    clear_request_context()


class Planner:
    """Stands in for `generate_overlay_plan`: records every call, answers three image rows."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.entered = threading.Event()
        self.release: threading.Event | None = None

    def __call__(self, db, *, owner_id, item_id, body, ctx, locale=None):  # noqa: ANN001, ANN204
        self.calls.append({"mode": body.mode, "prompt": body.prompt})
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(10), "the test never released the planner"
        rows = [
            PlannedOverlay(
                kind="image",
                title=f"Image {n}",
                prompt=f"prompt {n}",
                start_s=n,
                end_s=n + 2,
                evidence=[{"kind": "speech", "reference_id": f"speech-{n}"}],
            )
            for n in range(3)
        ]
        rows.append(
            PlannedOverlay(
                kind="text",
                title="Price",
                text="$5",
                start_s=0,
                end_s=2,
                evidence=[{"kind": "speech", "reference_id": "speech-0"}],
            )
        )
        return OverlayPlanResponse(
            document_fingerprint=body.document_fingerprint,
            base_generation=body.base_generation,
            overlays=rows,
            message="Review placement over your footage.",
        )


class Generator:
    """Stands in for `generate_image_for_card`. Keyed on the event id like the real one: a second
    call with an id it has seen finds the first operation instead of making another."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.made: dict[uuid.UUID, str] = {}
        self.fail_first: set[str] = set()
        self.unknown: set[str] = set()
        self.stall_first: set[str] = set()
        self._stalled: set[uuid.UUID] = set()

    def __call__(self, *, owner_id, item_id, prompt, event_id):  # noqa: ANN001, ANN204
        self.calls.append({"prompt": prompt, "event_id": event_id})
        if event_id not in self.made:
            if prompt in self.unknown:
                self.made[event_id] = "unknown"
            elif prompt in self.fail_first:
                self.fail_first.discard(prompt)
                self.made[event_id] = "failed"
            elif prompt in self.stall_first:
                self.stall_first.discard(prompt)
                self.made[event_id] = "submitting"
                self._stalled.add(event_id)
            else:
                self.made[event_id] = "ready"
        elif event_id in self._stalled:
            self._stalled.discard(event_id)
            self.made[event_id] = "ready"
        state = self.made[event_id]
        failure = {"failed": "provider_rejected", "unknown": "provider_outcome_unknown"}.get(state)
        return CardGeneration(state, event_id, failure)


@pytest.fixture
def planner(monkeypatch):
    recorder = Planner()
    monkeypatch.setattr(routes, "generate_overlay_plan", recorder)
    return recorder


@pytest.fixture
def generator(monkeypatch):
    recorder = Generator()
    monkeypatch.setattr(generated_media, "generate_image_for_card", recorder)
    return recorder


@pytest.fixture
def seeded():
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
    return SimpleNamespace(user_id=user_id, thread_id=thread_id, item_id=item_id)


def _plan_body(**overrides) -> OverlayPlanRequest:
    values = dict(
        job_id=uuid.uuid4(),
        variant_id="v1",
        base_generation="g1",
        document_fingerprint="f1",
        mode="automatic",
        editor_snapshot={},
    )
    return OverlayPlanRequest(**{**values, **overrides})


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


async def _call(seeded, handler, *args, protocol=PROTOCOL, user_id=None, thread_id=None, **extra):
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id or seeded.user_id)
        return await handler(
            str(thread_id or seeded.thread_id),
            *args,
            user,
            db,
            native_client=True,
            client_protocol=protocol,
            **extra,
        )


async def _ask(seeded, set_id, **kw):
    return await _call(seeded, routes.open_overlay_set, routes.OverlaySetOpen(set_id=set_id), **kw)


async def _compose(seeded, set_id, mode="automatic", prompts=(), **kw):
    body = routes.OverlayComposeBody(mode=mode, prompts=list(prompts))
    return await _call(seeded, routes.compose_overlay_set, set_id, body, **kw)


async def _plan(seeded, set_id, **kw):
    return await _call(
        seeded, routes.plan_overlay_set, set_id, _plan_body(), request=_request(), **kw
    )


async def _generate(seeded, set_id, overlay_id, **kw):
    return await _call(seeded, routes.generate_set_overlay, set_id, overlay_id, **kw)


async def _run_set(seeded, generator_, *, set_id=None):
    set_id = set_id or uuid.uuid4()
    await _ask(seeded, set_id)
    await _compose(seeded, set_id)
    await _plan(seeded, set_id)
    for overlay in _events(seeded, set_id=set_id)[-1].payload["overlays"]:
        await _generate(seeded, set_id, uuid.UUID(overlay["id"]))
    return set_id


def _events(seeded, *, set_id=None) -> list[CreationThreadEvent]:
    with sync_session() as db:
        rows = list(
            db.scalars(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == seeded.thread_id,
                    CreationThreadEvent.event_type == overlay_set.EVENT_TYPE,
                )
                .order_by(CreationThreadEvent.sequence)
            )
        )
        db.expunge_all()
    return [r for r in rows if set_id is None or r.payload["set_id"] == str(set_id)]


def _stored(seeded) -> dict:
    with sync_session() as db:
        return dict(db.get(CreationThread, seeded.thread_id).state or {}).get(
            overlay_set.STATE_KEY, {}
        )


async def _read_events(seeded, protocol):
    set_client_protocol(protocol)
    try:
        async with AsyncSessionLocal() as db:
            thread = await db.get(CreationThread, seeded.thread_id)
            out = await threads_routes._response(db, thread)
            delta = await read_delta(
                db,
                thread_id=seeded.thread_id,
                creator_id=seeded.user_id,
                after_sequence=-1,
                limit=200,
            )
        return (
            [e for e in out.events if e.event_type == overlay_set.EVENT_TYPE],
            [e for e in delta.events if e.event_type == overlay_set.EVENT_TYPE],
            out.state,
        )
    finally:
        clear_request_context()


def _by_prompt(snapshot) -> dict[str, dict]:
    return {o["prompt"]: o for o in snapshot["overlays"]}


# -- F1 ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f1_flag_off_answers_nothing_and_writes_nothing(seeded, planner, generator):
    settings.kria_suggestion_generated_overlay_enabled = False
    set_id = uuid.uuid4()
    for call in (
        lambda: _ask(seeded, set_id),
        lambda: _compose(seeded, set_id),
        lambda: _plan(seeded, set_id),
        lambda: _generate(seeded, set_id, uuid.uuid4()),
    ):
        with pytest.raises(HTTPException) as refused:
            await call()
        assert refused.value.status_code == 404
    assert _events(seeded) == [] and _stored(seeded) == {}
    assert planner.calls == [] and generator.calls == []
    assert overlay_set.capability(seeded.user_id, True, PROTOCOL) is False


# -- F2 ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f2_an_old_build_is_refused_and_never_sees_the_events(seeded, planner, generator):
    with pytest.raises(HTTPException) as refused:
        await _ask(seeded, uuid.uuid4(), protocol=PROTOCOL - 1)
    assert refused.value.status_code == 426
    set_id = await _run_set(seeded, generator)
    assert _events(seeded, set_id=set_id)

    for protocol in (PROTOCOL - 1, None):
        thread_events, delta_events, state = await _read_events(seeded, protocol)
        assert thread_events == [] and delta_events == []
        assert overlay_set.STATE_KEY not in state
    thread_events, delta_events, state = await _read_events(seeded, PROTOCOL)
    assert len(thread_events) == len(delta_events) == len(_events(seeded))
    assert overlay_set.STATE_KEY not in state, "server bookkeeping is not public thread state"
    assert overlay_set.capability(seeded.user_id, True, PROTOCOL) is True
    assert overlay_set.capability(seeded.user_id, True, PROTOCOL - 1) is False


@pytest.mark.asyncio
async def test_f2_through_the_app_the_protocol_header_decides(
    seeded, planner, generator, monkeypatch
):
    monkeypatch.setattr(settings, "mobile_jwt_secret", "offline")
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    owner = SimpleNamespace(id=seeded.user_id)
    app.dependency_overrides[get_current_user] = lambda: owner

    def headers(protocol):
        return {"Authorization": "Bearer offline", "X-Kria-Client-Protocol": str(protocol)}

    base = f"/creation-threads/{seeded.thread_id}"
    try:
        with TestClient(app) as client:
            ask = client.post(
                f"{base}/overlay-sets",
                json={"set_id": str(uuid.uuid4())},
                headers=headers(PROTOCOL),
            )
            assert ask.status_code == 200 and ask.json()["phase"] == "ask"
            old = client.post(
                f"{base}/overlay-sets",
                json={"set_id": str(uuid.uuid4())},
                headers=headers(PROTOCOL - 1),
            )
            assert old.status_code == 426

            def seen(protocol):
                read = client.get(base, headers=headers(protocol))
                assert read.status_code == 200, read.text
                return [e for e in read.json()["events"] if e["event_type"] == "overlay_set"]

            assert len(seen(PROTOCOL)) == 1
            assert seen(PROTOCOL - 1) == []
            caps = {
                p: client.get("/creation-threads/capabilities", headers=headers(p)).json()
                for p in (PROTOCOL, PROTOCOL - 1)
            }
            assert caps[PROTOCOL]["overlay_sets"] is True
            assert caps[PROTOCOL - 1]["overlay_sets"] is False
    finally:
        app.dependency_overrides.clear()


# -- F3 ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f3_events_replay_in_order_and_end_in_a_receipt(seeded, planner, generator):
    set_id = await _run_set(seeded, generator)
    rows = _events(seeded, set_id=set_id)
    phases = [r.payload["phase"] for r in rows]
    assert phases == ["ask", "compose", "progress", "progress", "progress", "done"]
    assert [r.sequence for r in rows] == sorted(r.sequence for r in rows)
    assert [r.role for r in rows[:2]] == ["assistant", "user"]
    assert all(r.payload["v"] == overlay_set.SCHEMA_VERSION for r in rows)
    assert all(r.payload["unit_price_usd"] > 0 for r in rows)

    first_progress = rows[2].payload
    assert {o["state"] for o in first_progress["overlays"]} == {"making"}
    assert len(first_progress["overlays"]) == 3, "only the image rows are made"
    done = rows[-1].payload
    assert {o["state"] for o in done["overlays"]} == {"ready"}
    assert done["overlay_ids"] == [o["id"] for o in done["overlays"]]
    assert done["message"] == "Review placement over your footage."
    assert done["mode"] == "automatic"

    stored = _stored(seeded)[str(set_id)]
    assert overlay_set.event_payload(str(set_id), stored) == done


@pytest.mark.asyncio
async def test_f3_describe_them_hands_the_planner_the_creators_own_rows(seeded, planner, generator):
    set_id = uuid.uuid4()
    await _ask(seeded, set_id)
    await _compose(seeded, set_id, mode="prompt", prompts=["a red scooter", "  a blue bike  "])
    await _plan(seeded, set_id)
    assert planner.calls == [{"mode": "prompt", "prompt": "a red scooter\na blue bike"}]
    compose = _events(seeded, set_id=set_id)[1].payload
    assert compose["prompts"] == ["a red scooter", "a blue bike"]

    for bad in ([], ["  "], ["x"] * (overlay_set.MAX_PROMPTS + 1)):
        with pytest.raises(ValueError):
            routes.OverlayComposeBody(mode="prompt", prompts=bad)


# -- F4 ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f4_a_duplicate_request_is_one_set_and_one_charge(seeded, planner, generator):
    set_id = uuid.uuid4()
    await _ask(seeded, set_id)
    await _ask(seeded, set_id)
    await _compose(seeded, set_id, mode="prompt", prompts=["a red scooter"])
    await _compose(seeded, set_id, mode="prompt", prompts=["a red scooter"])
    with pytest.raises(HTTPException) as changed:
        await _compose(seeded, set_id, mode="prompt", prompts=["something else"])
    assert changed.value.status_code == 409

    first = await _plan(seeded, set_id)
    second = await _plan(seeded, set_id)
    assert len(planner.calls) == 1, "the paid plan ran once"
    assert [o.id for o in first.overlays] == [o.id for o in second.overlays]

    target = uuid.UUID(_events(seeded, set_id=set_id)[-1].payload["overlays"][0]["id"])
    await _generate(seeded, set_id, target)
    await _generate(seeded, set_id, target)
    assert len(generator.calls) == 1, "a ready overlay is never generated again"
    assert len(set(c["event_id"] for c in generator.calls)) == 1
    phases = [r.payload["phase"] for r in _events(seeded, set_id=set_id)]
    assert phases.count("ask") == phases.count("compose") == 1


@pytest.mark.asyncio
async def test_f4_a_racing_second_plan_is_told_to_wait_and_does_not_pay_again(
    seeded, planner, generator
):
    set_id = uuid.uuid4()
    await _ask(seeded, set_id)
    await _compose(seeded, set_id)
    planner.release = threading.Event()
    first = asyncio.create_task(_plan(seeded, set_id))
    await asyncio.get_running_loop().run_in_executor(None, planner.entered.wait, 10)
    with pytest.raises(HTTPException) as busy:
        await _plan(seeded, set_id)
    assert busy.value.status_code == 409
    planner.release.set()
    planned = await first
    assert len(planner.calls) == 1
    assert len(planned.overlays) == 4


@pytest.mark.asyncio
async def test_f4_a_failed_plan_releases_its_claim(seeded, monkeypatch, generator):
    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ValueError("The editor snapshot is stale")

    set_id = uuid.uuid4()
    await _ask(seeded, set_id)
    await _compose(seeded, set_id)
    monkeypatch.setattr(routes, "generate_overlay_plan", refuse)
    with pytest.raises(HTTPException) as stale:
        await _plan(seeded, set_id)
    assert stale.value.status_code == 409
    recorder = Planner()
    monkeypatch.setattr(routes, "generate_overlay_plan", recorder)
    assert len((await _plan(seeded, set_id)).overlays) == 4
    assert len(recorder.calls) == 1


# -- F5 ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f5_a_failed_overlay_retries_alone(seeded, planner, generator):
    generator.fail_first.add("prompt 1")
    set_id = await _run_set(seeded, generator)
    snapshot = _events(seeded, set_id=set_id)[-1].payload
    overlays = _by_prompt(snapshot)
    assert snapshot["phase"] == "done"
    assert overlays["prompt 1"]["state"] == "failed"
    assert overlays["prompt 1"]["error_code"] == "provider_rejected"
    assert overlays["prompt 1"]["retryable"] is True
    assert overlays["prompt 0"]["state"] == overlays["prompt 2"]["state"] == "ready"
    assert snapshot["overlay_ids"] == [overlays["prompt 0"]["id"], overlays["prompt 2"]["id"]]

    calls_before = list(generator.calls)
    retried = await _generate(seeded, set_id, uuid.UUID(overlays["prompt 1"]["id"]))
    new_calls = generator.calls[len(calls_before) :]
    assert [c["prompt"] for c in new_calls] == ["prompt 1"]
    assert new_calls[0]["event_id"] not in {c["event_id"] for c in calls_before}
    assert retried.phase == "done"
    assert {o.state for o in retried.overlays} == {"ready"}
    assert len(retried.overlay_ids) == 3
    phases = [r.payload["phase"] for r in _events(seeded, set_id=set_id)]
    assert phases[-2:] == ["progress", "done"], "the retry shows making, then the receipt again"


@pytest.mark.asyncio
async def test_f5_an_unknown_outcome_is_not_retried_and_a_ready_overlay_is_left_alone(
    seeded, planner, generator
):
    generator.unknown.add("prompt 2")
    set_id = await _run_set(seeded, generator)
    overlays = _by_prompt(_events(seeded, set_id=set_id)[-1].payload)
    assert overlays["prompt 2"]["state"] == "failed"
    assert overlays["prompt 2"]["retryable"] is False
    calls = len(generator.calls)
    with pytest.raises(HTTPException) as refused:
        await _generate(seeded, set_id, uuid.UUID(overlays["prompt 2"]["id"]))
    assert refused.value.status_code == 409
    await _generate(seeded, set_id, uuid.UUID(overlays["prompt 0"]["id"]))
    assert len(generator.calls) == calls


# -- F6 ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f6_a_restart_mid_making_resumes_without_a_second_charge(seeded, planner, generator):
    generator.stall_first.add("prompt 1")
    set_id = await _run_set(seeded, generator)
    stalled = _by_prompt(_events(seeded, set_id=set_id)[-1].payload)["prompt 1"]
    assert stalled["state"] == "making", "an unresolved operation is still making"
    before = (
        _stored(seeded),
        [(e.sequence, e.revision, e.payload) for e in _events(seeded)],
    )

    after_restart = (
        _stored(seeded),
        [(e.sequence, e.revision, e.payload) for e in _events(seeded)],
    )
    assert after_restart == before
    thread_events, _, _ = await _read_events(seeded, PROTOCOL)
    assert thread_events[-1].payload == before[1][-1][2]

    resumed = await _generate(seeded, set_id, uuid.UUID(stalled["id"]))
    assert {o.state for o in resumed.overlays} == {"ready"}
    assert resumed.phase == "done"
    prompt_ids = {c["event_id"] for c in generator.calls if c["prompt"] == "prompt 1"}
    assert len(prompt_ids) == 1, "the resume repeated its own request"
    assert len(generator.made) == 3


# -- F7 ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f7_overlay_progress_does_not_reject_the_creators_next_turn(
    seeded, planner, generator
):
    async with AsyncSessionLocal() as db:
        expected = int((await db.get(CreationThread, seeded.thread_id)).revision)
    await _run_set(seeded, generator)
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, seeded.thread_id)
        assert int(thread.revision) > expected + 1
        assert await conversation_revision_matches(db, thread, expected)
        db.add(
            CreationThreadEvent(
                thread_id=thread.id,
                sequence=900,
                revision=int(thread.revision) + 1,
                role="user",
                event_type="user_message",
                content="hello",
            )
        )
        thread.revision = int(thread.revision) + 1
        await db.flush()
        assert not await conversation_revision_matches(db, thread, expected)
        await db.rollback()
    with sync_session() as db:
        thread = db.get(CreationThread, seeded.thread_id)
        assert conversation_revision_matches_sync(db, thread, expected)


# -- F8 ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f8_another_creators_thread_and_unknown_ids_are_not_found(seeded, planner, generator):
    other_user, _other_thread, _ = _seed_runtime_project()
    set_id = uuid.uuid4()
    with pytest.raises(HTTPException) as foreign:
        await _ask(seeded, set_id, user_id=other_user)
    assert foreign.value.status_code == 404
    assert _events(seeded) == []

    await _ask(seeded, set_id)
    for call in (
        lambda: _compose(seeded, uuid.uuid4()),
        lambda: _plan(seeded, uuid.uuid4()),
        lambda: _generate(seeded, uuid.uuid4(), uuid.uuid4()),
    ):
        with pytest.raises(HTTPException) as missing:
            await call()
        assert missing.value.status_code == 404
    await _compose(seeded, set_id)
    await _plan(seeded, set_id)
    with pytest.raises(HTTPException) as missing:
        await _generate(seeded, set_id, uuid.uuid4())
    assert missing.value.status_code == 404
    assert generator.calls == []


@pytest.mark.asyncio
async def test_planning_before_the_creator_answered_is_refused(seeded, planner, generator):
    set_id = uuid.uuid4()
    await _ask(seeded, set_id)
    with pytest.raises(HTTPException) as early:
        await _plan(seeded, set_id)
    assert early.value.status_code == 409
    assert planner.calls == []
