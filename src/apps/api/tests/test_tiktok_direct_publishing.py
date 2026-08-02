from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request

from app import storage
from app.config import Settings
from app.models import TikTokPublication
from app.routes.me import _to_library_job
from app.routes.tiktok import (
    CreatePublicationBody,
    _apply_webhook,
    _callback_redirect,
    _minimize_disconnected_publication,
    _parse_range,
    _read_bounded_webhook_body,
    _RedactTikTokMediaAccessFilter,
    _validate_post_settings,
    _verify_webhook,
    create_publication,
    media_verification,
    oauth_start,
    request_sync,
)
from app.services import tiktok_client
from app.services.tiktok_publishable import (
    PublishableOutputError,
    _duration_seconds,
    _edit_signature,
    resolve_publishable_output,
)
from app.services.token_crypto import _fernet, decrypt_token, encrypt_token
from app.tasks.tiktok import (
    _analysis_fingerprint,
    _can_submit_failed,
    _edit_correlations,
    _mature_fingerprint,
    _normalize_videos,
    _recover_stale_publication,
    _summary_with_edit_correlations,
    _within_evaluation_window,
)


def _job(path: str = "generative-jobs/00000000-0000-0000-0000-000000000002/output.mp4"):
    job = MagicMock()
    job.id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    job.user_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    job.mode = "generative"
    job.job_type = "default"
    job.music_track_id = None
    job.probe_metadata = {"duration": 18.0}
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "song_text",
                "render_status": "ready",
                "video_path": path,
                "output_url": "https://signed.example/video.mp4",
                "text_mode": "agent_text",
                "style_set_id": "bold",
            }
        ]
    }
    return job


def test_publishable_output_binds_owned_path_and_generation() -> None:
    metadata = storage.ObjectMetadata(
        path="generative-jobs/00000000-0000-0000-0000-000000000002/output.mp4",
        generation="42",
        etag="etag",
        size=123,
        content_type="video/mp4",
    )
    with patch("app.services.tiktok_publishable.storage.object_metadata", return_value=metadata):
        result = resolve_publishable_output(_job(), "song_text")
    assert result.generation == "42"
    assert result.variant_id == "song_text"
    assert len(result.source_revision) == 64
    assert result.duration_s == 18.0


def test_stale_publish_recovery_is_duplicate_safe_at_retry_boundary() -> None:
    now = datetime.now(UTC)
    snapshotting = TikTokPublication(processing_status="snapshotting")
    submitting = TikTokPublication(processing_status="submitting")

    assert _recover_stale_publication(snapshotting, now) is True
    assert snapshotting.processing_status == "queued"
    assert _recover_stale_publication(submitting, now) is False
    assert submitting.processing_status == "submission_unknown"
    assert submitting.retryable is False
    assert _can_submit_failed(True, 3) is True
    assert _can_submit_failed(True, 4) is False


def test_publishable_output_rejects_arbitrary_signed_url_path() -> None:
    with pytest.raises(PublishableOutputError, match="trusted Nova storage"):
        resolve_publishable_output(_job("other-user/video.mp4"), "song_text")


def test_publishable_output_rejects_missing_or_unready_variant() -> None:
    job = _job()
    job.assembly_plan["variants"][0]["render_status"] = "rendering"
    with pytest.raises(PublishableOutputError, match="not ready"):
        resolve_publishable_output(job, "song_text")


