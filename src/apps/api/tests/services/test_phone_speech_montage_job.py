"""KRI-282: the worker entry for a spoken-excerpt phone montage + its kill switch."""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.media_sources import OriginalMediaDescriptor
from app.schemas.speech_montage import SpeechMontagePlan
from app.services import phone_speech_montage_job as job_module
from app.services.device_render import DEVICE_RENDER_FIELD
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.services.speech_montage_planning import NO_SPEECH_QUESTION
from app.tasks import generative_build as gb

SPEECH = (
    "So the thing I learned in Lisbon was, never rush a good espresso. "
    "Honestly it changed my whole morning routine!"
)
JOB_ID = str(uuid.uuid4())


def _binding(media_id: str, *, width=1080, height=1920, duration_s=30.0) -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"user/analysis-proxy-{media_id}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=duration_s,
            width=width,
            height=height,
            orientation_degrees=0,
            has_audio=True,
        ),
    )


def _words():
    out, t = [], 0.0
    for token in SPEECH.split():
        out.append({"text": token, "start_s": round(t, 3), "end_s": round(t + 0.3, 3)})
        t += 0.4
    return out


class _FakeDb:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


@pytest.fixture
def world(monkeypatch):
    talk = _binding("talk")
    others = [_binding(f"b{i}", width=1920, height=1080, duration_s=9.0) for i in range(3)]
    bindings = [talk, *others]
    assignments = [
        {
            "gcs_path": b.proxy_path,
            "kind": "video",
            "analysis": (
                {
                    "understanding": {
                        "summary": "me talking",
                        "speech": {"has_speech": True, "to_camera": True, "transcript": SPEECH},
                    }
                }
                if b is talk
                else {"understanding": {"summary": "a scene"}}
            ),
        }
        for b in bindings
    ]
    job = SimpleNamespace(
        id=uuid.UUID(JOB_ID),
        user_id=uuid.uuid4(),
        status="processing",
        assembly_plan={
            "creator_generation_id": "gen-1",
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
        },
        error_detail="x",
        failure_reason="x",
    )
    db = _FakeDb()
    state = SimpleNamespace(job=job, db=db, request="Play my espresso line over the clips")
    monkeypatch.setattr(
        gb, "_load_unified_montage_inputs", lambda _id: (job.user_id, assignments, None)
    )
    monkeypatch.setattr(gb, "_first_user_message", lambda _id: state.request)
    monkeypatch.setattr(gb, "_phone_rendering_globally_available", lambda: True)
    monkeypatch.setattr(gb, "_sync_session", lambda: contextlib.nullcontext(db))
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda _db, _id: (job, 3))
    monkeypatch.setattr(type(settings), "phone_rendering_for", lambda self, uid: True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "audioMix"],
    )
    state.snapshot = dict(job.assembly_plan)
    state.candidates = {"clip_paths": [b.proxy_path for b in bindings]}
    return state


def _run(world, plan, **kw):
    return job_module.run_phone_speech_montage_job(
        JOB_ID,
        world.snapshot,
        world.candidates,
        ownership_epoch=3,
        run_planner=lambda _input: plan,
        load_words=lambda _c: (_words(), "en"),
        **kw,
    )


def _plan(*sections):
    return SpeechMontagePlan.model_validate(
        {"wants_speech_excerpts": True, "sections": list(sections)}
    )


def _contract(world, **requirements):
    from app.services.creator_render_contract import CONTRACT_FIELD, CreatorRenderContract

    contract = CreatorRenderContract(generation_id="gen-1").rebind(**requirements)
    world.snapshot[CONTRACT_FIELD] = contract.model_dump(mode="json")
    world.job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    return contract


