"""Review of #2129: "keep the whole clip" must not stay "met" after a freeze shortens the cut.

An 11 s clip, "make it 11 seconds" and "freeze the frame at 5 seconds for 1 second": the early
windows (`best_parts`, the length plan) say 0-11 s, but the compiled cut is 0-5 s (+ a 1 s
hold). The old receipt judged those early windows and the render-ready review kept the stale
stored "met". Keep-whole is now judged from the final source cuts only: the worker stores them
(`FINAL_SOURCE_CUTS`), and the finished variant's `story_timeline` re-judges the ask on the
render-ready review. Failure modes: `docs/reviews/says-done-receipts-failure-modes.md`
rows 2.0 / 2.0b. Drives the REAL review (`_approved_generation_review`); no database, no
model, no network.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    FINAL_SOURCE_CUTS,
    build_receipts,
    montage_final_source_cuts,
    plan_facts_from_unified_montage,
)
from app.kria.planner import _enrich_best_parts_snapshot
from app.kria.reply_language import reply_language_for
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.services.device_render import DEVICE_RENDER_FIELD
from app.tasks import kria_runtime

THREAD = uuid.UUID("0b1c7a5e-3f1d-4c52-9a7e-5d1d2f6b9a10")
GENERATION = "9f3c1d0a5b7e4c2f8a6d1e0b3c5a7f92"
ATTEMPT = "5c1e2b7a-3d4f-4a6b-8c9d-0e1f2a3b4c5d"
DEFAULT = "The confirmed edit is ready."
CLIP_S = 11.0
MOMENTS = [
    {"start_s": 0.5, "end_s": 3.5, "energy": 5.0, "description": "places phone on floor"},
    {"start_s": 3.5, "end_s": 7.5, "energy": 8.0, "description": "sits down by sofa"},
    {"start_s": 7.5, "end_s": 11.0, "energy": 6.0, "description": "opens laptop"},
]
STRATEGY = {
    "direction": "fast_montage",
    "edit_format": "montage",
    "audio_strategy": "licensed_music",
    "story_structure": ["she sits down", "she starts working"],
    "caption_style": "none",
    "pacing": "balanced",
    "target_duration_s": 11,
    "render_program": "guided",
    "selected_media_ids": [],
    "optional_treatments": [],
    "rationale": "one continuous shot",
    "pinned_texts": [],
}
KEEP_WHOLE = BriefRequirement(
    id="r4",
    kind="timing",
    scope="global",
    description="keep the whole clip, don't cut it",
    facts={"keep_whole_take": True},
)
ELEVEN_SECONDS = BriefRequirement(
    id="r9",
    kind="timing",
    scope="global",
    description="make it 11 seconds",
    facts={"duration_s": 11},
)
FREEZE = BriefRequirement(
    id="r5",
    kind="timing",
    scope="global",
    description="Freeze the frame at 5 seconds for 1 second",
    facts={"freeze_frame": True},
)
CUT_EN = "This draft keeps 5 s of your 11 s clip, so it's cut."
CUT_TR = "Bu taslak 11 sn'lik klibinin 5 sn'sini tutuyor, yani klip kesiliyor."
FINISHED_EN = "The finished video plays 5 s of your 11 s clip, so it's cut."
FINISHED_TR = "Bitmiş video 11 sn'lik klibinin yalnızca 5 sn'sini oynatıyor, yani klip kesiliyor."


def _brief(*, freeze: bool) -> CreativeBrief:
    extra = [FREEZE] if freeze else []
    return CreativeBrief(version=1, requirements=[KEEP_WHOLE, ELEVEN_SECONDS, *extra])


def _plan(brief: CreativeBrief):
    snapshot = {
        "clip_assignments": [
            {
                "media_id": "clip-1",
                "kind": "video",
                "duration_s": CLIP_S,
                "has_audio": True,
                "generation": "1",
                "analysis": {"best_moments": MOMENTS},
            }
        ]
    }
    length_plan = _enrich_best_parts_snapshot(STRATEGY, snapshot, brief)["length_plan"]
    clip = UnifiedClip(
        media_id="clip-1",
        proxy_path="users/u/analysis-proxy-clip-1.mp4",
        generation="1",
        duration_s=CLIP_S,
        width=1080,
        height=1920,
        analysis={"best_moments": MOMENTS},
    )
    return plan_unified_montage(
        [clip],
        brief_view(brief),
        strategy=STRATEGY,
        approved_length_plan=length_plan,
        freeze_frames=True,
    )


def _stored(req_id: str, status: str, *, reason: str | None = None) -> dict:
    return {
        "requirement_id": req_id,
        "status": status,
        "verification": "checked",
        "stage": "checked",
        "reason": reason,
        "inferred": [],
        "inferred_labels": [],
        "target_media_ids": [],
        "brief_version": 1,
        "generation_id": GENERATION,
    }


def _worker_record(plan, brief: CreativeBrief) -> dict:
    """The unified_montage record as the worker stores it (generative_build)."""
    record = plan.record()
    record[FINAL_SOURCE_CUTS] = montage_final_source_cuts(plan)
    record["brief_version"] = 1
    record["generation_id"] = GENERATION
    receipts = build_receipts(
        brief.live(), plan_facts_from_unified_montage(record), include_unchecked=True
    )
    record["requirement_receipts"] = [
        {**r.model_dump(mode="json"), "brief_version": 1, "generation_id": GENERATION}
        for r in receipts
    ]
    return record


def _stored_row(record: dict, req_id: str = "r4") -> dict:
    return next(r for r in record["requirement_receipts"] if r["requirement_id"] == req_id)


def _variant(plan) -> dict:
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    return {
        "variant_id": "guided_story",
        "render_status": "ready",
        "render_destination": "device",
        "render_generation_id": ATTEMPT,
        "resolved_archetype": "guided_story",
        "edit_duration_s": plan.duration_s,
        "duration_s": plan.duration_s,
        "source_audio_preserved": True,
        "music_playback_mode": "reference_only",
        "text_elements": execution.get("text_elements") or [],
        "story_timeline": execution["story_timeline"],
    }


def _job(record: dict) -> dict:
    return {
        "creator_generation_id": GENERATION,
        "unified_montage": record,
        DEVICE_RENDER_FIELD: {
            "guided_story": {
                "published_attempt": ATTEMPT,
                "base_generation": ATTEMPT,
                "requirement_generation": GENERATION,
            }
        },
    }


def _review(record: dict, variant: dict, brief: CreativeBrief, *, lang="en") -> dict:
    binding = BriefBinding.create(THREAD, brief).model_dump(mode="json")
    thread = SimpleNamespace(id=THREAD, creator_id=uuid.uuid4())
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan=_job(record), all_candidates={})
    execution = SimpleNamespace(result={"brief_binding": binding})
    with reply_language_for(lang):
        _text, payload = kria_runtime._approved_generation_review(
            None, thread, job, variant, execution, DEFAULT
        )
    return {row["requirement_id"]: row for row in payload}


def test_a_freeze_that_shortens_the_clip_is_not_met_after_render():
    brief = _brief(freeze=True)
    plan = _plan(brief)
    record = _worker_record(plan, brief)

    stored = _stored_row(record)
    assert (stored["status"], stored["verification"]) == ("partial", "checked")
    assert stored["reason"] == CUT_EN

    row = _review(record, _variant(plan), brief)["r4"]
    assert (row["status"], row["verification"]) == ("partial", "checked")
    assert row["reason"] == FINISHED_EN


def test_a_stale_stored_met_is_corrected_by_the_finished_cuts():
    """The reviewer's path: a record without final cuts whose r4 was stored "met" from
    `best_parts` 0-11 s (what the old code wrote). The finished cut is still read."""
    brief = _brief(freeze=True)
    plan = _plan(brief)
    record = _worker_record(plan, brief)
    del record[FINAL_SOURCE_CUTS]
    record["best_parts"] = [{"media_id": "clip-1", "source_start_s": 0.0, "source_end_s": 11.0}]
    record["requirement_receipts"] = [
        _stored("r4", "met"),
        *(r for r in record["requirement_receipts"] if r["requirement_id"] != "r4"),
    ]

    row = _review(record, _variant(plan), brief)["r4"]
    assert (row["status"], row["verification"]) == ("partial", "checked")
    assert row["reason"] == FINISHED_EN


def test_control_a_whole_clip_is_met_at_the_worker_and_after_render():
    brief = _brief(freeze=False)
    plan = _plan(brief)
    record = _worker_record(plan, brief)
    assert [(c["source_start_s"], c["source_end_s"]) for c in record[FINAL_SOURCE_CUTS]] == [
        (0.0, 11.0)
    ]
    assert _stored_row(record)["status"] == "met"

    row = _review(record, _variant(plan), brief)["r4"]
    assert (row["status"], row["verification"]) == ("met", "checked")


def test_the_cut_clip_is_told_in_turkish_too():
    brief = _brief(freeze=True)
    plan = _plan(brief)
    with reply_language_for("tr"):
        record = _worker_record(plan, brief)
    assert _stored_row(record)["reason"] == CUT_TR

    row = _review(record, _variant(plan), brief, lang="tr")["r4"]
    assert row["status"] == "partial"
    assert row["reason"] == FINISHED_TR
