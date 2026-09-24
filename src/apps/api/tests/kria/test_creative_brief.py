"""KRI-188: Creative Brief ledger, scope router, receipts, and flag-off pins."""

from __future__ import annotations

import json
import unicodedata
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import (
    AskUser,
    CapabilityAvailability,
    ResolvedCreatorManifest,
)
from app.agents.main_creator import (
    _BRIEF_PROMPT_SECTION,
    MAIN_CREATOR_PROMPT_VERSION,
    MainCreatorAgent,
    MainCreatorInput,
)
from app.config import Settings, settings
from app.kria import planner
from app.kria.brief import (
    BriefRequirement,
    BriefUpdate,
    CreativeBrief,
    CurrentPlanShape,
    apply_receipt_statuses,
    apply_updates,
    merge_requirements,
    new_requirements,
    parse_brief_updates,
    plan_shape_from_editor_snapshot,
    render_brief_request,
    route_requirements,
    wants_full_replan,
)
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    plan_facts_from_editor_payload,
    plan_facts_from_strategy,
    reply_from_receipts,
)
from app.kria.contracts import KriaObservedTurnResponse, KriaTurnPlan, RequirementReceipt
from app.kria.planner import PlannedKriaTurn, plan_live_turn
from app.models import ContentPlan, Persona, PlanItem
from app.tasks.kria_runtime import _useful_plan, _validate_draft_plan

FIXTURE = Path(__file__).parents[1] / "fixtures" / "kria_turns" / "east-run-brief.json"


def _req(kind: str, scope: str, **kw) -> BriefRequirement:  # noqa: ANN003
    kw.setdefault("description", "x")
    return BriefRequirement(id=kw.pop("id", "r1"), kind=kind, scope=scope, **kw)


def _upd(kind: str, scope: str, **kw) -> BriefUpdate:  # noqa: ANN003
    kw.setdefault("description", "x")
    return BriefUpdate(kind=kind, scope=scope, **kw)


# ------------------------------------------------------------------- ledger


def test_later_requirement_with_same_kind_and_scope_supersedes_earlier() -> None:
    first = apply_updates(None, [_upd("text", "title", literal="Old")], source_turn_id="t1")
    second = apply_updates(
        first,
        [_upd("text", "title", literal="New"), _upd("order", "global")],
        source_turn_id="t2",
    )
    by_id = {req.id: req for req in second.requirements}
    assert by_id["r1"].status == "superseded"
    assert by_id["r2"].literal == "New" and by_id["r2"].status == "open"
    assert by_id["r3"].kind == "order"
    assert [req.id for req in second.live()] == ["r2", "r3"]
    # A superseded item is not carried into the NEXT version.
    third = apply_updates(second, [_upd("timing", "global")], source_turn_id="t3")
    assert "r1" not in {req.id for req in third.requirements}


def test_merge_collapses_duplicate_keys_within_one_update_batch() -> None:
    new = [_req("text", "title", id="r5", literal="A"), _req("text", "title", id="r6", literal="B")]
    merged = merge_requirements([], new)
    assert [req.literal for req in merged] == ["B"]


def test_turkish_text_is_kept_nfc_and_never_folded_to_ascii() -> None:
    decomposed = unicodedata.normalize("NFD", "20K Koşu · Arnavutköy → Eminönü")
    update = _upd("text", "title", literal=decomposed)
    assert update.literal == "20K Koşu · Arnavutköy → Eminönü"
    assert unicodedata.is_normalized("NFC", update.literal)
    assert "ş" in update.literal and "ö" in update.literal


def test_parse_brief_updates_drops_bad_entries_without_raising() -> None:
    parsed = parse_brief_updates(
        [
            {"kind": "text", "scope": "title", "literal": "Hi"},
            {"kind": "bogus", "scope": "title", "literal": "x"},
            {"kind": "text", "scope": "somewhere", "literal": "x"},
            {"kind": "order", "scope": "global"},  # neither literal nor description
            "not-an-object",
        ]
    )
    assert [(u.kind, u.scope) for u in parsed] == [("text", "title")]
    assert parse_brief_updates(None) == []
    assert parse_brief_updates({"kind": "text"}) == []


