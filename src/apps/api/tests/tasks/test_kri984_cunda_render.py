"""KRI-984: the approved Cunda Island montage, replayed through the real phone writer.

Incident (prod thread e3e91b35, job cecdc8d9, 2026-10-27): 11 short videos and 10 pool
photos, an approved hook and the creator's typed intro facts. Nobody asked for music; the
strategy's licensed track is a posting suggestion (compiler v6+ is ``reference_only``). Ten
seconds after approval the job failed ``phone_plan_failed``: "This edit needs a length
choice before it can extend the selected music section." The frozen song section is 16.8 s
(the footage windows' budget) and the edit is 28.8 s (those windows plus ten 1.2 s photo
holds). The same trace had already recorded the typed intro as "That exact text isn't in
this draft" although the render draws it.

The approved inputs are the incident's own (``tests/fixtures/kri984_cunda_island_render.json``,
identities renamed, descriptions and transcripts dropped). Everything below runs the real
dispatcher (``_run_generative_job`` -> ``_run_phone_unified_montage_job`` ->
``_run_phone_guided_job``): the montage planner, the requirement receipts, the strict guided
compiler with the frozen song, the contract repair, the phone compiler, the pilot check and
the pin-time contract verification. Only the database, storage and the creation-text model's
answer (the incident's recorded text operations) are fixed values.
"""

from __future__ import annotations

import copy
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agents._runtime import ModelClient, ModelInvocation
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.unified_montage import plan_unified_montage
from app.services import creation_text_composition, phone_visuals, pipeline_trace
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneVisualBinding, bind_phone_sources
from app.tasks import generative_build as gb
from tests._prod_profile import apply_prod_profile
from tests.pipeline.user_song_helpers import (
    SONG_DURATION_S,
    SONG_GENERATION,
    SONG_ITEM_ID,
    analysis,
    bindings_for,
    song_bed,
    take,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri984_cunda_island_render.json"
DATA = json.loads(_FIXTURE.read_text(encoding="utf-8"))
GENERATION = DATA["generation_id"]
MUSIC = DATA["length_plan"]["licensed_music"]
FACTS = next(r["literal"] for r in DATA["brief"]["requirements"] if r["id"] == "r1")
HOOK = next(r["literal"] for r in DATA["brief"]["requirements"] if r["id"] == "r2")
APPROVED = DATA["guided_edit"]["approved_proposal"]
EDIT_S = 28.804


def _brief() -> CreativeBrief:
    return CreativeBrief(
        version=DATA["brief"]["version"],
        requirements=[BriefRequirement(**row) for row in DATA["brief"]["requirements"]],
    )


def _visual_rows() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(**row, source_filename="", user_context="", content_hash=None)
        for row in DATA["visuals"]
    ]


def _phone_visuals(*_args, story_timeline, **_kwargs) -> tuple[PhoneVisualBinding, ...]:
    pinned = {row["id"]: row for row in DATA["visuals"]}
    return tuple(
        PhoneVisualBinding(
            media_id=moment.media_id,
            gcs_path=pinned[moment.media_id]["gcs_path"],
            generation=pinned[moment.media_id]["gcs_generation"],
            sha256="d" * 64,
            byte_count=2_000_000,
        )
        for moment in story_timeline
        if moment.lane == "asset"
    )


