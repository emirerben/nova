import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

import app.tasks.kria_runtime as kria_runtime
from app.agents._schemas.creator_agent import (
    CreatorRenderIntentEvidence,
    ProposeStrategy,
)
from app.agents.main_creator import MainCreatorOutput
from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria import planner
from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief, load_latest_brief_sync
from app.kria.brief_checks import PlanFacts, build_receipts
from app.kria.contracts import KriaTurnPlan
from app.models import (
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorEditDraft,
    PlanItem,
)
from app.tasks.kria_runtime import _automatic_omission_ids
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.kria.test_runtime_simpler_version_loop import (
    _exercise_phone_approval_journey,
    _planned,
    _run_turns,
    _textless_plan,
)
from tests.kria.test_strategy_policy import _talking, _talking_manifest


@pytest.fixture(autouse=True)
def _binding_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _brief(*rows: tuple[str, str, str]) -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id=req_id, kind=kind, scope=scope, description=req_id)
            for req_id, kind, scope in rows
        ],
    )


def test_only_checked_unsupported_text_and_style_rows_are_automatic() -> None:
    brief = _brief(
        ("text", "text", "per_clip"),
        ("style", "style", "global"),
        ("order", "order", "global"),
        ("select", "select", "global"),
        ("audio", "audio", "global"),
    )

    brief.requirements[1].facts["footage_light"] = "brighter"
    report = {
        "requirements": [
            {"requirement_id": "text", "disposition": "unsupported"},
            {"requirement_id": "style", "disposition": "unsupported"},
            {"requirement_id": "order", "disposition": "unsupported"},
            {"requirement_id": "select", "disposition": "unsupported"},
            {"requirement_id": "audio", "disposition": "unsupported"},
        ]
    }

    assert _automatic_omission_ids(brief, (), report, strategy={"render_program": "native"}) == (
        "text",
        "style",
    )


@pytest.mark.parametrize(
    "kind,scope,facts",
    [
        ("text", "per_clip", {"footage_light": "brighter", "audio_intent": "source_unchanged"}),
        ("text", "per_clip", {"footage_light": "brighter", "audio_intent": "mute original audio"}),
        ("style", "global", {"footage_light": "brighter", "audio_intent": "keep original audio"}),
    ],
)
def test_text_with_source_or_audio_constraints_is_never_automatic(kind, scope, facts):
    brief = _brief(("text", kind, scope))
    brief.requirements[0].facts.update(facts)
    report = {"requirements": [{"requirement_id": "text", "disposition": "unsupported"}]}
    assert _automatic_omission_ids(brief, (), report, strategy={"render_program": "native"}) == ()


def test_policy_problem_and_unknown_evidence_never_auto_omit() -> None:
    brief = _brief(("text", "text", "per_clip"))
    assert (
        _automatic_omission_ids(
            brief,
            [{"requirement_id": "text", "verification": "unchecked", "status": "not_possible"}],
            None,
        )
        == ()
    )
    assert (
        _automatic_omission_ids(
            brief,
            (),
            {
                "policy_problem": "needs a choice",
                "requirements": [{"requirement_id": "text", "disposition": "unsupported"}],
            },
        )
        == ()
    )


def test_receipt_path_allows_only_checked_not_possible_text_or_style() -> None:
    brief = _brief(
        ("text", "text", "per_clip"),
        ("timing", "timing", "global"),
    )
    receipts = [
        {"requirement_id": "text", "verification": "checked", "status": "not_possible"},
        {"requirement_id": "timing", "verification": "checked", "status": "not_possible"},
    ]

    assert _automatic_omission_ids(
        brief, receipts, None, strategy={"render_program": "native"}
    ) == ("text",)


