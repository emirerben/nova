"""Contract tests for the Main Creator Agent v1 schemas."""

import pytest
from pydantic import ValidationError

from app.agents._schemas.creator_agent import (
    CREATOR_AGENT_OUTPUT_ADAPTER,
    ApplySpeechCutCommand,
    AskUser,
    CreativeStrategy,
    CreatorAutomationDecision,
    CreatorEditPlan,
    CreatorReviewEvidence,
    CreatorReviewReceipt,
    CreatorWorkspaceDeliverableReceipt,
    CreatorWorkspaceReceipt,
    CreatorWorkspaceRelevanceDecision,
    CreatorWorkspaceRelevanceProposal,
    DispatchRenderCommand,
    ProposeStrategy,
    SetCaptionStyleCommand,
    SetItemIntentCommand,
    SetLicensedSfxCommand,
    SetLookPresetCommand,
    SetMediaOverlayCommand,
    SetTransitionCommand,
    canonical_context_hash,
    canonical_manifest_hash,
)


def _strategy() -> CreativeStrategy:
    return CreativeStrategy(direction="fast_montage", render_program="guided")


def test_media_and_catalog_contracts_reject_storage_capabilities() -> None:
    from app.agents._schemas.creator_agent import CreatorCatalogRef, CreatorMediaRef

    with pytest.raises(ValidationError):
        CreatorMediaRef(media_id="gs://bucket/clip.mp4", kind="video")
    with pytest.raises(ValidationError):
        CreatorCatalogRef(catalog_id="https://example.test/music/1", kind="music")


def test_agent_output_is_discriminated_and_strict() -> None:
    result = CREATOR_AGENT_OUTPUT_ADAPTER.validate_python(
        {"kind": "ask_user", "question": "Which cut?", "reason_code": "ambiguous_goal"}
    )
    assert isinstance(result, AskUser)

    result = CREATOR_AGENT_OUTPUT_ADAPTER.validate_python(
        {"kind": "propose_strategy", "strategy": _strategy()}
    )
    assert isinstance(result, ProposeStrategy)

    with pytest.raises(ValidationError):
        CREATOR_AGENT_OUTPUT_ADAPTER.validate_python(
            {"kind": "ask_user", "question": "Which cut?", "reason_code": "x", "url": "bad"}
        )


def test_context_hash_is_canonical_and_manifest_hash_excludes_self() -> None:
    left = canonical_context_hash({"b": 2, "a": [1, 2]})
    right = canonical_context_hash({"a": [1, 2], "b": 2})
    assert left == right

    from app.agents._schemas.creator_agent import ResolvedCreatorManifest

    manifest = ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="montage",
        render_program="guided",
        context_hash=left,
        manifest_hash="0" * 64,
    )
    assert canonical_manifest_hash(manifest) == canonical_manifest_hash(
        manifest.model_copy(update={"manifest_hash": "f" * 64})
    )


def test_plan_commands_are_hash_pinned_and_bounded() -> None:
    manifest_hash = "a" * 64
    context_hash = "b" * 64
    plan = CreatorEditPlan(
        manifest_hash=manifest_hash,
        context_hash=context_hash,
        strategy=_strategy(),
        commands=[
            SetItemIntentCommand(
                command="set_item_intent",
                edit_format="montage",
                expected_manifest_hash=manifest_hash,
            ),
            DispatchRenderCommand(
                command="dispatch_render",
                expected_manifest_hash=manifest_hash,
                expected_context_hash=context_hash,
            ),
        ],
    )
    assert len(plan.commands) == 2

    with pytest.raises(ValidationError, match="pin the plan manifest_hash"):
        CreatorEditPlan(
            manifest_hash=manifest_hash,
            context_hash=context_hash,
            strategy=_strategy(),
            commands=[
                SetItemIntentCommand(
                    command="set_item_intent",
                    edit_format="montage",
                    expected_manifest_hash="c" * 64,
                )
            ],
        )


