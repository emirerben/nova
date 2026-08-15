from __future__ import annotations

import copy
import inspect
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import app.tasks.generative_build as gb


@contextmanager
def _session():
    yield SimpleNamespace(commit=lambda: None)


def test_guided_snapshot_routes_before_legacy_ingest_and_agents(monkeypatch) -> None:
    snapshot = {
        "proposal_version": 4,
        "media_digest": "a" * 64,
        "approved_proposal": {"title": "Corfu"},
        "media_identities": [],
    }
    job = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        status="queued",
        mode="content_plan",
        assembly_plan={"guided_edit": snapshot},
        all_candidates={"clip_paths": ["users/u/seed.jpg"]},
    )
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(gb, "_sync_session", _session)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda _db, _job_id: (job, None))
    monkeypatch.setattr(gb, "mark_started", lambda job_id: calls.append(("started", job_id)))
    monkeypatch.setattr(gb, "record_phase", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gb,
        "_run_guided_story_job",
        lambda job_id, raw, **kwargs: calls.append(("guided", (job_id, raw, kwargs))),
    )
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy ingest ran")),
    )
    monkeypatch.setattr(
        gb,
        "_run_text_agents",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy writer ran")),
    )

    gb._run_generative_job_impl(str(job.id))

    assert calls[0] == ("started", str(job.id))
    assert calls[1][0] == "guided"
    assert calls[1][1][1] == snapshot


def test_redelivery_reuses_pinned_execution_plan_without_rematching(monkeypatch) -> None:
    from app.pipeline import guided_story

    pinned = {
        "compiler_version": 1,
        "proposal_version": 4,
        "media_digest": "a" * 64,
        "music": None,
    }
    job = SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={"guided_story_execution_plan": pinned},
    )

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, _model, _pk, **_kwargs):
            return job

    monkeypatch.setattr(gb, "_sync_session", lambda: _Session())
    monkeypatch.setattr(
        guided_story,
        "validate_guided_snapshot",
        lambda _raw: (4, "a" * 64, SimpleNamespace()),
    )
    monkeypatch.setattr(
        guided_story,
        "validate_execution_plan",
        lambda plan, _raw: plan,
    )
    monkeypatch.setattr(
        gb,
        "_match_best_track",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("music rematched")),
    )

    plan, track = gb._guided_execution_plan(str(job.id), {"snapshot": "approved"})

    assert plan == pinned
    assert track is None


def test_guided_text_reburn_never_falls_through_to_legacy_montage(monkeypatch) -> None:
    job_id = "12345678-1234-5678-1234-567812345678"
    existing = {
        "variant_id": "guided_story",
        "rank": 1,
        "orientation": "portrait",
        "resolved_archetype": "guided_story",
        "text_mode": "agent_text",
        "intro_text": "Corfu in small moments",
        "base_video_path": f"generative-jobs/{job_id}/guided-base.mp4",
        "video_path": f"generative-jobs/{job_id}/guided-final.mp4",
        "render_generation_id": "edit-1",
        "text_elements_user_edited": True,
        "text_elements": [],
    }
    job = SimpleNamespace(
        status="variants_ready",
        mode="content_plan",
        all_candidates={"clip_paths": ["users/u/legacy-seed.mp4"]},
        assembly_plan={"variants": [existing]},
    )

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, model, _pk, **_kwargs):
            return job if model is gb.Job else None

    monkeypatch.setattr(gb, "_sync_session", lambda: _Session())
    monkeypatch.setattr(gb, "_update_variant_entry", lambda *_a, **_kw: True)
    monkeypatch.setattr(gb, "_fresh_variant_snapshot", lambda *_a, **_kw: existing)
    monkeypatch.setattr(gb, "_resolve_narrative_order", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        gb,
        "_reburn_text_on_base",
        lambda **_kwargs: (_ for _ in ()).throw(gb.CachedBaseProbeError("base missing")),
    )
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("legacy montage ran")),
    )

    with pytest.raises(gb.CachedBaseProbeError, match="base missing"):
        gb._run_regenerate_variant(
            job_id,
            "guided_story",
            None,
            None,
            False,
            render_gen_id="edit-1",
        )


