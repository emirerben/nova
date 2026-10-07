"""Worker side of the phone spoken-excerpt montage (KRI-282).

Called from ``app.tasks.generative_build._run_generative_job_impl`` for a
non-voiceover phone montage, BEFORE the ordinary unified montage planner. It
decides whether the creator asked to use what they say, and if so:

1. transcribes the speech clips (cached whisper) and asks the planner agent to
   choose excerpts and lay out speech / montage sections;
2. grounds every quoted excerpt to real word timings;
3. compiles the sections (``phone_speech_montage_plan``) and pins the device
   recipe as variant ``speech_montage`` (``resolved_archetype`` of the same name).

Returns ``False`` whenever the creator did not ask (or the kill switch is off),
and the caller continues with the ordinary montage, byte-identically. It raises
``SpeechMontageClarification`` (an ``UnsupportedPhonePlan``) when the request
needs an answer from the creator: the message is the specific question, and the
dispatcher persists it as the job's error detail.

Cloud fallback: there is none by design. The cloud renderer cannot represent
speech from one clip over another clip's picture, and a phone-proxy job has no
cloud sources at all (``require_cloud_source_paths``), so this edit is
phone-render only. A creator whose account cannot phone-render never reaches
this job.

Kill switch: ``SPEECH_EXCERPT_MONTAGE_ENABLED=false`` + worker restart.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import structlog

from app.config import settings
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    read_render_contract,
    verify_phone_recipe,
)
from app.services.speech_montage_planning import (
    SpeechCandidate,
    plan_speech_montage,
    speech_montage_possible,
)

log = structlog.get_logger()

SPEECH_MONTAGE_VARIANT_ID = "speech_montage"
SPEECH_MONTAGE_FIELD = "speech_montage"
_MAX_REQUEST_CHARS = 9000


class SpeechMontageClarification(UnsupportedPhonePlan):
    """The creator must answer one specific question before this edit can render."""


def _request_text(job_id: str, brief: Any, first_message: str) -> str:
    parts = [first_message.strip()]
    if brief is not None:
        try:
            parts.extend(req.text() for req in brief.live() if req.text())
        except Exception:  # noqa: BLE001 - the brief only adds recall
            log.warning("speech_montage_brief_unreadable", job_id=job_id)
    seen: set[str] = set()
    unique = [p for p in parts if p and not (p in seen or seen.add(p))]
    return "\n".join(unique)[:_MAX_REQUEST_CHARS]


def _strategy_voice_ids(strategy: Any) -> tuple[str, ...]:
    """The clips the Creator strategy named as the voice (``montage_audio``)."""
    audio = strategy.get("montage_audio") if isinstance(strategy, dict) else None
    if not isinstance(audio, dict) or not audio.get("preserve_source_audio"):
        return ()
    return tuple(str(m) for m in audio.get("source_media_ids") or [] if m)


def _strategy_target_s(strategy: Any) -> float | None:
    raw = strategy.get("target_duration_s") if isinstance(strategy, dict) else None
    ok = isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0
    return float(raw) if ok else None


def run_phone_speech_montage_job(
    job_id: str,
    snapshot: dict,
    all_candidates: dict,
    *,
    ownership_epoch: int | None,
    run_planner: Any = None,
    load_words: Any = None,
) -> bool:
    """See the module docstring. ``run_planner`` / ``load_words`` are test seams."""
    contract = read_render_contract(snapshot)
    speech_required = bool(
        contract and contract.audio_source_ids and contract.original_audio != "forbid"
    )
    if contract is not None and not speech_required:
        return False
    if not settings.speech_excerpt_montage_enabled:
        if speech_required:
            raise SpeechMontageClarification("The confirmed camera-audio renderer is unavailable.")
        return False

    from app.kria.device_render import make_device_request  # noqa: PLC0415
    from app.pipeline.phone_speech_montage_plan import (  # noqa: PLC0415
        PhoneSpeechSection,
        compile_phone_speech_montage_plan,
    )
    from app.services.clip_understanding import clip_record  # noqa: PLC0415
    from app.services.device_render import (  # noqa: PLC0415
        DEVICE_RENDER_FIELD,
        pin_device_request,
    )
    from app.services.phone_rollout import validate_phone_pilot_recipe  # noqa: PLC0415
    from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding  # noqa: PLC0415
    from app.services.pipeline_trace import (  # noqa: PLC0415
        pipeline_trace_for,
        record_pipeline_event,
    )
    from app.tasks import generative_build as gb  # noqa: PLC0415

    generation = snapshot.get("creator_generation_id")
    if not isinstance(generation, str) or not generation:
        return False
    bindings = tuple(
        PhoneSourceBinding.model_validate(row) for row in snapshot.get(PHONE_SOURCES_FIELD) or []
    )
    clip_paths = list(all_candidates.get("clip_paths") or [])
    binding_by_path = {b.proxy_path: b for b in bindings}
    if not bindings or not clip_paths or any(p not in binding_by_path for p in clip_paths):
        return False  # the ordinary planner reports the missing source binding
    if any(
        isinstance(record, dict) and record.get("base_generation") == generation
        for record in (snapshot.get(DEVICE_RENDER_FIELD) or {}).values()
    ):
        return True  # already pinned for this generation: nothing to redo

    user_id, assignments, brief = gb._load_unified_montage_inputs(job_id)
    by_path = {str(row.get("gcs_path")): row for row in assignments}
    candidates: list[SpeechCandidate] = []
    path_by_media: dict[str, str] = {}
    for path in clip_paths:
        binding = binding_by_path[path]
        entry = by_path.get(path) or {}
        kind = "image" if str(entry.get("kind") or "video") == "image" else "video"
        record = clip_record(entry.get("analysis"), kind=kind)
        candidates.append(
            SpeechCandidate(
                media_id=binding.media_id,
                duration_s=float(binding.original.duration_s),
                summary=record.summary or record.subject,
                kind=kind,
                has_speech=record.speech.has_speech,
                to_camera=record.speech.to_camera or record.people.speaks_to_camera,
            )
        )
        path_by_media[binding.media_id] = path

    # Contracted jobs use the approved instruction snapshot. A live thread or
    # the original message can no longer change whether this lane was requested.
    request = (
        str(all_candidates.get("creator_request") or "Use the confirmed camera-audio sources.")
        if contract is not None
        else _request_text(job_id, brief, gb._first_user_message(job_id))
    )
    if not speech_required and not speech_montage_possible(
        request, any_clip_has_speech=any(c.has_speech for c in candidates)
    ):
        return False

    if load_words is None:
        from app.services.speech_segments import transcribe_stored_clip  # noqa: PLC0415

        def load_words(candidate: SpeechCandidate):  # noqa: ANN202
            return transcribe_stored_clip(path_by_media[candidate.media_id])

    if run_planner is None:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.agents.speech_excerpt_planner import SpeechExcerptPlannerAgent  # noqa: PLC0415

        def run_planner(planner_input):  # noqa: ANN202
            return SpeechExcerptPlannerAgent(default_client()).run(
                planner_input,
                ctx=RunContext(
                    job_id=job_id,
                    creator_id=str(user_id),
                    request_id=f"speech-montage:{job_id}",
                ),
            )

    strategy = all_candidates.get("creator_strategy")
    voice_ids = contract.audio_source_ids if contract else _strategy_voice_ids(strategy)
    target = contract.duration_s if contract else None
    order_by_capture = False
    if contract is None and brief is not None:
        from app.pipeline.unified_montage import brief_view  # noqa: PLC0415

        view = brief_view(brief)
        target = view.target_duration_s
        order_by_capture = view.order_by_capture
    # Legacy jobs retain the strategy fallback. Contracted jobs only use the
    # explicit duration bound at approval, never the schema's implicit default.
    if contract is None:
        target = target or _strategy_target_s(strategy)

    with pipeline_trace_for(job_id):
        resolution = plan_speech_montage(
            creator_request=request,
            candidates=candidates,
            run_planner=run_planner,
            load_words=load_words,
            target_duration_s=target,
            required_source_ids=contract.audio_source_ids if contract else (),
            speech_required=speech_required,
            voice_media_ids=voice_ids,
        )
        # Counts and reasons only: quotes are the creator's private words.
        record_pipeline_event(
            "speech_montage",
            "resolved",
            {
                "status": resolution.status,
                "sections": len(resolution.sections),
                "dropped": [d["reason"] for d in resolution.dropped],
            },
        )

    if resolution.status == "not_requested":
        if speech_required:
            raise SpeechMontageClarification("I couldn't build the confirmed camera-audio edit.")
        return False
    if resolution.status == "needs_creator":
        raise SpeechMontageClarification(
            resolution.question or "Which part of what you say should I use?"
        )

    binding_by_media = {b.media_id: b for b in bindings}
    sections = []
    for section in resolution.sections:
        if section.kind == "montage":
            sections.append(
                PhoneSpeechSection(
                    kind="montage", duration_s=section.duration_s, cut_s=section.cut_s
                )
            )
        else:
            sections.append(
                PhoneSpeechSection(
                    kind="speech",
                    speaker=binding_by_media[section.media_id or ""],
                    source_start_s=section.source_start_s,
                    source_end_s=section.source_end_s,
                    visual=section.visual,
                    quote=section.quote,
                )
            )
    speaker_candidates = {c.media_id for c in candidates if c.words and (c.to_camera or voice_ids)}
    broll_ids = [
        c.media_id for c in candidates if c.kind == "video" and c.media_id not in speaker_candidates
    ]
    # The order actually used is ALWAYS recorded (attachment order is a basis too): a
    # receipt can only verify an order the record states, and "nothing recorded" read as a
    # failure for an unstamped brief that simply followed the order the clips were added in.
    ordering_basis = "attachment"
    if contract and contract.order_required:
        if not contract.order_ids or any(
            media_id not in broll_ids for media_id in contract.order_ids
        ):
            raise SpeechMontageClarification(
                "I can't prove the confirmed picture order for this audio edit."
            )
        broll_ids = list(contract.order_ids)
        ordering_basis = contract.order_basis or ordering_basis
    elif order_by_capture:
        from app.services.clip_facts import (  # noqa: PLC0415
            assignment_facts,
            capture_time_from_facts,
            facts_for_prompt,
            order_by_capture_time,
        )

        times = {}
        for path, media_id in ((path_by_media[m], m) for m in broll_ids):
            moment = capture_time_from_facts(facts_for_prompt(assignment_facts(by_path[path])))
            if moment is not None:
                times[media_id] = moment
        ordered = order_by_capture_time(broll_ids, times)
        broll_ids, ordering_basis = ordered.ordered_ids, ordered.basis
    broll = tuple(binding_by_media[m] for m in broll_ids)
    recipe, receipt = compile_phone_speech_montage_plan(
        tuple(sections),
        broll,
        target_lufs=settings.output_target_lufs,
    )
    validate_phone_pilot_recipe(recipe)
    if contract:
        verify_phone_recipe(
            contract, recipe, source_audio={b.media_id: b.original.has_audio for b in bindings}
        )
    if not gb._phone_rendering_globally_available():
        raise ValueError(
            "Phone rendering is currently unavailable; originals remain on the device."
        )

    record = {
        **receipt.record(),
        "planned": resolution.planned_record(),
        "adjustments": [*resolution.adjustments, *receipt.adjustments],
    }
    if ordering_basis:
        record["ordering_basis"] = ordering_basis
    if brief is not None and brief.live():
        from app.kria.brief_checks import (  # noqa: PLC0415
            build_receipts,
            plan_facts_from_speech_montage,
        )

        record["requirement_receipts"] = [
            r.model_dump(mode="json")
            for r in build_receipts(
                brief.live(),
                plan_facts_from_speech_montage(record),
                # The stricter order verdicts need an authority that can verify the order:
                # a contract-stamped job or a brief-binding cohort. Legacy jobs keep theirs.
                strict_order=contract is not None or bool(snapshot.get("creator_brief_binding")),
            )
        ]

    request_obj = make_device_request(
        job_id=uuid.UUID(job_id),
        variant_id=SPEECH_MONTAGE_VARIANT_ID,
        revision=1,
        recipe=recipe,
    )
    with gb._sync_session() as db:
        entry = gb._lock_owned_entry_job(db, job_id)
        if (
            entry is None
            or entry[1] != ownership_epoch
            or entry[0].status == gb._CANCELLED_JOB_STATUS
        ):
            return True
        job = entry[0]
        if not settings.phone_rendering_for(job.user_id):
            raise ValueError("Phone rendering is unavailable for this account")
        current = copy.deepcopy(job.assembly_plan or {})
        if (
            current.get("creator_generation_id") != generation
            or current.get(PHONE_SOURCES_FIELD) != snapshot.get(PHONE_SOURCES_FIELD)
            or current.get(CONTRACT_FIELD) != snapshot.get(CONTRACT_FIELD)
        ):
            return True
        if any(
            isinstance(rec, dict) and rec.get("base_generation") == generation
            for rec in (current.get(DEVICE_RENDER_FIELD) or {}).values()
        ):
            return True
        if current.get("variants"):
            raise ValueError("Initial phone planning cannot replace existing variants")
        current["variants"] = [
            {
                "variant_id": SPEECH_MONTAGE_VARIANT_ID,
                "rank": 1,
                "text_mode": "none",
                "resolved_archetype": SPEECH_MONTAGE_VARIANT_ID,
                "render_generation_id": generation,
                "render_status": "awaiting_device",
                "render_destination": "device",
                "duration_s": round(receipt.duration_s, 3),
                "intro_mode": "linear",
                "intro_layout": "linear",
                "text_elements": [],
                "orientation": "portrait",
                "ok": False,
            }
        ]
        current[SPEECH_MONTAGE_FIELD] = record
        job.assembly_plan = current
        pin_device_request(job, request_obj, base_generation=generation)
        job.status = "awaiting_device"
        job.error_detail = None
        job.failure_reason = None
        db.commit()
    return True


def _voice_decline(
    message: str, reason: str, field_path: str | None, alternative: str
) -> UnsupportedPhonePlan:
    from app.services.creator_render_contract import CreatorRenderContractError  # noqa: PLC0415

    return CreatorRenderContractError(
        message,
        decline_reason=reason,  # type: ignore[arg-type]
        field_path=field_path,
        alternative=alternative,
    )


def run_phone_voice_behind_footage_job(
    job_id: str,
    snapshot: dict,
    all_candidates: dict,
    *,
    ownership_epoch: int | None,
    load_words: Any = None,
) -> bool:
    """KRI-479: compose ONE clip's voice under the other clips, from the approved plan alone.

    Called by the phone dispatcher only for a STAMPED job whose route resolves to
    ``voice_behind_footage``. Everything it decides comes from the pinned contract (voice
    clip, picture order, length, opening words), the composition commitments next to it,
    and typed strategy values -- never the request text, a live thread, or an LLM planner.
    The compiled recipe must pass ``verify_phone_recipe`` WITH the commitments before it is
    pinned as the (non-editable) ``speech_montage`` variant, so a render path cannot ship a
    different edit than the one that was approved. Returns ``False`` when it cannot start
    (the dispatcher turns that into a typed decline); raises typed declines otherwise.

    ``load_words(binding)`` is a test seam returning ``(words, language)``.
    """
    from app.services.creator_render_contract import (  # noqa: PLC0415
        read_composition,
        stamped_plan_contract,
        verify_phone_recipe,
    )

    contract = stamped_plan_contract(snapshot, all_candidates)
    if contract is None:
        return False
    if not settings.speech_excerpt_montage_enabled:
        raise _voice_decline(
            "The confirmed camera-audio renderer is unavailable.",
            "capability_unavailable",
            "montage_audio.source_media_ids[]",
            "Try again later, or ask for a plain montage.",
        )

    from app.kria.device_render import make_device_request  # noqa: PLC0415
    from app.pipeline.phone_recipe_shared import EXPORT_SAFETY_MARGIN_S  # noqa: PLC0415
    from app.pipeline.phone_speech_montage_plan import (  # noqa: PLC0415
        compile_phone_voice_behind_footage_plan,
        implicit_picture_duration,
        select_voice_window,
    )
    from app.services.device_render import (  # noqa: PLC0415
        DEVICE_RENDER_FIELD,
        pin_device_request,
    )
    from app.services.phone_rollout import validate_phone_pilot_recipe  # noqa: PLC0415
    from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding  # noqa: PLC0415
    from app.services.pipeline_trace import (  # noqa: PLC0415
        pipeline_trace_for,
        record_pipeline_event,
    )
    from app.tasks import generative_build as gb  # noqa: PLC0415

    generation = snapshot.get("creator_generation_id")
    if not isinstance(generation, str) or not generation:
        return False
    bindings = tuple(
        PhoneSourceBinding.model_validate(row) for row in snapshot.get(PHONE_SOURCES_FIELD) or []
    )
    clip_paths = list(all_candidates.get("clip_paths") or [])
    binding_by_path = {b.proxy_path: b for b in bindings}
    if not bindings or not clip_paths or any(p not in binding_by_path for p in clip_paths):
        return False
    if any(
        isinstance(record, dict) and record.get("base_generation") == generation
        for record in (snapshot.get(DEVICE_RENDER_FIELD) or {}).values()
    ):
        return True  # already pinned for this generation: nothing to redo

    composition = read_composition(snapshot, contract.digest)
    if composition is None or composition.voice_picture != "hidden":
        raise _voice_decline(
            "This edit lost its composition plan; please try again.",
            "evidence_missing",
            "voice_mode",
            "Ask me to make the edit again from your latest request.",
        )
    if len(contract.audio_source_ids) != 1:
        raise _voice_decline(
            "I need to know which clip is your voice.",
            "needs_choice",
            "montage_audio.source_media_ids[]",
            "Tell me which clip is your voice.",
        )
    binding_by_media = {b.media_id: b for b in bindings}
    voice_id = contract.audio_source_ids[0]
    voice = binding_by_media.get(voice_id)
    if voice is None or voice.media_id not in {binding_by_path[p].media_id for p in clip_paths}:
        raise _voice_decline(
            "I can't find the clip you picked as your voice.",
            "capability_unavailable",
            "montage_audio.source_media_ids[]",
            "Pick a clip where you talk to use as the voice.",
        )
    if contract.order_required:
        ordered_ids = list(contract.order_ids)
        ordering_basis = contract.order_basis or "attachment"
    else:
        # No order was promised: the clips keep the order they were added in.
        ordered_ids = [binding_by_path[p].media_id for p in clip_paths]
        ordered_ids = [m for m in ordered_ids if m != voice_id]
        ordering_basis = "attachment"
    if voice_id in ordered_ids or any(m not in binding_by_media for m in ordered_ids):
        raise _voice_decline(
            "I can't prove the confirmed picture order for this audio edit.",
            "evidence_missing",
            "ordering_choice",
            "Ask me to make the edit again from your latest request.",
        )
    picture = tuple(binding_by_media[m] for m in ordered_ids)

    if load_words is None:
        from app.services.speech_segments import transcribe_stored_clip  # noqa: PLC0415

        def load_words(binding):  # noqa: ANN202
            return transcribe_stored_clip(binding.proxy_path)

    strategy = all_candidates.get("creator_strategy")
    margin = EXPORT_SAFETY_MARGIN_S
    with pipeline_trace_for(job_id):
        words, _language = load_words(voice)
        source_s = float(voice.original.duration_s)
        adjustments: list[str] = []
        if contract.duration_s is not None:
            duration_s = float(contract.duration_s)
        else:
            # No length was stated (the plan's own pick, or the voice's): the voice's length
            # capped at the pick, EXTENDED so every clip is shown and kept to the footage there
            # is, all in whole frames exactly as the composer allocates them. A creator-stated
            # length never comes through here (it is pinned in the contract).
            full = select_voice_window(words, source_duration_s=source_s, max_length_s=None)
            length = implicit_picture_duration(
                picture,
                speech_s=full.length_s + margin,
                target_s=_strategy_target_s(strategy) or 24.0,
                min_shot_s=composition.min_shot_s,
            )
            duration_s = length.duration_s
            adjustments.extend(length.adjustments)
        window = select_voice_window(
            words, source_duration_s=source_s, max_length_s=duration_s - margin
        )
        opening = next((t for t in contract.exact_texts if t.role == "opening"), None)
        recipe, receipt = compile_phone_voice_behind_footage_plan(
            voice,
            window,
            picture,
            duration_s=duration_s,
            opening_title=opening.text if opening else None,
            opening_title_hold_s=opening.duration_s if opening else None,
            min_shot_s=composition.min_shot_s,
            allow_silent_tail=composition.voice_span_s is not None,
            target_lufs=settings.output_target_lufs,
        )
        # Counts only: the voice is the creator's private words.
        record_pipeline_event(
            "voice_behind_footage",
            "composed",
            {
                "shots": len(receipt.shots),
                "duration_s": round(receipt.duration_s, 3),
                "voice_span_s": round(receipt.voice_span_s, 3),
                "hard_cut": window.hard_cut,
            },
        )
    validate_phone_pilot_recipe(recipe)
    verified = composition
    if composition.voice_span_s is not None:
        # A chosen silent tail promises the voice plays what it has: the clip's length at
        # plan time, never more than the speech the transcript actually found.
        verified = composition.model_copy(
            update={"voice_span_s": min(composition.voice_span_s, window.length_s)}
        )
    verify_phone_recipe(
        contract,
        recipe,
        source_audio={b.media_id: b.original.has_audio for b in bindings},
        composition=verified,
    )
    if not gb._phone_rendering_globally_available():
        raise ValueError(
            "Phone rendering is currently unavailable; originals remain on the device."
        )

    record = {
        **receipt.record(),
        "route": "voice_behind_footage",
        # The shape brief receipts read off a camera-audio montage: the voice section only.
        "sections": [
            {
                "index": 0,
                "kind": "speech",
                "visual": "cutaways",
                "media_id": voice_id,
                "quote": "",
                "start_s": 0.0,
                "end_s": round(receipt.duration_s, 3),
            }
        ],
        "cut_count": len(receipt.shots),
        "ordering_basis": ordering_basis,
        "adjustments": [*adjustments, *receipt.adjustments],
    }
    _user_id, _assignments, brief = gb._load_unified_montage_inputs(job_id)
    if brief is not None and brief.live():
        from app.kria.brief_checks import (  # noqa: PLC0415
            build_receipts,
            plan_facts_from_speech_montage,
        )

        record["requirement_receipts"] = [
            r.model_dump(mode="json")
            for r in build_receipts(
                brief.live(),
                plan_facts_from_speech_montage(record),
                strict_order=True,  # a stamped job has a pinned contract to verify the order
            )
        ]

    request_obj = make_device_request(
        job_id=uuid.UUID(job_id),
        variant_id=SPEECH_MONTAGE_VARIANT_ID,
        revision=1,
        recipe=recipe,
    )
    with gb._sync_session() as db:
        entry = gb._lock_owned_entry_job(db, job_id)
        if (
            entry is None
            or entry[1] != ownership_epoch
            or entry[0].status == gb._CANCELLED_JOB_STATUS
        ):
            return True
        job = entry[0]
        if not settings.phone_rendering_for(job.user_id):
            raise ValueError("Phone rendering is unavailable for this account")
        current = copy.deepcopy(job.assembly_plan or {})
        if (
            current.get("creator_generation_id") != generation
            or current.get(PHONE_SOURCES_FIELD) != snapshot.get(PHONE_SOURCES_FIELD)
            or current.get(CONTRACT_FIELD) != snapshot.get(CONTRACT_FIELD)
        ):
            return True
        if any(
            isinstance(rec, dict) and rec.get("base_generation") == generation
            for rec in (current.get(DEVICE_RENDER_FIELD) or {}).values()
        ):
            return True
        if current.get("variants"):
            raise ValueError("Initial phone planning cannot replace existing variants")
        current["variants"] = [
            {
                "variant_id": SPEECH_MONTAGE_VARIANT_ID,
                "rank": 1,
                "text_mode": "none",
                "resolved_archetype": SPEECH_MONTAGE_VARIANT_ID,
                "render_generation_id": generation,
                "render_status": "awaiting_device",
                "render_destination": "device",
                "duration_s": round(receipt.duration_s, 3),
                "intro_mode": "linear",
                "intro_layout": "linear",
                "text_elements": [],
                "orientation": "portrait",
                "ok": False,
            }
        ]
        current[SPEECH_MONTAGE_FIELD] = record
        job.assembly_plan = current
        pin_device_request(job, request_obj, base_generation=generation)
        job.status = "awaiting_device"
        job.error_detail = None
        job.failure_reason = None
        db.commit()
    return True


__all__ = [
    "run_phone_voice_behind_footage_job",
    "SPEECH_MONTAGE_FIELD",
    "SPEECH_MONTAGE_VARIANT_ID",
    "SpeechMontageClarification",
    "run_phone_speech_montage_job",
]
