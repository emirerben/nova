"""KRI-788: the creator's order answer reaches the render they approved.

Incident (prod 042c405c9, 2026-10-21, café kit T1 "Caro full take", runtime-v2 phone
Talking edit, thread 32A5B608, jobs 0701fcc9 + f88555bc). The creator answered the order
question with "Use the order you added the clips" (``order_basis`` = ``attachment_order``).
Kria offered approval, the creator approved, and both renders were refused at dispatch in
under a second: "I can't verify this ordering rule from the approved media."
(``phone_plan_unsupported``). No video after two renders.

The approved BriefBinding held the answer (``choice_answers`` = attachment_order -> r2). The
job factory erases client-writable ``choice_answers`` from the strategy (KRI-492, #1540,
"a later verified BriefBinding may supersede this"), and the dispatch rebuild re-pinned the
contract from that stripped strategy without folding the binding's answers back in. r2's
answered brief key is ``attachment``, which only a recorded answer makes verifiable, so it
fell through to ``unresolved``. The approval-time dry run read the draft strategy, which
still carried the answer, so it let approval through. Every choice answer (order basis,
length, silent tail, text placement) has been lost at dispatch the same way since #1540.

Failure modes (written before the fix):

1. A server-recorded answer on the approved binding never reaches the dispatch contract:
   ``attachment_order`` -> "I can't verify this ordering rule"; ``unordered`` -> an order
   promise the creator dropped.
2. A client-written ``choice_answers`` on the dispatched strategy is honored with no binding
   answer behind it (the server-owned-answers rule from KRI-476 / KRI-492 breaks).
3. The strategy's answer overrides a different answer the binding froze.
4. A binding with no answers changes the dispatched strategy or contract (byte-identity).
5. The persisted ``creator_strategy`` disagrees with the pinned contract (the worker's route
   and composition stamps read the persisted strategy, not the contract).
6. Approval is offered while the dispatch contract is unresolved, or the approval-time and
   dispatch-time contracts pin a different order (parity).

The tests run the REAL approval dry run (``_unresolved_choice_plan``), the REAL binding
(``BriefBinding.create``, as the runtime pins it), the REAL dispatch (``_dispatch_item_render``
over the REAL ``build_generative_job``, so the factory's strip runs) and the REAL phone
dispatch gate (``check_phone_dispatch_contract``). Shapes are T1 render 2's (job2_debug.json);
the take's transcript and paths are left out (the contract never reads them).
"""

from __future__ import annotations

import copy
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import BriefBinding, snapshot_media
from app.services.choice_questions import answered_brief
from app.services.creator_render_contract import (
    CreatorRenderContractError,
    build_render_contract,
    check_phone_dispatch_contract,
    read_render_contract,
)
from app.tasks.content_plan_build import _dispatch_item_render
from app.tasks.kria_runtime import _unresolved_choice_plan
from tests.tasks.test_content_plan_build import _phone_dispatch_item

MEDIA_ID = "analysis-proxy-ios-B22C7409-7CF4-4226-9E9F-CC6814A8D942.mp4"
REFUSAL = "I can't verify this ordering rule from the approved media."

# all_candidates.creator_strategy of job f88555bc, as the approved draft carried it
# (the factory erased choice_answers; the draft held the server-recorded answer).
T1_STRATEGY = {
    "pacing": "balanced",
    "direction": "native",
    "rationale": "Switching to the native pipeline so the video can render on your phone.",
    "edit_format": "subtitled",
    "media_scope": "selected",
    "caption_style": "clean",
    "opening_title": "Colombian dessert, Ecuadorian hands",
    "audio_strategy": "original_audio",
    "render_program": "native",
    "story_structure": [
        "Carolina introduces herself",
        "Carolina describes the Oblea de la Casa and its ingredients",
    ],
    "target_duration_s": 35,
    "selected_media_ids": [MEDIA_ID],
    "video_reuse_policy": "once",
    "optional_treatments": [],
}