def test_guided_text_reburn_pins_base_and_refreshes_output_receipt(monkeypatch) -> None:
    from app import storage
    from app.pipeline import guided_story
    from app.schemas.edit_proposal import (
        EditProposalSnapshot,
        MediaRef,
        StoryBeat,
        canonical_media_digest,
    )

    job_id = "12345678-1234-5678-1234-567812345678"
    base_path = f"generative-jobs/{job_id}/guided-base.mp4"
    old_output_path = f"generative-jobs/{job_id}/guided-final.mp4"
    media = [
        MediaRef(
            lane="asset",
            media_id="photo-1",
            gcs_path="users/u/corfu.jpg",
            generation="source-gen",
            kind="image",
            analysis={"subject": "Corfu coast"},
        )
    ]
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        goal="Share one Corfu moment",
        pace="balanced",
        duration_s=15,
        title="Corfu in small moments",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id="coast",
                topic="Coast",
                thought="The coast slowed the story down.",
                media_ids=["photo-1"],
                duration_s=12,
            )
        ],
    )
    raw = {
        "proposal_version": 3,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                "lane": "asset",
                "media_id": "photo-1",
                "gcs_path": "users/u/corfu.jpg",
                "generation": "source-gen",
                "kind": "image",
            }
        ],
    }
    plan = guided_story.compile_execution_plan(raw, track=None)
    beat_ids = [row["beat_id"] for row in plan["beat_windows"]]
    moment_ids = [row["moment_id"] for row in plan["story_timeline"]]
    text_ids = [row["id"] for row in plan["text_elements"]]
    receipt = {
        "schema_version": 1,
        "verified": True,
        "proposal_version": plan["proposal_version"],
        "media_digest": plan["media_digest"],
        "expected_beat_ids": beat_ids,
        "actual_beat_ids": beat_ids,
        "expected_moment_ids": moment_ids,
        "actual_moment_ids": moment_ids,
        "expected_media_ids": ["photo-1"],
        "actual_media_ids": ["photo-1"],
        "expected_text_ids": text_ids,
        "actual_text_ids": text_ids,
        "approved_text_ids": text_ids,
        "media_count": 1,
        "image_count": 1,
        "video_count": 0,
        "expected_duration_s": 15,
        "actual_duration_s": 15,
        "music_applied": False,
        "music": None,
        "output": {
            "width": 1080,
            "height": 1920,
            "video_codec": "h264",
            "audio_codec": "aac",
            "sha256": "a" * 64,
        },
        "base_storage": {
            "path": base_path,
            "generation": "base-gen",
            "size": 101,
            "md5_hash": "base-md5",
        },
        "output_storage": {
            "path": old_output_path,
            "generation": "old-output-gen",
            "size": 201,
            "md5_hash": "old-output-md5",
        },
        "media_stages": [
            {
                "media_id": "photo-1",
                "gcs_path": "users/u/corfu.jpg",
                "generation": "source-gen",
                "kind": "image",
            }
        ],
        "moment_stages": [
            {
                "moment_id": row["moment_id"],
                "beat_id": row["beat_id"],
                "media_id": row["media_id"],
                "generation": row["generation"],
                "kind": row["kind"],
                "layout": row["layout"],
                "image_motion": row["image_motion"],
            }
            for row in plan["story_timeline"]
        ],
        "text_stages": [{"element_id": element_id, "visible": True} for element_id in text_ids],
    }
    existing = {
        "variant_id": "guided_story",
        "rank": 1,
        "orientation": "portrait",
        "resolved_archetype": "guided_story",
        "text_mode": "agent_text",
        "base_video_path": base_path,
        "video_path": old_output_path,
        "render_status": "ready",
        "ok": True,
        "proposal_version": plan["proposal_version"],
        "media_digest": plan["media_digest"],
        "story_timeline": plan["story_timeline"],
        "duration_s": 15,
        "text_elements_user_edited": True,
        "text_elements": plan["text_elements"],
        "render_receipt": receipt,
    }
    exact_downloads: list[tuple[str, str]] = []

    def metadata(path: str):
        if path == base_path:
            return storage.ObjectMetadata(path, "base-gen", None, 101, "video/mp4", "base-md5")
        return storage.ObjectMetadata(path, "reburn-gen", None, 202, "video/mp4", "reburn-md5")

    def exact_download(path: str, local: str, *, generation: str) -> None:
        exact_downloads.append((path, generation))
        with open(local, "wb") as handle:
            handle.write(b"verified-base")

    monkeypatch.setattr(storage, "object_metadata", metadata)
    monkeypatch.setattr(storage, "download_generation_to_file", exact_download)
    monkeypatch.setattr(
        storage,
        "download_to_file",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("mutable download used")),
    )
    monkeypatch.setattr(
        storage,
        "upload_public_read",
        lambda _local, path: f"https://signed.test/{path}",
    )
    monkeypatch.setattr(
        "app.pipeline.probe.probe_video",
        lambda _path: SimpleNamespace(duration_s=15, width=1080, height=1920),
    )
    monkeypatch.setattr(
        gb,
        "_resolve_subject_matte_for_burn",
        lambda **kwargs: (None, None, kwargs["overlays"]),
    )

    def burn_with_evidence(_base, overlays, output, _tmpdir, **_kwargs):
        with open(output, "wb") as handle:
            handle.write(b"verified-output")
        return [{"element_id": row["element_id"], "visible": True} for row in overlays]

    monkeypatch.setattr(
        "app.pipeline.text_overlay_skia.burn_text_overlays_skia_with_evidence",
        burn_with_evidence,
    )

    def verify_reburn(previous, elements, evidence, *_args):
        updated = copy.deepcopy(previous)
        updated.update(
            expected_text_ids=[row["id"] for row in elements],
            actual_text_ids=[row["element_id"] for row in evidence],
            text_stages=evidence,
            text_edited_after_approval=True,
            output={**updated["output"], "sha256": "b" * 64},
        )
        return updated

    monkeypatch.setattr(guided_story, "verify_guided_text_reburn", verify_reburn)

    result = gb._reburn_text_on_base(
        job_id=job_id,
        variant_id="guided_story",
        existing=existing,
        agent_text=SimpleNamespace(text="Corfu in small moments", highlight_word=None),
        agent_form=None,
        text_mode="agent_text",
        resolved_style_set_id=None,
        size_override_px=None,
        settings=gb.settings,
    )
    persisted = {**existing, **result}

    assert exact_downloads == [(base_path, "base-gen")]
    assert result["render_receipt"]["output_storage"] == {
        "path": result["video_path"],
        "generation": "reburn-gen",
        "size": 202,
        "md5_hash": "reburn-md5",
    }
    assert (
        guided_story.validate_ready_result(
            plan,
            persisted,
            job_id=job_id,
            verify_storage=True,
        )
        == persisted
    )