@pytest.mark.parametrize(
    ("duration", "expected_bucket"),
    [(14.9, "under_15s"), (15, "15_to_30s"), (30, "30s_plus"), (None, "15_to_30s")],
)
def test_publishable_metadata_duration_fallback_and_signature_buckets(
    duration: float | None, expected_bucket: str
) -> None:
    job = _job()
    job.assembly_plan["variants"][0]["duration_s"] = duration
    job.probe_metadata = {"duration_s": 21}
    variant = job.assembly_plan["variants"][0]
    assert _duration_seconds(job, variant) == (duration if duration else 21)
    if duration is None:
        job.probe_metadata = {}
    assert _edit_signature(job, variant)["duration_bucket"] == expected_bucket

    job.status = "music_ready"
    job.mode = "auto_music"
    job.created_at = datetime.now(UTC)
    job.content_plan_item_id = None
    job.assembly_plan = {"output_url": "https://signed.example/auto.mp4"}
    publication_updated_at = datetime(2026, 8, 1, tzinfo=UTC)
    publication = MagicMock(
        id=uuid.uuid4(),
        job_id=job.id,
        variant_id=None,
        processing_status="complete",
        visibility_status="private",
        retryable=False,
        failure_code=None,
        failure_detail=None,
        latest_metrics=None,
        metrics_synced_at=None,
        created_at=job.created_at,
        updated_at=publication_updated_at,
    )
    mapped = _to_library_job(job, tiktok_publication=publication)
    assert mapped.tiktok_publishable is True
    assert mapped.tiktok_publication is not None
    assert mapped.tiktok_publication.updated_at == publication_updated_at


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, (0, 99, False)),
        ("bytes=10-19", (10, 19, True)),
        ("bytes=90-", (90, 99, True)),
        ("bytes=-10", (90, 99, True)),
    ],
)
def test_media_range_parsing(header: str | None, expected: tuple[int, int, bool]) -> None:
    assert _parse_range(header, 100) == expected


def test_media_range_rejects_multiple_ranges() -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_range("bytes=0-1,4-5", 100)
    assert exc.value.status_code == 416


@pytest.mark.asyncio
async def test_media_prefix_verification_is_public_plain_text() -> None:
    response = await media_verification()

    assert response.status_code == 200
    assert response.media_type == "text/plain"
    assert response.body == (
        b"tiktok-developers-site-verification=9a2bMaksajhuoYRL3P7tSex7MrV8z5lg"
    )


@pytest.mark.parametrize(
    ("value", "size"),
    [(None, 0), ("items=0-1", 100), ("bytes=abc-1", 100), ("bytes=100-101", 100)],
)
def test_media_range_rejects_empty_invalid_and_unsatisfiable_ranges(
    value: str | None, size: int
) -> None:
    with pytest.raises(HTTPException) as exc:
        _parse_range(value, size)
    assert exc.value.status_code in {404, 416}


