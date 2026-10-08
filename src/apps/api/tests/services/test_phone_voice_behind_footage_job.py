"""KRI-479: the worker entry that composes a continuous voice under the other clips.

Real composer, real verifier, real pin; only the database and the transcript are faked.
Failure modes written first:

* the job re-reads the request / a live thread and decides something the plan did not;
* a different clip's sound plays, or the voice clip's own picture shows (the KRI-469 bug);
* the order, length or opening words differ from the approved contract and it ships anyway;
* a short voice / too many clips / too little footage quietly render a different edit;
* a stale or missing commitment is guessed instead of declined;
* an UNSTAMPED job is touched.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.config import settings
from app.services import phone_speech_montage_job as job_module
from app.services.creator_render_contract import (
    COMPOSITION_FIELD,
    CONTRACT_FIELD,
    PLAN_AUTHORITY_FIELD,
    CreatorRenderContractError,
    build_render_contract,
    commitments_from_strategy,
    stamp_composition,
)
from app.services.device_render import DEVICE_RENDER_FIELD
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from tests.services.test_phone_speech_montage_job import _binding

JOB_ID = str(uuid.uuid4())
SECRET = "SECRET-REQUEST-TEXT-DO-NOT-READ"


def _words(count: int = 130, *, step: float = 0.5, sentence: int = 5, start: float = 0.4):
    out, t = [], start
    for i in range(count):
        mark = "." if (i + 1) % sentence == 0 else ""
        out.append({"text": f"w{i}{mark}", "start_s": round(t, 3), "end_s": round(t + 0.3, 3)})
        t += step
    return out


class _FakeDb:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _strategy(**update) -> dict:
    base = {
        "audio_strategy": "original_audio",
        "voice_mode": "continuous",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": ["talk"]},
        "ordering_choice": "chronological",
        "target_duration_s": 24,
        "target_duration_requested": True,
        "opening_title": "Summer in Lisbon",
        "opening_title_duration_s": 3,
    }
    return CreativeStrategy.model_validate({**base, **update}).model_dump(
        mode="json", exclude_none=True
    )


def _world(monkeypatch, *, voice_s=60.0, count=4, clip_s=9.0, strategy=None, stamped=True):
    strategy = strategy or _strategy()
    talk = _binding("talk", duration_s=voice_s)
    others = [_binding(f"b{i}", width=1920, height=1080, duration_s=clip_s) for i in range(count)]
    bindings = [talk, *others]
    snapshot_rows = [
        {
            "media_id": b.media_id,
            "kind": "video",
            "capture": {"capture_time": f"2026-06-01T09:{i:02d}:00Z"},
        }
        for i, b in enumerate([*others, talk])  # the voice clip was filmed LAST
    ]
    assembly = {
        "creator_generation_id": "gen-1",
        PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
    }
    contract = build_render_contract(
        strategy,
        generation_id="gen-1",
        media_snapshot={"clip_assignments": snapshot_rows},
        composition=commitments_from_strategy(strategy),
    )
    assert contract is not None and contract.unresolved == ()
    assembly[CONTRACT_FIELD] = contract.model_dump(mode="json")
    candidates = {
        "clip_paths": [b.proxy_path for b in bindings],
        "creator_strategy": strategy,
        "creator_request": SECRET,
    }
    if stamped:
        candidates[PLAN_AUTHORITY_FIELD] = 1
        assembly = stamp_composition(assembly, candidates)
    job = SimpleNamespace(
        id=uuid.UUID(JOB_ID),
        user_id=uuid.uuid4(),
        status="processing",
        assembly_plan=dict(assembly),
        error_detail="x",
        failure_reason="x",
    )
    db = _FakeDb()
    monkeypatch.setattr(gb, "_load_unified_montage_inputs", lambda _id: (job.user_id, [], None))
    monkeypatch.setattr(
        gb, "_first_user_message", lambda _id: pytest.fail("the live request was re-read")
    )
    monkeypatch.setattr(gb, "_phone_rendering_globally_available", lambda: True)
    monkeypatch.setattr(gb, "_sync_session", lambda: contextlib.nullcontext(db))
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda _db, _id: (job, 3))
    monkeypatch.setattr(type(settings), "phone_rendering_for", lambda self, uid: True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "audioMix", "positionedText", "animatedText"],
    )
    return SimpleNamespace(
        job=job,
        db=db,
        snapshot=dict(assembly),
        candidates=candidates,
        contract=contract,
        words=_words(),
    )


def _run(world, **kw):
    return job_module.run_phone_voice_behind_footage_job(
        JOB_ID,
        world.snapshot,
        world.candidates,
        ownership_epoch=3,
        load_words=lambda _binding: (world.words, "en"),
        **kw,
    )


def _recipe(world):
    from app.services.device_render import device_status

    return device_status(world.job, "speech_montage").request.recipe


def _decline(info) -> CreatorRenderContractError:
    assert isinstance(info.value, CreatorRenderContractError)
    assert info.value.decline_reason and info.value.alternative
    return info.value


def test_the_approved_plan_is_composed_verified_and_pinned(monkeypatch):
    world = _world(monkeypatch)
    assert _run(world) is True
    job = world.job
    assert world.db.commits == 1 and job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert variant["variant_id"] == variant["resolved_archetype"] == "speech_montage"
    recipe = _recipe(world)
    tracks = {t.id: t for t in recipe.tracks}
    assert set(tracks) == {"voice-footage", "voice"}
    picture = [c.source_asset_id for c in tracks["voice-footage"].clips]
    assert picture == list(world.contract.order_ids) == ["b0", "b1", "b2", "b3"]
    assert "talk" not in picture  # the voice clip's own picture is hidden
    assert all(c.volume == 0 for c in tracks["voice-footage"].clips)
    voice = tracks["voice"].clips[0]
    assert voice.source_asset_id == "talk" and voice.timeline_start == 0 and voice.volume == 1
    assert recipe.duration == pytest.approx(24.0, abs=0.04)
    assert [layer.start for layer in recipe.text_layers] == [0.0]
    record = job.assembly_plan["speech_montage"]
    assert record["route"] == "voice_behind_footage"
    assert record["ordering_basis"] == world.contract.order_basis == "capture_time"
    assert SECRET not in repr(record) and SECRET not in repr(recipe.model_dump(mode="json"))


def test_the_kri469_shape_a_long_voice_is_trimmed_to_the_length_and_disclosed(monkeypatch):
    strategy = _strategy(target_duration_s=30)
    world = _world(monkeypatch, voice_s=147.7, count=12, clip_s=10.0, strategy=strategy)
    world.words = _words(count=300)  # ~150 s of speech
    assert _run(world) is True
    recipe = _recipe(world)
    voice = next(t for t in recipe.tracks if t.id == "voice").clips[0]
    assert voice.source_asset_id == "talk"
    assert 30 - 3.05 <= voice.source_duration <= 30 - 0.05 + 1e-6  # the first sentences, not 7 s
    assert recipe.duration == pytest.approx(30.0, abs=0.04)
    picture = [
        c.source_asset_id for c in next(t for t in recipe.tracks if t.id == "voice-footage").clips
    ]
    assert picture == [f"b{i}" for i in range(12)]
    assert any("first" in a for a in world.job.assembly_plan["speech_montage"]["adjustments"])


def test_an_implicit_length_follows_the_voice_up_to_the_plans_pick(monkeypatch):
    strategy = _strategy(target_duration_requested=None, target_duration_s=30)
    strategy.pop("target_duration_requested", None)
    world = _world(monkeypatch, strategy=strategy)
    assert world.contract.duration_s is None
    world.words = _words(count=26)  # ~13 s of speech
    assert _run(world) is True
    assert _recipe(world).duration == pytest.approx(13.4, abs=0.6)


def test_a_voice_shorter_than_the_asked_length_is_a_typed_decline_not_a_shorter_video(monkeypatch):
    world = _world(monkeypatch, strategy=_strategy(target_duration_s=30))
    world.words = _words(count=40)  # ~20 s of speech for a 30 s edit
    with pytest.raises(CreatorRenderContractError) as info:
        _run(world)
    exc = _decline(info)
    assert (exc.decline_reason, exc.field_path) == ("requirement_conflict", "target_duration_s")
    assert world.db.commits == 0 and "variants" not in world.job.assembly_plan


def test_a_chosen_silent_tail_keeps_the_length_with_the_voice_stopping_early(monkeypatch):
    strategy = _strategy(
        target_duration_s=30,
        choice_answers=[
            {
                "conflict": "voice_vs_duration",
                "kind": "voice_vs_duration",
                "option": "silent_tail",
                "input_digest": "d",
            }
        ],
    )
    world = _world(monkeypatch, strategy=strategy)
    world.words = _words(count=40)
    assert world.job.assembly_plan[COMPOSITION_FIELD]["voice_span_s"] is not None
    assert _run(world) is True
    recipe = _recipe(world)
    assert recipe.duration == pytest.approx(30.0, abs=0.04)
    voice = next(t for t in recipe.tracks if t.id == "voice").clips[0]
    assert voice.source_duration < 25
    assert any(
        "without voice" in a for a in world.job.assembly_plan["speech_montage"]["adjustments"]
    )


def test_too_many_clips_for_the_length_and_too_little_footage_decline_with_a_way_forward(
    monkeypatch,
):
    crowded = _world(monkeypatch, count=30, clip_s=10.0, strategy=_strategy(target_duration_s=15))
    with pytest.raises(CreatorRenderContractError) as info:
        _run(crowded)
    assert _decline(info).field_path == "target_duration_s"
    short = _world(monkeypatch, count=3, clip_s=5.0, strategy=_strategy(target_duration_s=40))
    short.words = _words(count=200)
    with pytest.raises(CreatorRenderContractError) as info:
        _run(short)
    assert _decline(info).field_path == "target_duration_s"


def test_a_voice_clip_without_speech_declines_naming_the_clip_as_the_problem(monkeypatch):
    world = _world(monkeypatch)
    world.words = _words(count=3)
    with pytest.raises(CreatorRenderContractError) as info:
        _run(world)
    exc = _decline(info)
    assert (exc.decline_reason, exc.field_path) == (
        "capability_unavailable",
        "montage_audio.source_media_ids[]",
    )


def test_a_stale_or_missing_commitment_is_declined_not_guessed(monkeypatch):
    world = _world(monkeypatch)
    stale = {**world.snapshot[COMPOSITION_FIELD], "contract_digest": "someone-elses"}
    world.snapshot[COMPOSITION_FIELD] = stale
    with pytest.raises(CreatorRenderContractError) as info:
        _run(world)
    assert _decline(info).decline_reason == "evidence_missing"
    world.snapshot.pop(COMPOSITION_FIELD)
    with pytest.raises(CreatorRenderContractError):
        _run(world)
    assert world.db.commits == 0


def test_an_unstamped_job_is_not_touched(monkeypatch):
    world = _world(monkeypatch, stamped=False)
    before = dict(world.job.assembly_plan)
    assert _run(world) is False
    assert world.job.assembly_plan == before and world.db.commits == 0


def test_the_speech_kill_switch_still_stops_the_camera_audio_renderer(monkeypatch):
    world = _world(monkeypatch)
    monkeypatch.setattr(settings, "speech_excerpt_montage_enabled", False)
    with pytest.raises(CreatorRenderContractError) as info:
        _run(world)
    assert _decline(info).decline_reason == "capability_unavailable"


def test_redelivery_after_pinning_does_nothing(monkeypatch):
    world = _world(monkeypatch)
    snapshot = dict(world.snapshot)
    snapshot[DEVICE_RENDER_FIELD] = {"speech_montage": {"base_generation": "gen-1"}}

    def boom(*_a, **_k):
        raise AssertionError("already pinned")

    assert (
        job_module.run_phone_voice_behind_footage_job(
            JOB_ID, snapshot, world.candidates, ownership_epoch=3, load_words=boom
        )
        is True
    )
    assert world.db.commits == 0


def test_a_changed_contract_before_publication_drops_the_stale_result(monkeypatch):
    world = _world(monkeypatch)
    world.job.assembly_plan[CONTRACT_FIELD] = world.contract.rebind(duration_s=20).model_dump(
        mode="json"
    )
    assert _run(world) is True
    assert world.db.commits == 0 and "variants" not in world.job.assembly_plan


def test_the_runner_never_reads_request_text():
    import inspect

    source = inspect.getsource(job_module.run_phone_voice_behind_footage_job)
    for forbidden in ("creator_request", "_first_user_message", "_request_text", "mentions_speech"):
        assert forbidden not in source


# --- review fixes: implicit length arithmetic and model-picked lengths ---------------------


def _implicit_strategy(**update):
    strategy = _strategy(target_duration_s=24, **update)
    strategy.pop("target_duration_requested", None)
    return strategy


def test_the_reviewers_repro_two_short_clips_and_no_stated_length_renders(monkeypatch):
    world = _world(monkeypatch, voice_s=60.0, count=2, clip_s=5.51, strategy=_implicit_strategy())
    assert world.contract.duration_s is None
    world.words = _words(count=110)
    assert _run(world) is True
    recipe = _recipe(world)
    assert recipe.duration == pytest.approx(10.9, abs=0.1)
    adjustments = world.job.assembly_plan["speech_montage"]["adjustments"]
    assert any("footage you gave me" in note for note in adjustments)
    assert not any("7 seconds" in note for note in adjustments)


def test_forty_one_clips_and_a_model_picked_length_extend_instead_of_declining(monkeypatch):
    world = _world(monkeypatch, voice_s=147.7, count=41, clip_s=10.0, strategy=_implicit_strategy())
    world.words = _words(count=290)
    assert _run(world) is True
    recipe = _recipe(world)
    assert recipe.duration == pytest.approx(32.8, abs=0.04)
    picture = next(t for t in recipe.tracks if t.id == "voice-footage").clips
    assert len(picture) == 41 and all(c.source_duration >= 0.8 - 1e-3 for c in picture)
    assert (
        "Extended the edit to 32.8 seconds so every clip is shown."
        in (world.job.assembly_plan["speech_montage"]["adjustments"])
    )


def test_a_creator_stated_length_is_never_stretched_to_fit_the_clips(monkeypatch):
    world = _world(
        monkeypatch, voice_s=147.7, count=41, clip_s=10.0, strategy=_strategy(target_duration_s=30)
    )
    assert world.contract.duration_s == 30
    world.words = _words(count=290)
    with pytest.raises(CreatorRenderContractError) as info:
        _run(world)
    exc = _decline(info)
    assert (exc.decline_reason, exc.field_path) == ("requirement_conflict", "target_duration_s")
    assert world.db.commits == 0


def test_clips_sharing_a_capture_time_are_disclosed_not_asked(monkeypatch):
    world = _world(monkeypatch)
    rows = [
        {"media_id": "b0", "capture": {"capture_time": "2026-06-01T09:00:00Z"}},
        {"media_id": "b1", "capture": {"capture_time": "2026-06-01T09:01:00Z"}},
        {"media_id": "b2", "capture": {"capture_time": "2026-06-01T09:01:00Z"}},
        {"media_id": "b3", "capture": {"capture_time": "2026-06-01T09:03:00Z"}},
    ]
    world.snapshot["creator_brief_binding"] = {"media_snapshot": {"clip_assignments": rows}}
    assert _run(world) is True
    adjustments = world.job.assembly_plan["speech_montage"]["adjustments"]
    assert (
        "2 clips share a capture time, so I kept them in the order they were added" in adjustments
    )
