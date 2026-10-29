"""Explicitly accepting the simpler edit changes the request approval will render.

Thread 0ef4fdde (2026-10-11, KRI-610): a new creator asked a montage to "explain the
footages". No plan could put text on each clip, so the brief-binding gate rolled the
draft back and asked "Should I try a different approach, or make this simpler
version?". The creator answered "simpler version", "you decide", "make it more
basic", "skip, decide for me"... and got the same question five times in an hour,
with no draft ever made: answering relied on the planner dropping the requirement,
and it never did.

Ways this can go wrong, each pinned below:
- the second identical refusal asks again (the loop) instead of making the draft;
- the draft hides the omissions instead of naming what was left out;
- a clip question between the two refusals makes the breaker forget the first ask;
- a NEW unmet requirement is waved through because an older one was already asked;
- the very first refusal stops asking (the gate itself must keep working).
- unrelated replies or stale offers waive requirements without consent;
- recovery loses accepted omissions or a repeated offer loses its brief version;
- the approval binding and real native compiler still enforce the old request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

import app.tasks.kria_runtime as kria_runtime
from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody
from app.kria.brief import BriefUpdate, CreativeBrief, load_latest_brief_sync
from app.kria.brief_binding import BriefBinding
from app.kria.contracts import KriaTurnPlan
from app.kria.device_render import make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.render_assets import RenderFingerprint
from app.kria.runtime import approval_fingerprint, decide_approval
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorEditDraft,
    PlanItem,
)
from app.pipeline.phone_narrated_plan import NarratedPhoneStep, compile_phone_narrated_plan
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContractError,
    build_render_contract,
    verify_phone_recipe,
)
from app.services.device_render import (
    device_record,
    device_status,
    pin_device_request,
    verify_device_record_contract,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.tasks.kria_runtime import _claim_approval_dispatch, run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _submit

_REAL_AUTOMATIC_OMISSION_IDS = kria_runtime._automatic_omission_ids
_SUMMARY = "a summary of the match day event"
_ASK = "Should I make it without these, or do you want to change your request?"


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


def _textless_plan():
    """A plan that puts no text on any clip: every per-clip text ask is unmet."""
    return adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="guided_story",
                edit_format="day_vlog",
                audio_strategy="licensed_music",
                pacing="fast",
                render_program="guided",
                selected_media_ids=[],
                rationale="All four clips, in the order of the day.",
                target_duration_s=20,
            ),
            summary="A fast montage of the match day.",
        )
    )


def _planned(plan, *updates: BriefUpdate) -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=plan,
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        brief_updates=tuple(updates),
        brief_route="replan",
        brief_clip_ids=("clip-1", "clip-2"),
    )


def _summary_ask() -> BriefUpdate:
    return BriefUpdate(kind="text", scope="per_clip", description=_SUMMARY)


def _revision(thread_id) -> int:  # noqa: ANN001
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


def _last_reply(thread_id) -> tuple[str, str]:  # noqa: ANN001
    with sync_session() as db:
        event = (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_(["draft_applied", "assistant_response"]),
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )
        return event.event_type, str(event.content or "")


async def _run_turns(monkeypatch, user_id, thread_id, turns):  # noqa: ANN001, ANN202
    """Run (message, PlannedKriaTurn) pairs in order; return each turn's reply."""
    replies = []
    for message, planned in turns:

        async def _plan(*_args, _planned=planned, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return _planned

        monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _plan)
        accepted = await _submit(user_id, thread_id, message, _revision(thread_id))
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        replies.append((result["status"], *_last_reply(thread_id)))
    return replies


async def _decide(user_id, thread_id, decision):  # noqa: ANN001, ANN202
    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one()
        approval_id = approval.id
        body = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval_id,
            creator_id=user_id,
            decision=decision,
            body=body,
        )
    return approval_id


@pytest.mark.asyncio
async def test_answering_the_question_makes_the_simpler_draft_instead_of_asking_again(
    monkeypatch,
):
    user_id, thread_id, _session_id = _seed_runtime_project()
    first, second = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            (
                "collab all those videos and explain the footages",
                _planned(_textless_plan(), _summary_ask()),
            ),
            # The planner kept the requirement (it emitted no change/remove), as in prod.
            ("simpler version", _planned(_textless_plan())),
        ],
    )

    # The first refusal still asks: the gate itself is unchanged.
    assert first[0] == "completed"
    assert first[1] == "assistant_response"
    assert first[2].endswith(_ASK)

    # The answer is honoured: a draft is made, and the miss is reported, not hidden.
    assert second[0] == "awaiting_approval"
    assert second[1] == "draft_applied"
    assert _ASK not in second[2]
    assert second[2].startswith("Here's the simpler version.")
    assert f"Left out as agreed: {_SUMMARY}" in second[2]

    # KRI-698: accepting the omission changes the durable request, not just
    # the chat reply. Approval and render must inherit that same revision.
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
        draft = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
            )
        ).scalar_one()
        assert brief.version == 2
        assert not brief.live()
        assert brief.requirements[0].status == "superseded"
        binding = draft.snapshot_json["brief_binding"]
        assert binding["brief"]["version"] == brief.version
        assert binding["brief"]["requirements"] == [
            r.model_dump(mode="json") for r in brief.requirements
        ]
        assert "Superseded requirements" in binding["creator_request"]