def _answer(option: str) -> dict:
    """The planner gate's recorded answer, exactly as the binding froze it."""

    return {
        "kind": "order_basis",
        "option": option,
        "source": "creator",
        "conflict": "order_basis",
        "input_digest": "a48e489839d3d2c2a8a12e1a",
        "requirement_ids": ["r2"],
    }


def _requirement(rid: str, kind: str, description: str, **extra) -> dict:  # noqa: ANN003
    return {
        "id": rid,
        "kind": kind,
        "facts": extra.pop("facts", {}),
        "scope": extra.pop("scope", "global"),
        "status": extra.pop("status", "open"),
        "literal": extra.pop("literal", None),
        "description": description,
        "source_turn_id": "d02b5630-d3fd-492c-a749-0a7383cf91a1",
    }


# The thread's stored brief, before the answer: r2 is the key-less rule the order question
# asked about ("You gave me a specific order for the clips"). Approval pins
# ``answered_brief(stored, strategy)``; `PROD_R2` is what prod's binding held.
STORED_BRIEF = {
    "version": 2,
    "requirements": [
        _requirement("r1", "timing", "About 35 seconds", facts={"duration_s": 35}),
        _requirement(
            "r2",
            "order",
            "Start at \"Hi, I'm Karolina, I'm from Ecuador\" and end right after "
            '"I\'m gonna be here all week"',
            facts={
                "end": "I'm gonna be here all week",
                "start": "Hi, I'm Karolina, I'm from Ecuador",
            },
        ),
        _requirement(
            "r3",
            "audio",
            "Cut her slip 'with bread, no,' so it says 'waffle cookie with blackberry jam'",
            facts={"speech_cleanup": True},
        ),
        _requirement(
            "r4",
            "audio",
            "Cut the half question 'how are you gonna'",
            facts={"speech_cleanup": True},
        ),
        _requirement(
            "r5",
            "style",
            "Captions for both voices, ensuring correct spellings: Oblea de la Casa, "
            "dulce de leche, leche condensada, Merope",
            facts={"captions": "on"},
        ),
        _requirement(
            "r6",
            "text",
            "Title on screen, placed so there is no text on her face or on the plate",
            scope="title",
            literal="Colombian dessert, Ecuadorian hands",
        ),
        _requirement(
            "r7",
            "text",
            "Show MEROPE in big letters the first time she says Merope",
            status="superseded",
            literal="MEROPE",
        ),
    ],
}
PROD_R2 = _requirement(
    "r2",
    "order",
    "in the order the clips were added",
    facts={
        "end": "I'm gonna be here all week",
        "key": "attachment",
        "start": "Hi, I'm Karolina, I'm from Ecuador",
    },
)

REQUEST = (
    "Full creator request (chronological):\nTalking-to-camera edit of Caro at Merope. "
    "Start at \"Hi, I'm Karolina, I'm from Ecuador\" and end right after "
    '"I\'m gonna be here all week".'
)


def _brief(answers: list[dict] | None) -> CreativeBrief:
    """The brief approval pins: the stored one with the server-recorded answers applied."""

    stored = CreativeBrief.model_validate(copy.deepcopy(STORED_BRIEF))
    return answered_brief(stored, {**T1_STRATEGY, "choice_answers": answers})


def _item() -> SimpleNamespace:
    """The T1 plan item: one 68.37 s take, attached once, with its iPhone receipt."""

    item = _phone_dispatch_item("subtitled", clip_count=1)
    row = item.clip_assignments[0]
    item.clip_assignments = [
        {
            **row,
            "media_id": MEDIA_ID,
            "manifest_identity": MEDIA_ID,
            "kind": "video",
            "shot_id": None,
            "duration_s": 68.37,
            "capture": {"capture_time": "2026-10-16T15:24:17Z"},
            "analysis": {
                "source": "clip_metadata",
                "duration_s": 68.37,
                "width": 320,
                "height": 568,
                "subject": "woman presenting a dessert",
            },
            "upload_contract": {
                "purpose": "analysis_proxy",
                "proxy": {
                    "original": {
                        "kind": "video",
                        "sha256": "c" * 64,
                        "byte_count": 91_000_000,
                        "duration_s": 68.37,
                        "width": 1080,
                        "height": 1920,
                        "has_audio": True,
                    },
                    "duration_s": 68.37,
                    "width": 320,
                    "height": 568,
                    "frame_rate": 30.0,
                    "timing_version": 1,
                },
            },
        }
    ]
    item.clip_gcs_paths = [row["gcs_path"]]
    return item