def test_new_requirements_returns_only_what_this_turn_added() -> None:
    before = apply_updates(None, [_upd("text", "title", literal="A")], source_turn_id="t1")
    after = apply_updates(before, [_upd("order", "global")], source_turn_id="t2")
    assert [req.kind for req in new_requirements(before, after)] == ["order"]
    assert [req.id for req in new_requirements(None, before)] == ["r1"]


def test_receipt_statuses_overlay_live_requirements_only() -> None:
    brief = apply_updates(None, [_upd("text", "title", literal="A")], source_turn_id="t1")
    shown = apply_receipt_statuses(brief, [{"requirement_id": "r1", "status": "partial"}])
    assert shown.requirements[0].status == "partial"


# -------------------------------------------------------------------- router

HAS_RENDER = CurrentPlanShape(has_render=True, has_per_clip_text_lane=False)
WITH_LANE = CurrentPlanShape(has_render=True, has_per_clip_text_lane=True)


@pytest.mark.parametrize(
    ("reqs", "shape", "message", "expected"),
    [
        ([_upd("order", "global")], HAS_RENDER, None, "replan"),
        ([_upd("select", "global")], WITH_LANE, None, "replan"),
        ([_upd("text", "per_clip")], HAS_RENDER, None, "replan"),
        ([_upd("text", "clip:c1")], HAS_RENDER, None, "replan"),
        ([_upd("text", "per_clip")], WITH_LANE, None, "editor_ops"),
        ([_upd("text", "title", literal="x"), _upd("timing", "global")], WITH_LANE, None, "replan"),
        ([_upd("text", "title", literal="New title")], HAS_RENDER, None, "editor_ops"),
        ([_upd("style", "global")], HAS_RENDER, None, "editor_ops"),
        ([], HAS_RENDER, "make the title bigger", "editor_ops"),
        ([], HAS_RENDER, "Do it again based on my prompt", "replan"),
        ([_upd("style", "global")], CurrentPlanShape(has_render=False), None, "replan"),
        ([], None, "anything", "replan"),
    ],
)
def test_router_table(reqs, shape, message, expected) -> None:  # noqa: ANN001
    assert route_requirements(reqs, shape, message=message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "do it again",
        "Create the video again",
        "redo this",
        "start over please",
        "please base it on the brief: based on my prompt",
        "tekrar yap",
        "Baştan kes",
    ],
)
def test_redo_phrases_are_a_supported_replan(message: str) -> None:
    assert wants_full_replan(message)


@pytest.mark.parametrize(
    "message", ["make the text bigger", "shorter intro", "yaz rengini değiştir"]
)
def test_ordinary_edits_are_not_replans(message: str) -> None:
    assert not wants_full_replan(message)


def test_plan_shape_reads_per_clip_lane_from_editor_snapshot() -> None:
    assert plan_shape_from_editor_snapshot(None).has_render is False
    plain = plan_shape_from_editor_snapshot({"text_bars": [{"id": "t", "role": "title"}]})
    assert plain.has_render and not plain.has_per_clip_text_lane
    lane = plan_shape_from_editor_snapshot({"text_bars": [{"id": "t", "role": "shot_label"}]})
    assert lane.has_per_clip_text_lane


def test_render_brief_request_lists_every_live_requirement_not_chip_text() -> None:
    brief = apply_updates(
        None,
        [
            _upd("text", "title", literal="20K Koşu"),
            _upd("text", "per_clip", description="the landmark in each clip", literal=None),
        ],
        source_turn_id="t1",
    )
    text = render_brief_request(brief, latest_message="Do it again based on my prompt")
    assert '"20K Koşu"' in text
    assert "landmark in each clip" in text
    assert text.endswith("Latest message: Do it again based on my prompt")
    assert render_brief_request(None, latest_message="hi") == "hi"


# ------------------------------------------------------------------ checkers

_CLIPS = ("c1", "c2", "c3")


def _strategy(**kw):  # noqa: ANN003, ANN202
    base = {"edit_format": "montage", "target_duration_s": 20.0}
    base.update(kw)
    return base


def _labels(media_ids, grounding="creator_text"):  # noqa: ANN001, ANN202
    return [
        {
            "intent_id": "l",
            "op": "label",
            "attribute": "landmark",
            "assignments": [
                {"media_id": m, "value": f"V{m}", "grounding": grounding} for m in media_ids
            ],
        }
    ]


