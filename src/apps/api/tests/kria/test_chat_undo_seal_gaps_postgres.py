"""KRI-1384: "undo that" for the chat ops that did not seal, end to end on real PostgreSQL.

Each case seeds the parity probe's own variant (``author_action_fixtures``) as a real job, runs
the chat turn and then "Undo that" through ``run_kria_turn`` from a phone at protocol 50 that
staged the head. Only the copilot model is scripted.

Failure modes, written before the code:

U1  A restore that must take a key away (a caption line override, the all-lines size and
    position, the captions' animation default) keeps the chat's value: a commit's None means
    "leave it", so the merge drops the delete.
U2  The delete takes away a key the creator wrote by hand after the chat turn. A hand edit to a
    key the undo restores refuses the undo; a hand edit to another key survives it.
U3  The delete takes away what the creator had before the turn (their unsaved override on
    another line): a key with a pre-turn value is put back, only a key with none is deleted.
U4  Picture rows read off the variant lack the defaults the replayed rows carry, so an exact
    restore compares unequal and the undo is refused (or, compared loosely, a real difference
    passes). Both sides go through the commit schema, the Save's own persistence.
U5  ``orientation`` / ``landscape_fit`` belong to no plan-review section, so the record is empty
    and the turn cannot be undone.
U6  The seal checks a replay that is not what the commit writes: both read one ``_replay``.
U7  A hand edit to the lane after the chat turn is overwritten (the F1 rule): refused, nothing
    written.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Any

import pytest

from app.database import engine as async_engine
from app.database import sync_session
from app.models import CreatorAgentSession, Job, PlanItem
from app.services import kria_editor_ops
from tests.evals.request_following.author_action_fixtures import (
    PHONE_CAPS,
    PHONE_OP_PROBES,
    SCENARIOS,
    Scenario,
    build,
)
from tests.kria.test_chat_undo_postgres import (  # noqa: F401 - fixtures
    UNDO,
    _flags,
    _head,
    _record,
    _reply,
    _say,
    copilot,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

GEN = "generation-1"


def _probe(group: str, op: dict[str, Any]) -> Scenario:
    return next(
        p for p in (*SCENARIOS, *PHONE_OP_PROBES) if p.group == group and p.reference == [op]
    )


@dataclasses.dataclass(frozen=True)
class Case:
    probe: Scenario
    lane: str
    view: str  # the snapshot section the copilot reads this lane in
    hand_edit: Any  # the lane value a creator's hand edit leaves after the chat turn


def _line_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {**meta, "appearance": {"line_overrides": {"cue-2": {"size_px": 120}}}}


def _swapped(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**row, "z": 5 if row["id"] == "photo-1" else row.get("z")} for row in rows]


CASES = {
    "caption line size": Case(
        _probe(
            "caption_style_scope", {"op": "patch_caption_line", "cue_id": "cue-2", "size_px": 96}
        ),
        "caption_meta",
        "captions",
        _line_meta,
    ),
    "all caption lines": Case(
        _probe(
            "caption_style_scope",
            {"op": "patch_caption_line", "all": True, "size_px": 64, "position_y_frac": 0.8},
        ),
        "caption_meta",
        "captions",
        lambda meta: {**meta, "size_px": 70},
    ),
    "caption animation default": Case(
        _probe(
            "caption_animation",
            {"op": "set_caption_meta", "patch": {"animation_phases": {"entrance": "pop"}}},
        ),
        "caption_meta",
        "captions",
        lambda meta: {**meta, "animation_phases": {**meta["animation_phases"], "entrance": "fade"}},
    ),
    "picture layer order": Case(
        _probe("layer_order", {"op": "set_layer_order", "layer": "photo-2", "z": 0}),
        "visual_blocks",
        "layer_order",
        _swapped,
    ),
    "orientation": Case(
        _probe("video_shape", {"op": "set_video_shape", "orientation": "landscape"}),
        "orientation",
        "video_shape",
        lambda _value: "portrait",
    ),
    "landscape fit": Case(
        _probe("video_shape", {"op": "set_video_shape", "landscape_fit": "fit"}),
        "landscape_fit",
        "video_shape",
        lambda _value: "fill",
    ),
}


def _seed(user_id, session_id, probe: Scenario, monkeypatch) -> dict[str, Any]:  # noqa: ANN001
    """The probe's variant as the rendered variant of a real job; its capabilities pinned."""
    built = build(probe)
    variant = json.loads(json.dumps(built["variant"]))
    variant.update(
        variant_id="original_text",
        render_status="ready",
        render_generation_id=GEN,
        render_finished_at="2026-09-07T08:00:00Z",
        video_path="generative-jobs/test/output.mp4",
        base_video_path="generative-jobs/test/base.mp4",
    )
    caps = {**PHONE_CAPS, **probe.caps}

    def capabilities(_job: Any, shown: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401
        out = json.loads(json.dumps(caps))
        if isinstance(out.get("landscape_fit"), dict):
            # The real map reads the fit off the (projected) variant (`cloud_fit_axis`).
            out["landscape_fit"]["value"] = shown.get("landscape_fit") or "fill"
        return out

    monkeypatch.setattr(kria_editor_ops, "_editor_capabilities", capabilities)
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        item = db.get(PlanItem, session.plan_item_id, with_for_update=True)
        job = Job(
            id=uuid.uuid4(),
            user_id=user_id,
            status="variants_ready",
            mode="generative",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            all_candidates=built["job"].all_candidates,
            assembly_plan={"variants": [variant]},
        )
        db.add(job)
        db.flush()
        item.current_job_id = job.id
        session.target_job_id = job.id
        session.target_variant_id = "original_text"
        session.target_generation_id = GEN
        session.manifest_hash = "a" * 64
        db.commit()
    return variant


def _clean(sid: str) -> dict[str, Any]:
    return {"base_generation": GEN, "client_state_id": sid, "lanes": {}}


def _phone(head: dict[str, Any], lane: str, *, sid: str, value: Any = None) -> dict[str, Any]:  # noqa: ANN401
    """What a phone at protocol 50 sends after staging ``head``'s ``lane`` (or a hand edit)."""
    wrote = head["snap"]["editor_payload"][lane]
    return {
        "base_generation": GEN,
        "client_state_id": sid,
        "draft_revision": head["rev"],
        "lanes": {lane: wrote if value is None else value},
    }


def _lane(head: dict[str, Any], lane: str) -> Any:  # noqa: ANN401
    return head["snap"]["editor_payload"].get(lane)


async def _chat_then_undo(copilot, case: Case, user_id, thread_id) -> tuple[dict, dict]:  # noqa: ANN001, F811
    copilot.answers = {"Do it": case.probe.reference, "Undo that": [UNDO], "Look now": []}
    await _say(user_id, thread_id, "Do it", _clean("s0"))
    edited = _head(thread_id)
    assert _record(edited["id"])["chat_undo"], "the turn is sealed (undoable from chat)"
    await _say(user_id, thread_id, "Undo that", _phone(edited, case.lane, sid="s1"))
    return edited, _head(thread_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CASES))