def test_approved_source_and_duration_reach_planner_without_live_request(world, monkeypatch):
    from app.services.creator_render_contract import CreatorRenderContractError

    _contract(world, audio_source_ids=("talk",), original_audio="require", duration_s=30)
    monkeypatch.setattr(gb, "_first_user_message", lambda _: pytest.fail("live text reread"))
    seen = []

    def planner(value):
        seen.append(value)
        return _plan({"kind": "speech", "clip_ref": "c1", "quote": "never rush a good espresso"})

    with pytest.raises(CreatorRenderContractError, match="length"):
        job_module.run_phone_speech_montage_job(
            JOB_ID,
            world.snapshot,
            world.candidates,
            ownership_epoch=3,
            run_planner=planner,
            load_words=lambda _: (_words(), "en"),
        )
    assert seen[0].target_duration_s == 30
    assert world.db.commits == 0


def test_approved_speech_never_falls_through_when_switch_disabled(world, monkeypatch):
    _contract(world, audio_source_ids=("talk",), original_audio="require")
    monkeypatch.setattr(settings, "speech_excerpt_montage_enabled", False)
    with pytest.raises(job_module.SpeechMontageClarification, match="unavailable"):
        _run(world, SpeechMontagePlan(wants_speech_excerpts=False))


def test_contract_without_speech_does_not_probe_raw_text(world, monkeypatch):
    _contract(world, original_audio="forbid")
    monkeypatch.setattr(gb, "_load_unified_montage_inputs", lambda _: pytest.fail("speech probe"))
    assert _run(world, SpeechMontagePlan(wants_speech_excerpts=False)) is False


def test_approved_voice_and_length_pin_a_matching_real_compiler_recipe(world):
    from app.services.device_render import (
        device_record,
        device_status,
        verify_device_record_contract,
    )

    contract = _contract(world, original_audio="require", audio_source_ids=("talk",), duration_s=8)
    assert _run(
        world, _plan({"kind": "speech", "clip_ref": "c1", "quote": SPEECH, "visual": "cutaways"})
    )
    status = device_status(world.job, "speech_montage")
    record = device_record(world.job, "speech_montage")
    assert world.job.status == "awaiting_device"
    assert abs(status.request.recipe.duration - 8) < 0.8
    assert record["contract_digest"] == contract.digest
    verify_device_record_contract(world.job, record, status)


def test_contracted_retry_preserves_recipe_and_final_guard_rejects_changed_authority(world):
    from app.services.creator_render_contract import CONTRACT_FIELD, CreatorRenderContractError
    from app.services.device_render import (
        device_record,
        device_status,
        mark_device_failed,
        retry_device_render,
        verify_device_record_contract,
    )

    contract = _contract(world, original_audio="require", audio_source_ids=("talk",), duration_s=8)
    _run(world, _plan({"kind": "speech", "clip_ref": "c1", "quote": SPEECH, "visual": "cutaways"}))
    original = device_status(world.job, "speech_montage").request
    mark_device_failed(world.job, "speech_montage", reason_code="export_failed", detail="try again")
    retried = retry_device_render(world.job, "speech_montage")
    assert retried.request.recipe == original.recipe
    assert retried.request.identity.recipe_revision == original.identity.recipe_revision + 1
    record = device_record(world.job, "speech_montage")
    assert record["contract_digest"] == contract.digest
    verify_device_record_contract(world.job, record, retried)
    world.job.assembly_plan[CONTRACT_FIELD] = contract.rebind(duration_s=30).model_dump(mode="json")
    with pytest.raises(CreatorRenderContractError, match="changed"):
        verify_device_record_contract(world.job, record, retried)


def test_ready_plan_pins_a_speech_montage_variant(world) -> None:
    handled = _run(
        world,
        _plan(
            {"kind": "montage", "duration_s": 3.0},
            {
                "kind": "speech",
                "clip_ref": "c1",
                "quote": "never rush a good espresso",
                "visual": "cutaways",
            },
            {
                "kind": "speech",
                "clip_ref": "c1",
                "quote": "changed my whole morning routine",
                "visual": "speaker",
            },
        ),
    )
    assert handled is True and world.db.commits == 1
    job = world.job
    assert job.status == "awaiting_device" and job.error_detail is None
    variant = job.assembly_plan["variants"][0]
    assert variant["variant_id"] == "speech_montage" == variant["resolved_archetype"]
    assert variant["render_destination"] == "device"
    record = job.assembly_plan["speech_montage"]
    assert [s["kind"] for s in record["sections"]] == ["montage", "speech", "speech"]
    assert [s.get("visual") for s in record["sections"][1:]] == ["cutaways", "speaker"]
    pinned = job.assembly_plan[DEVICE_RENDER_FIELD]["speech_montage"]
    tracks = {t["id"]: t for t in pinned["status"]["request"]["recipe"]["tracks"]}
    assert set(tracks) == {"speech-montage", "speech-audio"}


