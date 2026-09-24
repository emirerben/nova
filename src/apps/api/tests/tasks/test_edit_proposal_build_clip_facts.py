"""KRI-189: clip facts flow through proposal drafting, gated by CLIP_FACTS."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import app.tasks.edit_proposal_build as proposal_build
from app.config import settings as app_settings
from app.schemas.clip_understanding import ClipFact
from app.schemas.edit_proposal import MediaRef, ProposalBrief, parse_edit_proposal
from app.services import clip_facts as cf
from tests.tasks.test_edit_proposal_build import (
    _PROD_CLIP_ASSIGNMENT,
    _Db,
    _prepare_terminal_agent_attempt,
    _prod_item,
    _proposal,
    _Result,
)

_CAPTURE = {
    "capture_time": "2026-09-20T07:31:02Z",
    "coarse_location": {"lat": 41.19, "lon": 28.74},
    "place": {"sub_locality": "Arnavutköy", "locality": "İstanbul", "country": "Türkiye"},
}
_MEDIA_ID = str(_PROD_CLIP_ASSIGNMENT["media_id"])


def _draft(monkeypatch, *, enabled: bool):
    """Run one draft attempt and return (planner input, persisted proposal)."""
    item_id, item = _prepare_terminal_agent_attempt(monkeypatch)
    item.clip_assignments[0]["capture"] = dict(_CAPTURE)
    seen: dict[str, object] = {}

    def _run(agent, agent_input, ctx=None):  # noqa: ANN001, ARG001
        seen["input"] = agent_input
        return agent.parse(
            json.dumps(
                {
                    "title": "T",
                    "duration_s": 6,
                    "story_beats": [
                        {
                            "topic": "Topic",
                            "thought": "A visible detail worth noting.",
                            "media_ids": [_MEDIA_ID],
                            "duration_s": 6,
                        }
                    ],
                }
            ),
            agent_input,
        )

    monkeypatch.setattr("app.agents.edit_proposal.EditProposalAgent.run", _run)
    monkeypatch.setattr(app_settings, "clip_facts_enabled", enabled)
    monkeypatch.setattr(app_settings, "clip_facts_user_ids", [])
    monkeypatch.setattr(
        cf,
        "_guess_landmark",
        lambda assignment, *, ctx: ClipFact(
            kind="landmark", value="Rumeli Hisarı", provenance="inferred", confidence=0.8
        ),
    )
    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=False
    )
    return seen["input"], parse_edit_proposal(item.edit_proposal)


def test_planner_sees_capture_place_and_landmark_facts_when_enabled(monkeypatch) -> None:
    agent_input, _proposal = _draft(monkeypatch, enabled=True)

    assert agent_input.clip_facts is True
    [media] = agent_input.media
    by_kind = {fact["kind"]: fact for fact in media.facts}
    assert by_kind["capture_time"] == {
        "kind": "capture_time",
        "value": "2026-09-20T07:31:02Z",
        "provenance": "exif",
    }
    assert by_kind["place"]["value"] == "Arnavutköy, İstanbul, Türkiye"
    assert by_kind["place"]["provenance"] == "geocode"
    assert by_kind["landmark"]["value"] == "Rumeli Hisarı"
    assert by_kind["landmark"]["provenance"] == "inferred"


def test_planner_input_is_unchanged_when_disabled(monkeypatch) -> None:
    agent_input, proposal = _draft(monkeypatch, enabled=False)

    assert agent_input.clip_facts is False
    [media] = agent_input.media
    assert media.facts == []
    assert "facts" not in media.model_dump()
    assert proposal.ordering is None


def test_landmark_agent_never_runs_when_disabled(monkeypatch) -> None:
    item_id, _item = _prepare_terminal_agent_attempt(monkeypatch)
    monkeypatch.setattr(app_settings, "clip_facts_enabled", False)
    monkeypatch.setattr(app_settings, "clip_facts_user_ids", [])
    monkeypatch.setattr(
        cf, "_guess_landmark", lambda *a, **k: pytest.fail("landmark agent ran with the flag off")
    )
    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=False
    )


def _day_vlog_draft(monkeypatch, *, enabled: bool):
    """Three clips attached out of filming order, planned as a day vlog."""
    item_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    hours = {"late": 11, "early": 7, "mid": 9}
    assignments = [
        {
            "media_id": name,
            "gcs_path": f"users/u/plan/{item_id}/clips/{name}.mp4",
            "capture": {"capture_time": f"2026-09-20T{hour:02d}:00:00Z"},
        }
        for name, hour in hours.items()
    ]
    item = _prod_item(item_id, clip_assignments=assignments)
    item.edit_proposal = _proposal(
        brief=ProposalBrief(direction="guided_story", duration_s=24, story_shape="day_vlog"),
    )
    db = _Db(_Result(rows=[]))

    @contextmanager
    def _session():
        yield db

    refs = [
        MediaRef(
            lane="clip",
            media_id=a["media_id"],
            gcs_path=a["gcs_path"],
            generation="1",
            kind="video",
            duration_s=8,
        )
        for a in assignments
    ]
    monkeypatch.setattr(proposal_build, "sync_session", _session)
    monkeypatch.setattr(proposal_build, "_locked_item", lambda *_a, **_kw: (item, owner_id))
    monkeypatch.setattr(proposal_build, "_attempt_is_active", lambda *_a, **_kw: True)
    monkeypatch.setattr(proposal_build, "_pool_refs", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        proposal_build,
        "_analyze_clip_assignments",
        lambda clip_assignments, *_a, **_kw: list(zip(clip_assignments, refs, strict=True)),
    )
    monkeypatch.setattr(proposal_build, "media_generations_match_sync", lambda _refs: True)
    monkeypatch.setattr(app_settings, "clip_facts_enabled", enabled)
    monkeypatch.setattr(app_settings, "clip_facts_user_ids", [])
    monkeypatch.setattr(cf, "_guess_landmark", lambda *a, **k: None)

    def _run(agent, agent_input, ctx=None):  # noqa: ANN001, ARG001
        beats = [
            {
                "topic": f"Stop {name}",
                "thought": f"The {name} stop of the run.",
                "media_ids": [name],
                "duration_s": 8,
            }
            for name in hours  # the model echoes attachment order
        ]
        return agent.parse(
            json.dumps({"title": "Run", "duration_s": 24, "story_beats": beats}), agent_input
        )

    monkeypatch.setattr("app.agents.edit_proposal.EditProposalAgent.run", _run)
    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=False
    )
    proposal = parse_edit_proposal(item.edit_proposal)
    return proposal, [beat.media_ids[0] for beat in proposal.draft.story_beats]


def test_day_vlog_draft_is_ordered_by_capture_time_and_records_the_basis(monkeypatch) -> None:
    proposal, order = _day_vlog_draft(monkeypatch, enabled=True)

    assert order == ["early", "mid", "late"]
    assert proposal.ordering == {
        "ordering_basis": "capture_time",
        "ordering_fallback_clip_ids": [],
    }


def test_day_vlog_draft_is_attachment_order_with_no_ordering_record_when_disabled(
    monkeypatch,
) -> None:
    proposal, order = _day_vlog_draft(monkeypatch, enabled=False)

    assert order == ["late", "early", "mid"]
    assert proposal.ordering is None


def test_a_filming_order_request_the_planner_did_not_apply_is_recorded_as_not_applied(
    monkeypatch,
) -> None:
    """Semantic/snapshot planners never read facts; the receipt must not read as honored."""
    from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent

    item_id, item = _prepare_terminal_agent_attempt(monkeypatch)
    item.clip_assignments[0]["capture"] = dict(_CAPTURE)
    intent = ResolvedClipIntent(
        intent_id="i-order",
        op="order",
        attribute="the order I filmed them",
        order_by="capture_time",
        assignments=[ClipAssignment(media_id=_MEDIA_ID, confidence=1.0)],
    )
    item.edit_proposal = {
        **item.edit_proposal,
        "brief": {
            **item.edit_proposal["brief"],
            "clip_intents": [intent.model_dump(mode="json")],
        },
    }

    def _run(agent, agent_input, ctx=None):  # noqa: ANN001, ARG001
        output = agent.parse(
            json.dumps(
                {
                    "title": "T",
                    "duration_s": 6,
                    "story_beats": [
                        {
                            "topic": "Topic",
                            "thought": "A visible detail worth noting.",
                            "media_ids": [_MEDIA_ID],
                            "duration_s": 6,
                        }
                    ],
                }
            ),
            agent_input,
        )
        output.ordering = None  # a planner path that never ran the capture-time ordering
        return output

    monkeypatch.setattr("app.agents.edit_proposal.EditProposalAgent.run", _run)
    monkeypatch.setattr(app_settings, "clip_facts_enabled", True)
    monkeypatch.setattr(app_settings, "clip_facts_user_ids", [])
    monkeypatch.setattr(app_settings, "clip_intents_enabled", True)
    monkeypatch.setattr(cf, "_guess_landmark", lambda *a, **k: None)
    proposal_build._run_draft_attempt(
        SimpleNamespace(), item_id, str(item_id), "attempt-1", 0, auto_finalize=False
    )
    proposal = parse_edit_proposal(item.edit_proposal)
    assert proposal.ordering == {
        "ordering_basis": "not_applied",
        "ordering_fallback_clip_ids": [],
        "reason": "planner_path",
    }