def test_per_clip_text_full_partial_and_none() -> None:
    req = _req("text", "per_clip")
    full = plan_facts_from_strategy(
        _strategy(resolved_clip_intents=_labels(_CLIPS)), clip_ids=_CLIPS
    )
    assert check_requirement(req, full).status == "met"
    part = plan_facts_from_strategy(
        _strategy(resolved_clip_intents=_labels(_CLIPS[:2])), clip_ids=_CLIPS
    )
    receipt = check_requirement(req, part)
    assert receipt.status == "partial" and "2 of 3" in (receipt.reason or "")
    none = plan_facts_from_strategy(_strategy(), clip_ids=_CLIPS)
    assert check_requirement(req, none).status == "not_possible"


def test_footage_derived_labels_are_reported_as_inferred() -> None:
    req = _req("text", "per_clip")
    facts = plan_facts_from_strategy(
        _strategy(resolved_clip_intents=_labels(_CLIPS, grounding="vision_verified")),
        clip_ids=_CLIPS,
    )
    receipt = check_requirement(req, facts)
    assert receipt.status == "met"
    assert receipt.inferred == ["Vc1", "Vc2", "Vc3"]


def test_order_check_uses_basis_and_reports_fallback_clips() -> None:
    req = _req("order", "global", facts={"key": "capture_time"})
    assert check_requirement(req, PlanFacts()).status == "partial"
    assert check_requirement(req, PlanFacts(ordering_basis="attachment_order")).status == "partial"
    assert check_requirement(req, PlanFacts(ordering_basis="capture_time")).status == "met"
    fallback = check_requirement(
        req, PlanFacts(ordering_basis="capture_time", ordering_fallback_clip_ids=("c2",))
    )
    assert fallback.status == "partial" and "1 clip" in (fallback.reason or "")
    day_vlog = plan_facts_from_strategy(_strategy(archetype="day_vlog"))
    assert check_requirement(req, day_vlog).status == "met"


@pytest.mark.parametrize(
    ("actual", "status"),
    [(20.0, "met"), (21.9, "met"), (18.1, "met"), (22.5, "partial"), (17.0, "partial")],
)
def test_duration_within_ten_percent(actual: float, status: str) -> None:
    req = _req("timing", "global", facts={"duration_s": 20})
    assert check_requirement(req, PlanFacts(duration_s=actual)).status == status
    assert check_requirement(req, PlanFacts()).status == "partial"


def test_literal_title_must_match_exactly_after_normalization() -> None:
    req = _req("text", "title", literal="20K Koşu")
    assert check_requirement(req, PlanFacts(title="20k  koşu")).status == "met"
    assert check_requirement(req, PlanFacts(title="20K Kosu")).status == "partial"


def test_editor_payload_literal_text_is_found() -> None:
    req = _req("text", "title", literal="Yeni Başlık")
    facts = plan_facts_from_editor_payload({"title": {"text": "Yeni Başlık"}})
    assert check_requirement(req, facts).status == "met"
    assert (
        check_requirement(req, plan_facts_from_editor_payload({"title": "x"})).status == "partial"
    )


def test_requirement_without_a_checker_is_never_reported_met() -> None:
    receipt = check_requirement(_req("audio", "global"), PlanFacts())
    assert receipt.status == "partial"


def test_reply_never_claims_an_unmet_requirement() -> None:
    brief = apply_updates(
        None,
        [_upd("text", "per_clip"), _upd("order", "global", facts={"key": "capture_time"})],
        source_turn_id="t",
    )
    receipts = build_receipts(brief.live(), PlanFacts(clip_ids=_CLIPS))
    reply = reply_from_receipts(brief, receipts, summary="I rebuilt the whole edit around you.")
    assert "rebuilt the whole edit" not in reply  # the model summary is dropped
    assert reply.startswith("Not everything you asked for made it in")
    assert "Couldn't:" in reply and "Partly:" in reply
    assert len(reply) <= 1200