def test_guided_regeneration_policy_covers_every_control_parameter() -> None:
    parameters = set(inspect.signature(gb._run_regenerate_variant).parameters)
    parameters -= {"job_id", "variant_id", "render_gen_id"}

    assert parameters == set(gb._GUIDED_REGEN_CONTROL_NAMES)


def test_guided_failure_keeps_job_non_ready_and_persists_machine_code(monkeypatch) -> None:
    from app.pipeline.guided_story import GuidedStoryError

    captured: dict[str, object] = {}
    monkeypatch.setattr(gb, "_owned_job_task_fence", lambda _job_id: _session())
    monkeypatch.setattr(
        gb,
        "_run_generative_job",
        lambda _job_id: (_ for _ in ()).throw(
            GuidedStoryError("guided_story_text_missing", "Approved text disappeared.")
        ),
    )
    monkeypatch.setattr(gb, "mark_failed_phase", lambda job_id: captured.update(failed=job_id))
    monkeypatch.setattr(gb, "mark_finished", lambda *_a: captured.update(finished=True))
    monkeypatch.setattr(
        gb,
        "_fail_job",
        lambda job_id, detail, failure_reason=None: captured.update(
            job_id=job_id,
            detail=detail,
            failure_reason=failure_reason,
            status="processing_failed",
        ),
    )

    gb.orchestrate_generative_job.run("12345678-1234-5678-1234-567812345678")

    assert captured["status"] == "processing_failed"
    assert captured["failure_reason"] == "guided_story_text_missing"
    assert "finished" not in captured


