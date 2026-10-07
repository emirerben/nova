"""Real-code harnesses the corpus drives: phone recipe, cloud preflight, planner turn.

The planner harness runs the real creator-output adapter and then the real clarification
gate (``planner._gate_unresolved_choices``), exactly as ``plan_live_turn`` does once the
approved media snapshot is attached."""

from __future__ import annotations

import uuid
from dataclasses import replace
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
from tests.incidents.loader import binding_for, build_contract, media_snapshot
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


def _guided_plan(record: IncidentRecord) -> dict:
    """Compile a guided plan with the REAL compiler from the recorded media order."""
    from app.pipeline.guided_story import compile_execution_plan
    from app.schemas.edit_proposal import (
        EditProposalSnapshot,
        MediaRef,
        StoryBeat,
        canonical_media_digest,
    )

    spec = record.inputs.guided_plan
    assert spec is not None
    media = [
        MediaRef(
            lane="clip",
            media_id=media_id,
            gcs_path=f"incident/{media_id}.mp4",
            generation="1",
            kind="video",
            duration_s=6,
            analysis={"best_moments": [{"start_s": 0, "end_s": 6, "description": "run"}]},
        )
        for media_id in spec.order
    ]
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        goal="Show the run",
        duration_s=3 * len(media),
        title=spec.opening_title or "Run",
        closing_title=spec.closing_title,
        media=media,
        story_beats=[
            StoryBeat(
                beat_id=f"beat-{index}",
                topic="Run",
                thought="Keep going.",
                media_ids=[media_id],
                duration_s=3,
            )
            for index, media_id in enumerate(spec.order)
        ],
    )
    guided = {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {k: getattr(m, k) for k in ("lane", "media_id", "gcs_path", "generation", "kind")}
            for m in media
        ],
    }
    return compile_execution_plan(guided, track=None)


def cloud_verdicts(record: IncidentRecord, contract: CreatorRenderContract) -> dict:
    """Run the real cloud preflight, plan gate and publication verifier for the record.

    Returns ``{"preflight"|"plan_gate"|"publication": exc|None}``: the typed error each real
    entry point raised, or ``None`` when it let the plan / evidence through.
    """
    from app.agents._schemas.text_element import TextElement
    from app.pipeline.guided_story import guided_cloud_evidence
    from app.services.cloud_render_contract import (
        CloudRenderContractError,
        check_guided_plan,
        verify_cloud_variant,
    )

    adapter = record.inputs.cloud_adapter
    assert adapter, "a cloud expectation needs inputs.cloud_adapter"
    assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}
    candidates = {REQUIREMENT_VERSION_FIELD: 1}
    archetype = {
        "cloud_guided_story": "guided_story",
        "cloud_classic": "montage",
        "cloud_slides": "slides",
    }[adapter]
    out: dict = {"preflight": None, "plan_gate": None, "publication": None}
    try:
        preflight_cloud_contract(assembly, candidates=candidates, adapter=adapter)
    except CloudRenderContractError as exc:
        out["preflight"] = exc
    receipt = record.inputs.cloud_receipt
    evidence = record.inputs.cloud_evidence
    if record.inputs.guided_plan is not None:
        plan = _guided_plan(record)
        try:
            check_guided_plan(assembly, candidates=candidates, plan=plan)
        except CloudRenderContractError as exc:
            out["plan_gate"] = exc
        if evidence is None:
            moments = [
                {"moment_id": m["moment_id"], "media_id": m["media_id"]}
                for m in plan["story_timeline"]
            ]
            texts = [{"element_id": e["id"], "visible": True} for e in plan["text_elements"]]
            evidence = guided_cloud_evidence(
                plan,
                moments,
                texts,
                [TextElement.model_validate(e) for e in plan["text_elements"]],
                list(plan["selected_media_ids"]),
                narration_applied=False,
                music_applied=False,
                actual_duration_s=plan["resolved_duration_s"],
            )
        receipt = receipt or {"verified": True, "actual_duration_s": plan["resolved_duration_s"]}
    variant = {
        "ok": True,
        "render_status": "ready",
        "video_path": "incident/output.mp4",
        "resolved_archetype": archetype,
        **({"render_receipt": receipt} if receipt else {}),
        **({"cloud_evidence": evidence} if evidence else {}),
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
    # reach the planner only through the strategy.
    binding = binding_for(record)
    planned = await planner._plan_from_creator_output(
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
    # KRI-476 (PR-C): the clarification gate runs in `plan_live_turn` AFTER the approved
    # media snapshot is attached (the order/length conflicts depend on it), so the
    # harness attaches the record's snapshot and runs that same gate over the plan.
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=binding.resolve()))
    monkeypatch.setattr(type(planner.settings), "creative_brief_for", lambda _self, _id: True)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _self, _id: True)
    if record.inputs.phone_proxy_media:
        monkeypatch.setattr(type(planner.settings), "phone_rendering_for", lambda _self, _id: True)
    planned = replace(planned, media_snapshot=media_snapshot(record))
    return await planner._gate_unresolved_choices(
        SimpleNamespace(), planned, thread_id=uuid.uuid4(), creator_id=uuid.uuid4()
    )


