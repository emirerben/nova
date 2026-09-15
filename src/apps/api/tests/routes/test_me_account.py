"""Route tests for account deletion + data export (privacy policy §9).

Mock-DB style, mirroring test_me_jobs.py. Covers: the confirmation-code round
trip (request → confirm), the IDOR guard (a code minted for user A must not
delete user B even if B is somehow signed in with it), the FK-safe deletion
order dispatching the async GCS purge, and the export bundle's shape.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.routes import me
from app.services.mobile_auth import ProviderClaims

_KEY = Fernet.generate_key().decode()


def _user(*, user_id: uuid.UUID | None = None, email: str = "creator@example.com") -> MagicMock:
    u = MagicMock()
    u.id = user_id or uuid.uuid4()
    u.email = email
    u.name = "Creator"
    u.auth_provider = "google"
    u.onboarding_status = "complete"
    u.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return u


def _job(*, user_id: uuid.UUID, raw_storage_path: str = "users/x/job/raw.mp4") -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.user_id = user_id
    job.raw_storage_path = raw_storage_path
    job.mode = "generative"
    job.job_type = "default"
    job.status = "variants_ready"
    job.created_at = datetime(2026, 5, 1, tzinfo=UTC)
    job.transcript = None
    job.selected_platforms = None
    job.assembly_plan = {}
    job.all_candidates = {}
    job.content_plan_item_id = None
    job.celery_task_id = None
    return job


def _scalars(rows: list) -> MagicMock:
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    return r


def _db(execute_results: list) -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    remaining = iter(execute_results)

    async def execute(statement):
        # Creator memory is an additive privacy surface. Keep the existing
        # account-flow fixtures focused on their historical query sequence;
        # dedicated memory tests cover the new tables themselves.
        if "creator_memory_" in str(statement):
            return _scalars([])
        # KRI-55 adds lifecycle serialization and an Apple identity snapshot;
        # legacy fixtures have no Apple identities and retain their original
        # deletion-order assertions below.
        sql = str(statement)
        if "pg_advisory_xact_lock" in sql:
            return MagicMock()
        if "count(mobile_identities.id)" in sql:
            result = MagicMock()
            result.scalar_one.return_value = 0
            return result
        if "mobile_identities" in sql:
            return _scalars([])
        if "SELECT users.id" in sql:
            result = MagicMock()
            result.scalar_one_or_none.return_value = object()
            return result
        return next(remaining)

    db.execute = AsyncMock(side_effect=execute)
    return db


def _override(user, db) -> None:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db


client = TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


# ── POST /me/account/delete-request ────────────────────────────────────────────


def test_delete_request_dispatches_email_with_valid_token() -> None:
    user = _user()
    _override(user, _db([]))
    with (
        patch("app.routes.me.settings.token_encryption_key", _KEY),
        patch("app.routes.me.settings.resend_api_key", "test-resend"),
        patch("app.tasks.account_lifecycle.send_account_deletion_email.delay") as mock_delay,
    ):
        resp = client.post("/me/account/delete-request")
    assert resp.status_code == 202
    assert resp.json() == {
        "requested": True,
        "apple_authorization_required": False,
        "apple_authorization_count": 0,
    }
    mock_delay.assert_called_once()
    email_arg, token_arg = mock_delay.call_args.args
    assert email_arg == user.email
    # The token decrypts back to the caller's own id.
    assert Fernet(_KEY.encode()).decrypt(token_arg.encode()).decode() == str(user.id)


def test_delete_request_503s_when_encryption_key_unset() -> None:
    user = _user()
    _override(user, _db([]))
    with patch("app.routes.me.settings.token_encryption_key", ""):
        resp = client.post("/me/account/delete-request")
    assert resp.status_code == 503


def test_delete_request_fails_when_email_or_apple_revocation_is_not_configured() -> None:
    user = _user()
    _override(user, _db([]))
    with patch("app.routes.me.settings.token_encryption_key", _KEY):
        assert client.post("/me/account/delete-request").status_code == 503


@pytest.mark.asyncio
async def test_delete_request_checks_apple_configuration_before_enqueuing_email(
    monkeypatch,
) -> None:
    user = _user()
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one.return_value = 1
    db.execute.return_value = result
    delayed = MagicMock()
    monkeypatch.setattr(me.settings, "token_encryption_key", _KEY)
    monkeypatch.setattr(me.settings, "resend_api_key", "configured")
    monkeypatch.setattr(
        me,
        "validate_apple_revocation_configuration",
        lambda: (_ for _ in ()).throw(me.AppleRevocationError("apple_revocation_not_configured")),
    )
    monkeypatch.setattr("app.tasks.account_lifecycle.send_account_deletion_email.delay", delayed)
    with pytest.raises(HTTPException) as raised:
        await me.request_account_deletion(user, db)
    assert raised.value.status_code == 503
    delayed.assert_not_called()


# ── POST /me/account/delete-confirm ─────────────────────────────────────────────


def test_delete_confirm_rejects_garbage_token() -> None:
    user = _user()
    _override(user, _db([]))
    with patch("app.routes.me.settings.token_encryption_key", _KEY):
        resp = client.post("/me/account/delete-confirm", json={"token": "not-a-real-token"})
    assert resp.status_code == 400


def test_delete_confirm_rejects_token_minted_for_a_different_user() -> None:
    caller = _user()
    other_user_id = uuid.uuid4()
    other_token = Fernet(_KEY.encode()).encrypt(str(other_user_id).encode()).decode()
    _override(caller, _db([]))
    with patch("app.routes.me.settings.token_encryption_key", _KEY):
        resp = client.post("/me/account/delete-confirm", json={"token": other_token})
    # 404, not 403 — matches this file's IDOR convention (never confirm the
    # token was well-formed for an account that isn't the caller's).
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_apple_missing_or_foreign_proofs_do_not_consume_codes(monkeypatch) -> None:
    """Subject-set validation is complete before Apple's one-use exchange."""
    user = _user()
    token = Fernet(_KEY.encode()).encrypt(str(user.id).encode()).decode()
    identity = MagicMock(subject="linked-apple-subject")

    async def execute(statement):
        sql = str(statement)
        if "pg_advisory_xact_lock" in sql:
            return MagicMock()
        if "SELECT users.id" in sql:
            result = MagicMock()
            result.scalar_one_or_none.return_value = object()
            return result
        if "mobile_identities" in sql:
            return _scalars([identity])
        raise AssertionError(sql)

    db = AsyncMock()
    db.execute.side_effect = execute
    foreign = ProviderClaims(
        provider="apple",
        subject="foreign-subject",
        issuer="https://appleid.apple.com",
        email="creator@example.com",
        name=None,
        email_verified=True,
        client_id="com.example.kria",
    )
    verify = AsyncMock(return_value=foreign)
    exchange = AsyncMock()
    monkeypatch.setattr(me.settings, "token_encryption_key", _KEY)
    monkeypatch.setattr(me, "verify_provider_id_token", verify)
    monkeypatch.setattr(me, "exchange_deletion_authorization", exchange)

    with pytest.raises(HTTPException) as raised:
        await me.confirm_account_deletion(
            me.DeleteConfirmBody(
                token=token,
                apple_authorizations=[
                    me.AppleDeletionAuthorization(
                        id_token="x" * 20, nonce="n" * 8, authorization_code="code"
                    )
                ],
            ),
            user,
            db,
        )
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "apple_authorization_required"
    exchange.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_apple_proof_persists_encrypted_outbox_before_commit_and_dispatches_after(
    monkeypatch,
) -> None:
    """The irreversible code exchange becomes durable before erasing the user."""
    user = _user()
    token = Fernet(_KEY.encode()).encrypt(str(user.id).encode()).decode()
    identity = MagicMock(subject="linked-subject")
    calls: list[str] = []
    added: list[object] = []
    db = AsyncMock()
    db.add = MagicMock(side_effect=lambda row: added.append(row))

    def scalar(value):
        result = MagicMock()
        result.scalar_one_or_none.return_value = value
        return result

    async def execute(statement):
        sql = str(statement)
        if "pg_advisory_xact_lock" in sql:
            return MagicMock()
        if "SELECT users.id" in sql:
            return scalar(object())
        if "mobile_identities" in sql:
            return _scalars([identity])
        if "content_plans" in sql:
            return _scalars([])
        if "personas" in sql:
            return MagicMock()
        if "FROM jobs" in sql:
            return _scalars([])
        if "oauth_tokens" in sql:
            return _scalars([])
        if "creator_memory_" in sql:
            return _scalars([])
        return MagicMock()

    db.execute.side_effect = execute

    async def commit():
        calls.append("commit")

    db.commit.side_effect = commit
    claims = ProviderClaims(
        provider="apple",
        subject="linked-subject",
        issuer="https://appleid.apple.com",
        email="creator@example.com",
        name=None,
        email_verified=True,
        client_id="com.kria.ios",
    )
    credential = MagicMock(encrypted_refresh_token=b"encrypted-refresh", client_id="com.kria.ios")
    monkeypatch.setattr(me.settings, "token_encryption_key", _KEY)
    monkeypatch.setattr(me, "verify_provider_id_token", AsyncMock(return_value=claims))
    exchange = AsyncMock(return_value=credential)
    monkeypatch.setattr(me, "exchange_deletion_authorization", exchange)
    monkeypatch.setattr(me, "_delete_job_storage_after_commit", AsyncMock())
    monkeypatch.setattr("app.tasks.account_lifecycle.purge_user_storage.delay", lambda *_: None)
    from app.tasks.apple_revocation import revoke_apple_credential

    monkeypatch.setattr(
        revoke_apple_credential, "apply_async", lambda *_args, **_kwargs: calls.append("dispatch")
    )

    response = await me.confirm_account_deletion(
        me.DeleteConfirmBody(
            token=token,
            apple_authorizations=[
                me.AppleDeletionAuthorization(
                    id_token="x" * 20, nonce="n" * 8, authorization_code="one-use"
                )
            ],
        ),
        user,
        db,
    )
    assert response.status_code == 204
    exchange.assert_awaited_once()
    outbox = next(row for row in added if row.__class__.__name__ == "AppleRevocationOutbox")
    assert outbox.encrypted_refresh_token == b"encrypted-refresh"
    assert calls == ["commit", "dispatch"]