def _approve(item: SimpleNamespace, answers: list[dict] | None) -> dict:
    """The binding the runtime pins at approval (``kria_runtime``, ``BriefBinding.create``)."""

    return BriefBinding.create(
        uuid.uuid4(),
        _brief(answers),
        latest_message="Use the order you added the clips",
        full_creator_request=REQUEST,
        media_snapshot=snapshot_media(item),
        choice_answers=answers,
    ).model_dump(mode="json")


def _dispatch(monkeypatch: pytest.MonkeyPatch, item: SimpleNamespace, draft: dict, binding: dict):
    """The runtime-v2 approval dispatch (``kria_runtime`` -> ``dispatch_item_render_for``)."""

    from app.services import generative_jobs

    monkeypatch.setattr(settings, "speech_cleanup_mode", "opt_in")
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(settings, "kria_runtime_v2_phone_enabled", True)
    monkeypatch.setattr(settings, "kria_runtime_v2_phone_user_ids", [])
    monkeypatch.setattr(settings, "kria_plan_authority_enabled", True)

    plan = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), preference_summary="", ownership_epoch=0
    )
    session = MagicMock()
    session.execute.return_value.scalar_one.return_value = 0
    session.execute.return_value.all.return_value = []
    # The approved thread the binding belongs to.
    session.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(
        id=uuid.UUID(binding["thread_id"]), state=None
    )
    real_build = generative_jobs.build_generative_job
    built: dict = {}

    def build(**kwargs):  # noqa: ANN003, ANN202
        # The REAL factory, so its KRI-492 strip of client-writable answers runs.
        built["job"] = real_build(**kwargs)
        return built["job"]

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "app.services.smart_captions.resolve_smart_captions_context_sync",
                return_value=None,
            )
        )
        stack.enter_context(
            patch(
                "app.services.edit_proposals.validate_approved_proposal_media_sync",
                return_value=(None, None),
            )
        )
        stack.enter_context(patch.object(generative_jobs, "build_generative_job", build))
        stack.enter_context(patch("app.services.job_dispatch.enqueue_orchestrator_sync"))
        result = _dispatch_item_render(
            session,
            item,
            plan,
            {"tone": "direct", "content_pillars": []},
            ownership_epoch=0,
            bypass_guided_edit_gate=True,
            allow_phone_unapproved_montage=True,
            phone_speech_cleanup_unattended=True,
            creator_strategy=copy.deepcopy(draft),
            creator_request=binding["creator_request"],
            creator_brief_binding=binding,
        )
    assert result.outcome == "dispatched", result
    return built["job"]


def _approval_offered(item: SimpleNamespace, draft: dict) -> bool:
    """The approval-time dry run: ``None`` means the runtime offers the approval card."""

    brief = _brief(draft.get("choice_answers"))
    return (
        _unresolved_choice_plan(
            draft,
            brief,
            snapshot_media(item),
            contract_brief=brief,
            events=[],
            has_draft=True,
        )
        is None
    )


def _phone_gate(job) -> None:  # noqa: ANN001
    """The worker's first check on a phone render (``generative_build``)."""

    check_phone_dispatch_contract(
        read_render_contract(job.assembly_plan),
        snapshot_generation_id=job.assembly_plan["creator_generation_id"],
        has_voiceover_candidate=False,
    )


def _order(contract) -> dict:  # noqa: ANN001
    return {
        "order_required": contract.order_required,
        "order_basis": contract.order_basis,
        "order_ids": list(contract.order_ids),
        "unresolved": list(contract.unresolved),
    }


# -- the incident ------------------------------------------------------------------------


def test_the_answered_brief_is_the_one_prod_pinned() -> None:
    brief = _brief([_answer("attachment_order")])
    r2 = next(req for req in brief.requirements if req.id == "r2")
    assert r2.model_dump(mode="json") == PROD_R2


