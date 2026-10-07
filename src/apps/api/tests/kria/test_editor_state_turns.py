"""Chat copilot continues on the editor's CURRENT UNSAVED state (no DB).

Product rule: the creator never has to save mid-flow. The turn body may carry
``editor_state``; ONE resolver (``resolve_editor_base``) decides what the planner
snapshot AND the draft compile build on (client state > fresh head > saved variant).
"""

from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.kria import planner
from app.kria.api_schemas import EDITOR_STATE_MAX_BYTES, EditorStateIn, SubmitTurnBody
from app.kria.runtime import request_digest
from app.services import kria_editor_ops as ops
from app.services.kria_editor_ops import (
    EditorStateStaleError,
    compile_editor_ops,
    merge_editor_draft,
    resolve_editor_base,
)
from tests.kria.test_stale_head_draft import _head, _job, _planner_db, _variant
from tests.services._guided_timeline_fixtures import (
    arm_guided,
    guided_bars,
    guided_job,
    guided_revision,
)


def _state(lanes: dict | None = None, *, base: str = "G2", sid: str = "cs-1") -> EditorStateIn:
    return EditorStateIn.model_validate(
        {"base_generation": base, "client_state_id": sid, "lanes": lanes or {}}
    )


def _bars(variant: dict) -> dict:
    return {row["id"]: row for row in variant["text_elements"]}


# --- contract -------------------------------------------------------------------


def _body(**extra) -> dict:  # noqa: ANN003
    return {"message": "hi", "client_event_id": "e1", "expected_thread_revision": 0, **extra}


def test_empty_lanes_are_valid_and_old_clients_send_no_state() -> None:
    assert SubmitTurnBody(**_body()).editor_state is None
    state = SubmitTurnBody(
        **_body(editor_state={"base_generation": "", "client_state_id": "c", "lanes": {}})
    ).editor_state
    assert state is not None and state.version == 1
    # Present-but-empty lanes serialise as authoritative-empty, not as absent.
    assert state.model_dump(mode="json", exclude_unset=True, exclude_none=True)["lanes"] == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("copilot_receipt_ids", [str(uuid.uuid4())]),
        ("accepted_suggestion_ids", ["a"]),
        ("retry_guided_revision", True),
        ("guided_revision", {"x": 1}),
        ("guided_revision_number", 2),
    ],
)
def test_save_only_fields_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match="Save-only"):
        _state({field: value})


def test_unknown_envelope_keys_and_bad_version_are_rejected() -> None:
    with pytest.raises(ValidationError):
        EditorStateIn.model_validate(
            {"base_generation": "g", "client_state_id": "c", "lanes": {}, "extra": 1}
        )
    with pytest.raises(ValidationError):
        EditorStateIn.model_validate(
            {"version": 2, "base_generation": "g", "client_state_id": "c", "lanes": {}}
        )
    with pytest.raises(ValidationError):
        _state(sid="")
    with pytest.raises(ValidationError):
        _state(sid="x" * 65)


def test_size_cap_and_lane_caps() -> None:
    big = [{"id": f"t{i}", "text": "x" * 2000} for i in range(150)]
    assert len(json.dumps(big)) > EDITOR_STATE_MAX_BYTES
    with pytest.raises(ValidationError, match="at most"):
        _state({"text_elements": big})
    for lane in ("text_elements", "caption_cues", "motion_scenes", "camera_effects"):
        with pytest.raises(ValidationError):
            _state({lane: [{"id": str(i)} for i in range(201)]})


def test_added_clip_carries_its_media_identity_through_the_projection(monkeypatch) -> None:
    """A clip added in the editor is not in the persisted sources; the row names it."""
    job_id = str(uuid.uuid4())
    revision = guided_revision(job_id, count=2)
    job, variant = guided_job(revision, guided_bars(revision), job_id)
    arm_guided(monkeypatch, revision)
    rows = [
        {"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_s": 2.0},
        {"slot_id": "s2", "clip_index": 1, "in_s": 0.0, "duration_s": 2.0},
        {
            "slot_id": None,
            "clip_index": 7,
            "in_s": 0.0,
            "duration_s": 3.0,
            "media_id": "m-new",
            "media_kind": "video",
            "source_duration_s": 9.0,
        },
    ]
    state = _state({"timeline_slots": rows}, base="gen-1")
    base = resolve_editor_base(job, variant, None, state)
    added = base.projected["guided_draft_slots"][-1]
    assert added["media_id"] == "m-new"
    assert added["source_duration_s"] == 9.0
    assert added["slot_id"].startswith("cs-cs-1")  # deterministic id for a new row


