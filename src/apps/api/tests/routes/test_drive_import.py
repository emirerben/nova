"""Tests for Drive import endpoints: POST /uploads/drive-import and batch variants."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app

# Generate a real Fernet key for tests
TEST_FERNET_KEY = Fernet.generate_key().decode()


def _mock_db():
    """Yield a mock async DB session."""
    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    yield mock_db


@pytest.fixture()
def client():
    app.dependency_overrides[get_db] = _mock_db
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    app.dependency_overrides.clear()


def _valid_single_request():
    return {
        "drive_file_id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
        "filename": "vacation-raw.mp4",
        "file_size_bytes": 500_000_000,
        "mime_type": "video/mp4",
        "platforms": ["instagram", "youtube"],
        "google_access_token": "ya29.test-token-value-here",
    }


def _valid_batch_request():
    return {
        "files": [
            {
                "drive_file_id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
                "filename": "clip1.mp4",
                "file_size_bytes": 100_000_000,
                "mime_type": "video/mp4",
            },
            {
                "drive_file_id": "2CyiNWt1YSB6oGNueKwCeAkhnVVrquumct85PhWF3qnt",
                "filename": "clip2.mov",
                "file_size_bytes": 200_000_000,
                "mime_type": "video/quicktime",
            },
        ],
        "google_access_token": "ya29.test-token-value-here",
    }


# ── Single Drive Import ──────────────────────────────────────────────────────


class TestDriveImportValidation:
    """POST /uploads/drive-import validation."""

    def test_valid_request_returns_202(self, client):
        with (
            patch("app.routes.uploads.settings") as mock_s,
            patch("app.routes.uploads._fernet_valid", None),
            patch("app.routes.uploads._fernet", None),
            patch("app.tasks.drive_import.import_from_drive") as mock_task,
        ):
            mock_s.token_encryption_key = TEST_FERNET_KEY
            mock_s.max_upload_bytes = 4 * 1024 * 1024 * 1024
            mock_task.apply_async = MagicMock()

            # Reset cached Fernet state
            import app.routes.uploads as uploads_mod
            uploads_mod._fernet_valid = None
            uploads_mod._fernet = None

            res = client.post("/uploads/drive-import", json=_valid_single_request())

        assert res.status_code == 202
        data = res.json()
        assert data["status"] == "importing"
        assert "job_id" in data
        call = mock_task.apply_async.call_args
        assert call.kwargs["task_id"]
        assert call.kwargs["task_id"] != data["job_id"]

    def test_enqueue_failure_only_fails_matching_unclaimed_import(self, client):
        db = AsyncMock()
        db.add = MagicMock()
        failure = MagicMock(rowcount=1)
        db.execute = AsyncMock(return_value=failure)

        async def _db():
            yield db

        app.dependency_overrides[get_db] = _db
        with (
            patch("app.routes.uploads.settings") as mock_s,
            patch("app.routes.uploads._fernet_valid", None),
            patch("app.routes.uploads._fernet", None),
            patch("app.tasks.drive_import.import_from_drive") as mock_task,
        ):
            mock_s.token_encryption_key = TEST_FERNET_KEY
            mock_s.max_upload_bytes = 4 * 1024 * 1024 * 1024
            mock_task.apply_async.side_effect = RuntimeError("broker response lost")

            import app.routes.uploads as uploads_mod

            uploads_mod._fernet_valid = None
            uploads_mod._fernet = None
            res = client.post("/uploads/drive-import", json=_valid_single_request())

        assert res.status_code == 503
        job = db.add.call_args.args[0]
        assert job.celery_task_id == mock_task.apply_async.call_args.kwargs["task_id"]
        statement = db.execute.await_args.args[0]
        sql = str(statement)
        assert "jobs.status" in sql
        assert "jobs.celery_task_id" in sql
        assert "jobs.started_at IS NULL" in sql
        assert "processing_failed" in statement.compile().params.values()

    def test_ambiguous_enqueue_does_not_overwrite_claimed_import(self, client):
        db = AsyncMock()
        db.add = MagicMock()
        failure = MagicMock(rowcount=0)
        status_result = MagicMock()
        status_result.scalar_one_or_none.return_value = "importing"
        db.execute = AsyncMock(side_effect=[failure, status_result])

        async def _db():
            yield db

        app.dependency_overrides[get_db] = _db
        with (
            patch("app.routes.uploads.settings") as mock_s,
            patch("app.tasks.drive_import.import_from_drive") as mock_task,
        ):
            mock_s.token_encryption_key = TEST_FERNET_KEY
            mock_s.max_upload_bytes = 4 * 1024 * 1024 * 1024
            mock_task.apply_async.side_effect = RuntimeError("ambiguous publish")

            import app.routes.uploads as uploads_mod

            uploads_mod._fernet_valid = None
            uploads_mod._fernet = None
            res = client.post("/uploads/drive-import", json=_valid_single_request())

        assert res.status_code == 202
        assert res.json()["status"] == "importing"
        job = db.add.call_args.args[0]
        assert job.status == "importing"

    def test_missing_fernet_key_returns_503(self, client):
        import app.routes.uploads as uploads_mod
        uploads_mod._fernet_valid = None
        uploads_mod._fernet = None

        with patch("app.routes.uploads.settings") as mock_s:
            mock_s.token_encryption_key = ""

            res = client.post("/uploads/drive-import", json=_valid_single_request())

        assert res.status_code == 503
        assert "token_encryption_key" in res.json()["detail"]

    def test_file_too_large_returns_422(self, client):
        import app.routes.uploads as uploads_mod
        uploads_mod._fernet_valid = None
        uploads_mod._fernet = None

        with patch("app.routes.uploads.settings") as mock_s:
            mock_s.token_encryption_key = TEST_FERNET_KEY
            mock_s.max_upload_bytes = 1_000_000  # 1MB limit

            body = _valid_single_request()
            body["file_size_bytes"] = 2_000_000  # 2MB, exceeds limit

            res = client.post("/uploads/drive-import", json=body)

        assert res.status_code == 422
        assert "limit" in res.json()["detail"].lower()

    def test_invalid_mime_returns_422(self, client):
        import app.routes.uploads as uploads_mod
        uploads_mod._fernet_valid = None
        uploads_mod._fernet = None

        with patch("app.routes.uploads.settings") as mock_s:
            mock_s.token_encryption_key = TEST_FERNET_KEY
            mock_s.max_upload_bytes = 4 * 1024 * 1024 * 1024

            body = _valid_single_request()
            body["mime_type"] = "image/jpeg"

            res = client.post("/uploads/drive-import", json=body)

        assert res.status_code == 422
        assert "unsupported" in res.json()["detail"].lower()

    def test_accepts_application_octet_stream(self, client):
        """Drive may report application/octet-stream for valid videos."""
        import app.routes.uploads as uploads_mod
        uploads_mod._fernet_valid = None
        uploads_mod._fernet = None

        with (
            patch("app.routes.uploads.settings") as mock_s,
            patch("app.tasks.drive_import.import_from_drive"),
        ):
            mock_s.token_encryption_key = TEST_FERNET_KEY
            mock_s.max_upload_bytes = 4 * 1024 * 1024 * 1024

            body = _valid_single_request()
            body["mime_type"] = "application/octet-stream"

            res = client.post("/uploads/drive-import", json=body)

        assert res.status_code == 202

    def test_invalid_drive_file_id_returns_422(self, client):
        body = _valid_single_request()
        body["drive_file_id"] = "short"  # too short (< 10 chars)

        res = client.post("/uploads/drive-import", json=body)
        assert res.status_code == 422

    def test_invalid_drive_file_id_special_chars(self, client):
        body = _valid_single_request()
        body["drive_file_id"] = "../../../etc/passwd"

        res = client.post("/uploads/drive-import", json=body)
        assert res.status_code == 422

    def test_empty_platforms_returns_422(self, client):
        import app.routes.uploads as uploads_mod
        uploads_mod._fernet_valid = None
        uploads_mod._fernet = None

        with patch("app.routes.uploads.settings") as mock_s:
            mock_s.token_encryption_key = TEST_FERNET_KEY
            mock_s.max_upload_bytes = 4 * 1024 * 1024 * 1024

            body = _valid_single_request()
            body["platforms"] = []

            res = client.post("/uploads/drive-import", json=body)

        assert res.status_code == 422

    def test_unknown_platform_returns_422(self, client):
        import app.routes.uploads as uploads_mod
        uploads_mod._fernet_valid = None
        uploads_mod._fernet = None

        with patch("app.routes.uploads.settings") as mock_s:
            mock_s.token_encryption_key = TEST_FERNET_KEY
            mock_s.max_upload_bytes = 4 * 1024 * 1024 * 1024

            body = _valid_single_request()
            body["platforms"] = ["twitch"]

            res = client.post("/uploads/drive-import", json=body)

        assert res.status_code == 422


class TestFernetRoundTrip:
    """Verify token encryption/decryption works end-to-end."""

    def test_encrypt_decrypt_roundtrip(self):
        key = Fernet.generate_key()
        f = Fernet(key)
        token = "ya29.a0AfB_byAbCdEfGhIjKlMnOpQrStUvWxYz"

        encrypted = f.encrypt(token.encode()).decode()
        decrypted = f.decrypt(encrypted.encode()).decode()

        assert decrypted == token
        assert encrypted != token  # actually encrypted

    def test_fernet_json_serializer_compat(self):
        """Celery uses JSON serializer. Encrypted tokens must survive JSON round-trip."""
        import json

        key = Fernet.generate_key()
        f = Fernet(key)
        token = "ya29.test-token"

        encrypted = f.encrypt(token.encode()).decode()

        # Simulate Celery JSON serialization round-trip
        serialized = json.dumps({"token": encrypted})
        deserialized = json.loads(serialized)

        decrypted = f.decrypt(deserialized["token"].encode()).decode()
        assert decrypted == token


# ── Batch Drive Import ────────────────────────────────────────────────────────


class TestDriveImportBatchValidation:
    """POST /uploads/drive-import-batch validation."""

    def test_empty_files_returns_422(self, client):
        import app.routes.uploads as uploads_mod
        uploads_mod._fernet_valid = None
        uploads_mod._fernet = None

        with patch("app.routes.uploads.settings") as mock_s:
            mock_s.token_encryption_key = TEST_FERNET_KEY

            body = {"files": [], "google_access_token": "ya29.test"}
            res = client.post("/uploads/drive-import-batch", json=body)

        assert res.status_code == 422

    def test_too_many_files_returns_422(self, client):
        import app.routes.uploads as uploads_mod
        uploads_mod._fernet_valid = None
        uploads_mod._fernet = None

        with patch("app.routes.uploads.settings") as mock_s:
            mock_s.token_encryption_key = TEST_FERNET_KEY

            files = [
                {
                    "drive_file_id": f"1BxiMVs0XRA5nFMdKvBdBZjgmUUqp{i:04d}",
                    "filename": f"clip{i}.mp4",
                    "file_size_bytes": 1000,
                    "mime_type": "video/mp4",
                }
                for i in range(21)
            ]
            body = {"files": files, "google_access_token": "ya29.test"}
            res = client.post("/uploads/drive-import-batch", json=body)

        assert res.status_code == 422
        assert "20" in res.json()["detail"]

    def test_batch_id_uuid_validation(self, client):
        """GET /uploads/drive-import-batch/{id}/status rejects non-UUID IDs."""
        res = client.get("/uploads/drive-import-batch/not-a-uuid/status")
        assert res.status_code == 400

    def test_batch_not_found_returns_404(self, client):
        with patch("app.routes.uploads._get_redis") as mock_redis:
            mock_redis.return_value.get.return_value = None
            batch_id = str(uuid.uuid4())
            res = client.get(f"/uploads/drive-import-batch/{batch_id}/status")
        assert res.status_code == 404

    def test_file_extension_whitelist(self, client):
        """User-supplied filename extension must be sanitized."""
        import app.routes.uploads as uploads_mod
        uploads_mod._fernet_valid = None
        uploads_mod._fernet = None

        with (
            patch("app.routes.uploads.settings") as mock_s,
            patch("app.tasks.drive_import.batch_import_from_drive"),
        ):
            mock_s.token_encryption_key = TEST_FERNET_KEY
            mock_s.max_upload_bytes = 4 * 1024 * 1024 * 1024

            body = {
                "files": [{
                    "drive_file_id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
                    "filename": "video.mp4/../../etc/passwd",
                    "file_size_bytes": 1000,
                    "mime_type": "video/mp4",
                }],
                "google_access_token": "ya29.test",
            }
            res = client.post("/uploads/drive-import-batch", json=body)

        # Should succeed but extension should be sanitized to "mp4"
        assert res.status_code == 202
        gcs_path = res.json()["gcs_paths"][0]
        assert gcs_path.endswith(".mp4")
        assert "etc" not in gcs_path
        assert ".." not in gcs_path