def test_strategy_carries_bounded_editorial_decisions() -> None:
    strategy = CreativeStrategy(
        edit_format="single_hero",
        archetype="single_hero",
        audio_strategy="licensed_music",
        story_structure=["hook", "demonstration", "payoff"],
        caption_style="kinetic",
        intro_hook="The result surprised me.",
        pacing="fast",
        optional_treatments=["overlays", "sfx", "transitions", "looks"],
        render_program="guided",
    )
    assert strategy.pace == "fast"
    assert strategy.story_structure[-1] == "payoff"

    with pytest.raises(ValidationError):
        CreativeStrategy(optional_treatments=["sfx", "sfx"])


def _target_kwargs() -> dict[str, str | int]:
    return {
        "expected_manifest_hash": "a" * 64,
        "expected_context_hash": "b" * 64,
        "expected_job_id": "job-1",
        "expected_variant_id": "variant-1",
        "expected_generation_id": "generation-1",
        "expected_revision": 2,
        "expected_ownership_epoch": 4,
    }


@pytest.mark.parametrize(
    "command_fields",
    [
        {"command": "set_caption_style", "caption_style": "word"},
        {
            "command": "set_transition",
            "boundary_index": 0,
            "transition": "crossfade",
            "duration_s": 0.3,
        },
        {"command": "set_look_preset", "slot_index": 0, "look_preset": "none"},
        {
            "command": "set_media_overlay",
            "asset_id": "asset-1",
            "start_s": 0,
            "end_s": 1,
        },
        {"command": "set_licensed_sfx", "sound_effect_id": "sfx-1", "at_s": 1},
        {"command": "apply_speech_cut", "candidate_id": "cut-1"},
    ],
)
def test_craft_commands_are_exact_generation_pinned(command_fields) -> None:
    payload = {**_target_kwargs(), **command_fields}
    command_type = {
        "set_caption_style": SetCaptionStyleCommand,
        "set_transition": SetTransitionCommand,
        "set_look_preset": SetLookPresetCommand,
        "set_media_overlay": SetMediaOverlayCommand,
        "set_licensed_sfx": SetLicensedSfxCommand,
        "apply_speech_cut": ApplySpeechCutCommand,
    }[payload["command"]]
    command = command_type.model_validate(payload)
    plan = CreatorEditPlan(
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        strategy=_strategy(),
        commands=[command],
    )
    assert plan.commands[0].expected_job_id == "job-1"


def test_craft_command_rejects_stale_context_and_capability_ids() -> None:
    with pytest.raises(ValidationError):
        stale_target = _target_kwargs()
        stale_target["expected_context_hash"] = "c" * 64
        CreatorEditPlan(
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            strategy=_strategy(),
            commands=[
                SetTransitionCommand(
                    **stale_target,
                    command="set_transition",
                    boundary_index=0,
                    transition="none",
                )
            ],
        )

    with pytest.raises(ValidationError):
        SetMediaOverlayCommand(
            **_target_kwargs(),
            command="set_media_overlay",
            asset_id="gs://bucket/private.png",
            start_s=0,
            end_s=1,
        )


