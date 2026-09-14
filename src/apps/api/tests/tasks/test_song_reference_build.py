from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.pipeline.guided_story import validate_execution_plan
from tests.pipeline.test_guided_story import _guided_snapshot


def test_guided_execution_plan_v6_matches_without_audio_metadata(monkeypatch) -> None:
    import app.tasks.generative_build as gb

    guided = _guided_snapshot()
    job = SimpleNamespace(
        id=uuid4(),
        status="queued",
        assembly_plan={},
        all_candidates={},
    )
    calls = {"gets": 0, "matches": 0}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _model, _pk, **_kwargs):
            calls["gets"] += 1
            if calls["gets"] > 1:
                job.assembly_plan.setdefault("guided_edit", guided)
            return job

        def commit(self):
            return None

    matched = SimpleNamespace(
        id="song-1",
        title="Song",
        artist=None,
        duration_s=30.0,
        audio_gcs_path="music/song.m4a",
        track_config={"best_start_s": 25.0},
        beat_timestamps_s=[2.8, 5.2],
    )

    monkeypatch.setattr(gb, "_sync_session", lambda: Session())
    monkeypatch.setattr(
        gb,
        "_match_best_track",
        lambda *_args, **_kwargs: calls.__setitem__("matches", calls["matches"] + 1) or matched,
    )
    monkeypatch.setattr(gb, "_cancelled_job_write_rejected", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        "app.services.creator_execution_contract.validate_execution_binding",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("v6 must not inspect audio metadata")
        ),
    )

    plan, render_track = gb._guided_execution_plan(str(job.id), guided)

    assert render_track is None
    assert plan["compiler_version"] == 6
    assert plan["music"] is None
    assert plan["song_reference"]["start_s"] == 12.0
    assert plan["song_reference"]["end_s"] == 30.0
    assert validate_execution_plan(plan, guided) == plan
    assert job.assembly_plan["guided_story_execution_plan"] == plan

    monkeypatch.setattr(
        gb,
        "_match_best_track",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("rematched pinned song")),
    )
    second, second_track = gb._guided_execution_plan(str(job.id), guided)
    assert second == plan
    assert second_track is None
    assert calls["matches"] == 1


def test_update_variant_entry_propagates_reference_receipt_fields(monkeypatch):
    import app.tasks.generative_build as gb

    job_id = str(uuid4())
    variant = {
        "variant_id": "guided_story",
        "render_generation_id": "attempt-1",
        "source_audio_options": [{"mix": "source_a", "audio_url": "speech"}],
    }
    job = SimpleNamespace(id=uuid4(), status="processing", assembly_plan={"variants": [variant]})

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

        def commit(self):
            return None

    monkeypatch.setattr(gb, "_sync_session", lambda: Session())
    monkeypatch.setattr(gb, "_attach_variant_posters", lambda patch, **_kwargs: (patch, []))
    monkeypatch.setattr(gb, "_cancelled_job_write_rejected", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(gb, "append_retired_variant_poster_receipts", lambda *_args: [])
    monkeypatch.setattr(gb, "_reconcile_retired_variant_posters", lambda *_args: None)
    monkeypatch.setattr(
        gb, "_delete_generated_poster_objects_if_unreferenced", lambda *_args, **_kwargs: None
    )
    reference = {
        "schema_version": 1,
        "delivery": "external_platform",
        "track_id": "song-1",
        "title": "Song",
        "artist": None,
        "start_s": 1.0,
        "end_s": 4.0,
    }
    assert gb._update_variant_entry(
        job_id,
        "guided_story",
        {"render_receipt": {"song_reference": reference, "source_audio_preserved": False}},
        expected_render_gen_id="attempt-1",
        cleanup_followup="none",
    )
    updated = job.assembly_plan["variants"][0]
    assert updated["music_playback_mode"] == "reference_only"
    assert updated["music_track_id"] is None
    assert updated["song_reference"] == reference
    assert updated["source_audio_preserved"] is False
    assert updated["source_audio_options"] == variant["source_audio_options"]


def test_initial_phone_variant_carries_reference_metadata(monkeypatch):
    import app.tasks.generative_build as gb
    from tests.pipeline.test_phone_guided_plan import fixture
    from tests.tasks.test_phone_guided_dispatch import setup

    job, snapshot, session, planner, cloud = setup(monkeypatch)
    plan, _bindings = fixture()
    raw_plan = plan.model_dump(mode="json")
    raw_plan.update(
        compiler_version=6,
        song_reference={
            "schema_version": 1,
            "delivery": "external_platform",
            "track_id": "song-1",
            "title": "Song",
            "artist": None,
            "start_s": 1.0,
            "end_s": 4.0,
        },
        song_reference_track_duration_s=30.0,
    )
    planner.return_value = (raw_plan, None)
    gb._run_phone_guided_job(str(job.id), snapshot, ownership_epoch=3)
    variant = job.assembly_plan["variants"][0]
    assert variant["music_playback_mode"] == "reference_only"
    assert variant["music_track_id"] is None
    assert variant["song_reference"]["track_id"] == "song-1"
    assert variant["source_audio_preserved"] is False