class _TextComposerModel(ModelClient):
    """The creation-text composer's answer for this draft: the incident's own operations
    (prod's agent_run 23011318 output, recorded as the job's text composition), or none."""

    def __init__(self, ops: list[dict]) -> None:
        self.ops = ops
        self.calls = 0

    def invoke(self, *, model: str, prompt: str, **_kwargs) -> ModelInvocation:  # noqa: ARG002
        self.calls += 1
        answer = {
            "intent": "edit" if self.ops else "describe",
            "confidence": 0.9,
            "reply": "Added the introduction about Cunda." if self.ops else "Kept the title.",
            "ops": self.ops,
            "needs_clarification": False,
            "unmet_requests": [],
        }
        return ModelInvocation(raw_text=json.dumps(answer))


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    approved_plan: dict | None = None,
    text_ops: list[dict] = APPROVED["text_composition"]["operations"],
):
    """Dispatch the incident's approved job; return ``(job, trace events)``.

    ``approved_plan`` is the pinned ``guided_edit`` of an earlier delivery (a retry);
    without it the montage writer plans the edit from the approved inputs first, and
    ``text_ops`` is what the creation-text model answered.
    """
    apply_prod_profile(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_guided_text_face_placement_enabled", False)
    brief = _brief()
    assignments = copy.deepcopy(DATA["clip_assignments"])
    bindings = bind_phone_sources(assignments, DATA["clip_paths"])
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-984"),
        brief,
        latest_message="Try again",
        media_snapshot={
            "clip_paths": list(DATA["clip_paths"]),
            "clip_assignments": assignments,
            "length_plan": copy.deepcopy(DATA["length_plan"]),
        },
    )
    assembly = {
        PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
        "creator_generation_id": GENERATION,
        "creator_route": copy.deepcopy(DATA["creator_route"]),
        "creator_brief_binding": binding.model_dump(mode="json"),
        "creator_render_requirements": copy.deepcopy(DATA["contract"]),
        "creator_length_policy": copy.deepcopy(DATA["length_policy"]),
    }
    if approved_plan is not None:
        assembly["guided_edit"] = copy.deepcopy(approved_plan)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        content_plan_item_id=uuid.uuid4(),
        content_plan_id=None,
        assembly_plan=assembly,
        status="queued",
        all_candidates={
            **copy.deepcopy(DATA["candidates"]),
            "clip_paths": list(DATA["clip_paths"]),
            "creator_strategy": copy.deepcopy(DATA["strategy"]),
        },
        error_detail=None,
        failure_reason=None,
    )
    session = Mock()
    session.get.return_value = job

    @contextmanager
    def sessions():
        yield session

    events: list[dict] = []
    monkeypatch.setattr(
        pipeline_trace,
        "record_pipeline_event",
        lambda stage, event, data=None: events.append({"event": event, "data": data or {}}),
    )
    monkeypatch.setattr(gb, "_sync_session", sessions)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 3))
    monkeypatch.setattr(gb, "_cancelled_job_write_rejected", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        gb, "_load_unified_montage_inputs", lambda _job_id: (job.user_id, assignments, brief)
    )
    monkeypatch.setattr(
        gb,
        "_load_unified_montage_visuals",
        lambda _job_id, selected: [gb.unified_visual(row) for row in _visual_rows()],
    )
    composer = _TextComposerModel(copy.deepcopy(text_ops))
    monkeypatch.setattr(creation_text_composition, "default_client", lambda: composer)
    monkeypatch.setattr(phone_visuals, "bind_phone_visuals", _phone_visuals)
    monkeypatch.setattr(
        gb, "_run_guided_story_job", Mock(side_effect=AssertionError("entered the cloud renderer"))
    )

    def fail(_job_id, detail, failure_reason=None, **_kwargs) -> bool:
        job.status, job.error_detail, job.failure_reason = (
            "processing_failed",
            detail,
            failure_reason,
        )
        return True

    monkeypatch.setattr(gb, "_fail_job", fail)
    gb._run_generative_job(str(job.id))
    return job, events


def _failures(events: list[dict]) -> list[dict]:
    return [row["data"] for row in events if row["event"] == "phone_plan_failed"]


def _recovered(events: list[dict]) -> dict[str, dict]:
    return {
        receipt["requirement_id"]: receipt
        for row in events
        if row["event"] == "requirement_recovery"
        for receipt in row["data"]["requirement_receipts"]
    }


def _variant(job) -> dict:
    (variant,) = job.assembly_plan["variants"]
    return variant


def _drawn(job) -> list[tuple[str, float, float]]:
    return [
        (row["text"], row["start_s"], row["end_s"])
        for row in _variant(job)["text_elements"]
        if not row.get("removed")
    ]


# -- the incident ----------------------------------------------------------------------


def test_incident_the_approved_montage_renders_with_its_song_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, events = _run(monkeypatch)
    assert _failures(events) == []
    assert job.status == "awaiting_device", (job.failure_reason, job.error_detail)
    # The writer planned exactly the incident's approved edit: 21 cuts, 28.8 s, its text.
    planned = job.assembly_plan["guided_edit"]["approved_proposal"]
    assert planned["fast_cuts"] == APPROVED["fast_cuts"]
    assert planned["text_composition"] == APPROVED["text_composition"]
    variant = _variant(job)
    assert variant["duration_s"] == pytest.approx(EDIT_S, abs=1e-3)
    # Nobody asked for music: the clips keep their own sound, and the matched song stays
    # a suggestion from its frozen start for the length of the video.
    assert variant["music_playback_mode"] == "reference_only"
    assert variant["source_audio_preserved"] is True
    assert variant["song_reference"] == {
        "schema_version": 1,
        "delivery": "external_platform",
        "track_id": MUSIC["track_id"],
        "title": MUSIC["title"],
        "artist": MUSIC["artist"],
        "start_s": MUSIC["start_s"],
        "end_s": round(MUSIC["start_s"] + EDIT_S, 3),
    }
    assert device_status(job, "guided_story").request.recipe.duration == pytest.approx(
        EDIT_S, abs=0.05
    )


