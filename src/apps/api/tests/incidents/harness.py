"""Real-code harnesses the corpus drives: phone recipe, cloud preflight, planner turn."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreativeStrategy,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.kria import planner
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.strategy_policy import CheckedStrategy
from app.pipeline.phone_speech_montage_plan import (
    PhoneSpeechSection,
    compile_phone_speech_montage_plan,
)
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.services.cloud_render_contract import preflight_cloud_contract
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    CreatorRenderContract,
    verify_phone_recipe,
)
from app.services.phone_sources import PhoneSourceBinding
from tests.incidents.loader import binding_for
from tests.incidents.models import IncidentRecord, MediaFact


def _binding(media: MediaFact) -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id=media.id,
        proxy_path=f"incident/analysis-proxy-{media.id}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=media.duration_s,
            width=1080,
            height=1920,
            orientation_degrees=0,
            has_audio=bool(media.has_audio),
        ),
    )


def phone_recipe(record: IncidentRecord) -> EditRecipeV2:
    """Compile the recorded sections with the REAL speech-montage compiler."""
    spec = record.inputs.phone_recipe
    assert spec is not None
    by_id = {m.id: m for m in record.inputs.media}
    sections = tuple(
        PhoneSpeechSection(
            kind=s.kind,
            speaker=_binding(by_id[s.media_id]) if s.media_id else None,
            source_start_s=s.source_start_s,
            source_end_s=s.source_end_s,
            visual=s.visual,
            duration_s=s.duration_s,
            cut_s=s.cut_s,
        )
        for s in spec.sections
    )
    speakers = {s.media_id for s in spec.sections if s.media_id}
    broll = tuple(_binding(m) for m in record.inputs.media if m.id not in speakers)
    return compile_phone_speech_montage_plan(sections, broll)[0]


def refusal_message(record: IncidentRecord, contract: CreatorRenderContract) -> BaseException:
    """Run the real verifier the record supplies context for and return what it raised."""
    if record.inputs.cloud_preflight:
        assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}
        try:
            preflight_cloud_contract(
                assembly,
                candidates={REQUIREMENT_VERSION_FIELD: 1},
                adapter=record.inputs.cloud_adapter,
            )
        except Exception as exc:  # the verifier's own typed error
            return exc
    else:
        source_audio = {m.id: bool(m.has_audio) for m in record.inputs.media}
        try:
            verify_phone_recipe(contract, phone_recipe(record), source_audio=source_audio)
        except Exception as exc:
            return exc
    raise AssertionError("the verifier accepted the recorded plan; a refusal was expected")


def cloud_verdicts(record: IncidentRecord, contract: CreatorRenderContract) -> dict:
    """Run the real cloud preflight and publication verifier for the recorded adapter/receipt.

    Returns ``{"preflight": exc|None, "publication": exc|None}``: the typed error each real
    entry point raised, or ``None`` when it let the plan / receipt through.
    """
    from app.services.cloud_render_contract import CloudRenderContractError, verify_cloud_variant

    adapter = record.inputs.cloud_adapter
    assert adapter, "a cloud expectation needs inputs.cloud_adapter"
    assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}
    candidates = {REQUIREMENT_VERSION_FIELD: 1}
    archetype = {
        "cloud_guided_story": "guided_story",
        "cloud_classic": "montage",
        "cloud_slides": "slides",
    }[adapter]
    out: dict = {"preflight": None, "publication": None}
    try:
        preflight_cloud_contract(assembly, candidates=candidates, adapter=adapter)
    except CloudRenderContractError as exc:
        out["preflight"] = exc
    variant = {
        "ok": True,
        "render_status": "ready",
        "video_path": "incident/output.mp4",
        "resolved_archetype": archetype,
        **({"render_receipt": record.inputs.cloud_receipt} if record.inputs.cloud_receipt else {}),
    }
    try:
        verify_cloud_variant(assembly, variant, candidates=candidates)
    except CloudRenderContractError as exc:
        out["publication"] = exc
    return out


def _intent_clips(record: IncidentRecord) -> list[IntentClip]:
    return [
        IntentClip(media_id=m.id, kind=m.kind, analysis=None, capture_time=m.capture_time)
        for m in record.inputs.media
    ]


def _group(group) -> ResolvedClipIntent:  # noqa: ANN001
    intent = ClipIntent(intent_id=f"g-{group.name}", op="group", attribute=group.name)
    return ResolvedClipIntent(
        **intent.model_dump(),
        assignments=[
            ClipAssignment(media_id=i, evidence="x", confidence=0.9) for i in group.media_ids
        ],
    )


async def planner_turn(record: IncidentRecord, monkeypatch: pytest.MonkeyPatch):
    """One creator-planner turn over the recorded conversation."""
    turns = record.inputs.turns
    user_turns = [t for t in turns if t.role == "user" and t.text]
    assert user_turns, "a clarification record needs a final user message"
    request = user_turns[-1].text
    events = [
        (
            t.role,
            {
                k: v
                for k, v in (
                    ("choice_question", t.choice_question),
                    ("choice_selection", t.choice_selection),
                )
                if v
            },
        )
        for t in turns
        if t.choice_question or t.choice_selection
    ]
    item_id = uuid.uuid4()
    manifest = ResolvedCreatorManifest(
        item_id=str(item_id),
        edit_format="montage",
        render_program="guided",
        capabilities={"dispatch_render": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    strategy = CreativeStrategy.model_validate(record.approved.strategy or {})
    action = ProposeStrategy(kind="propose_strategy", strategy=strategy, summary="Recorded plan.")
    groups = [_group(g) for g in record.inputs.clip_groups]
    resolver = AsyncMock(
        return_value=PlannedIntentResolution(
            [ClipIntent(intent_id="g", op="group", attribute="x")] if groups else [],
            IntentResolution(intents=groups),
        )
    )
    monkeypatch.setattr(planner.settings, "clip_intents_enabled", True)
    monkeypatch.setattr(planner.settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(planner, "plan_and_resolve_clip_intents", resolver)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events))
    monkeypatch.setattr(
        planner,
        "check_strategy_for_runtime_v2",
        lambda _manifest, strategy, **_kw: CheckedStrategy(strategy=strategy, notices=()),
    )
    # As the real caller does (plan_live_turn): the capture-order flag is read off the live
    # brief and the brief's rendered request rides along. The target length / clip count
    # reach the planner only through the strategy. NOTE: PR-C (KRI-476) moves the gate into
    # plan_live_turn AFTER the media snapshot is attached, so it must adapt this harness.
    binding = binding_for(record)
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=item_id,
        creator_id=uuid.uuid4(),
        user_message=request,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=SimpleNamespace(),
            intent_clips=_intent_clips(record),
            creator_request=request,
        ),
        output=SimpleNamespace(action=action),
        brief_request=binding.creator_request,
        wants_capture_order=planner._brief_wants_capture_order(binding.resolve()),
    )
