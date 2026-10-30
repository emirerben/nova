"""Real initial approval/dispatch/observer with an explicit synthetic local publication.

Storage bytes are a generated test pattern, not an export of the approved recipe.
This verifies exact canonical input admission and evidence capture, not fulfillment.
"""

# ruff: noqa: F811
import asyncio
import copy
import hashlib
import uuid
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from app import storage
from app.config import settings
from app.database import sync_session
from app.models import CreatorAgentExecution, CreatorGenerationReview, Job
from app.services.device_render import device_record, save_device_record
from app.tasks.render_review import prepare_generation_review
from tests.kria import test_initial_render_review_authority_postgres as initial
from tests.kria.test_approval_readiness_postgres import readiness_flags  # noqa: F401
from tests.kria.test_initial_render_review_authority_postgres import (
    initial_publication,  # noqa: F401
)
from tests.kria.test_prepared_execution_postgres import broker  # noqa: F401
from tests.services.test_render_review_evidence import local_video  # noqa: F401


@pytest.fixture(autouse=True)
def canonical_admission(monkeypatch, local_video):
    _, video, _ = local_video
    monkeypatch.setattr(settings, "kria_intent_review_enabled", True)
    monkeypatch.setattr(prepare_generation_review, "apply_async", Mock())
    original = initial._observe_dispatched_execution

    def publish_then_observe(execution_id):
        with sync_session() as db:
            execution = db.get(CreatorAgentExecution, execution_id)
            job = db.get(Job, execution.target_job_id)
            owner, job_id = job.user_id, job.id
        attempt = str(uuid.uuid4())
        path = f"{owner}/{job_id}/device/{attempt}.mp4"
        storage.upload_public_read(str(video), path)
        metadata = storage.object_metadata_once(path, timeout_s=2)
        sha = hashlib.sha256(video.read_bytes()).hexdigest()
        with sync_session() as db:
            job = db.get(Job, job_id)
            record = device_record(job, "initial")
            record.update(published_attempt=attempt, base_generation=attempt)
            record["attempts"][attempt] = {
                "path": path,
                "size": metadata.size,
                "sha256": sha,
                "publication_proof": {
                    "version": 1,
                    "artifact_path": path,
                    "storage_generation": metadata.generation,
                    "file_size_bytes": metadata.size,
                    "sha256": sha,
                    "identity": record["status"]["request"]["identity"],
                    "source_receipts_digest": None,
                },
            }
            save_device_record(job, "initial", record)
            assembly = copy.deepcopy(job.assembly_plan)
            assembly["variants"][0].update(render_generation_id=attempt, video_path=path)
            job.assembly_plan = assembly
            db.commit()
        return original(execution_id)

    monkeypatch.setattr(initial, "_observe_dispatched_execution", publish_then_observe)


@pytest.mark.asyncio
async def test_initial_strategy_admitted_and_captured_without_editor_prep(initial_publication):
    with sync_session() as db:
        execution = db.get(CreatorAgentExecution, initial_publication)
        assert "editor_prep" not in execution.result
        row = db.scalar(
            select(CreatorGenerationReview).where(
                CreatorGenerationReview.execution_id == initial_publication
            )
        )
        assert row is not None and row.status == "pending"
        rid = row.id
    result = await asyncio.to_thread(prepare_generation_review.run, str(rid))
    assert result["status"] == "evidence_ready", result
    with sync_session() as db:
        row = db.get(CreatorGenerationReview, rid)
        packet = row.result["packet"]
        assert packet["requirement_generation"] != packet["published_attempt"]
        assert "six second video" in packet["original_request"]
        assert row.result["evidence"]["frames"]
        assert row.result["review_model_received_visual_input"] is False