def test_review_receipt_requires_bounded_evidence_and_revision_when_needed() -> None:
    evidence = CreatorReviewEvidence(
        evidence_id="evidence-1",
        kind="visual",
        start_s=1,
        end_s=2,
        observation="The opening lacks a clear visual change.",
    )
    with pytest.raises(ValidationError, match="proposed_revision"):
        CreatorReviewReceipt(
            creator_id="creator-1",
            creator_session_id="session-1",
            plan_item_id="item-1",
            ownership_epoch=4,
            session_revision=2,
            job_id="job-1",
            variant_id="variant-1",
            render_generation_id="generation-1",
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            review_mode="objective",
            decision="revise",
            evidence=[evidence],
            reviewed_at="2026-08-25T00:00:00Z",
        )

    receipt = CreatorReviewReceipt(
        creator_id="creator-1",
        creator_session_id="session-1",
        plan_item_id="item-1",
        ownership_epoch=4,
        session_revision=2,
        job_id="job-1",
        variant_id="variant-1",
        render_generation_id="generation-1",
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        review_mode="objective",
        decision="revise",
        evidence=[evidence],
        proposed_revision={
            "revision_id": "revision-1",
            "summary": "Strengthen the opening beat.",
            "evidence_ids": ["evidence-1"],
        },
        reviewed_at="2026-08-25T00:00:00Z",
    )
    assert receipt.proposed_revision is not None

    with pytest.raises(ValidationError, match="reference review evidence"):
        CreatorReviewReceipt(
            creator_id="creator-1",
            creator_session_id="session-1",
            plan_item_id="item-1",
            ownership_epoch=4,
            session_revision=2,
            job_id="job-1",
            variant_id="variant-1",
            render_generation_id="generation-1",
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            review_mode="objective",
            decision="revise",
            evidence=[evidence],
            proposed_revision={
                "revision_id": "revision-2",
                "summary": "Use an evidence id that was not observed.",
                "evidence_ids": ["missing-evidence"],
            },
            reviewed_at="2026-08-25T00:00:00Z",
        )


def test_automation_decision_requires_explicit_revision() -> None:
    with pytest.raises(ValidationError, match="require a revision"):
        CreatorAutomationDecision(
            decision="eligible",
            reason_code="high_confidence",
            review_generation_id="generation-1",
            opted_in=True,
            review_mode="objective",
            confidence=0.9,
            current_quality=3.5,
            expected_improvement=0.6,
            render_budget_remaining=1,
            automatic_revision_count=0,
        )

    decision = CreatorAutomationDecision(
        decision="skip",
        reason_code="taste_ambiguous",
        review_generation_id="generation-1",
        opted_in=False,
        review_mode="taste",
        confidence=0.4,
        render_budget_remaining=1,
        automatic_revision_count=0,
    )
    assert decision.decision == "skip"


def test_workspace_relevance_requires_explicit_decision_and_matching_payload() -> None:
    proposal = CreatorWorkspaceRelevanceProposal(
        proposal_id="proposal-1",
        creator_id="creator-1",
        plan_id="plan-1",
        ownership_epoch=4,
        idempotency_key="request-1",
        media_ids=["clip-1"],
        relevance="new_topic",
        topic="Morning market walk",
        confidence=0.8,
        proposal_hash="c" * 64,
    )
    decision = CreatorWorkspaceRelevanceDecision(
        proposal_id=proposal.proposal_id,
        expected_proposal_hash=proposal.proposal_hash,
        decision="accept_new_topic",
        client_event_id="event-1",
    )
    assert decision.decision == "accept_new_topic"

    with pytest.raises(ValidationError):
        CreatorWorkspaceRelevanceProposal(
            proposal_id="proposal-2",
            creator_id="creator-1",
            plan_id="plan-1",
            ownership_epoch=4,
            idempotency_key="request-2",
            media_ids=["clip-1"],
            relevance="existing_item",
            confidence=0.8,
            proposal_hash="c" * 64,
        )


def test_workspace_receipt_requires_one_epoch_and_distinct_item_session_pins() -> None:
    child = CreatorWorkspaceDeliverableReceipt(
        plan_item_id="item-1", creator_session_id="session-1", ownership_epoch=3
    )
    receipt = CreatorWorkspaceReceipt(
        receipt_id="receipt-1",
        creator_id="creator-1",
        plan_id="plan-1",
        ownership_epoch=3,
        deliverables=[child],
    )
    assert receipt.deliverables[0].ownership_epoch == 3
    with pytest.raises(ValidationError):
        CreatorWorkspaceReceipt(
            receipt_id="receipt-2",
            creator_id="creator-1",
            plan_id="plan-1",
            ownership_epoch=3,
            deliverables=[child, child.model_copy(update={"plan_item_id": "item-2"})],
        )
