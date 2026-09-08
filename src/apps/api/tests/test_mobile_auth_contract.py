"""Hermetic contracts for native authentication and the portable edit recipe."""

import base64
import importlib
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.cli.kria_contracts import _openapi30_schema, mobile_openapi_json
from app.config import settings
from app.kria.recipes import (
    EditRecipeV1,
    adapt_authoritative_job_snapshot,
    adapt_editor_snapshot,
)
from app.models import TemporaryMediaUpload
from app.services import mobile_auth


def _part(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def test_access_token_is_short_lived_and_tenant_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "unit-test-mobile-secret")
    user_id, session_id = uuid.uuid4(), uuid.uuid4()
    token, ttl = mobile_auth.issue_access_token(user_id, session_id)
    assert ttl == settings.mobile_access_token_ttl_seconds
    assert mobile_auth.verify_access_token(token) == user_id

    claims = mobile_auth.decode_access_token(token)
    assert claims.session_id == session_id
    assert claims.token_id != session_id
    assert claims.expires_at - claims.issued_at == ttl

    parts = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    payload["sub"] = str(uuid.uuid4())
    forged = ".".join([parts[0], _part(payload), parts[2]])
    with pytest.raises(mobile_auth.MobileAuthError):
        mobile_auth.verify_access_token(forged)