def test_delete_confirm_deletes_in_fk_safe_order_and_dispatches_purge() -> None:
    user = _user()
    job = _job(user_id=user.id)
    publication_id = uuid.uuid4()
    # Snapshot worker committed its claim but has not yet persisted the
    # deterministic copy path. Account erasure must still schedule that key.
    publication = MagicMock(
        id=publication_id,
        user_id=user.id,
        job_id=job.id,
        source_object_path=f"generative-jobs/{job.id}/output.mp4",
        snapshot_object_path=None,
        processing_status="snapshotting",
    )
    token = Fernet(_KEY.encode()).encrypt(str(user.id).encode()).decode()

    # execute() call order in confirm_account_deletion:
    #   1-2. lock ContentPlan then Persona (global mutation order)
    #   3. select+lock jobs (for job ids/raw paths/storage manifests)
    #   4-6. lock clips, publications, and existing deletion outboxes
    #   7. update Job.content_plan_item_id -> NULL
    #   8. select+lock OAuthToken rows
    #   9-12. delete captured publications, tokens, Jobs, then User
    db = _db(
        [
            _scalars([]),  # 1 — no plans
            MagicMock(),  # 2 — Persona lock
            _scalars([job]),  # 3
            _scalars([]),  # 4 — no JobClip rows
            _scalars([publication]),  # 5 — in-flight TikTok snapshot
            _scalars([]),  # 6 — no existing storage outbox
            MagicMock(),  # 7
            _scalars([]),  # 8 — no OAuth token, nothing to revoke
            MagicMock(),  # 9
            MagicMock(),  # 10
            MagicMock(),  # 11
            MagicMock(),  # 12
        ]
    )
    _override(user, db)
    dispatch = AsyncMock()
    with (
        patch("app.routes.me.settings.token_encryption_key", _KEY),
        patch("app.tasks.account_lifecycle.purge_user_storage.delay") as mock_purge,
        patch("app.routes.me._delete_job_storage_after_commit", dispatch),
    ):
        resp = client.post("/me/account/delete-confirm", json={"token": token})

    assert resp.status_code == 204
    assert db.execute.await_count == 16
    lock_sql = [str(call.args[0]) for call in db.execute.await_args_list[3:6]]
    assert "content_plans" in lock_sql[0]
    assert "personas" in lock_sql[1]
    assert "jobs" in lock_sql[2]
    db.commit.assert_awaited_once()
    added_rows = [call.args[0] for call in db.add.call_args_list]
    outbox = next(row for row in added_rows if getattr(row, "job_id", None) == job.id)
    assert outbox.job_id == job.id
    assert outbox.object_paths["version"] == 2
    assert f"generative-jobs/{job.id}/" in {
        entry["prefix"] for entry in outbox.object_paths["prefixes"]
    }
    account_outbox = next(
        row
        for row in added_rows
        if row is not outbox and getattr(row, "object_prefixes", None) == [f"users/{user.id}/"]
    )
    assert account_outbox.object_paths == [f"tiktok-publish/{publication_id}.mp4"]
    assert account_outbox.next_attempt_at is not None
    assert account_outbox.next_attempt_at > datetime.now(UTC) + timedelta(hours=24)
    assert dispatch.await_count == 2
    assert {call.args[0] for call in dispatch.await_args_list} == {
        outbox.id,
        account_outbox.id,
    }
    mock_purge.assert_called_once_with(str(user.id), [str(job.id)], [job.raw_storage_path])