def test_reply_keeps_summary_only_when_everything_is_met() -> None:
    brief = apply_updates(None, [_upd("text", "title", literal="Hi")], source_turn_id="t")
    receipts = build_receipts(brief.live(), PlanFacts(title="Hi"))
    reply = reply_from_receipts(brief, receipts, summary="Opening on the title.")
    assert reply.startswith("Opening on the title.\n- Done:")


# -------------------------------------------------------------- East Run replay


def test_east_run_replay_carries_every_requirement_in_one_replan_with_receipts() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    shape = CurrentPlanShape(**fixture["plan_shape"])
    brief: CreativeBrief | None = None
    for index, turn in enumerate(fixture["turns"], start=1):
        updates = parse_brief_updates(turn["brief_updates"])
        assert len(updates) == len(turn["brief_updates"]), "fixture updates must all parse"
        effective = apply_updates(brief, updates, source_turn_id=f"turn-{index}")
        route = route_requirements(
            new_requirements(brief, effective), shape, message=turn["user_message"]
        )
        assert route == turn["expected_route"], turn["user_message"]
        assert [req.id for req in effective.live()] == turn["expected_live_ids"]
        brief = effective

    assert brief is not None
    # The re-plan's request is rendered from the brief: the landmark ask, typed
    # after the render in the original thread, is present verbatim.
    request = render_brief_request(brief, latest_message="Do it again based on my prompt")
    assert "the landmark or place shown in each clip" in request
    assert "20K Koşu · Arnavutköy → Eminönü" in request
    assert "in the order I filmed them" in request

    facts = plan_facts_from_strategy(fixture["replan_strategy"], clip_ids=fixture["clip_ids"])
    receipts = build_receipts(brief.live(), facts)
    assert [r.requirement_id for r in receipts] == [r.id for r in brief.live()]
    for receipt in receipts:
        expected = fixture["expected_receipts"][receipt.requirement_id]
        assert receipt.status == expected["status"], receipt
        assert receipt.inferred == expected["inferred"], receipt
    reply = reply_from_receipts(brief, receipts, summary="One pass through Bosphorus.")
    for req in brief.live():
        assert req.text() in reply
    assert "I guessed these" in reply and "Bebek" in reply


# --------------------------------------------------- draft validation + planner


def _editor_plan() -> KriaTurnPlan:
    return KriaTurnPlan.model_validate(
        {
            "mode": "act",
            "turn_value": "action",
            "intents": [
                {
                    "intent_id": "apply-editor-ops",
                    "tool_name": "draft.apply_editor_ops",
                    "tool_version": 1,
                    "arguments": {"operations": [{"op": "edit_text"}], "summary": "s"},
                }
            ],
        }
    )


def _strategy_plan() -> KriaTurnPlan:
    return KriaTurnPlan.model_validate(
        {
            "mode": "act",
            "turn_value": "action",
            "intents": [
                {
                    "intent_id": "apply-strategy",
                    "tool_name": "draft.apply_strategy",
                    "tool_version": 1,
                    "arguments": {},
                }
            ],
        }
    )


def test_validate_draft_plan_rejects_editor_ops_when_router_says_replan() -> None:
    _validate_draft_plan(_editor_plan())  # legacy call shape unchanged
    _validate_draft_plan(_editor_plan(), required_route="editor_ops")
    _validate_draft_plan(_strategy_plan(), required_route="replan")
    with pytest.raises(RuntimeError, match="new plan"):
        _validate_draft_plan(_editor_plan(), required_route="replan")


def test_useful_plan_keeps_brief_fields_when_it_rewrites_a_plan() -> None:
    update = _upd("text", "title", literal="Hi")
    planned = PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="respond", turn_value="question", response="Make a 20K Koşu video for me"
        ),
        manifest_hash="m",
        context_hash="c",
        brief_updates=(update,),
        brief_route="replan",
        brief_clip_ids=("c1",),
    )
    out = _useful_plan(planned, user_message="Make a 20K Koşu video for me")
    assert out.plan.response != planned.plan.response
    assert (out.brief_updates, out.brief_route, out.brief_clip_ids) == (
        (update,),
        "replan",
        ("c1",),
    )
    assert replace(out, brief_route=None).brief_updates == (update,)