def test_t1_attachment_order_answer_renders_after_approval(monkeypatch) -> None:
    """Approval offered -> approved -> dispatched -> the phone gate passes (prod refused)."""

    item = _item()
    draft = {**T1_STRATEGY, "choice_answers": [_answer("attachment_order")]}
    assert _approval_offered(item, draft)

    job = _dispatch(monkeypatch, item, draft, _approve(item, draft["choice_answers"]))

    assert _order(read_render_contract(job.assembly_plan)) == {
        "order_required": True,
        "order_basis": "attachment_order",
        "order_ids": [MEDIA_ID],
        "unresolved": [],
    }
    _phone_gate(job)  # prod: CreatorRenderContractError(REFUSAL)


def test_unordered_answer_keeps_no_order_promise_at_dispatch(monkeypatch) -> None:
    """Regression guard: "Continue without a fixed order" supersedes the rule in the
    answered brief, so it rendered even while answers were lost; it must keep rendering."""

    item = _item()
    draft = {**T1_STRATEGY, "choice_answers": [_answer("unordered")]}
    assert _approval_offered(item, draft)

    job = _dispatch(monkeypatch, item, draft, _approve(item, draft["choice_answers"]))

    contract = read_render_contract(job.assembly_plan)
    assert contract.order_required is False
    assert contract.unresolved == ()
    _phone_gate(job)


# -- the answers stay server-owned ------------------------------------------------------


def test_a_client_written_answer_with_no_binding_answer_is_not_honored(monkeypatch) -> None:
    """The dispatched strategy claims an answer approval never froze: still refused."""

    item = _item()
    forged = {**T1_STRATEGY, "choice_answers": [_answer("attachment_order")]}
    job = _dispatch(monkeypatch, item, forged, _approve(item, None))

    assert "choice_answers" not in job.all_candidates["creator_strategy"]
    assert read_render_contract(job.assembly_plan).unresolved == (REFUSAL,)
    with pytest.raises(CreatorRenderContractError, match="ordering rule"):
        _phone_gate(job)


def test_the_binding_answer_wins_over_a_different_strategy_answer(monkeypatch) -> None:
    item = _item()
    approved = [_answer("attachment_order")]
    forged = {**T1_STRATEGY, "choice_answers": [_answer("unordered")]}
    job = _dispatch(monkeypatch, item, forged, _approve(item, approved))

    assert job.all_candidates["creator_strategy"]["choice_answers"] == approved
    contract = read_render_contract(job.assembly_plan)
    assert contract.order_required is True
    assert contract.order_basis == "attachment_order"
    _phone_gate(job)


def test_a_binding_without_answers_dispatches_the_factory_strategy_unchanged(
    monkeypatch,
) -> None:
    item = _item()
    job = _dispatch(monkeypatch, item, T1_STRATEGY, _approve(item, None))

    persisted = job.all_candidates["creator_strategy"]
    assert "choice_answers" not in persisted
    assert {k: v for k, v in persisted.items() if k in T1_STRATEGY} == T1_STRATEGY


# -- one contract: what approval checked is what dispatch pins -------------------------


@pytest.mark.parametrize("option", ["attachment_order", "unordered"])
def test_dispatch_pins_the_contract_approval_checked(monkeypatch, option: str) -> None:
    item = _item()
    draft = {**T1_STRATEGY, "choice_answers": [_answer(option)]}
    # The approval-time contract, built the way the runtime's dry run builds it.
    approval_time = build_render_contract(
        draft,
        generation_id="preflight",
        brief=_brief(draft["choice_answers"]),
        media_snapshot=snapshot_media(item),
    )

    job = _dispatch(monkeypatch, item, draft, _approve(item, draft["choice_answers"]))
    dispatched = read_render_contract(job.assembly_plan)

    assert _order(dispatched) == _order(approval_time)
    assert dispatched.duration_s == approval_time.duration_s
    assert dispatched.exact_texts == approval_time.exact_texts
    # The worker reads the persisted strategy (route + composition stamps): same answers.
    assert job.all_candidates["creator_strategy"]["choice_answers"] == draft["choice_answers"]