@pytest.mark.asyncio
async def test_a_clip_question_in_between_does_not_reset_the_question(monkeypatch):
    user_id, thread_id, _session_id = _seed_runtime_project()
    clip_card = KriaTurnPlan(
        mode="respond", turn_value="question", response="Which clip is IMG_7382?"
    )
    replies = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
            ("first IMG_7382 then IMG_7820", _planned(clip_card)),
            ("skip, decide for me", _planned(_textless_plan())),
        ],
    )

    assert replies[0][2].endswith(_ASK)
    assert replies[1][2] == "Which clip is IMG_7382?"
    assert replies[2][0] == "awaiting_approval"
    assert _ASK not in replies[2][2]


@pytest.mark.asyncio
async def test_a_new_unmet_requirement_is_still_asked_about(monkeypatch):
    user_id, thread_id, _session_id = _seed_runtime_project()
    first, second = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
            (
                "and put the player's name on each clip",
                _planned(
                    _textless_plan(),
                    BriefUpdate(kind="text", scope="per_clip", description="the player's name"),
                ),
            ),
        ],
    )

    assert first[2].endswith(_ASK)
    # r2 was never offered as a choice, so the creator decides about it first.
    assert second[0] == "completed"
    assert second[1] == "assistant_response"
    assert second[2].endswith(_ASK)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["No, keep the text", "What would change?", "hello"])
async def test_an_offer_is_not_consent_to_drop_requirements(monkeypatch, message):
    user_id, thread_id, _ = _seed_runtime_project()
    _, second = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
            (message, _planned(_textless_plan())),
        ],
    )
    assert second[0] == "completed"
    assert second[1] == "assistant_response"
    with sync_session() as db:
        assert len(load_latest_brief_sync(db, thread_id).live()) == 1


@pytest.mark.asyncio
async def test_exact_omissions_leave_the_title_in_the_render_contract(monkeypatch):
    user_id, thread_id, _ = _seed_runtime_project()
    plan = _textless_plan()
    plan.intents[0].arguments["strategy"]["opening_title"] = "3 rules of a night boat in Tigre"
    updates = [
        BriefUpdate(kind="text", scope="title", literal="3 rules of a night boat in Tigre"),
        BriefUpdate(
            kind="text", scope="per_clip", literal="1 · leave before 11", description="rule 1"
        ),
        BriefUpdate(
            kind="text", scope="per_clip", literal="2 · you're all amigos", description="rule 2"
        ),
    ]
    await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("Title and two rule lines", _planned(plan, *updates)),
            ("Make this simpler version", _planned(plan)),
        ],
    )
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
        assert [r.id for r in brief.live()] == ["r1"]
        contract = build_render_contract(
            plan.intents[0].arguments["strategy"], generation_id="simpler", brief=brief
        )
        assert {t.text for t in contract.exact_texts} == {"3 rules of a night boat in Tigre"}


@pytest.mark.asyncio
async def test_an_old_offer_cannot_drop_a_changed_requirement(monkeypatch):
    user_id, thread_id, _ = _seed_runtime_project()
    await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
            (
                "change that to exact player names",
                _planned(
                    KriaTurnPlan(mode="respond", turn_value="decision", response="Understood."),
                    BriefUpdate(
                        operation="change",
                        target_requirement_id="r1",
                        expected_version=1,
                        kind="text",
                        scope="per_clip",
                        description="exact player names",
                    ),
                ),
            ),
        ],
    )
    [reply] = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("Make this simpler version", _planned(_textless_plan())),
        ],
    )
    assert reply[0] == "completed"
    assert reply[1] == "assistant_response"
    with sync_session() as db:
        assert load_latest_brief_sync(db, thread_id).live()[0].description == "exact player names"


@pytest.mark.asyncio
async def test_a_later_request_can_restore_omitted_text(monkeypatch):
    user_id, thread_id, _ = _seed_runtime_project()
    await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
            ("Make this simpler version", _planned(_textless_plan())),
        ],
    )
    await _decide(user_id, thread_id, "deny")
    await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("Actually, add the summary", _planned(_textless_plan(), _summary_ask())),
        ],
    )
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
        assert brief.version == 3
        assert [r.id for r in brief.live()] == ["r2"]
        assert brief.requirements[0].status == "superseded"