@pytest.mark.asyncio
async def test_webhook_signature_freshness_body_cap_and_access_log_redaction() -> None:
    body = json.dumps({"event": "post.publish.complete"}, separators=(",", ":")).encode()
    timestamp = int(time.time())
    secret = "webhook-secret"
    signature = hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    with patch("app.routes.tiktok.settings.tiktok_client_secret", secret):
        assert _verify_webhook(f"t={timestamp},s={signature}", body) == (timestamp, signature)
        with pytest.raises(HTTPException, match="Stale"):
            _verify_webhook(f"t={timestamp - 600},s={signature}", body)

    oversized = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/tiktok/webhook",
            "headers": [(b"content-length", b"1048577")],
        }
    )
    with pytest.raises(HTTPException) as exc:
        await _read_bounded_webhook_body(oversized)
    assert exc.value.status_code == 413

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/tiktok/media/id/super-secret.mp4", "1.1", 200),
        None,
    )
    assert _RedactTikTokMediaAccessFilter().filter(record) is True
    assert record.args[2] == "/tiktok/media/[redacted]"

    publication = TikTokPublication(
        id=uuid.uuid4(),
        processing_status="processing",
        visibility_status="unknown",
        privacy_level="SELF_ONLY",
        tiktok_publish_id="publish-1",
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = publication
    db = AsyncMock()
    db.execute.return_value = result
    retry_id = await _apply_webhook(
        db,
        {"event": "post.publish.complete", "content": {"publish_id": "publish-1"}},
    )
    assert retry_id is None
    assert publication.processing_status == "complete"
    assert publication.visibility_status == "private"
    assert publication.next_poll_at is None

    publication.visibility_status = "removed"
    assert (
        await _apply_webhook(
            db,
            {
                "event": "post.publish.publicly_available",
                "content": {"publish_id": "publish-1", "post_id": "post-1"},
            },
        )
        is None
    )
    assert publication.visibility_status == "removed"

    publication.visibility_status = "unknown"
    publication.processing_status = "complete"
    assert (
        await _apply_webhook(
            db,
            {
                "event": "post.publish.failed",
                "content": {"publish_id": "publish-1", "fail_reason": "internal"},
            },
        )
        is None
    )
    assert publication.processing_status == "complete"

    queued = TikTokPublication(
        processing_status="queued",
        source_object_path="jobs/id/output.mp4",
        source_generation="42",
        title="sensitive title",
        retryable=True,
    )
    _minimize_disconnected_publication(queued)
    assert queued.processing_status == "failed"
    assert queued.failure_code == "authorization_removed"
    assert queued.source_object_path == "redacted"
    assert queued.title == ""


@pytest.mark.parametrize("header", [None, "bad", "t=nope,s=deadbeef", "t=1,s=deadbeef"])
def test_webhook_rejects_missing_malformed_and_invalid_signatures(header: str | None) -> None:
    with patch("app.routes.tiktok.settings.tiktok_client_secret", "secret"):
        with pytest.raises(HTTPException) as exc:
            _verify_webhook(header, b"{}")
    assert exc.value.status_code == 401


def test_callback_redirect_rejects_external_invalid_or_credentialed_targets() -> None:
    unsafe = [
        "https://attacker.example/library",
        "javascript:alert(1)",
        "https://user:pass@example.test/library",
        "https://example.test/not-library",
    ]
    for value in unsafe:
        with patch("app.routes.tiktok.settings.tiktok_web_app_url", value):
            response = _callback_redirect(tiktok="connected")
        assert str(response.headers["location"]).startswith("http://localhost:3000/library?")


def _body(**changes) -> CreatePublicationBody:
    payload = {
        "job_id": uuid.uuid4(),
        "source_revision": "a" * 64,
        "idempotency_key": "idem-12345",
        "privacy_level": "SELF_ONLY",
        "music_usage_confirmed": True,
    }
    payload.update(changes)
    return CreatePublicationBody(**payload)


@pytest.mark.asyncio
async def test_mutating_routes_fail_closed_before_external_side_effects() -> None:
    user = MagicMock(id=uuid.uuid4())
    db = MagicMock()
    with (
        patch("app.routes.tiktok.settings.tiktok_publishing_enabled", False),
        patch("app.routes.tiktok.settings.tiktok_performance_sync_enabled", False),
        patch("app.routes.tiktok.settings.tiktok_publishing_beta_user_ids", []),
    ):
        with pytest.raises(HTTPException) as oauth_exc:
            await oauth_start(user)
        with pytest.raises(HTTPException) as publish_exc:
            await create_publication(_body(), user, db)
        with pytest.raises(HTTPException) as sync_exc:
            await request_sync(user, db)
    status_codes = (
        oauth_exc.value.status_code,
        publish_exc.value.status_code,
        sync_exc.value.status_code,
    )
    assert status_codes == (
        404,
        404,
        404,
    )

    with (
        patch("app.routes.tiktok.settings.tiktok_publishing_enabled", True),
        patch("app.routes.tiktok.settings.tiktok_content_posting_audited", True),
    ):
        with pytest.raises(HTTPException, match="Music usage"):
            await create_publication(_body(music_usage_confirmed=False), user, db)


def test_beta_post_settings_are_private_and_interactions_respect_creator_info() -> None:
    creator = {
        "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
        "comment_disabled": True,
    }
    with patch("app.routes.tiktok.settings.tiktok_content_posting_audited", False):
        _validate_post_settings(_body(), creator)
        with pytest.raises(HTTPException, match="private-only"):
            _validate_post_settings(_body(privacy_level="PUBLIC_TO_EVERYONE"), creator)
        with pytest.raises(HTTPException, match="allow_comment"):
            _validate_post_settings(_body(allow_comment=True), creator)


def test_post_settings_reject_unavailable_privacy_and_private_branded_content() -> None:
    creator = {"privacy_level_options": ["SELF_ONLY"]}
    with patch("app.routes.tiktok.settings.tiktok_content_posting_audited", True):
        with pytest.raises(HTTPException, match="privacy option"):
            _validate_post_settings(_body(privacy_level="PUBLIC_TO_EVERYONE"), creator)
        with pytest.raises(HTTPException, match="privacy option"):
            _validate_post_settings(_body(), {"privacy_level_options": []})
        with pytest.raises(HTTPException, match="Branded content"):
            _validate_post_settings(_body(brand_content_toggle=True), creator)


def test_official_video_normalization_uses_share_count() -> None:
    rows = _normalize_videos(
        [
            {
                "id": "v1",
                "view_count": 100,
                "like_count": 10,
                "comment_count": 2,
                "share_count": 3,
                "duration": 12,
                "create_time": 1_700_000_000,
            }
        ]
    )
    assert rows[0]["share_count"] == 3
    assert rows[0]["repost_count"] is None
    assert rows[0]["engagement_rate"] == 0.15


def test_direct_post_pull_payload_does_not_send_file_upload_chunk_fields() -> None:
    response = MagicMock()
    response.is_error = False
    response.json.return_value = {"data": {"publish_id": "publish-1"}, "error": {"code": "ok"}}
    with patch("app.services.tiktok_client.httpx.request", return_value=response) as request:
        assert (
            tiktok_client.initialize_direct_post(
                "access", post_info={"privacy_level": "SELF_ONLY"}, media_url="https://api/media"
            )
            == "publish-1"
        )
    source_info = request.call_args.kwargs["json"]["source_info"]
    assert source_info == {"source": "PULL_FROM_URL", "video_url": "https://api/media"}


def test_tiktok_client_rejects_invalid_success_and_maps_retryable_errors() -> None:
    invalid = MagicMock(is_error=False, status_code=200)
    invalid.json.return_value = {"data": {}}
    with pytest.raises(tiktok_client.TikTokAPIError, match="invalid token"):
        tiktok_client._token_payload({"refresh_token": "refresh"})

    invalid.json.side_effect = ValueError("not json")
    with pytest.raises(tiktok_client.TikTokAPIError) as exc:
        tiktok_client._decode(invalid)
    assert exc.value.code == "invalid_response"

    limited = MagicMock(is_error=True, status_code=429)
    limited.json.return_value = {"error": {"code": "rate_limit", "message": "slow down"}}
    with pytest.raises(tiktok_client.TikTokAPIError) as exc:
        tiktok_client._decode(limited)
    assert exc.value.status_code == 429
    assert exc.value.retryable is True

    oauth_denial = MagicMock(is_error=True, status_code=400)
    oauth_denial.json.return_value = {
        "error": "invalid_grant",
        "error_description": "Authorization code was already used",
    }
    with pytest.raises(tiktok_client.TikTokAPIError) as exc:
        tiktok_client._decode(oauth_denial)
    assert exc.value.code == "invalid_grant"
    assert str(exc.value) == "Authorization code was already used"
    assert exc.value.retryable is False

    with patch(
        "app.services.tiktok_client.httpx.request",
        side_effect=httpx.ConnectTimeout("connect timeout"),
    ):
        with pytest.raises(tiktok_client.TikTokAPIError) as exc:
            tiktok_client._json("GET", "/v2/user/info/", "access")
    assert exc.value.retryable is True
    assert exc.value.ambiguous is False

    with patch(
        "app.services.tiktok_client.httpx.request",
        side_effect=httpx.ReadTimeout("read timeout"),
    ):
        with pytest.raises(tiktok_client.TikTokAPIError) as exc:
            tiktok_client.initialize_direct_post(
                "access",
                post_info={"privacy_level": "SELF_ONLY"},
                media_url="https://api/media",
            )
    assert exc.value.retryable is False
    assert exc.value.ambiguous is True


def test_beta_user_id_setting_accepts_csv_json_and_empty_values() -> None:
    assert Settings.parse_tiktok_beta_user_ids(" a, b ,, ") == ["a", "b"]
    assert Settings.parse_tiktok_beta_user_ids('["a", "b"]') == ["a", "b"]
    assert Settings.parse_tiktok_beta_user_ids("  ") == []


def test_evaluation_snapshot_is_limited_to_the_72_to_84_hour_window() -> None:
    now = datetime.now(UTC)
    assert _within_evaluation_window(now - timedelta(hours=72), now)
    assert _within_evaluation_window(now - timedelta(hours=84), now)
    assert not _within_evaluation_window(now - timedelta(hours=71, minutes=59), now)
    assert not _within_evaluation_window(now - timedelta(hours=84, minutes=1), now)


def test_edit_correlations_require_supported_buckets_and_are_low_confidence() -> None:
    rows = []
    for layout, views in (("cluster", 200), ("linear", 100)):
        for _ in range(3):
            rows.append(
                TikTokPublication(
                    edit_signature={"text_mode": layout},
                    evaluation_metrics={"view_count": views},
                    visibility_status="public",
                )
            )
    correlations = _edit_correlations(rows)
    assert correlations == [
        {
            "feature": "text_mode",
            "observed_value": "cluster",
            "comparison_value": "linear",
            "view_ratio": 2.0,
            "sample_size": 6,
            "window_hours": "72-84",
            "provenance": "linked_nova_public_posts",
            "confidence": "low",
            "language": "correlation_only",
        }
    ]


def test_edit_correlations_reject_small_weak_and_zero_baseline_samples() -> None:
    assert _edit_correlations([]) == []
    rows = []
    for layout, views in (("cluster", 110), ("linear", 100)):
        for _ in range(3):
            rows.append(
                TikTokPublication(
                    edit_signature={"text_mode": layout},
                    evaluation_metrics={"view_count": views},
                )
            )
    assert _edit_correlations(rows) == []

    for row in rows[3:]:
        row.evaluation_metrics = {"view_count": 0}
    assert _edit_correlations(rows) == []


def test_mature_fingerprint_changes_with_frozen_metrics() -> None:
    publication_id = uuid.uuid4()
    before = TikTokPublication(
        id=publication_id,
        edit_signature={"text_mode": "cluster"},
        evaluation_metrics={"view_count": 100},
    )
    after = TikTokPublication(
        id=publication_id,
        edit_signature={"text_mode": "cluster"},
        evaluation_metrics={"view_count": 200},
    )
    assert _mature_fingerprint([before]) != _mature_fingerprint([after])

    videos = [{"video_id": "v1", "view_index": 2.0, "engagement_rate": 0.1}]
    baseline_fingerprint = _analysis_fingerprint(videos)
    with patch("app.tasks.tiktok.TIKTOK_ANALYZER_PROMPT_VERSION", "future-version"):
        assert _analysis_fingerprint(videos) != baseline_fingerprint

    summary = _summary_with_edit_correlations(
        {
            "summary_for_prompts": "Creator summary. " + "x" * 1150,
            "edit_patterns_observed": ["Cluster edits indexed 1.4x"],
        }
    )
    assert len(summary) <= 1200
    assert "low confidence" in summary
    assert "linked public Nova posts" in summary
    assert "association only" in summary


def test_shared_token_crypto_round_trip_and_rejects_wrong_key() -> None:
    key = Fernet.generate_key().decode()
    with patch("app.services.token_crypto.settings.token_encryption_key", key):
        _fernet.cache_clear()
        encrypted = encrypt_token("secret-token")
        assert encrypted != b"secret-token"
        assert decrypt_token(encrypted) == "secret-token"
    _fernet.cache_clear()
    other_key = Fernet.generate_key().decode()
    with patch("app.services.token_crypto.settings.token_encryption_key", other_key):
        with pytest.raises(Exception, match="could not be decrypted"):
            decrypt_token(encrypted)
    _fernet.cache_clear()


def test_shared_token_crypto_rejects_missing_key_and_empty_values() -> None:
    with patch("app.services.token_crypto.settings.token_encryption_key", ""):
        _fernet.cache_clear()
        with pytest.raises(Exception, match="not configured"):
            encrypt_token("secret")
    _fernet.cache_clear()
    with pytest.raises(Exception, match="empty"):
        encrypt_token("")
    with pytest.raises(Exception, match="unavailable"):
        decrypt_token(None)