async def test_undo_puts_back_exactly_what_the_creator_had(copilot, monkeypatch, name) -> None:  # noqa: F811
    case = CASES[name]
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed(user_id, session_id, case.probe, monkeypatch)
    try:
        edited, undone = await _chat_then_undo(copilot, case, user_id, thread_id)
        before = copilot.snapshots["Do it"][case.view]
        assert copilot.snapshots["Undo that"][case.view] != before, "the chat turn changed it"
        assert copilot.snapshots["Undo that"]["history_state"] == {"can_undo_last_turn": True}
        assert undone["rev"] > edited["rev"] and undone["gen"] == edited["gen"]
        assert _reply(thread_id).content.startswith("I undid my last change"), name

        await _say(user_id, thread_id, "Look now", _phone(undone, case.lane, sid="s2"))
        assert copilot.snapshots["Look now"][case.view] == before, f"{name}: U1/U4/U5"
        assert _record(undone["id"]) is None, "an undo stores no record"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CASES))
async def test_a_hand_edit_after_the_chat_turn_takes_the_undo_away(
    copilot,  # noqa: F811
    monkeypatch,
    name,
) -> None:
    case = CASES[name]
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed(user_id, session_id, case.probe, monkeypatch)
    copilot.answers = {"Do it": case.probe.reference, "Undo that": [UNDO]}
    try:
        await _say(user_id, thread_id, "Do it", _clean("s0"))
        edited = _head(thread_id)
        hand = case.hand_edit(json.loads(json.dumps(_lane(edited, case.lane))))
        assert hand != _lane(edited, case.lane)
        await _say(user_id, thread_id, "Undo that", _phone(edited, case.lane, sid="s1", value=hand))
        assert "history_state" not in copilot.snapshots["Undo that"], f"{name}: U2/U7"
        assert _head(thread_id)["id"] == edited["id"], "nothing was written"
    finally:
        await async_engine.dispose()