@pytest.mark.asyncio
async def test_clip_number_is_not_consent_to_an_earlier_simpler_offer(monkeypatch):
    user_id, thread_id, _ = _seed_runtime_project()
    replies = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
            (
                "which clip?",
                _planned(
                    KriaTurnPlan(
                        mode="respond", turn_value="question", response="Which clip should I use?"
                    )
                ),
            ),
            ("2", _planned(_textless_plan())),
        ],
    )
    assert replies[-1][0] == "completed"
    with sync_session() as db:
        assert len(load_latest_brief_sync(db, thread_id).live()) == 1


@pytest.mark.asyncio
async def test_a_repeated_offer_without_new_requirements_remains_answerable(monkeypatch):
    user_id, thread_id, _ = _seed_runtime_project()
    replies = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
            ("What would change?", _planned(_textless_plan())),
            ("Make this simpler version", _planned(_textless_plan())),
        ],
    )
    assert replies[-1][0] == "awaiting_approval"
    with sync_session() as db:
        assert not load_latest_brief_sync(db, thread_id).live()


@pytest.mark.asyncio
async def test_agreed_omissions_survive_a_following_unresolved_choice(monkeypatch):
    user_id, thread_id, _ = _seed_runtime_project()
    await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
        ],
    )
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(
        "app.tasks.kria_runtime._unresolved_choice_plan",
        lambda *_a, **_kw: (
            KriaTurnPlan(mode="respond", turn_value="question", response="Which length?"),
            "unresolved_choice",
        ),
    )
    [reply] = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("Make this simpler version", _planned(_textless_plan())),
        ],
    )
    assert reply[0] == "completed"
    with sync_session() as db:
        brief = load_latest_brief_sync(db, thread_id)
        assert brief.version == 2
        assert not brief.live()


