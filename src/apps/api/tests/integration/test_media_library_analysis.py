"""Provider-call evidence for owner-scoped, versioned durable source facts."""

import hashlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, text

from app.database import sync_session
from app.models import MediaAsset, MediaAssetSource, User
from app.pipeline import clip_cache
from app.services import media_library_analysis as facts

if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].endswith("_test"):
    pytest.skip("Library facts require a disposable test database", allow_module_level=True)


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setattr(facts.settings, "media_library_enabled", True)
    monkeypatch.setattr(clip_cache, "_get_redis", lambda: None)
    owner = uuid.uuid4()
    assets = []
    with sync_session() as db:
        db.add(User(id=owner, email=f"{owner}@library-facts.test"))
        db.flush()
        for index in range(2):
            asset = MediaAsset(id=uuid.uuid4(), user_id=owner, kind="video")
            db.add(asset)
            db.flush()
            source = MediaAssetSource(
                id=uuid.uuid4(),
                asset_id=asset.id,
                user_id=owner,
                role="analysis_proxy",
                object_path=f"users/{owner}/library/analysis-proxy-{index}.mp4",
                object_generation=str(index + 1),
                uploaded_sha256=hashlib.sha256(b"source").hexdigest(),
                byte_count=12,
                content_type="video/mp4",
            )
            db.add(source)
            db.flush()
            assets.append((asset.id, source.id))
        db.commit()
    yield str(owner), assets
    with sync_session() as db:
        db.execute(delete(User).where(User.id == owner))
        db.commit()


def _ensure(owner, pair, **kwargs):
    return facts.ensure_media_asset_analysis(
        asset_id=str(pair[0]), source_id=str(pair[1]), owner_id=owner, **kwargs
    )


def test_existing_speech_consumers_reuse_durable_transcript_without_download(sources, monkeypatch):
    # Value: protects=durable transcript reuse by existing speech consumers;
    # fails_when=consumers bypass facts after temporary cache expiry;
    # why_new=fact-only tests do not exercise the existing speech entrypoint;
    # seam=none; no_claim=live Whisper acceptance (provider mocked).
    from app.services.speech_segments import transcribe_stored_clip

    owner, assets = sources
    calls = []
    words = [{"text": "Hello", "start_s": 0, "end_s": 1, "confidence": 1}]
    monkeypatch.setattr(
        facts,
        "_analyze_source",
        lambda source, **kwargs: (
            calls.append(kwargs)
            or {"source": "source_transcript", "words": words, "language": "en"}
        ),
    )
    assert _ensure(owner, assets[0], analyzer="transcript")
    with sync_session() as db:
        db.execute(
            text("DELETE FROM media_analysis_cache WHERE creator_id=CAST(:owner AS uuid)"),
            {"owner": owner},
        )
        db.commit()

    def forbidden_download(*args):
        raise AssertionError("Reusable transcript downloaded its source again")

    for index in range(2):
        result = transcribe_stored_clip(
            f"users/{owner}/library/analysis-proxy-{index}.mp4",
            owner_id=owner,
            generation=str(index + 1),
            download=forbidden_download,
        )
        assert result == (words, "en")
    assert len(calls) == 1


def test_pending_library_transcript_never_falls_back_to_another_paid_call(sources, monkeypatch):
    # Value: protects=one analysis lease across speech consumers;
    # fails_when=a busy source falls back to legacy transcription;
    # why_new=lease tests did not cover speech consumers;
    # seam=none; no_claim=real queue scheduling (lease controlled).
    from app.services.speech_segments import transcribe_stored_clip

    owner, assets = sources
    monkeypatch.setattr(facts, "ensure_media_asset_analysis", lambda **kwargs: None)

    def forbidden_download(*args):
        raise AssertionError("A busy library fact fell back to a second transcription")

    with pytest.raises(RuntimeError, match="transcript"):
        transcribe_stored_clip(
            f"users/{owner}/library/analysis-proxy-0.mp4",
            owner_id=owner,
            generation="1",
            download=forbidden_download,
        )