def test_server_editor_commit_fails_closed_with_a_reason(world) -> None:
    from fastapi import HTTPException

    from app.services.phone_editor import prepare_phone_editor_commit

    _run(
        world,
        _plan({"kind": "speech", "clip_ref": "c1", "quote": "never rush a good espresso"}),
    )
    with pytest.raises(HTTPException) as info:
        prepare_phone_editor_commit(
            world.job,
            "speech_montage",
            prepare=lambda _staged: {"has_render_section": True, "sections": {}},
        )
    assert info.value.status_code == 422
    assert "spoken-excerpt montage" in info.value.detail["reason"]


def test_not_requested_falls_through_to_the_ordinary_montage(world) -> None:
    world.request = "fast montage with city names"
    plan = SpeechMontagePlan(wants_speech_excerpts=False)
    assert _run(world, plan) is False
    assert world.db.commits == 0 and "variants" not in world.job.assembly_plan


def test_ungroundable_quote_raises_a_specific_question(world) -> None:
    plan = _plan({"kind": "speech", "clip_ref": "c1", "quote": "I flew to the moon"})
    with pytest.raises(job_module.SpeechMontageClarification, match="I flew to the moon"):
        _run(world, plan)
    assert world.db.commits == 0


def test_planner_question_becomes_the_jobs_clarification(world) -> None:
    plan = SpeechMontagePlan(wants_speech_excerpts=True, question="Whose words should I play?")
    with pytest.raises(job_module.SpeechMontageClarification, match="Whose words"):
        _run(world, plan)


def test_missing_speech_is_an_actionable_question(world, monkeypatch) -> None:
    for row in gb._load_unified_montage_inputs(JOB_ID)[1]:
        row["analysis"] = {"understanding": {"summary": "a scene"}}
    plan = _plan({"kind": "speech", "clip_ref": "c1", "quote": "anything"})
    with pytest.raises(job_module.SpeechMontageClarification) as info:
        _run(world, plan)
    assert str(info.value) == NO_SPEECH_QUESTION


def test_kill_switch_off_is_a_no_op_that_touches_nothing(world, monkeypatch) -> None:
    monkeypatch.setattr(settings, "speech_excerpt_montage_enabled", False)
    before = dict(world.job.assembly_plan)

    def boom(*_a, **_k):
        raise AssertionError("must not run when the kill switch is off")

    monkeypatch.setattr(gb, "_load_unified_montage_inputs", boom)
    handled = job_module.run_phone_speech_montage_job(
        JOB_ID,
        world.snapshot,
        world.candidates,
        ownership_epoch=3,
        run_planner=boom,
        load_words=boom,
    )
    assert handled is False and world.job.assembly_plan == before and world.db.commits == 0


def test_redelivery_after_pinning_does_nothing(world) -> None:
    snapshot = dict(world.snapshot)
    snapshot[DEVICE_RENDER_FIELD] = {"speech_montage": {"base_generation": "gen-1"}}

    def boom(*_a, **_k):
        raise AssertionError("already pinned")

    assert (
        job_module.run_phone_speech_montage_job(
            JOB_ID,
            snapshot,
            world.candidates,
            ownership_epoch=3,
            run_planner=boom,
            load_words=boom,
        )
        is True
    )
    assert world.db.commits == 0


def test_lost_ownership_publishes_nothing(world, monkeypatch) -> None:
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda _db, _id: (world.job, 9))
    plan = _plan({"kind": "speech", "clip_ref": "c1", "quote": "never rush a good espresso"})
    assert _run(world, plan) is True
    assert world.db.commits == 0 and "variants" not in world.job.assembly_plan