async def _exercise_phone_approval_journey(monkeypatch, *, automatic: bool = False):
    """Real DB -> approval -> dispatch claim -> phone compiler -> device/publication guard.

    The replay supplies the planner output; the model is not under test here.
    KRIA_SIMPLER_EXPORT_DIR optionally supplies real media and receives the EXACT
    pinned recipe for the existing production AVFoundation exporter harness.
    """
    user_id, thread_id, session_id = _seed_runtime_project()
    if automatic:
        monkeypatch.setattr(kria_runtime, "_automatic_omission_ids", _REAL_AUTOMATIC_OMISSION_IDS)
    else:
        # Replay an already offered historical choice through the same native journey.
        monkeypatch.setattr(kria_runtime, "_automatic_omission_ids", lambda *_a, **_kw: ())
    export_dir = (
        Path(os.environ["KRIA_SIMPLER_EXPORT_DIR"])
        if os.environ.get("KRIA_SIMPLER_EXPORT_DIR")
        else None
    )
    ids = ("clip-1", "clip-2", "clip-3")

    def fingerprint(name):
        path = export_dir / name if export_dir else None
        data = path.read_bytes() if path else name.encode()
        return RenderFingerprint(sha256=hashlib.sha256(data).hexdigest(), byte_count=len(data))

    bindings = tuple(
        PhoneSourceBinding(
            media_id=media_id,
            proxy_path=f"replay/analysis-proxy-{media_id}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                **fingerprint(f"{media_id}.mp4").model_dump(),
                duration_s=3,
                width=320,
                height=568,
                has_audio=False,
            ),
        )
        for media_id in ids
    )
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreatorAgentSession, session_id).plan_item_id)
        item.clip_gcs_paths = [b.proxy_path for b in bindings]
        item.clip_assignments = [
            {"media_id": b.media_id, "gcs_path": b.proxy_path, "storage_generation": b.generation}
            for b in bindings
        ]
        item.voiceover_gcs_path = "replay/voice.wav"
        item.voiceover_duration_s = 6
        item.voiceover_generation = "1"
        db.commit()
    title = "3 rules of a night boat in Tigre"
    plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            summary="Title and narration, without the two big rule lines.",
            strategy=CreativeStrategy(
                edit_format="narrated_planned",
                audio_strategy="voiceover",
                render_program="native",
                selected_media_ids=list(ids),
                opening_title=title,
                opening_title_duration_s=2,
                target_duration_s=6,
            ),
        )
    )
    first_replies = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            (
                "Keep the title and add the rule lines",
                _planned(
                    plan,
                    BriefUpdate(kind="text", scope="title", literal=title),
                    BriefUpdate(kind="text", scope="clip:clip-1", literal="1 · leave before 11"),
                    BriefUpdate(kind="text", scope="clip:clip-2", literal="2 · you're all amigos"),
                ),
            ),
        ],
    )
    with sync_session() as db:
        full_brief = load_latest_brief_sync(db, thread_id)
    if automatic:
        # Use the immutable original ledger entry for the negative control, not
        # expectations reconstructed from the simplified result.
        with sync_session() as db:
            original = db.execute(
                select(CreativeBriefVersion)
                .where(CreativeBriefVersion.thread_id == thread_id)
                .order_by(CreativeBriefVersion.version)
                .limit(1)
            ).scalar_one()
            full_brief = CreativeBrief(version=original.version, requirements=original.requirements)
    if automatic:
        # KRI-1009: the real rule never drops the creator's quoted lines unasked. The held
        # draft names them; the tap below agrees to leave them out.
        reply = first_replies[0]
        assert reply[:2] == ("completed", "assistant_response"), reply
        assert '"1 · leave before 11"' in reply[2]
        assert '"2 · you\'re all amigos"' in reply[2]
        assert "Left out" not in reply[2]
    [reply] = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("Make this simpler version", _planned(plan)),
        ],
    )
    assert reply[0] == "awaiting_approval"
    approval_id = await _decide(user_id, thread_id, "approve")
    claim = await asyncio.to_thread(_claim_approval_dispatch, approval_id)
    assert claim is not None
    binding = BriefBinding.model_validate(claim.brief_binding)
    brief = binding.resolve(thread_id)
    assert brief.version == 2
    assert [r.id for r in brief.live()] == ["r1"]
    contract = build_render_contract(
        claim.strategy,
        generation_id="simpler",
        brief=brief,
        media_snapshot=binding.media_snapshot,
        has_voiceover=True,
    )
    narration = PhoneNarrationBed(
        plan_item_id=str(claim.item_id),
        generation="1",
        fingerprint=fingerprint("voice.wav"),
        duration_s=6,
    )
    recipe = compile_phone_narrated_plan(
        [
            NarratedPhoneStep(step_id=f"s{i}", media_id=media_id, start_s=i * 2, end_s=(i + 1) * 2)
            for i, media_id in enumerate(ids)
        ],
        bindings,
        narration,
        voiceover_duration_s=6,
        opening_title=claim.strategy["opening_title"],
        opening_title_end_s=claim.strategy["opening_title_duration_s"],
        mix=1,
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        all_candidates={"creator_render_requirements_version": 1},
        assembly_plan={
            CONTRACT_FIELD: contract.model_dump(mode="json"),
            "creator_brief_binding": binding.model_dump(mode="json"),
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
        },
    )
    request = make_device_request(job_id=job.id, variant_id="narrated", revision=1, recipe=recipe)
    assert pin_device_request(job, request, base_generation="simpler")
    assert not pin_device_request(job, request, base_generation="simpler")  # retry is idempotent
    record = device_record(job, "narrated")
    verify_device_record_contract(job, record, device_status(job, "narrated"))

    # A missing line is still an error without the creator's explicit waiver.
    full_contract = build_render_contract(claim.strategy, generation_id="full", brief=full_brief)
    with pytest.raises(CreatorRenderContractError, match="text"):
        verify_phone_recipe(full_contract, recipe)
    no_title = compile_phone_narrated_plan(
        [
            NarratedPhoneStep(step_id=f"s{i}", media_id=media_id, start_s=i * 2, end_s=(i + 1) * 2)
            for i, media_id in enumerate(ids)
        ],
        bindings,
        narration,
        voiceover_duration_s=6,
        mix=1,
    )
    with pytest.raises(CreatorRenderContractError, match="text"):
        verify_phone_recipe(contract, no_title)
    if export_dir:
        for name, value in (("approved", recipe), ("missing_title", no_title)):
            case = export_dir / "cases" / name
            case.mkdir(parents=True, exist_ok=True)
            (case / "recipe.json").write_text(value.model_dump_json(indent=2))
            paths = {
                asset.id: str(
                    Path(__file__).resolve().parents[2]
                    / "assets/fonts"
                    / asset.id.removeprefix("font-")
                    if asset.id.startswith("font-")
                    else export_dir
                    / (f"{asset.media_id}.mp4" if asset.kind == "original" else "voice.wav")
                )
                for asset in value.asset_manifest.assets
            }
            (case / "assets.json").write_text(json.dumps(paths, indent=2))
        (export_dir / "approval.json").write_text(
            json.dumps(
                {
                    "reply": reply[2],
                    "binding": claim.brief_binding,
                    "contract": contract.model_dump(mode="json"),
                    "device_record": record,
                    "negative_controls": [
                        "unwaived rule lines refused",
                        "missing retained title refused",
                    ],
                },
                indent=2,
            )
        )


@pytest.mark.asyncio
async def test_simpler_approval_compiles_and_pins_while_full_request_still_refuses(monkeypatch):
    """An explicit simpler-version answer reaches the real native recipe boundary."""
    await _exercise_phone_approval_journey(monkeypatch)