def test_digest_excludes_editor_state() -> None:
    plain = SubmitTurnBody(**_body())
    with_state = SubmitTurnBody(
        **_body(editor_state={"base_generation": "a", "client_state_id": "c1", "lanes": {}})
    )
    other = SubmitTurnBody(
        **_body(editor_state={"base_generation": "b", "client_state_id": "c2", "lanes": {}})
    )
    assert request_digest(plain) == request_digest(with_state) == request_digest(other)
    # Old-client digests are unchanged by the new optional field.
    assert request_digest(plain) != request_digest(SubmitTurnBody(**{**_body(), "message": "yo"}))


# --- resolver ---------------------------------------------------------------------


def _moved(**changes) -> dict:  # noqa: ANN003
    rows = copy.deepcopy(_variant()["text_elements"])
    rows[0].update(x_frac=0.55, y_frac=0.12, **changes)
    rows[1]["text"] = "Creator label"
    return {"text_elements": rows}


def test_fresh_state_wins_over_a_fresher_looking_fresh_head() -> None:
    variant = _variant("G2")
    head = _head("G2")  # same generation: a FRESH head full of other edits
    base = resolve_editor_base(_job(variant), variant, head, _state(_moved()))
    assert base.source == "client_state"
    assert _bars(base.projected)["title"]["x_frac"] == 0.55
    assert _bars(base.projected)["label"]["text"] == "Creator label"
    assert "Stale" not in str(base.prior_payload)
    assert base.prior_payload["base_generation"] == "G2"


def test_head_is_ignored_even_for_empty_lanes() -> None:
    variant = _variant("G2")
    base = resolve_editor_base(_job(variant), variant, _head("G2"), _state({}))
    assert base.source == "client_state"
    assert _bars(base.projected)["label"]["text"] == "Saved label"
    assert "Stale" not in str(base.projected)


def test_precedence_head_then_variant_without_state() -> None:
    variant = _variant("G2")
    head = resolve_editor_base(_job(variant), variant, _head("G2"), None)
    assert head.source == "head" and _bars(head.projected)["label"]["text"] == "Stale label"
    stale_head = resolve_editor_base(_job(variant), variant, _head("G1"), None)
    assert stale_head.source == "variant" and stale_head.prior_payload == {}
    assert _bars(stale_head.projected)["label"]["text"] == "Saved label"


def test_stale_state_is_never_silently_rebased() -> None:
    variant = _variant("G3")
    with pytest.raises(EditorStateStaleError):
        resolve_editor_base(_job(variant), variant, _head("G3"), _state(_moved(), base="G2"))


def test_malformed_lane_falls_back_instead_of_crashing() -> None:
    variant = _variant("G2")
    bad = {
        "base_generation": "G2",
        "client_state_id": "c",
        "lanes": {"timeline_slots": ["not-a-row"]},
    }
    base = resolve_editor_base(_job(variant), variant, _head("G2"), bad)
    assert base.source == "head"
    assert base.fallback_reason and base.fallback_reason.startswith("client_state_unusable")
    base = resolve_editor_base(_job(variant), variant, None, bad)
    assert base.source == "variant" and base.fallback_reason


def test_parse_editor_state_tolerates_garbage() -> None:
    assert ops.parse_editor_state(None) is None
    assert ops.parse_editor_state({"nope": 1}) is None
    # Explicit nulls / Save defaults serialised by a naive client are tolerated.
    assert ops.parse_editor_state(_state().model_dump(mode="json")) is not None
    assert ops.editor_state_has_lanes(_state({"text_elements": []})) is True
    assert ops.editor_state_has_lanes(_state({})) is False


# --- planner snapshot ---------------------------------------------------------------


async def _planner_target(monkeypatch, head, state, *, generation="G2", status="ready"):  # noqa: ANN001, ANN202
    variant = _variant(generation)
    variant["render_status"] = status
    job = _job(variant)
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=job.id)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        plan_item_id=item.id,
        target_job_id=job.id,
        target_variant_id="v1",
        target_generation_id="G1",
    )
    monkeypatch.setattr(planner, "_copilot_clip_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner,
        "build_editor_snapshot",
        lambda _job, v, **_k: {"allowed_op_families": ["text"], "text_bars": v["text_elements"]},
    )
    kwargs = {"editor_state": state} if state is not None else {}
    return await planner._load_editor_target(
        _planner_db(head, session, job), thread_id=uuid.uuid4(), item=item, **kwargs
    )


@pytest.mark.asyncio
async def test_planner_snapshot_is_the_clients_current_state(monkeypatch) -> None:
    target = await _planner_target(monkeypatch, _head("G2"), _state(_moved()))
    bars = {row["id"]: row for row in target.snapshot["text_bars"]}
    assert bars["title"]["x_frac"] == 0.55 and bars["label"]["text"] == "Creator label"