def test_observed_response_omits_empty_receipts_but_keeps_real_ones() -> None:
    plain = KriaObservedTurnResponse(turn_value="action", message="ok")
    assert "requirement_receipts" not in plain.model_dump(mode="json")
    with_receipts = KriaObservedTurnResponse(
        turn_value="action",
        message="ok",
        requirement_receipts=[RequirementReceipt(requirement_id="r1", status="met")],
    )
    assert with_receipts.model_dump(mode="json")["requirement_receipts"][0]["status"] == "met"


# ------------------------------------------------------------ flag + allowlist


def test_creative_brief_flag_and_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    user = uuid.uuid4()
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", False)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    assert settings.creative_brief_for(user) is False
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [str(user)])
    assert settings.creative_brief_for(user) is True
    assert settings.creative_brief_for(uuid.uuid4()) is False
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    assert settings.creative_brief_for(uuid.uuid4()) is True
    assert Settings.model_fields["kria_creative_brief_enabled"].default is False


def test_allowlist_env_accepts_comma_separated_and_json() -> None:
    parse = Settings.parse_creative_brief_user_ids
    assert parse("a, b") == ["a", "b"]
    assert parse('["a","b"]') == ["a", "b"]
    assert parse("") == []


# -------------------------------------------------- main_creator prompt + parse

_MANIFEST = ResolvedCreatorManifest(
    item_id=str(uuid.uuid4()),
    edit_format="montage",
    render_program="guided",
    capabilities={"dispatch_render": CapabilityAvailability(available=True)},
    context_hash="a" * 64,
    manifest_hash="b" * 64,
)
_ASK = {
    "kind": "ask_user",
    "question": "Which moment should open the edit?",
    "reason_code": "pacing_choice",
    "options": ["Start", "Finish"],
}


def _agent_input(**kw) -> MainCreatorInput:  # noqa: ANN003
    return MainCreatorInput(user_message="Title it 20K Koşu", capability_manifest=_MANIFEST, **kw)


def test_prompt_is_unchanged_when_brief_is_off_and_taught_when_on() -> None:
    agent = MainCreatorAgent(object())
    off = agent.render_prompt(_agent_input())
    on = agent.render_prompt(_agent_input(brief_enabled=True))
    assert "CREATIVE BRIEF" not in off and "brief_updates" not in off
    assert "$brief_section" not in off
    assert on.count("CREATIVE BRIEF") == 1 and "brief_updates" in on
    # Removing the section from the flag-on prompt yields exactly the flag-off prompt.
    assert on.replace("\n" + _BRIEF_PROMPT_SECTION, "") == off


def test_prompt_version_was_bumped_for_the_brief_section() -> None:
    assert MAIN_CREATOR_PROMPT_VERSION == "2026-09-24-v37"


def test_parse_reads_brief_updates_only_when_enabled_and_never_fails_on_bad_ones() -> None:
    agent = MainCreatorAgent(object())
    raw = json.dumps(
        {
            "action": _ASK,
            "brief_updates": [
                {"kind": "text", "scope": "title", "literal": "20K Koşu"},
                {"kind": "nonsense"},
            ],
        }
    )
    off = agent.parse(raw, _agent_input())
    assert off.brief_updates == []
    assert "brief_updates" not in off.model_dump(mode="json")
    on = agent.parse(raw, _agent_input(brief_enabled=True))
    assert [(u.kind, u.scope, u.literal) for u in on.brief_updates] == [
        ("text", "title", "20K Koşu")
    ]


# ------------------------------------------------------- planner routing (flag on)


def _planner_db(item):  # noqa: ANN001, ANN202
    plan_id = item.content_plan_id
    creator_id = uuid.uuid4()
    content_plan = SimpleNamespace(id=plan_id, user_id=creator_id, persona_id=uuid.uuid4())
    persona = SimpleNamespace(user_id=creator_id)

    async def get(model, _identifier, **_kwargs):  # noqa: ANN001, ANN202
        return {PlanItem: item, ContentPlan: content_plan, Persona: persona}[model]

    db = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        ),
        rollback=AsyncMock(),
        commit=AsyncMock(),
    )
    return db, creator_id


