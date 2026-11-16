"""A clip the admin debug redacted still seeds as a phone source the manifest binds (KRI-1375).

The admin job debug strips media ids, and every value that names a media file, from a job whose
speech cleanup is still `required_v1` with no finished variant (`_mask_active_speech_rerender`).
Job f88555bc's binding row kept its kind, duration and upload receipt, but neither its media id
nor `manifest_identity` (the proxy's file name). The replay matched the row to the item clip by
duration and named it, without the identity, so `bind_phone_sources` refused the seeded clip, the
manifest held no phone source and the runtime answered "Add at least one clip" without a model
call. The driver printed that as `diverged_from_production`.

Ways this can go wrong, checked on Postgres through the real seed and manifest resolution:
* the matched row gets its media id but not its `manifest_identity`, so the clip is no phone source;
* seeded clips that do not bind are replayed anyway and their refusal reads as a route result;
* the check refuses a cloud clip, which carries no proxy receipt and never needed one;
* a phone-only job whose messages carry no client protocol seeds a web thread, so its render
  could take a path production never took.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.models import CreationThread, Persona, PlanItem
from app.services import creator_sessions
from app.services.phone_destination import has_native_device_only_intent
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

API = Path(__file__).parents[2] / "scripts"
for name in ("replay_creator", "replay_job"):
    spec = importlib.util.spec_from_file_location(name, API / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
replay_job = sys.modules["replay_job"]
replay_creator = sys.modules["replay_creator"]

JOB = "f88555bc-ddd3-4320-b078-c58f17a275be"
USER = "7b56dd6f-6b9a-4634-89c8-ce91bb50a50d"
ITEM = "0c1f2a8e-5f0b-4b8a-9d55-2f6f1d3c9a10"
THREAD = "32a5b608-6397-4608-889f-dff5ad9bec40"
PROXY = "analysis-proxy-ios-B22C7409-7CF4-4226-9E9F-CC6814A8D942.mp4"
RECEIPT = {
    "purpose": "analysis_proxy",
    "proxy": {
        "width": 320,
        "height": 568,
        "original": {
            "kind": "video",
            "width": 1080,
            "height": 1920,
            "sha256": "0" * 64,
            "has_audio": True,
            "byte_count": 55291210,
            "duration_s": 68.367,
            "orientation_degrees": 0,
        },
        "duration_s": 68.36666666666666,
        "frame_rate": 30.0,
        "timing_version": 1,
        "orientation_degrees": 0,
    },
}


def _redacted_job() -> dict:
    """f88555bc's job debug: the binding row lost `media_id`, `manifest_identity` and paths."""

    return {
        "runtime_version": 2,
        "thread_id": THREAD,
        "kria_turn_id": "f23395d8-9cfc-44f9-bbe5-a6f86a3fb485",
        "job": {
            "id": JOB,
            "user_id": USER,
            "content_plan_item_id": ITEM,
            "status": "processing_failed",
            "created_at": "2026-10-21T13:39:06.517992Z",
            "all_candidates": {"creator_request": "Talking-to-camera edit of the cafe owner."},
            "assembly_plan": {
                "native_device_only": True,
                "speech_cleanup_contract": "required_v1",
                "creator_brief_binding": {
                    "media_snapshot": {
                        "clip_assignments": [
                            {
                                "kind": "video",
                                "capture": {"capture_time": "2026-10-16T15:24:17Z"},
                                "shot_id": None,
                                "has_audio": True,
                                "duration_s": 68.37,
                                "upload_contract": RECEIPT,
                            }
                        ],
                        "voiceover": None,
                    }
                },
            },
        },
    }


def _item(*clips: tuple[str, float]) -> dict:
    return {
        "item": {"item_status": "done", "edit_format": "talking_head"},
        "clip_assignments": [
            {
                "media_id": media_id,
                "gcs_path": f"users/{USER}/creation-threads/{THREAD}/{media_id}",
                "kind": "video",
                "duration_s": duration,
                "generation": None,
            }
            for media_id, duration in clips
        ],
        "pool_assets": [],
    }