def test_delete_confirm_revokes_tiktok_token_before_deleting(monkeypatch) -> None:
    user = _user()
    token = Fernet(_KEY.encode()).encrypt(str(user.id).encode()).decode()

    tiktok_row = MagicMock()
    tiktok_row.id = uuid.uuid4()
    tiktok_row.platform = "tiktok"
    tiktok_row.access_token = b"encrypted-blob"

    db = _db(
        [
            _scalars([]),  # no plans
            MagicMock(),  # Persona lock
            _scalars([]),  # jobs
            MagicMock(),  # null content_plan_item_id
            _scalars([tiktok_row]),  # tiktok OAuthToken rows
            MagicMock(),  # delete TikTokPublication
            MagicMock(),  # delete OAuthToken
            MagicMock(),  # delete Job
            MagicMock(),  # delete User
        ]
    )
    _override(user, db)
    with (
        patch("app.routes.me.settings.token_encryption_key", _KEY),
        patch("app.routes.me.decrypt_token", return_value="raw-access-token") as mock_decrypt,
        patch("app.routes.me.tiktok_client.revoke_access") as mock_revoke,
        patch("app.tasks.account_lifecycle.purge_user_storage.delay"),
    ):
        resp = client.post("/me/account/delete-confirm", json={"token": token})

    assert resp.status_code == 204
    mock_decrypt.assert_called_once_with(b"encrypted-blob")
    mock_revoke.assert_called_once_with("raw-access-token")