@pytest.mark.parametrize(
    "text_ops",
    [
        pytest.param(APPROVED["text_composition"]["operations"], id="text-lane-placed-it"),
        pytest.param([], id="text-lane-left-it-out"),
    ],
)
def test_incident_the_typed_intro_receipt_agrees_with_the_video(
    monkeypatch: pytest.MonkeyPatch, text_ops: list[dict]
) -> None:
    """Prod recorded r1 as "That exact text isn't in this draft" and sent it to requirement
    recovery. The phone render draws a pinned whole-video text whatever the text lane held
    (``repair_plan_for_contract``), so the receipt says met and the compiled video shows it."""
    job, events = _run(monkeypatch, text_ops=text_ops)
    receipts = {
        row["requirement_id"]: (row["status"], row["reason"])
        for row in job.assembly_plan["unified_montage"]["requirement_receipts"]
    }
    assert receipts == {"r1": ("met", None), "r2": ("met", None)}
    assert _recovered(events) == {}
    assert job.status == "awaiting_device", (job.failure_reason, job.error_detail)
    drawn = _drawn(job)
    assert (HOOK, 0.0, 2.2) in drawn
    assert any(text == FACTS and end - start >= 2.2 for text, start, end in drawn), drawn


def test_a_retry_of_the_pinned_plan_renders_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failed job's own pinned ``guided_edit`` (a redelivery or "Try again")."""
    job, events = _run(monkeypatch, approved_plan=DATA["guided_edit"])
    assert _failures(events) == []
    assert job.status == "awaiting_device", (job.failure_reason, job.error_detail)
    assert _variant(job)["song_reference"]["end_s"] == round(MUSIC["start_s"] + EDIT_S, 3)


# -- the song suggestion against the video's length -------------------------------------


def _suggested(**section) -> dict | None:
    """The approved plan compiled against the frozen catalog section, as the worker does."""
    track = {
        "track_id": MUSIC["track_id"],
        "title": MUSIC["title"],
        "artist": MUSIC["artist"],
        "start_s": MUSIC["start_s"],
        "end_s": MUSIC["end_s"],
        "catalog_duration_s": MUSIC["catalog_duration_s"],
        "beat_timestamps_s": MUSIC["beat_timestamps_s"],
        **section,
    }
    plan = compile_execution_plan(DATA["guided_edit"], track=track)
    assert plan["resolved_duration_s"] == pytest.approx(EDIT_S, abs=1e-3)
    reference = plan.get("song_reference")
    return None if reference is None else (reference["start_s"], reference["end_s"])


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        pytest.param(
            {"end_s": MUSIC["start_s"] + 40.0},
            (MUSIC["start_s"], round(MUSIC["start_s"] + EDIT_S, 3)),
            id="section-longer-than-the-video",
        ),
        pytest.param(
            {},
            (MUSIC["start_s"], round(MUSIC["start_s"] + EDIT_S, 3)),
            id="section-shorter-than-the-video",
        ),
        pytest.param(
            {"catalog_duration_s": MUSIC["start_s"] + 20.0, "end_s": MUSIC["start_s"] + 16.8},
            None,
            id="song-ends-before-the-video",
        ),
    ],
)
def test_the_song_suggestion_runs_the_videos_length_from_its_frozen_start(
    section: dict, expected: tuple[float, float] | None
) -> None:
    assert _suggested(**section) == expected


def test_a_rendered_song_still_refuses_a_video_it_cannot_cover() -> None:
    """The creator's own song plays in the video (KRI-374): the compiler ignores a catalog
    section beside it, and a song shorter than the approved window still fails closed."""
    result = plan_unified_montage(
        [take(f"c{i}", 6.0) for i in range(1, 6)],
        song_beats=analysis().beats_s,
        song_lines=analysis().lines,
        song_duration_s=SONG_DURATION_S,
        song_plan_item_id=SONG_ITEM_ID,
        song_generation=SONG_GENERATION,
    )
    short_section = {
        "track_id": MUSIC["track_id"],
        "title": MUSIC["title"],
        "start_s": 0.0,
        "end_s": 5.0,
        "catalog_duration_s": MUSIC["catalog_duration_s"],
    }
    plan = GuidedStoryExecutionPlan.model_validate(
        compile_execution_plan(result.guided_edit(), track=short_section)
    )
    assert plan.song_reference is None and plan.user_song is not None
    footage, visuals = bindings_for(result)
    with pytest.raises(UnsupportedPhonePlan, match="replaced since approval"):
        compile_phone_guided_plan(
            plan, footage, visuals, song=song_bed(duration_s=plan.user_song.window_end_s - 1.0)
        )