def _events(media_id: str) -> dict:
    return {
        "events": [
            {"sequence": 0, "kind": "thread_created", "actor": "system", "payload": None},
            {
                "sequence": 1,
                "kind": "media_added",
                "actor": "user",
                "payload": {
                    "media": [
                        {
                            "kind": "video",
                            "filename": "8EE7A18C-M2_01_footage.mp4",
                            "media_id": media_id,
                            "duration_s": 68.37,
                            "content_type": "video/mp4",
                            "upload_contract": RECEIPT,
                        }
                    ],
                    "media_count": 1,
                },
            },
            {
                "sequence": 2,
                "kind": "user_message",
                "actor": "user",
                "content": "Talking-to-camera edit of the cafe owner.",
                "payload": {"turn_id": "f23395d8-9cfc-44f9-bbe5-a6f86a3fb485"},
            },
        ],
        "next_cursor": None,
    }


@pytest.fixture
def seeded_replay(monkeypatch, tmp_path, prod_profile):
    """collect_inputs on the production shape, then the replay's own seed, on Postgres."""

    _seed_runtime_project()  # skips the test when the Postgres test database is unreachable
    monkeypatch.setattr(settings, "storage_provider", "local")
    monkeypatch.setattr(settings, "e2e_fixtures", True)
    monkeypatch.setattr(settings, "local_storage_root", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "local_storage_base_url", "http://127.0.0.1:8000/dev-qa/storage")
    monkeypatch.setattr(replay_creator, "_probe_duration", lambda _path: 68.37)

    def fake_copy(bucket, object_path, destination, generation=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"proxy bytes")

    def run(job_debug: dict, item_debug: dict, media_id: str = PROXY) -> dict:
        monkeypatch.setattr(
            replay_job,
            "prod_get",
            lambda path: (
                _events(media_id)
                if "/creation-threads/" in path
                else job_debug
                if "/jobs/" in path
                else item_debug
            ),
        )
        monkeypatch.setattr(replay_job, "storage_copy", fake_copy)
        inputs_dir = tmp_path / "inputs"
        info = replay_job.collect_inputs(JOB, inputs_dir, "bucket")
        inputs = replay_creator.load_inputs(
            manifest_path=inputs_dir / "source-manifest.json",
            job_debug_path=inputs_dir / "job-debug.json",
            item_debug_path=inputs_dir / "item-debug.json",
            allow_source_count_drift=True,
        )
        ids = {key: uuid.uuid4() for key in ("user_id", "thread_id", "plan_id", "item_id")}
        replay_creator.seed_replay_database(
            inputs=inputs,
            output_dir=tmp_path / "run",
            session_id=uuid.uuid4(),
            thread_events=json.loads(info["thread_events"].read_text())["events"],
            **ids,
        )
        return ids

    return run


async def _manifest(ids: dict):  # noqa: ANN202 - ResolvedCreatorManifest
    try:
        async with AsyncSessionLocal() as db:
            item = await db.get(PlanItem, ids["item_id"])
            persona = (
                await db.execute(select(Persona).where(Persona.user_id == ids["user_id"]))
            ).scalar_one()
            manifest, _ = await creator_sessions.resolve_item_creator_context(
                db, item, persona=persona, runs_guided_voiceover=False
            )
            return manifest
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_redacted_binding_row_seeds_a_clip_the_manifest_binds(seeded_replay) -> None:
    ids = seeded_replay(_redacted_job(), _item((PROXY, 68.37)))
    manifest = await _manifest(ids)
    dispatch = manifest.capabilities["dispatch_render"]
    assert dispatch.reason_code != "unverified_phone_sources", dispatch
    assert manifest.capabilities["phone_source_audio"].available
    # Its messages carry no client protocol, yet the job was phone-only: an iPhone thread.
    with sync_session() as db:
        state = db.get(CreationThread, ids["thread_id"]).state
    assert has_native_device_only_intent(state)


def test_seeded_clips_that_do_not_bind_stop_as_an_input_error(seeded_replay) -> None:
    job = _redacted_job()
    row = job["job"]["assembly_plan"]["creator_brief_binding"]["media_snapshot"]
    row["clip_assignments"][0]["has_audio"] = False
    with pytest.raises(replay_creator.ReplayInputError, match="phone source"):
        seeded_replay(job, _item((PROXY, 68.37)))


def test_a_cloud_clip_without_a_receipt_still_seeds(seeded_replay) -> None:
    cloud = "clip-7382.mp4"
    job = _redacted_job()
    del job["job"]["assembly_plan"]["creator_brief_binding"]["media_snapshot"]["clip_assignments"][
        0
    ]["upload_contract"]
    seeded_replay(job, _item((cloud, 68.37)), media_id=cloud)