LINE = CASES["caption line size"]


@pytest.mark.asyncio
async def test_the_undo_keeps_a_caption_override_the_creator_had_before_the_turn(
    copilot,  # noqa: F811
    monkeypatch,
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed(user_id, session_id, LINE.probe, monkeypatch)
    mine = {"appearance": {"line_overrides": {"cue-1": {"size_px": 80}}}}
    copilot.answers = {"Do it": LINE.probe.reference, "Undo that": [UNDO]}
    try:
        await _say(
            user_id,
            thread_id,
            "Do it",
            {"base_generation": GEN, "client_state_id": "s0", "lanes": {"caption_meta": mine}},
        )
        edited = _head(thread_id)
        overrides = _lane(edited, "caption_meta")["appearance"]["line_overrides"]
        assert set(overrides) == {"cue-1", "cue-2"}
        await _say(user_id, thread_id, "Undo that", _phone(edited, "caption_meta", sid="s1"))
        meta = _lane(_head(thread_id), "caption_meta")
        assert meta["appearance"] == mine["appearance"], "U3: cue-1 stays, only cue-2 goes"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_hand_edit_to_another_caption_key_survives_the_delete(
    copilot,  # noqa: F811
    monkeypatch,
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed(user_id, session_id, LINE.probe, monkeypatch)
    copilot.answers = {"Do it": LINE.probe.reference, "Undo that": [UNDO]}
    try:
        await _say(user_id, thread_id, "Do it", _clean("s0"))
        edited = _head(thread_id)
        recoloured = {**_lane(edited, "caption_meta"), "color": "#FFCC00"}
        await _say(
            user_id,
            thread_id,
            "Undo that",
            _phone(edited, "caption_meta", sid="s1", value=recoloured),
        )
        assert copilot.snapshots["Undo that"]["history_state"]["can_undo_last_turn"] is True
        meta = _lane(_head(thread_id), "caption_meta")
        assert "appearance" not in meta, "U1: the chat's line override is gone"
        assert meta["color"] == "#FFCC00", "U2: the creator's colour stays"
    finally:
        await async_engine.dispose()


SONG = _probe("creator_song", {"op": "set_user_song", "volume": 0.6})


@pytest.mark.asyncio
async def test_a_song_change_is_not_undone_from_chat(
    copilot,  # noqa: F811
    monkeypatch,
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed(user_id, session_id, SONG, monkeypatch)
    copilot.answers = {"Do it": SONG.reference, "Undo that": [UNDO]}
    try:
        await _say(user_id, thread_id, "Do it", _clean("s0"))
        edited = _head(thread_id)
        assert "user_song" in edited["snap"]["editor_payload"], "the phone reads the flat key"
        assert _record(edited["id"]) is None or "chat_undo" not in _record(edited["id"])
        await _say(user_id, thread_id, "Undo that", _phone(edited, "user_song", sid="s1"))
        assert "history_state" not in copilot.snapshots["Undo that"], "refused, not half undone"
        assert _head(thread_id)["id"] == edited["id"], "nothing was written"
    finally:
        await async_engine.dispose()