def _wire_planner(monkeypatch, *, output, editor_plan, snapshot):  # noqa: ANN001, ANN202
    item = SimpleNamespace(
        id=uuid.uuid4(),
        content_plan_id=uuid.uuid4(),
        current_job_id=uuid.uuid4(),
        edit_format="montage",
    )
    db, creator_id = _planner_db(item)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    manifest = _MANIFEST.model_copy(update={"item_id": str(item.id)})
    monkeypatch.setattr(
        planner, "resolve_item_creator_context", AsyncMock(return_value=(manifest, []))
    )
    monkeypatch.setattr(planner, "creator_context", lambda *_a: ("creator", "item"))
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=None))
    target = (
        None
        if snapshot is None
        else SimpleNamespace(job_id=item.current_job_id, snapshot=snapshot, conversation=[])
    )
    monkeypatch.setattr(planner, "_load_editor_target", AsyncMock(return_value=target))
    copilot = AsyncMock(return_value=editor_plan)
    monkeypatch.setattr(planner, "_plan_editor_revision", copilot)
    runs = []

    class FakeAgent:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, agent_input, **_kwargs):  # noqa: ANN001, ANN003, ANN201
            runs.append(agent_input)
            return output

    monkeypatch.setattr(planner, "MainCreatorAgent", FakeAgent)
    monkeypatch.setattr(planner, "default_client", lambda: object())
    return db, item, creator_id, copilot, runs


@pytest.mark.asyncio
async def test_order_requirement_skips_the_copilot_and_replans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = SimpleNamespace(
        action=AskUser(**_ASK),
        brief_updates=[_upd("order", "global", facts={"key": "capture_time"})],
    )
    db, item, creator_id, copilot, runs = _wire_planner(
        monkeypatch,
        output=output,
        editor_plan=_editor_plan(),
        snapshot={"text_bars": []},
    )
    result = await plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item.id,
        creator_id=creator_id,
        user_message="Order them by when I filmed them",
    )
    copilot.assert_not_awaited()
    assert result.brief_route == "replan"
    assert [(u.kind, u.scope) for u in result.brief_updates] == [("order", "global")]
    assert runs and runs[0].brief_enabled is True


@pytest.mark.asyncio
async def test_plain_title_edit_still_goes_to_the_editor_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = SimpleNamespace(
        action=AskUser(**_ASK), brief_updates=[_upd("text", "title", literal="New")]
    )
    editor_plan = _editor_plan()
    db, item, creator_id, copilot, _runs = _wire_planner(
        monkeypatch, output=output, editor_plan=editor_plan, snapshot={"text_bars": []}
    )
    result = await plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item.id,
        creator_id=creator_id,
        user_message="Change the title to New",
    )
    copilot.assert_awaited_once()
    assert result.plan is editor_plan
    assert result.brief_route == "editor_ops"
    assert [u.literal for u in result.brief_updates] == ["New"]


@pytest.mark.asyncio
async def test_brief_off_keeps_the_original_copilot_first_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor_plan = _editor_plan()
    db, item, creator_id, copilot, runs = _wire_planner(
        monkeypatch,
        output=SimpleNamespace(action=AskUser(**_ASK), brief_updates=[]),
        editor_plan=editor_plan,
        snapshot={"text_bars": []},
    )
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", False)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    result = await plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item.id,
        creator_id=creator_id,
        user_message="Order them by when I filmed them",
    )
    assert result.plan is editor_plan
    assert result.brief_route is None and result.brief_updates == ()
    assert runs == []  # the Main Creator never ran


@pytest.mark.asyncio
async def test_replan_strategy_request_reaches_the_planner_from_the_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = SimpleNamespace(
        action=AskUser(**_ASK),
        brief_updates=[_upd("text", "per_clip", description="the landmark in each clip")],
    )
    db, item, creator_id, _copilot, runs = _wire_planner(
        monkeypatch, output=output, editor_plan=None, snapshot=None
    )
    prior = apply_updates(None, [_upd("text", "title", literal="20K Koşu")], source_turn_id="t0")
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=prior))
    result = await plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item.id,
        creator_id=creator_id,
        user_message="Do it again based on my prompt",
    )
    # No editable target -> re-plan; the model saw the brief, not chat text.
    assert result.brief_route == "replan"
    assert '"20K Koşu"' in runs[0].creator_request