# ── GET /me/export ───────────────────────────────────────────────────────────


def test_export_returns_full_bundle() -> None:
    user = _user()
    job = _job(user_id=user.id)

    persona = MagicMock()
    persona.questionnaire = {"work": "barista"}
    persona.persona = {"summary": "..."}
    persona.tiktok_profile = None
    persona.style = None
    persona.idea_seeds = []
    persona.persona_status = "ready"
    persona.created_at = datetime(2026, 1, 2, tzinfo=UTC)

    db = _db(
        [
            MagicMock(scalar_one_or_none=MagicMock(return_value=persona)),  # persona
            _scalars([]),  # content_plans
            _scalars([job]),  # jobs
            _scalars([]),  # feedback
            _scalars([]),  # tiktok publications
        ]
    )
    _override(user, db)
    with patch("app.routes.me.signed_get_url", return_value="https://signed.example/raw.mp4"):
        resp = client.get("/me/export")

    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["email"] == user.email
    assert body["persona"]["questionnaire"] == {"work": "barista"}
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["source_media_url"] == "https://signed.example/raw.mp4"
    assert "All jobs include a re-signed source-media link" in body["note"]


def test_export_projects_private_speech_cleanup_state_without_mutating_job() -> None:
    user = _user()
    job = _job(user_id=user.id)
    job.assembly_plan = {
        "title": "Keep this",
        "_speech_cleanup_internal": {"terminal_pending": {"secret": True}},
        "candidate_snapshot": {
            "clip_source_instance_ids": ["00000000-0000-4000-8000-000000000001"],
            "clip_metadata_identity_index_v2": {"records": []},
            "clip_paths": ["source.mp4"],
        },
    }
    stored = copy.deepcopy(job.assembly_plan)
    db = _db(
        [
            MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            _scalars([]),
            _scalars([job]),
            _scalars([]),
            _scalars([]),
        ]
    )
    _override(user, db)

    with patch("app.routes.me.signed_get_url", return_value="https://signed.example/raw.mp4"):
        resp = client.get("/me/export")

    assert resp.status_code == 200
    assert resp.json()["jobs"][0]["assembly_plan"] == {
        "title": "Keep this",
        "candidate_snapshot": {},
    }
    assert job.assembly_plan == stored


