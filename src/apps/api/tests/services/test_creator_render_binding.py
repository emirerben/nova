"""Approval provenance survives Creator strategy normalization and job minting."""

from __future__ import annotations

import json
import uuid

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreatorMediaRef,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.kria.drafts import _sanitize_strategy_duration_provenance
from app.services.creator_render_contract import CONTRACT_FIELD, read_render_contract
from app.services.generative_jobs import build_generative_job


def _input() -> MainCreatorInput:
    manifest = ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="montage",
        render_program="guided",
        media=[CreatorMediaRef(media_id="clip-1", kind="video", duration_s=30)],
        capabilities={
            "edit_format:montage": CapabilityAvailability(available=True),
            "draft_guided_proposal": CapabilityAvailability(available=True),
            "dispatch_render": CapabilityAvailability(available=True),
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    return MainCreatorInput(user_message="Make it 24 seconds.", capability_manifest=manifest)


def _raw_strategy(*, target_duration_s: int | None, provenance: object = None) -> str:
    strategy: dict[str, object] = {
        "direction": "fast_montage",
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "render_program": "guided",
        "rationale": "Keep a clear visual story.",
    }
    if target_duration_s is not None:
        strategy["target_duration_s"] = target_duration_s
    if provenance is not None:
        strategy["target_duration_requested"] = provenance
    return json.dumps(
        {"action": {"kind": "propose_strategy", "strategy": strategy, "summary": "Plan."}}
    )


def test_explicit_default_duration_keeps_server_provenance_through_normalization() -> None:
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw_strategy(target_duration_s=24), _input()
    )

    assert isinstance(output.action, ProposeStrategy)
    persisted = output.action.strategy.model_dump(mode="json", exclude_none=True)
    assert persisted["target_duration_requested"] is True
    # Legacy defaults remain omitted, while an explicitly approved default
    # retains both the numeric requirement and its server provenance.
    assert persisted["target_duration_s"] == 24


def test_raw_model_provenance_is_not_trusted() -> None:
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw_strategy(target_duration_s=24, provenance=False), _input()
    )

    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.target_duration_requested is True


def test_client_draft_duration_provenance_is_derived_from_raw_duration() -> None:
    explicit = _sanitize_strategy_duration_provenance(
        {"target_duration_s": 24, "target_duration_requested": False}
    )
    spoofed = _sanitize_strategy_duration_provenance({"target_duration_requested": True})

    assert explicit["target_duration_s"] == 24
    assert explicit["target_duration_requested"] is True
    assert "target_duration_s" not in spoofed
    assert "target_duration_requested" not in spoofed


def test_implicit_default_has_no_duration_requirement() -> None:
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/clip.mp4"],
        creator_strategy={"opening_title": "Hello"},
    )

    contract = read_render_contract(job.assembly_plan)
    assert contract is not None
    assert contract.duration_s is None


def test_explicit_default_duration_binds_to_factory_generation() -> None:
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw_strategy(target_duration_s=24), _input()
    )
    assert isinstance(output.action, ProposeStrategy)

    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/clip.mp4"],
        creator_strategy=output.action.strategy.model_dump(mode="json", exclude_none=True),
    )

    contract = read_render_contract(job.assembly_plan)
    assert contract is not None
    assert contract.generation_id == job.assembly_plan["creator_generation_id"]
    assert CONTRACT_FIELD in job.assembly_plan
    assert contract.duration_s == 24


def test_noncreator_job_keeps_legacy_assembly_shape() -> None:
    job = build_generative_job(user_id=uuid.uuid4(), clip_paths=["slot-uploads/legacy.mp4"])

    assert job.assembly_plan is None