@pytest.mark.parametrize("readiness", [False, True])
def test_missing_supported_labels_are_not_an_unsupported_capability(readiness):
    brief = _brief(("text", "text", "per_clip"))
    receipts = [
        r.model_dump()
        for r in build_receipts(
            brief.live(), PlanFacts(clip_ids=("clip-1", "clip-2")), include_unchecked=True
        )
    ]
    assert receipts[0]["status"] == "not_possible"
    report = (
        {"requirements": [{"requirement_id": "text", "disposition": "unsupported"}]}
        if readiness
        else None
    )
    assert (
        _automatic_omission_ids(
            brief,
            receipts,
            report,
            strategy={"render_program": "guided", "edit_format": "day_vlog"},
        )
        == ()
    )


@pytest.mark.parametrize("readiness", [False, True])
def test_missing_spoken_trigger_is_a_question_not_an_omission(monkeypatch, readiness):
    monkeypatch.setattr("app.kria.brief_checks._talking_spoken_text_lane", lambda: True)
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="text",
                kind="text",
                scope="global",
                literal="Welcome",
                facts={"when_i_say": True},
            )
        ],
    )
    receipts = [
        r.model_dump()
        for r in build_receipts(
            brief.live(),
            PlanFacts(edit_format="subtitled", phone_talking=True),
            include_unchecked=True,
        )
    ]
    assert receipts[0]["status"] == "not_possible"
    report = (
        {"requirements": [{"requirement_id": "text", "disposition": "unsupported"}]}
        if readiness
        else None
    )
    assert (
        _automatic_omission_ids(
            brief,
            receipts,
            report,
            strategy={"render_program": "native", "edit_format": "subtitled"},
        )
        == ()
    )


def _native_textless_plan():
    plan = _textless_plan()
    plan.intents[0].arguments["strategy"].update(
        edit_format="subtitled", render_program="native", audio_strategy="original_audio"
    )
    return plan


async def _real_unsupported_title_plan(monkeypatch: pytest.MonkeyPatch):
    """Run the production planner/policy boundary for an unavailable phone title."""
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", False)
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    if hasattr(settings, "kria_approval_readiness_enabled"):
        monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=_talking(opening_title="Start", caption_style="karaoke"),
        summary="A titled talking clip with captions.",
        render_intent_evidence=CreatorRenderIntentEvidence(opening_title="Start"),
    )
    return await planner._plan_creator_action(
        None,
        thread_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        user_message='Use title "Start" with captions.',
        manifest=_talking_manifest(),
        inputs=SimpleNamespace(
            agent_input=SimpleNamespace(conversation=[]),
            intent_clips=[],
            creative_copy_state={},
            creative_copy_digest="d" * 24,
            creator_request='Use title "Start" with captions.',
            raw_creator_request='Use title "Start" with captions.',
        ),
        output=MainCreatorOutput(action=action),
    )


@pytest.mark.asyncio
async def test_real_policy_omitted_title_binds_db_brief_and_keeps_captions(monkeypatch):
    """A policy-stripped title is waived only when its exact text is in the brief."""
    planned = await _real_unsupported_title_plan(monkeypatch)
    assert planned.plan.mode == "act"
    assert planned.plan.intents[0].arguments["strategy"].get("opening_title") is None
    assert planned.policy_omitted_texts == {"opening_title": ("Start",)}

    user_id, thread_id, session_id = _seed_runtime_project()
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [user_id])
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreatorAgentSession, session_id).plan_item_id)
        item.edit_format = "subtitled"
        item.clip_gcs_paths = ["users/test/raw/analysis-proxy-readiness.mp4"]
        item.clip_assignments = [
            {
                "media_id": "clip-1",
                "gcs_path": item.clip_gcs_paths[0],
                "kind": "video",
                "duration_s": 8,
                "analysis": {"summary": "A talking clip", "has_speech": True},
                "manifest_identity": "clip-1",
                "storage_generation": "1",
                "has_audio": True,
                "upload_contract": {
                    "purpose": "analysis_proxy",
                    "proxy": {
                        "duration_s": 8.0,
                        "width": 640,
                        "height": 360,
                        "frame_rate": 30,
                        "original": {
                            "sha256": "1" * 64,
                            "byte_count": 100,
                            "duration_s": 8.0,
                            "width": 640,
                            "height": 360,
                            "has_audio": True,
                        },
                    },
                },
            }
        ]
        db.commit()
    planned = replace(
        planned,
        brief_route="replan",
        brief_clip_ids=("clip-1",),
        readiness_manifest=_talking_manifest(),
        brief_updates=(
            BriefUpdate(kind="text", scope="title", literal="Start"),
            BriefUpdate(kind="text", scope="global", description="captions"),
        ),
    )
    [reply] = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [('Use title "Start" with captions.', planned)],
    )
    assert reply[0] == "awaiting_approval", reply
    assert 'Left out: "Start" (The selected renderer cannot draw this requested text.)' in reply[2]
    assert "Should I" not in reply[2]
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
        assert [requirement.text() for requirement in brief.live()] == ["captions"]
        draft = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
            )
        ).scalar_one()
        readiness = draft.snapshot_json["brief_coverage"]["approval_readiness"]
        assert readiness["ready"] is True
        assert [row["requirement_id"] for row in readiness["requirements"]] == ["r2"]