def test_export_redacts_provisional_media_when_required_speech_state_is_malformed() -> None:
    user = _user()
    job = _job(user_id=user.id)
    provisional = {
        "variant_id": "subtitled",
        "render_status": "rendering",
        "render_generation_id": "generation-new",
        "ok": True,
        "video_path": (
            f"generative-jobs/{job.id}/render-generations/generation-new/PROVISIONAL.mp4"
        ),
        "output_url": "https://private.example/PROVISIONAL",
        "poster_path": f"generative-jobs/{job.id}/PROVISIONAL.jpg",
        "candidate_snapshot": {
            "clip_source_instance_ids": ["00000000-0000-4000-8000-000000000001"],
            "source_references": [f"generative-jobs/{job.id}/PROVISIONAL-source.mov"],
            "source_tag": "0123456789abcdef",
        },
    }
    job.assembly_plan = {
        "speech_cleanup_contract": "required_v1",
        "speech_cut_control": {
            "variant_id": "subtitled",
            "render_generation_id": "generation-new",
        },
        "speech_cut_previous_variant": copy.deepcopy(provisional),
        "speech_cut_previous_variants": [copy.deepcopy(provisional)],
        "_speech_cleanup_internal": {
            "required_speech_generation_locks": {"subtitled": "generation-mismatch"},
        },
        "variants": [copy.deepcopy(provisional)],
    }
    stored = copy.deepcopy(job.assembly_plan)
    db = _db(
        [
            MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            _scalars([]),
            _scalars([job]),
            _scalars([]),
            _scalars([]),
        ]
    )
    _override(user, db)

    with patch("app.routes.me.signed_get_url", return_value="https://signed.example/raw.mp4"):
        resp = client.get("/me/export")

    assert resp.status_code == 200
    exported_plan = resp.json()["jobs"][0]["assembly_plan"]
    [visible] = exported_plan["variants"]
    assert visible == {
        "variant_id": "subtitled",
        "render_status": "rendering",
        "ok": False,
        "candidate_snapshot": {},
    }
    assert "provisional" not in str(exported_plan).lower()
    assert "generation-new" not in str(exported_plan)
    assert "generation-mismatch" not in str(exported_plan)
    assert "00000000-0000-4000-8000-000000000001" not in str(exported_plan)
    assert job.assembly_plan == stored


def test_export_serialization_never_leaks_source_uuid_through_omni_references() -> None:
    """Owner export keeps opaque durable paths while stripping stable identities."""
    user = _user()
    job = _job(user_id=user.id)
    source_instance_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    copy_attempt_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    durable_source_path = (
        f"generative-jobs/{job.id}/sources/copy-attempts/{copy_attempt_id}/slot-0000.mov"
    )
    assert source_instance_id not in durable_source_path

    job.raw_storage_path = durable_source_path
    job.all_candidates = {
        "clip_paths": [durable_source_path],
        "clip_source_instance_ids": [source_instance_id],
    }
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "song_text",
                "candidate_snapshot": {
                    "clip_paths": [durable_source_path],
                    "clip_source_instance_ids": [source_instance_id],
                },
                "ai_timeline": {
                    "slots": [{"clip_index": 0, "source_gcs_path": durable_source_path}]
                },
            }
        ],
        "omni_generated_assets": {
            "asset-opaque": {
                "status": "ready",
                "source_references": [durable_source_path],
                "clip_source_instance_ids": [source_instance_id],
            }
        },
    }
    db = _db(
        [
            MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            _scalars([]),
            _scalars([job]),
            _scalars([]),
            _scalars([]),
        ]
    )
    _override(user, db)
    signed_source_paths: list[str] = []

    def _sign(path: str, expiration_minutes: int) -> str:
        signed_source_paths.append(path)
        return f"https://signed.example/{path}?ttl={expiration_minutes}"

    with patch("app.routes.me.signed_get_url", side_effect=_sign):
        resp = client.get("/me/export")

    assert resp.status_code == 200
    serialized_export = resp.content.decode("utf-8")
    exported_job = resp.json()["jobs"][0]
    exported_omni = exported_job["assembly_plan"]["omni_generated_assets"]["asset-opaque"]
    assert exported_omni["source_references"] == [durable_source_path]
    assert signed_source_paths == [durable_source_path]
    assert source_instance_id not in serialized_export
    assert source_instance_id not in exported_job["source_media_url"]


def test_export_survives_a_signing_failure_without_500ing() -> None:
    user = _user()
    job = _job(user_id=user.id)

    db = _db(
        [
            MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # no persona
            _scalars([]),  # content_plans
            _scalars([job]),  # jobs
            _scalars([]),  # feedback
            _scalars([]),  # tiktok publications
        ]
    )
    _override(user, db)
    with patch("app.routes.me.signed_get_url", side_effect=RuntimeError("gcs down")):
        resp = client.get("/me/export")

    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"][0]["source_media_url"] is None
    assert body["persona"] is None


def test_export_rejects_quarantined_plan_before_loading_children() -> None:
    user = _user()
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user.id
    plan.persona_id = uuid.uuid4()
    plan.ownership_quarantined_at = datetime(2026, 8, 11, tzinfo=UTC)
    db = _db(
        [
            MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            _scalars([plan]),
        ]
    )
    _override(user, db)

    resp = client.get("/me/export")

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Content plan is unavailable"
    assert db.execute.await_count == 2