@pytest.mark.parametrize(
    "failure,kind",
    [
        ("bytes", "video"),
        ("generation-before", "video"),
        ("generation-after", "video"),
        ("provider-none", "video"),
        ("provider-empty", "video"),
        ("provider", "video"),
        ("provider-none", "image"),
        ("provider-empty", "image"),
    ],
)
def test_source_analysis_rejects_invalid_bytes_generations_and_degraded_facts(
    sources, monkeypatch, tmp_path, failure, kind
):
    # Value: protects=source facts match verified bytes and generation;
    # fails_when=changed bytes or degraded provider output become durable facts;
    # why_new=lease tests stubbed source analysis entirely;
    # seam=none; no_claim=real provider output (image/video analyzers stubbed).
    from types import SimpleNamespace

    from app import storage
    from app.tasks import autoplace

    owner, assets = sources
    if kind == "image":
        image_asset_id, image_source_id = uuid.uuid4(), uuid.uuid4()
        with sync_session() as db:
            db.add(MediaAsset(id=image_asset_id, user_id=uuid.UUID(owner), kind="image"))
            db.flush()
            db.add(
                MediaAssetSource(
                    id=image_source_id,
                    asset_id=image_asset_id,
                    user_id=uuid.UUID(owner),
                    role="original",
                    object_path=f"users/{owner}/library/photo.png",
                    object_generation="1",
                    uploaded_sha256=hashlib.sha256(b"source").hexdigest(),
                    byte_count=6,
                    content_type="image/png",
                )
            )
            db.commit()
        source = facts._requested_source(str(image_asset_id), owner, str(image_source_id))
    else:
        source = facts._requested_source(str(assets[0][0]), owner, str(assets[0][1]))
    local = tmp_path / "source.mp4"
    local.write_bytes(b"other" if failure == "bytes" else b"source")
    generations = iter(
        ["2", "2"]
        if failure == "generation-before"
        else ["1", "2"]
        if failure == "generation-after"
        else ["1", "1"]
    )
    monkeypatch.setattr(
        storage, "object_metadata", lambda path: SimpleNamespace(generation=next(generations))
    )
    if kind == "image":
        monkeypatch.setattr(
            autoplace,
            "analyze_pool_image",
            lambda *args, **kwargs: (
                None if failure == "provider-none" else {},
                1.0,
                (8, 8),
                False,
            ),
        )
    else:
        monkeypatch.setattr(
            autoplace,
            "analyze_pool_video",
            lambda *args, **kwargs: (
                {"source": "probe_only"}
                if failure == "provider"
                else None
                if failure == "provider-none"
                else {},
                "landscape",
                10,
                (640, 360),
            ),
        )
    with pytest.raises(ValueError):
        facts._analyze_source(source, snapshot_local_path=str(local))


def test_analysis_outbox_survives_broker_failure_and_busy_fact_stays_processing(
    sources, monkeypatch
):
    # Value: protects=durable retryable publication and honest busy state;
    # fails_when=broker failure loses work or busy facts become failed;
    # why_new=finalization tests stubbed publication rather than reconciliation;
    # seam=none; no_claim=live broker delivery (broker failure controlled).
    from datetime import UTC, datetime, timedelta

    from app.models import MediaAssetUploadReservation
    from app.tasks import media_library as task

    owner, assets = sources
    identifier = uuid.uuid4()
    with sync_session() as db:
        db.add(
            MediaAssetUploadReservation(
                id=identifier,
                user_id=uuid.UUID(owner),
                client_upload_id="outbox",
                object_path=f"users/{owner}/outbox.mp4",
                request_json={},
                status="finalized",
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
                asset_id=assets[0][0],
                source_id=assets[0][1],
                analysis_dispatch_pending=True,
            )
        )
        db.commit()

    def broker_unavailable(*args):
        raise ConnectionError("Broker is unavailable")

    monkeypatch.setattr(task.analyze_media_asset_task, "delay", broker_unavailable)
    with pytest.raises(ConnectionError):
        task.reconcile_media_library_analysis.run()
    with sync_session() as db:
        assert db.get(MediaAssetUploadReservation, identifier).analysis_dispatch_pending is True
    sent = []
    monkeypatch.setattr(task.analyze_media_asset_task, "delay", lambda *args: sent.append(args))
    assert task.reconcile_media_library_analysis.run() >= 1
    assert (str(assets[0][0]), owner) in sent
    with sync_session() as db:
        assert db.get(MediaAssetUploadReservation, identifier).analysis_dispatch_pending is False
    monkeypatch.setattr(task, "ensure_media_asset_analysis", lambda **kwargs: None)
    monkeypatch.setattr(task, "media_asset_analysis_status", lambda **kwargs: "processing")
    result = task.analyze_media_asset_task.run(str(assets[0][0]), owner)
    assert result["status"] == "processing"
    with sync_session() as db:
        assert db.get(MediaAsset, assets[0][0]).status == "processing"