def test_busy_duplicate_delivery_does_not_mark_live_job_finished(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(gb, "_owned_job_task_fence", lambda _job_id: _session())
    monkeypatch.setattr(
        gb,
        "_run_generative_job",
        lambda _job_id: (_ for _ in ()).throw(gb._GuidedStoryAttemptBusy()),
    )
    monkeypatch.setattr(gb, "mark_finished", lambda *_a: calls.append("finished"))
    monkeypatch.setattr(gb, "mark_failed_phase", lambda *_a: calls.append("failed"))

    gb.orchestrate_generative_job.run("12345678-1234-5678-1234-567812345678")

    assert calls == []


def test_guided_render_failure_never_finalizes_or_publishes_ready(monkeypatch) -> None:
    from app.pipeline import guided_story

    plan = {
        "compiler_version": 1,
        "proposal_version": 2,
        "media_digest": "a" * 64,
        "selected_media_ids": ["photo"],
        "beat_windows": [{"beat_id": "food"}],
    }
    monkeypatch.setattr(gb, "_guided_execution_plan", lambda *_a, **_kw: (plan, None))
    monkeypatch.setattr(gb, "record_phase", lambda *_a, **_kw: None)
    monkeypatch.setattr(gb, "_claim_guided_story_attempt", lambda *_a, **_kw: ("claimed", None))
    monkeypatch.setattr(
        guided_story,
        "render_execution_plan",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            guided_story.GuidedStoryError(
                "guided_story_receipt_mismatch", "Approved media disappeared."
            )
        ),
    )
    monkeypatch.setattr(
        gb,
        "_finalize_job",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("finalized failed story")),
    )

    with pytest.raises(guided_story.GuidedStoryError) as exc:
        gb._run_guided_story_job("job-1", {}, render_trace_id="trace-1")

    assert exc.value.code == "guided_story_receipt_mismatch"


def test_guided_finalize_rejection_deletes_exact_hashed_attempt_keys(monkeypatch) -> None:
    discarded: list[tuple[dict, str | None]] = []
    monkeypatch.setattr(gb, "_set_status", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        gb,
        "_discard_generation_storage",
        lambda result, *, job_id, generation: discarded.append((result, generation)),
    )
    result = {
        "variant_id": "guided_story",
        "rank": 1,
        "text_mode": "agent_text",
        "resolved_archetype": "guided_story",
        "render_status": "ready",
        "render_generation_id": "raw-task-token",
        "video_path": "generative-jobs/job/variant_guided_8c0ffee.mp4",
        "base_video_path": "generative-jobs/job/base_guided_8c0ffee.mp4",
        "ok": True,
    }

    assert gb._finalize_job("job", [result]) is False
    assert discarded == [(result, None)]


def test_concurrent_guided_render_claim_has_one_owner(monkeypatch) -> None:
    job_id = "12345678-1234-5678-1234-567812345678"
    shared_job = SimpleNamespace(
        status="processing",
        assembly_plan={"variants": []},
        error_detail=None,
        failure_reason=None,
    )
    row_lock = threading.Lock()

    class _Session:
        def __init__(self):
            self.locked = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            if self.locked:
                row_lock.release()
            return False

        def get(self, *_args, **kwargs):
            if kwargs.get("with_for_update"):
                row_lock.acquire()
                self.locked = True
            return shared_job

        def commit(self):
            return None

    monkeypatch.setattr(gb, "_sync_session", _Session)
    monkeypatch.setattr(gb, "_cancelled_job_write_rejected", lambda *_a, **_kw: False)
    plan = {"proposal_version": 4, "media_digest": "a" * 64}
    start = threading.Barrier(2)

    def claim(attempt_id: str):
        start.wait(timeout=5)
        return gb._claim_guided_story_attempt(job_id, plan, attempt_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["attempt-a", "attempt-b"]))

    assert sorted(result[0] for result in results) == ["busy", "claimed"]
    persisted = shared_job.assembly_plan["variants"][0]
    assert persisted["render_generation_id"] in {"attempt-a", "attempt-b"}