@pytest.mark.asyncio
async def test_real_policy_omitted_title_does_not_waive_different_brief_text(monkeypatch):
    planned = await _real_unsupported_title_plan(monkeypatch)
    user_id, thread_id, _ = _seed_runtime_project()
    planned = replace(
        planned,
        brief_route="replan",
        brief_clip_ids=("clip-1",),
        brief_updates=(
            BriefUpdate(kind="text", scope="title", literal="Different"),
            BriefUpdate(kind="text", scope="global", description="captions"),
        ),
    )
    [reply] = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [('Use title "Different" with captions.', planned)],
    )
    assert reply[0] == "completed"
    assert reply[1] == "assistant_response"
    assert "Different" in reply[2]


@pytest.mark.asyncio
async def test_first_turn_auto_omits_unsupported_text_and_pins_supported_title(monkeypatch):
    user_id, thread_id, _ = _seed_runtime_project()
    plan = _native_textless_plan()
    plan.intents[0].arguments["strategy"]["opening_title"] = "Match day"
    updates = (
        BriefUpdate(kind="text", scope="title", literal="Match day"),
        BriefUpdate(kind="text", scope="per_clip", description="player name on each clip"),
    )
    [reply] = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [("Keep the title and player names", _planned(plan, *updates))],
    )

    assert reply[0] == "awaiting_approval", reply
    assert reply[1] == "draft_applied"
    assert "Left out: player name on each clip" in reply[2]
    assert "Should I" not in reply[2]
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
        assert [req.text() for req in brief.live()] == ['"Match day"']
        draft = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id,
                CreatorEditDraft.is_head.is_(True),
            )
        ).scalar_one()
        assert draft.snapshot_json["brief_binding"]["brief"]["requirements"]
        assert draft.snapshot_json["brief_coverage"]["automatic_omission_ids"]
        assert (
            db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.thread_id == thread_id,
                    CreatorAgentApproval.status == "pending",
                )
            ).scalar_one_or_none()
            is not None
        )


@pytest.mark.asyncio
async def test_first_turn_automatic_omission_reaches_native_phone_recipe(monkeypatch):
    """The no-follow-up recovery path uses the same approval and native recipe guards."""
    await _exercise_phone_approval_journey(monkeypatch, automatic=True)


