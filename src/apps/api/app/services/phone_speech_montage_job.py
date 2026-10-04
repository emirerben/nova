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
    if not settings.speech_excerpt_montage_enabled:
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

    request = _request_text(job_id, brief, gb._first_user_message(job_id))
    if not speech_montage_possible(
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

    target = None
    if brief is not None:
        from app.pipeline.unified_montage import brief_view  # noqa: PLC0415

        target = brief_view(brief).target_duration_s

    with pipeline_trace_for(job_id):
        resolution = plan_speech_montage(
            creator_request=request,
            candidates=candidates,
            run_planner=run_planner,
            load_words=load_words,
            target_duration_s=target,
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
    speaker_candidates = {c.media_id for c in candidates if c.words and c.to_camera}
    broll = tuple(
        binding_by_media[c.media_id]
        for c in candidates
        if c.kind == "video" and c.media_id not in speaker_candidates
    )
    recipe, receipt = compile_phone_speech_montage_plan(
        tuple(sections),
        broll,
        target_lufs=settings.output_target_lufs,
    )
    validate_phone_pilot_recipe(recipe)
    if not gb._phone_rendering_globally_available():
        raise ValueError(
            "Phone rendering is currently unavailable; originals remain on the device."
        )

    record = {
        **receipt.record(),
        "planned": resolution.planned_record(),
        "adjustments": [*resolution.adjustments, *receipt.adjustments],
    }
    if brief is not None and brief.live():
        from app.kria.brief_checks import (  # noqa: PLC0415
            build_receipts,
            plan_facts_from_speech_montage,
        )

        record["requirement_receipts"] = [
            r.model_dump(mode="json")
            for r in build_receipts(brief.live(), plan_facts_from_speech_montage(record))
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
        if current.get("creator_generation_id") != generation or current.get(
            PHONE_SOURCES_FIELD
        ) != snapshot.get(PHONE_SOURCES_FIELD):
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
    "SPEECH_MONTAGE_FIELD",
    "SPEECH_MONTAGE_VARIANT_ID",
    "SpeechMontageClarification",
    "run_phone_speech_montage_job",
]