def test_duplicate_delivery_with_same_task_id_has_one_live_owner(monkeypatch) -> None:
    job_id = "12345678-1234-5678-1234-567812345678"
    shared_job = SimpleNamespace(
        status="processing",
        assembly_plan={"variants": []},
        error_detail=None,
        failure_reason=None,
    )
    row_lock = threading.Lock()

    class _Session:
        def __init__(self):
            self.locked = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            if self.locked:
                row_lock.release()
            return False

        def get(self, *_args, **kwargs):
            if kwargs.get("with_for_update"):
                row_lock.acquire()
                self.locked = True
            return shared_job

        def commit(self):
            return None

    monkeypatch.setattr(gb, "_sync_session", _Session)
    monkeypatch.setattr(gb, "_cancelled_job_write_rejected", lambda *_a, **_kw: False)
    plan = {"proposal_version": 4, "media_digest": "a" * 64}
    start = threading.Barrier(2)

    def claim(_index: int):
        start.wait(timeout=5)
        return gb._claim_guided_story_attempt(job_id, plan, "same-celery-task-id")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))

    assert sorted(result[0] for result in results) == ["busy", "claimed"]


def test_stale_guided_render_lease_is_reclaimed(monkeypatch) -> None:
    from datetime import datetime, timedelta

    job_id = "12345678-1234-5678-1234-567812345678"
    stale = (datetime.utcnow() - timedelta(seconds=gb._GUIDED_RENDER_LEASE_S + 1)).isoformat() + "Z"
    job = SimpleNamespace(
        status="rendering",
        error_detail="old failure",
        failure_reason="old_code",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "guided_story",
                    "render_status": "rendering",
                    "render_generation_id": "dead-attempt",
                    "render_heartbeat_at": stale,
                }
            ]
        },
    )

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

        def commit(self):
            return None

    monkeypatch.setattr(gb, "_sync_session", _Session)
    monkeypatch.setattr(gb, "_cancelled_job_write_rejected", lambda *_a, **_kw: False)

    state, pending = gb._claim_guided_story_attempt(
        job_id,
        {"proposal_version": 5, "media_digest": "c" * 64},
        "replacement-attempt",
    )

    assert state == "claimed"
    assert pending["render_generation_id"] == "replacement-attempt"
    assert job.assembly_plan["variants"][0] == pending
    assert job.error_detail is None
    assert job.failure_reason is None


def test_guided_render_claim_rejection_does_not_mutate_job(monkeypatch) -> None:
    job_id = "12345678-1234-5678-1234-567812345678"
    job = SimpleNamespace(
        status="cancelled",
        error_detail="cancelled",
        failure_reason="cancelled",
        assembly_plan={"variants": []},
    )
    before = copy.deepcopy(job.assembly_plan)

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

    monkeypatch.setattr(gb, "_sync_session", _Session)
    monkeypatch.setattr(gb, "_cancelled_job_write_rejected", lambda *_a, **_kw: True)

    assert gb._claim_guided_story_attempt(
        job_id,
        {"proposal_version": 5, "media_digest": "c" * 64},
        "rejected-attempt",
    ) == ("rejected", None)
    assert job.assembly_plan == before