def resolved_route(record: IncidentRecord):
    """The pure resolver's verdict for the record's approved plan on its expected platform."""
    from app.services.render_route import resolve_route, route_inputs_from_job
    from tests.incidents.loader import route_job

    want = record.expect.route
    assert want is not None
    assembly, candidates = route_job(record, want.platform)
    inputs = route_inputs_from_job(assembly, candidates, platform=want.platform)
    assert inputs is not None
    return resolve_route(inputs)


def voice_behind_footage_composition(record: IncidentRecord, monkeypatch: pytest.MonkeyPatch):
    """Compose and verify the record's approved plan with the REAL voice composer (KRI-479).

    Returns ``(recipe, receipt, facts)``; raises whatever the real composer or verifier
    declines with. The contract is rebuilt the way a stamped dispatch does (answers applied
    by the real ``resolve_choices``, commitments derived from the answered strategy).
    """
    from app.pipeline.phone_speech_montage_plan import (
        compile_phone_voice_behind_footage_plan,
        select_voice_window,
    )
    from app.services.choice_questions import resolve_choices
    from app.services.creator_render_contract import commitments_from_strategy
    from tests.incidents.models import OutputFacts

    spec = record.inputs.voice_behind_footage
    assert spec is not None
    strategy = dict(record.approved.strategy or {})
    binding = binding_for(record)
    if spec.answers:
        # Replay the creator's answers through the real gate, question by question.
        events: list = []
        for option in spec.answers:
            resolution = resolve_choices(
                strategy, binding.resolve(), binding.media_snapshot, events
            )
            assert resolution.question is not None, f"nothing to answer with {option!r}"
            question = {
                "question_id": f"q{len(events)}",
                **{
                    "conflict": resolution.question.conflict_id,
                    "kind": resolution.question.kind,
                    "input_digest": resolution.question.input_digest,
                    "options": [{"key": o.key} for o in resolution.question.options],
                },
            }
            events += [
                ("assistant", {"choice_question": question}),
                (
                    "user",
                    {
                        "choice_selection": {
                            "question_id": question["question_id"],
                            "option_key": option,
                        }
                    },
                ),
            ]
        strategy = resolve_choices(
            strategy, binding.resolve(), binding.media_snapshot, events
        ).strategy
    previous = (record.approved.strategy, planner.settings.clip_intents_enabled)
    record.approved.strategy = strategy
    try:
        contract = build_contract(record)
    finally:
        record.approved.strategy = previous[0]
    assert contract is not None and not contract.unresolved, contract
    commitments = commitments_from_strategy(strategy)
    assert commitments is not None, "the plan carries no continuous voice"
    by_id = {m.id: m for m in record.inputs.media}
    voice_id = contract.audio_source_ids[0]
    voice = _binding(by_id[voice_id])
    picture = tuple(_binding(by_id[m]) for m in contract.order_ids)
    duration = contract.duration_s or 24.0
    seconds = by_id[voice_id].duration_s - 1.0
    words, t, i = [], 0.4, 0
    while t + 0.3 < seconds:
        words.append(
            {
                "text": f"w{i}" + ("." if (i + 1) % 5 == 0 else ""),
                "start_s": round(t, 3),
                "end_s": round(t + 0.3, 3),
            }
        )
        t, i = t + 0.5, i + 1
    window = select_voice_window(
        words, source_duration_s=voice.original.duration_s, max_length_s=duration - 0.05
    )
    opening = next((x for x in contract.exact_texts if x.role == "opening"), None)
    recipe, receipt = compile_phone_voice_behind_footage_plan(
        voice,
        window,
        picture,
        duration_s=duration,
        opening_title=opening.text if opening else None,
        opening_title_hold_s=opening.duration_s if opening else None,
        allow_silent_tail=commitments.voice_span_s is not None,
    )
    verify_phone_recipe(
        contract,
        recipe,
        source_audio={m.id: bool(m.has_audio) for m in record.inputs.media},
        composition=commitments,
    )
    manifest = {a.id: a for a in recipe.asset_manifest.assets}
    heard = sorted(
        {
            manifest[clip.source_asset_id].media_id
            for track in recipe.tracks
            if track.kind == "audio"
            for clip in track.clips
            if clip.volume > 0
        }
    )
    shown = [
        manifest[clip.source_asset_id].media_id
        for track in recipe.tracks
        if track.kind == "video"
        for clip in sorted(track.clips, key=lambda c: c.timeline_start)
    ]
    facts = OutputFacts(
        duration_s=recipe.duration,
        voice_source_ids=heard,
        order_ids=shown,
        exact_texts=[opening.text] if opening else None,
    )
    return recipe, receipt, facts