@pytest.mark.asyncio
async def test_planner_stale_state_is_a_guarded_miss_with_the_recovery_reply(monkeypatch) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        target = await _planner_target(monkeypatch, None, _state(_moved(), base="G1"))
    assert target is None
    assert planner._editor_target_miss.get() == "editor_state_stale"
    assert planner._editor_target_miss_guarded()
    assert [e["reason"] for e in logs if e["event"] == "kria_editor_target_unavailable"] == [
        "editor_state_stale"
    ]
    reply = planner._editor_target_recovery(SimpleNamespace(manifest_hash="m", context_hash="c"))
    assert reply.plan.turn_value == "recovery"
    assert reply.plan.response == "Your video changed — reopen the editor and try again."


@pytest.mark.asyncio
async def test_render_in_flight_guard_is_skipped_only_for_a_fresh_state(monkeypatch) -> None:
    # No state: the guard stays.
    assert await _planner_target(monkeypatch, None, None, status="rendering") is None
    assert planner._editor_target_miss.get() == "render_in_flight"
    # Fresh state: the snapshot reads only editable lanes, so it can answer.
    target = await _planner_target(monkeypatch, None, _state(_moved()), status="rendering")
    assert target is not None
    # A state built on an older render is still refused.
    assert (
        await _planner_target(monkeypatch, None, _state(_moved(), base="G1"), status="rendering")
        is None
    )
    assert planner._editor_target_miss.get() == "editor_state_stale"


@pytest.mark.asyncio
async def test_speech_cut_with_unsaved_lanes_gets_the_honest_reply(monkeypatch) -> None:
    target = SimpleNamespace(job_id=uuid.uuid4(), snapshot={}, conversation=[], variant={})
    monkeypatch.setattr(planner, "_load_editor_target", AsyncMock(return_value=target))
    reply = SimpleNamespace(
        ops=[{"op": "apply_speech_cut_candidate", "candidate_id": "c"}],
        outcome="ok",
        reply="cutting",
    )
    monkeypatch.setattr(planner, "run_copilot_turn", AsyncMock(return_value=reply))
    db = SimpleNamespace(rollback=AsyncMock())
    item = SimpleNamespace(id=uuid.uuid4())

    with_lanes = await planner._plan_editor_revision(
        db,
        thread_id=uuid.uuid4(),
        item=item,
        user_message="cut silences",
        editor_state=_state({"text_elements": []}),
    )
    assert with_lanes.mode == "respond" and with_lanes.turn_value == "recovery"
    assert with_lanes.response == "Save your edits first, then I can cut the silences."
    # No unsaved edits (empty lanes) or no state: today's behaviour (a render plan).
    for state in (_state({}), None):
        kw = {"editor_state": state} if state is not None else {}
        plan = await planner._plan_editor_revision(
            db, thread_id=uuid.uuid4(), item=item, user_message="cut silences", **kw
        )
        assert plan.mode == "act"


@pytest.mark.asyncio
async def test_no_state_calls_stay_byte_identical(monkeypatch) -> None:
    """`_load_editor_target` is only handed `editor_state` when there is one."""
    seen: list[dict] = []

    async def load(_db, **kwargs):  # noqa: ANN001, ANN202
        seen.append(kwargs)
        return None

    monkeypatch.setattr(planner, "_load_editor_target", load)
    db = SimpleNamespace(rollback=AsyncMock())
    item = SimpleNamespace(id=uuid.uuid4())
    await planner._plan_editor_revision(db, thread_id=uuid.uuid4(), item=item, user_message="x")
    assert "editor_state" not in seen[0]


# --- the incident, at the API level ---------------------------------------------------


@pytest.fixture
def guided(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True, raising=False)
    job_id = str(uuid.uuid4())
    revision = guided_revision(job_id)
    job, variant = guided_job(revision, guided_bars(revision), job_id)
    arm_guided(monkeypatch, revision)
    return job, variant, revision


def _incident_state(variant: dict) -> EditorStateIn:
    """Creator moved the title, edited label m1 and delayed label m2 -- never saved."""
    rows = copy.deepcopy(variant["text_elements"])
    by_id = {row["id"]: row for row in rows}
    by_id["guided-title"].update(x_frac=0.62, y_frac=0.09, size_px=110)
    by_id["clip-label-media-m1"]["text"] = "Rooftop at golden hour"
    by_id["clip-label-media-m2"]["start_s"] = 4.6
    return _state({"text_elements": rows}, base="gen-1", sid="incident")


