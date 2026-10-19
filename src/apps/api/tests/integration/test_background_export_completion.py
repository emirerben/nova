"""Real PostgreSQL claims + actual MP4 decoding + HTTP publication boundary."""

# ruff: noqa: F811 -- shared fixture registration
import copy
import hashlib
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.auth import get_current_user
from app.database import sync_session
from app.kria.device_render import make_device_request
from app.main import app
from app.models import Job, TemporaryMediaUpload
from app.routes import device_render as routes
from app.services.creator_render_contract import CONTRACT_FIELD, CreatorRenderContract
from app.services.device_render import (
    approved_export_sources,
    device_record,
    export_source_digest,
    pin_device_request,
    save_device_record,
)
from app.tasks import export_verification
from tests.integration.test_actual_export_verification import encode, exports  # noqa: F401
from tests.kria.test_render_retry_postgres import client, seed_failed  # noqa: F401


def test_broker_gap_duplicate_claim_and_replay_publish_only_one_checked_artifact(
    client, exports, monkeypatch
):
    case = seed_failed()
    root, sources, recipe = exports
    candidate = encode(root, sources)
    attempt = str(uuid.uuid4())
    path = f"{case.user_id}/{case.job_id}/device/{attempt}.mp4"
    request = make_device_request(job_id=case.job_id, variant_id="first", revision=2, recipe=recipe)
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        assembly = copy.deepcopy(job.assembly_plan)
        assembly[CONTRACT_FIELD] = (
            CreatorRenderContract(generation_id="approved")
            .rebind(original_audio="require", audio_source_ids=("a", "b"))
            .model_dump(mode="json")
        )
        assembly["_phone_sources_v1"] = [
            {
                "media_id": key,
                "proxy_path": f"analysis-proxy-{key}.mp4",
                "generation": f"source-{key}",
                "original": {
                    "sha256": key * 64,
                    "byte_count": 1,
                    "duration_s": 4,
                    "width": 640,
                    "height": 360,
                    "orientation_degrees": 0,
                    "has_audio": True,
                },
            }
            for key in sources
        ]
        job.assembly_plan = assembly
        pin_device_request(job, request, base_generation="approved")
        record = device_record(job, "first")
        record["actual_export_policy"] = {
            "version": 1,
            "source_receipts_digest": export_source_digest(approved_export_sources(job, request)),
        }
        record["status"]["phase"] = "syncing"
        record["attempts"][attempt] = {
            "path": path,
            "size": candidate.stat().st_size,
            "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        }
        save_device_record(job, "first", record)
        db.add(
            TemporaryMediaUpload(
                user_id=case.user_id,
                object_path=path,
                purpose="device_export",
                retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.commit()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=case.user_id)
    payload = {"identity": request.identity.model_dump(mode="json"), "attempt_id": attempt}
    dispatches = []
    monkeypatch.setattr(export_verification, "enqueue", lambda *args: dispatches.append(args))
    response = client[0].post(
        f"/me/jobs/{case.job_id}/device-render/complete",
        json=payload,
        headers={"X-Kria-Client-Protocol": "13"},
    )
    assert response.status_code == 202, response.text
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        assert job.assembly_plan["variants"][0]["video_path"] == "last-good.mp4"
    dispatches.clear()  # Lose the first broker hint; the durable outbox remains.
    export_verification.reconcile_export_verifications.run()
    assert (str(case.job_id), str(case.user_id), payload) in dispatches
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _: SimpleNamespace(
            size=candidate.stat().st_size, content_type="video/mp4", generation="42"
        ),
    )

    def download(remote, local, *, generation):
        shutil.copyfile(
            candidate
            if remote == path
            else sources[remote.removeprefix("analysis-proxy-").removesuffix(".mp4")],
            local,
        )

    monkeypatch.setattr(routes.storage, "download_generation_to_file", download)
    monkeypatch.setattr(
        routes.storage, "signed_get_url", lambda *args: "https://example.test/checked.mp4"
    )
    monkeypatch.setattr(routes, "upload_video_poster", lambda *args, **kwargs: None)
    entered, release = threading.Event(), threading.Event()
    real_verify = routes._verify_export
    calls = []

    def verify(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(15)
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(routes, "_verify_export", verify)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            export_verification.verify_device_export.run,
            str(case.job_id),
            str(case.user_id),
            payload,
        )
        assert entered.wait(15)
        duplicate = pool.submit(
            export_verification.verify_device_export.run,
            str(case.job_id),
            str(case.user_id),
            payload,
        )
        assert duplicate.result(timeout=15) == "claimed"
        release.set()
        assert first.result(timeout=60) == "published"
    assert len(calls) == 1
    replay = client[0].post(
        f"/me/jobs/{case.job_id}/device-render/complete",
        json=payload,
        headers={"X-Kria-Client-Protocol": "13"},
    )
    assert replay.status_code == 200
    with sync_session() as db:
        job = db.get(Job, case.job_id)
        record = device_record(job, "first")
        assert record["published_attempt"] == attempt
        assert record["attempts"][attempt]["actual_export_verification"]["status"] == "checked"
        assert record["attempts"][attempt]["verification_task"]["runs"] == 1
        assert job.assembly_plan["variants"][0]["video_path"] == path