@pytest.mark.asyncio
async def test_provider_claims_require_verified_email_and_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mobile_google_client_ids", ["ios-client"])

    async def no_signature(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(mobile_auth, "_verify_signature", no_signature)
    token = ".".join(
        [
            _part({"alg": "RS256", "kid": "test"}),
            _part(
                {
                    "iss": "https://accounts.google.com",
                    "aud": "ios-client",
                    "sub": "google-sub",
                    "email": "Creator@Example.com",
                    "email_verified": True,
                    "nonce": "nonce-1",
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 60,
                }
            ),
            "signature",
        ]
    )
    claims = await mobile_auth.verify_provider_id_token(token, "google", "nonce-1")
    assert claims.email == "creator@example.com"
    with pytest.raises(mobile_auth.MobileAuthError, match="nonce"):
        await mobile_auth.verify_provider_id_token(token, "google", "wrong")


def test_recipe_adapter_never_copies_internal_assembly_plan() -> None:
    recipe = adapt_editor_snapshot(
        {
            "assembly_plan": {"secret": "must not escape"},
            "timeline": {
                "clips": [{"media_id": "media-1", "in_s": 1, "duration_s": 2}],
            },
            "text_elements": [{"text": "Hello", "start_s": 0, "end_s": 1}],
        }
    )
    assert isinstance(recipe, EditRecipeV1)
    assert recipe.tracks[0].clips[0].source_asset_id == "media-1"
    assert "assembly_plan" not in recipe.model_dump()


def test_recipe_rejects_non_positive_clip_duration() -> None:
    with pytest.raises(ValueError):
        EditRecipeV1(
            assets=[{"id": "clip", "relative_path": "clip"}],
            tracks=[
                {
                    "id": "video",
                    "kind": "video",
                    "clips": [
                        {
                            "id": "clip-1",
                            "source_asset_id": "clip",
                            "source_duration": 0,
                            "source_start": 0,
                            "timeline_start": 0,
                            "rate": 1,
                        }
                    ],
                }
            ],
        )


def test_recipe_fixture_round_trips() -> None:
    fixture = Path(__file__).parent / "fixtures" / "kria_edit_recipe_v1.json"
    recipe = EditRecipeV1.model_validate_json(fixture.read_text())
    assert EditRecipeV1.model_validate_json(recipe.model_dump_json()) == recipe


def test_temporary_upload_cleanup_schema_and_schedule_are_registered() -> None:
    assert TemporaryMediaUpload.__tablename__ == "temporary_media_uploads"
    assert {"purpose", "retention_expires_at", "delete_attempts"}.issubset(
        TemporaryMediaUpload.__table__.columns.keys()
    )

    from app.worker import MAINTENANCE_TASK_NAMES, celery_app

    task_name = "tasks.cleanup_temporary_media_uploads"
    assert task_name in MAINTENANCE_TASK_NAMES
    assert any(entry["task"] == task_name for entry in celery_app.conf.beat_schedule.values())

    migration = importlib.import_module("app.migrations.versions.0100_mobile_identity_sessions")
    with (
        patch.object(migration.op, "create_table") as create_table,
        patch.object(migration.op, "create_index") as create_index,
    ):
        migration.upgrade()
    assert [call.args[0] for call in create_table.call_args_list] == [
        "mobile_identities",
        "mobile_sessions",
        "temporary_media_uploads",
    ]
    assert create_index.call_count == 6

    with (
        patch.object(migration.op, "drop_table") as drop_table,
        patch.object(migration.op, "drop_index") as drop_index,
    ):
        migration.downgrade()
    assert [call.args[0] for call in drop_table.call_args_list] == [
        "temporary_media_uploads",
        "mobile_sessions",
        "mobile_identities",
    ]
    assert drop_index.call_count == 6


@pytest.mark.parametrize(
    "overrides",
    [
        {"typ": "refresh"},
        {"iss": "attacker"},
        {"aud": "other-client"},
        {"exp": 0},
        {"iat": "future"},
        {"iat": int(time.time()), "exp": int(time.time())},
        {"sub": ""},
        {"sub": "not-a-uuid"},
        {"sid": "not-a-uuid"},
        {"jti": "not-a-uuid"},
        {"token_version": 0},
    ],
)
def test_access_token_rejects_invalid_claim_boundaries(monkeypatch, overrides) -> None:
    monkeypatch.setattr(settings, "mobile_jwt_secret", "unit-test-mobile-secret")
    now = int(time.time())
    payload = {
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "jti": str(uuid.uuid4()),
        "token_version": 1,
        "iss": mobile_auth.JWT_ISSUER,
        "aud": mobile_auth.JWT_AUDIENCE,
        "typ": "access",
        "iat": now,
        "exp": now + 900,
    }
    overrides = dict(overrides)
    if overrides.get("iat") == "future":
        overrides["iat"] = now + 61
    payload.update(overrides)

    with pytest.raises(mobile_auth.MobileAuthError) as raised:
        mobile_auth.decode_access_token(mobile_auth._sign(payload))
    assert raised.value.code == "invalid_access_token"


def test_authoritative_recipe_uses_ai_fallback_and_skips_removed_or_malformed_slots() -> None:
    job = SimpleNamespace(
        assembly_plan={
            "variants": [
                {"variant_id": "other", "user_timeline": {"slots": []}},
                {
                    "variant_id": "selected",
                    "ai_timeline": {
                        "slots": [
                            "not-a-row",
                            {
                                "slot_id": "removed",
                                "clip_index": 0,
                                "duration_s": 2,
                                "removed": True,
                            },
                            {
                                "slot_id": "kept",
                                "clip_index": 3,
                                "in_s": 0.5,
                                "duration_s": 2,
                            },
                        ]
                    },
                },
            ]
        }
    )
    recipe = adapt_authoritative_job_snapshot(job, variant_id="selected")

    assert [asset.id for asset in recipe.assets] == ["3"]
    assert [clip.id for clip in recipe.tracks[0].clips] == ["kept"]
    assert recipe.tracks[0].clips[0].source_start == 0.5
    assert adapt_authoritative_job_snapshot(SimpleNamespace(assembly_plan=None)).tracks == []


def test_recipe_rejects_duplicate_assets_and_unknown_audio_reference() -> None:
    with pytest.raises(ValueError, match="asset IDs must be unique"):
        EditRecipeV1(
            assets=[
                {"id": "same", "relative_path": "first"},
                {"id": "same", "relative_path": "second"},
            ]
        )
    with pytest.raises(ValueError, match="audio references an unknown asset"):
        EditRecipeV1(audio={"music_asset_id": "missing"})


def test_mobile_openapi_translates_nullable_bounds_and_constants() -> None:
    nullable = _openapi30_schema({"anyOf": [{"type": "string"}, {"type": "null"}]})
    assert nullable == {"type": "string", "nullable": True}
    bounded = _openapi30_schema({"exclusiveMinimum": 0, "exclusiveMaximum": 10, "const": "Bearer"})
    assert bounded == {
        "minimum": 0,
        "exclusiveMinimum": True,
        "maximum": 10,
        "exclusiveMaximum": True,
        "enum": ["Bearer"],
    }

    document = json.loads(mobile_openapi_json())
    assert document["paths"]["/auth/mobile/link"]["post"]["security"] == [{"mobileBearer": []}]
    assert (
        document["paths"]["/generative-jobs/uploads/{reservation_id}"]["delete"]["operationId"]
        == "cancelTemporaryUpload"
    )
    assert (
        document["paths"]["/creation-threads/{thread_id}/upload-urls"]["post"]["operationId"]
        == "reserveCreationThreadUploads"
    )
    assert (
        document["paths"]["/creation-threads/{thread_id}/media"]["post"]["operationId"]
        == "attachCreationThreadMedia"
    )