def test_first_guided_music_pin_failure_has_stable_code(monkeypatch) -> None:
    from app.pipeline import guided_story

    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={})

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return job

    monkeypatch.setattr(gb, "_sync_session", _Session)
    monkeypatch.setattr(
        guided_story,
        "validate_guided_snapshot",
        lambda _raw: (4, "d" * 64, SimpleNamespace()),
    )
    monkeypatch.setattr(guided_story, "matcher_clip_metas", lambda _snapshot: [])
    monkeypatch.setattr(
        gb,
        "_match_best_track",
        lambda *_a, **_kw: SimpleNamespace(
            id="track-1",
            title="Corfu Drift",
            audio_gcs_path="music/corfu.m4a",
            track_config={"best_start_s": 2.0},
        ),
    )
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: (_ for _ in ()).throw(FileNotFoundError("music generation missing")),
    )

    with pytest.raises(guided_story.GuidedStoryError) as exc:
        gb._guided_execution_plan(str(job.id), {})

    assert exc.value.code == "guided_story_music_missing"


def test_redelivery_finalizes_verified_guided_result_without_rerender(monkeypatch) -> None:
    plan = {
        "compiler_version": 1,
        "proposal_version": 2,
        "media_digest": "a" * 64,
        "selected_media_ids": ["photo"],
        "beat_windows": [{"beat_id": "food"}],
    }
    ready = {
        "variant_id": "guided_story",
        "render_status": "ready",
        "ok": True,
        "render_receipt": {"verified": True},
    }
    finalized: list[list[dict]] = []
    monkeypatch.setattr(gb, "_guided_execution_plan", lambda *_a, **_kw: (plan, None))
    monkeypatch.setattr(gb, "record_phase", lambda *_a, **_kw: None)
    monkeypatch.setattr(gb, "record_pipeline_event", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(gb, "_claim_guided_story_attempt", lambda *_a, **_kw: ("ready", ready))
    monkeypatch.setattr(
        "app.pipeline.guided_story.validate_ready_result",
        lambda _plan, result, **_kw: result,
    )
    monkeypatch.setattr(
        "app.pipeline.guided_story.render_execution_plan",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("rerendered verified result")),
    )
    monkeypatch.setattr(gb, "_finalize_job", lambda _job_id, rows: finalized.append(rows) or True)

    gb._run_guided_story_job("job-1", {}, render_trace_id="trace-1")

    assert finalized == [[ready]]


def test_concurrent_first_delivery_uses_one_pinned_execution_plan(monkeypatch) -> None:
    shared_job = SimpleNamespace(
        id=uuid.uuid4(),
        status="processing",
        assembly_plan={},
    )
    initial_read_barrier = threading.Barrier(2)
    row_lock = threading.Lock()

    class _Session:
        def __init__(self):
            self.locked = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            if self.locked:
                row_lock.release()
            return False

        def get(self, _model, _pk, *, with_for_update=False):
            if with_for_update:
                row_lock.acquire()
                self.locked = True
                return shared_job
            snapshot = SimpleNamespace(
                id=shared_job.id,
                assembly_plan=copy.deepcopy(shared_job.assembly_plan),
            )
            initial_read_barrier.wait(timeout=5)
            return snapshot

        def commit(self):
            return None

    monkeypatch.setattr(gb, "_sync_session", _Session)
    monkeypatch.setattr(gb, "_cancelled_job_write_rejected", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        guided_story := __import__("app.pipeline.guided_story", fromlist=["guided_story"]),
        "validate_guided_snapshot",
        lambda _raw: (3, "b" * 64, SimpleNamespace()),
    )
    monkeypatch.setattr(guided_story, "matcher_clip_metas", lambda _snapshot: [])
    monkeypatch.setattr(guided_story, "validate_execution_plan", lambda plan, _raw: plan)
    monkeypatch.setattr(gb, "_match_best_track", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        guided_story,
        "compile_execution_plan",
        lambda *_a, **_kw: {
            "compiler_version": 1,
            "proposal_version": 3,
            "media_digest": "b" * 64,
            "candidate": threading.current_thread().name,
            "music": None,
        },
    )

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="delivery") as pool:
        results = list(
            pool.map(
                lambda _index: gb._guided_execution_plan(
                    "12345678-1234-5678-1234-567812345678", {}
                ),
                range(2),
            )
        )

    persisted = shared_job.assembly_plan["guided_story_execution_plan"]
    assert results[0][0] == results[1][0] == persisted
    assert persisted["candidate"] in {"delivery_0", "delivery_1"}