async def _compound_title_approval(monkeypatch, light, *, no_title=False):
    # Reuse the provider-transport replay and real planner/capability/source
    # setup. A synthetic strategy with an unavailable format cannot establish
    # automatic omission on the enrolled approval-readiness path.
    from tests.kria.test_approval_readiness_postgres import prepare

    for name in (
        "kria_approval_readiness_enabled",
        "guided_edit_capability_enabled",
        "edit_format_day_vlog_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    return await prepare(
        monkeypatch,
        phone=True,
        no_title=no_title,
        message=f'Keep the title "Gameday." and make the footage {light}.',
        updates_override=[
            {
                "kind": "text",
                "scope": "title",
                "literal": "Gameday.",
                "description": f"Keep the title Gameday. and make the footage {light}",
                "facts": {"footage_light": light},
            }
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("light", ["brighter", "darker"])
async def test_compound_brightness_omission_keeps_supported_title_in_saved_approval(
    monkeypatch, light
):
    _user, thread_id, approvals, events = await _compound_title_approval(monkeypatch, light)
    assert len(approvals) == 1
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
        assert [req.literal for req in brief.live()] == ["Gameday."]
        assert "footage_light" not in brief.live()[0].facts
        versions = (
            db.execute(
                select(CreativeBriefVersion)
                .where(
                    CreativeBriefVersion.thread_id == thread_id,
                )
                .order_by(CreativeBriefVersion.version)
            )
            .scalars()
            .all()
        )
        assert versions[-2].requirements[0]["facts"]["footage_light"] == light
        assert versions[-2].requirements[0]["literal"] == "Gameday."
        draft = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id,
                CreatorEditDraft.is_head.is_(True),
            )
        ).scalar_one()
        bound = CreativeBrief.model_validate(draft.snapshot_json["brief_binding"]["brief"])
        assert [req.literal for req in bound.live()] == ["Gameday."]
        coverage = draft.snapshot_json["brief_coverage"]
        assert brief.live()[0].id in coverage["enforced_ids"]
        assert brief.live()[0].id not in coverage["automatic_omission_ids"]
        assert coverage["automatic_omission_facets"] == {brief.live()[0].id: ["footage_light"]}
    reply = next(event.content for event in reversed(events) if event.event_type == "draft_applied")
    omission_lines = [line for line in reply.splitlines() if "Left out:" in line]
    assert len(omission_lines) == 1
    assert "brightness" in omission_lines[0]
    assert "Gameday." not in omission_lines[0], "the supported title was not omitted"


@pytest.mark.asyncio
async def test_compound_brightness_cannot_waive_a_missing_supported_title(monkeypatch):
    _user, thread_id, approvals, _events = await _compound_title_approval(
        monkeypatch,
        "brighter",
        no_title=True,
    )
    assert approvals == []
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
        assert [req.literal for req in brief.live()] == ["Gameday."]


@pytest.mark.asyncio
async def test_automatic_omissions_survive_order_question_and_recompute(monkeypatch):
    user_id, thread_id, _ = _seed_runtime_project()
    plan = _native_textless_plan()
    plan.intents[0].arguments["strategy"]["opening_title"] = "Match day"
    updates = (
        BriefUpdate(kind="text", scope="title", literal="Match day"),
        BriefUpdate(kind="text", scope="per_clip", description="player name on each clip"),
    )
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(
        kria_runtime,
        "_unresolved_choice_plan",
        lambda *_args, **_kwargs: (
            KriaTurnPlan(mode="respond", turn_value="question", response="Which order?"),
            "unresolved_choice",
        ),
    )
    [first] = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [("Keep the title and player names", _planned(plan, *updates))],
    )
    assert first[0] == "completed"
    assert first[2] == "Which order?"
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
        assert [requirement.text() for requirement in brief.live()] == [
            '"Match day"',
            "player name on each clip",
        ]
        assert (
            db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.thread_id == thread_id,
                    CreatorAgentApproval.status == "pending",
                )
            ).scalar_one_or_none()
            is None
        )

    monkeypatch.setattr(kria_runtime, "_unresolved_choice_plan", lambda *_a, **_kw: None)
    [blocked] = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [("Use the filmed order", _planned(plan))],
    )
    assert blocked[0] == "awaiting_approval"
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
        assert [requirement.text() for requirement in brief.live()] == ['"Match day"']
        approvals = (
            db.execute(
                select(CreatorAgentApproval).where(CreatorAgentApproval.thread_id == thread_id)
            )
            .scalars()
            .all()
        )
        assert len(approvals) == 1