def test_incident_replay_make_it_15_seconds_keeps_every_unsaved_edit(guided) -> None:
    job, variant, _rev = guided
    base = resolve_editor_base(job, variant, None, _incident_state(variant))
    compiled = compile_editor_ops(
        job,
        base.projected,
        [{"op": "set_total_duration", "target_s": 15, "strategy": "proportional"}],
    )
    staged = merge_editor_draft(
        base.prior_payload, compiled.payload.model_dump(mode="json", exclude_none=True)
    )
    bars = {row["id"]: row for row in staged["text_elements"]}
    assert (bars["guided-title"]["x_frac"], bars["guided-title"]["y_frac"]) == (0.62, 0.09)
    assert bars["guided-title"]["size_px"] == 110
    assert bars["clip-label-media-m1"]["text"] == "Rooftop at golden hour"
    # The delayed label did not snap back to its segment's exact start (4.0 -> 7.5
    # after the stretch): it is still delayed relative to the segment it follows.
    segments = {s.slot_id: s for s in compiled.payload.timeline_slots}
    assert sum(s.duration_s for s in compiled.payload.timeline_slots) == pytest.approx(
        15.0, abs=0.01
    )
    assert bars["clip-label-media-m2"]["start_s"] > 7.5 + 1e-3
    assert segments  # timeline really changed
    assert staged["base_generation"] == "gen-1"


def test_incident_rebase_reads_old_segments_from_the_clients_timeline(guided, monkeypatch) -> None:
    """The user shortened clip 1 in the (unsaved) editor; 'set_total_duration 15' must
    rebase text against THAT timeline, not the saved one."""
    job, variant, _rev = guided
    saved_rows = ops._variant_slots(variant, job)
    client_rows = copy.deepcopy(saved_rows)
    client_rows[0]["duration_s"] = 1.0  # trimmed in the editor, unsaved
    state = _state({"timeline_slots": client_rows}, base="gen-1", sid="tl")
    base = resolve_editor_base(job, variant, None, state)

    import app.services.kria_editor_timeline as timeline

    captured: dict = {}
    real = timeline.rebase_guided_text

    def spy(editor_state, guided_rev):  # noqa: ANN001, ANN202
        captured["initial"] = copy.deepcopy(editor_state.initial_slots)
        return real(editor_state, guided_rev)

    monkeypatch.setattr(timeline, "rebase_guided_text", spy)
    compile_editor_ops(
        job,
        base.projected,
        [{"op": "set_total_duration", "target_s": 15, "strategy": "proportional"}],
    )
    first = captured["initial"][0]
    assert first["duration_s"] == 1.0  # CLIENT timeline, not the saved 2.0
    assert first["output_end_s"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_planner_snapshot_goes_through_the_resolver(monkeypatch) -> None:
    calls: list[object] = []
    real = planner.resolve_editor_base

    def spy(job, variant, head, state=None):  # noqa: ANN001, ANN202
        calls.append(state)
        return real(job, variant, head, state)

    monkeypatch.setattr(planner, "resolve_editor_base", spy)
    state = _state(_moved())
    assert await _planner_target(monkeypatch, None, state) is not None
    assert calls == [state]


@pytest.mark.asyncio
async def test_unsupported_editor_refusal_offers_a_redo_that_routes_to_a_replan(
    monkeypatch,
) -> None:
    """A re-sent request on a limited draft got a dead-end refusal (KRI-473)."""
    from app.kria.brief import wants_full_replan

    target = SimpleNamespace(job_id=uuid.uuid4(), snapshot={}, conversation=[], variant={})
    monkeypatch.setattr(planner, "_load_editor_target", AsyncMock(return_value=target))
    refusal = "That kind of edit isn't available for this draft yet."
    reply = SimpleNamespace(ops=[], outcome="unsupported", reply=refusal)
    monkeypatch.setattr(planner, "run_copilot_turn", AsyncMock(return_value=reply))
    db = SimpleNamespace(rollback=AsyncMock())
    item = SimpleNamespace(id=uuid.uuid4())

    plan = await planner._plan_editor_revision(
        db, thread_id=uuid.uuid4(), item=item, user_message="same prompt again but longer"
    )
    assert plan.mode == "respond" and plan.turn_value == "recovery"
    assert plan.response.startswith(refusal) and '"redo"' in plan.response
    # The word the offer asks for is the deterministic re-plan trigger.
    assert wants_full_replan("redo")

    # Other refusals (and a reply that already carries the offer) are not touched.
    for outcome in ("failed", "stale", "no_effect"):
        reply.outcome = outcome
        plan = await planner._plan_editor_revision(
            db, thread_id=uuid.uuid4(), item=item, user_message="x"
        )
        assert plan.response == refusal
    reply.outcome, reply.reply = "unsupported", plan.response + planner._REDO_OFFER
    plan = await planner._plan_editor_revision(
        db, thread_id=uuid.uuid4(), item=item, user_message="x"
    )
    assert plan.response.count('"redo"') == 1