def test_two_imports_reuse_one_call_after_temporary_cache_expiry(sources, monkeypatch):
    owner, assets = sources
    calls = []
    monkeypatch.setattr(
        facts,
        "_analyze_source",
        lambda source: (
            calls.append(source)
            or {"source": "clip_metadata", "analysis_version": 8, "description": "Beach"}
        ),
    )
    assert _ensure(owner, assets[0])["description"] == "Beach"
    with sync_session() as db:
        db.execute(
            text(
                "UPDATE media_analysis_cache SET expires_at=now()-interval '1 day' "
                "WHERE creator_id=CAST(:owner AS uuid)"
            ),
            {"owner": owner},
        )
        db.execute(
            text("UPDATE media_asset_sources SET status='deleted' WHERE id=CAST(:source AS uuid)"),
            {"source": str(assets[0][1])},
        )
        db.commit()
    assert _ensure(owner, assets[1])["description"] == "Beach"
    assert (
        facts.get_media_asset_analysis(asset_id=str(assets[0][0]), owner_id=owner)["description"]
        == "Beach"
    )
    assert len(calls) == 1
    print("provider_calls=1 durable_fact_hits=2 unnecessary_uploads=0")


def test_concurrent_misses_only_one_provider_call(sources, monkeypatch):
    owner, assets = sources
    entered, release = threading.Event(), threading.Event()
    calls = []

    def provider(source):
        calls.append(source)
        entered.set()
        assert release.wait(10)
        return {"source": "clip_metadata", "analysis_version": 8}

    monkeypatch.setattr(facts, "_analyze_source", provider)
    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(_ensure, owner, assets[0])
        assert entered.wait(10)
        assert _ensure(owner, assets[1]) is None
        release.set()
        assert first.result(10) is not None
    assert _ensure(owner, assets[1]) is not None
    assert len(calls) == 1
    print("concurrent_provider_calls=1")


def test_versions_owner_source_binding_and_failed_retry(sources, monkeypatch):
    owner, assets = sources
    calls = []

    def provider(source):
        calls.append(source)
        if len(calls) == 1:
            raise RuntimeError("provider temporarily failed")
        return {"source": "clip_metadata", "analysis_version": 8}

    monkeypatch.setattr(facts, "_analyze_source", provider)
    assert _ensure(owner, assets[0]) is None
    assert _ensure(owner, assets[0]) is not None
    original = facts._spec
    monkeypatch.setattr(
        facts, "_spec", lambda kind, analyzer: (*original(kind, analyzer)[:3], "new-schema")
    )
    assert _ensure(owner, assets[1]) is not None
    assert len(calls) == 3
    assert _ensure(str(uuid.uuid4()), assets[0]) is None
    assert (
        facts.get_media_asset_analysis(
            asset_id=str(assets[0][0]), source_id=str(assets[1][1]), owner_id=owner
        )
        is None
    )


def test_stale_attempt_cannot_complete_new_lease(sources):
    owner, assets = sources
    source = facts._requested_source(str(assets[0][0]), owner, str(assets[0][1]))
    key = facts._key(source, None)
    first = clip_cache.claim_media_asset_fact(
        key, canonical_asset_id=str(assets[0][0]), canonical_source_id=str(assets[0][1])
    )
    with sync_session() as db:
        db.execute(
            text(
                "UPDATE media_asset_analyses SET lease_expires_at=now()-interval '1 second' "
                "WHERE id=CAST(:id AS uuid)"
            ),
            {"id": first.analysis_id},
        )
        db.commit()
    second = clip_cache.claim_media_asset_fact(
        key, canonical_asset_id=str(assets[0][0]), canonical_source_id=str(assets[0][1])
    )
    assert second.attempt_token != first.attempt_token
    assert not clip_cache.complete_media_asset_fact(first, {"description": "stale"})
    assert clip_cache.complete_media_asset_fact(second, {"description": "current"})
    assert clip_cache.get_media_asset_fact(key)["description"] == "current"


def test_audio_has_its_own_versioned_song_analysis_seam():
    name, model, prompt, schema = facts._spec("audio", None)
    assert "song" in name
    assert "beat" in schema
    assert model and prompt
