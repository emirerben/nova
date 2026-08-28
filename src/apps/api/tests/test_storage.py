"""Tests for the GCS credential chain in storage._get_client()."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app import storage

# Realistic-looking (but fake) service account payload
_FAKE_SA_INFO = {
    "type": "service_account",
    "project_id": "nova-test",
    "private_key_id": "key123",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\n",
    "client_email": "nova@nova-test.iam.gserviceaccount.com",
    "client_id": "123456789",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}


@pytest.fixture(autouse=True)
def _reset_client():
    """Reset the module-level singleton before each test."""
    storage._client = None
    yield
    storage._client = None


@patch("app.storage.gcs.Client")
@patch("app.storage.service_account.Credentials.from_service_account_info")
def test_json_credentials_used_when_set(mock_from_info, mock_gcs_client):
    """Tier 2: JSON string credentials are used when GOOGLE_SERVICE_ACCOUNT_JSON is set."""
    fake_creds = MagicMock()
    mock_from_info.return_value = fake_creds

    with (
        patch.object(storage.settings, "google_application_credentials", ""),
        patch.object(storage.settings, "google_service_account_json", json.dumps(_FAKE_SA_INFO)),
        patch.object(storage.settings, "gcloud_project", "nova-test"),
    ):
        storage._get_client()

    # The new get_gcp_credentials() helper (PR #224) forwards `scopes` to
    # from_service_account_info() — GCS callers pass None.
    mock_from_info.assert_called_once_with(_FAKE_SA_INFO, scopes=None)
    mock_gcs_client.assert_called_once_with(project="nova-test", credentials=fake_creds)


@patch("app.storage.gcs.Client")
@patch("app.storage.service_account.Credentials.from_service_account_file")
def test_file_credentials_take_priority_over_json(mock_from_file, mock_gcs_client):
    """Tier 1 wins: file path credentials take priority when both are set."""
    fake_creds = MagicMock()
    mock_from_file.return_value = fake_creds

    with (
        patch.object(storage.settings, "google_application_credentials", "/path/to/sa.json"),
        patch.object(storage.settings, "google_service_account_json", json.dumps(_FAKE_SA_INFO)),
        patch.object(storage.settings, "gcloud_project", "nova-test"),
    ):
        storage._get_client()

    mock_from_file.assert_called_once_with("/path/to/sa.json")
    mock_gcs_client.assert_called_once_with(project="nova-test", credentials=fake_creds)


def test_malformed_json_raises_runtime_error():
    """Tier 2 with bad JSON raises RuntimeError with an actionable message."""
    with (
        patch.object(storage.settings, "google_application_credentials", ""),
        patch.object(storage.settings, "google_service_account_json", "not-valid-json{"),
        patch.object(storage.settings, "gcloud_project", ""),
    ):
        with pytest.raises(RuntimeError, match="invalid JSON"):
            storage._get_client()


@patch("app.storage.gcs.Client")
def test_adc_fallback_when_neither_set(mock_gcs_client):
    """Tier 3: ADC fallback when no explicit credentials are configured."""
    with (
        patch.object(storage.settings, "google_application_credentials", ""),
        patch.object(storage.settings, "google_service_account_json", ""),
        patch.object(storage.settings, "gcloud_project", ""),
    ):
        storage._get_client()

    mock_gcs_client.assert_called_once_with(project=None, credentials=None)


def test_invalid_sa_structure_raises_runtime_error():
    """Tier 2: valid JSON but not a valid service account key structure."""
    with (
        patch.object(storage.settings, "google_application_credentials", ""),
        patch.object(storage.settings, "google_service_account_json", '{"foo": "bar"}'),
        patch.object(storage.settings, "gcloud_project", ""),
    ):
        with pytest.raises(RuntimeError, match="missing required fields"):
            storage._get_client()


@patch("app.storage.gcs.Client")
def test_whitespace_only_json_falls_through_to_adc(mock_gcs_client):
    """Whitespace-only GOOGLE_SERVICE_ACCOUNT_JSON is treated as unset (ADC fallback)."""
    with (
        patch.object(storage.settings, "google_application_credentials", ""),
        patch.object(storage.settings, "google_service_account_json", "  \n  "),
        patch.object(storage.settings, "gcloud_project", ""),
    ):
        storage._get_client()

    mock_gcs_client.assert_called_once_with(project=None, credentials=None)


def test_signed_download_url_sets_sanitized_attachment_disposition():
    fake_blob = MagicMock()
    fake_blob.generate_signed_url.return_value = "https://storage.example/download"
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    with patch.object(storage, "_get_client", return_value=fake_client):
        url = storage.signed_download_url(
            "generative-jobs/job/output.mp4",
            'kria-../../bad\r\nname".mp4',
            expiration_minutes=360,
        )

    assert url == "https://storage.example/download"
    kwargs = fake_blob.generate_signed_url.call_args.kwargs
    assert kwargs["method"] == "GET"
    assert kwargs["response_disposition"] == 'attachment; filename="kria-badname.mp4"'


def test_signed_put_url_pins_exact_content_type():
    fake_blob = MagicMock()
    fake_blob.generate_signed_url.return_value = "https://storage.example/upload"
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    with patch.object(storage, "_get_client", return_value=fake_client):
        url = storage.signed_put_url(
            "dev-user/u/generative/batch/clip.mov",
            "video/quicktime",
            12_345,
        )

    assert url == "https://storage.example/upload"
    fake_bucket.blob.assert_called_once_with("dev-user/u/generative/batch/clip.mov")
    kwargs = fake_blob.generate_signed_url.call_args.kwargs
    assert kwargs["method"] == "PUT"
    assert kwargs["content_type"] == "video/quicktime"
    assert kwargs["headers"] == {
        "content-length": "12345",
        "x-goog-if-generation-match": "0",
    }


def test_legacy_signed_put_requires_only_content_type():
    fake_blob = MagicMock()
    fake_blob.generate_signed_url.return_value = "https://storage.example/upload"
    fake_bucket = MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket

    with patch.object(storage, "_get_client", return_value=fake_client):
        url = storage.signed_put_url_legacy(
            "users/u/plan/i/pool/x.png",
            "image/png",
            123,
        )

    assert url == "https://storage.example/upload"
    kwargs = fake_blob.generate_signed_url.call_args.kwargs
    assert kwargs["content_type"] == "image/png"
    assert kwargs["headers"] == {"content-length": "123"}


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(None, True), (storage.NotFound("gone"), True), (RuntimeError("private"), False)],
)
def test_delete_object_best_effort_is_idempotent(failure, expected):  # noqa: ANN001
    blob = MagicMock()
    if failure is not None:
        blob.delete.side_effect = failure
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    with patch.object(storage, "_get_client", return_value=client):
        assert storage.delete_object_best_effort("users/u/plan/i/pool/x") is expected


def test_object_exists_once_uses_bounded_request_without_sdk_retry():
    blob = MagicMock()
    blob.exists.return_value = True
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    with patch.object(storage, "_get_client", return_value=client):
        assert storage.object_exists_once("jobs/j/output.poster.jpg", timeout_s=3.0)

    blob.exists.assert_called_once_with(timeout=3.0, retry=None)


def test_object_exists_once_propagates_storage_outage():
    blob = MagicMock()
    blob.exists.side_effect = RuntimeError("HEAD unavailable")
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    with (
        patch.object(storage, "_get_client", return_value=client),
        pytest.raises(RuntimeError, match="HEAD unavailable"),
    ):
        storage.object_exists_once("jobs/j/output.poster.jpg", timeout_s=3.0)


def test_object_metadata_once_uses_bounded_request_without_sdk_retry():
    blob = MagicMock(
        generation=7,
        etag="etag",
        size=123,
        content_type="video/mp4",
        md5_hash="hash",
    )
    bucket = MagicMock()
    bucket.get_blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    with patch.object(storage, "_get_client", return_value=client):
        metadata = storage.object_metadata_once("jobs/j/output.mp4", timeout_s=3.0)

    bucket.get_blob.assert_called_once_with(
        "jobs/j/output.mp4",
        timeout=3.0,
        retry=None,
    )
    assert metadata == storage.ObjectMetadata(
        path="jobs/j/output.mp4",
        generation="7",
        etag="etag",
        size=123,
        content_type="video/mp4",
        md5_hash="hash",
    )


def test_object_metadata_once_rejects_missing_or_empty_generation():
    bucket = MagicMock()
    bucket.get_blob.return_value = None
    client = MagicMock()
    client.bucket.return_value = bucket

    with (
        patch.object(storage, "_get_client", return_value=client),
        pytest.raises(FileNotFoundError),
    ):
        storage.object_metadata_once("jobs/j/missing.mp4", timeout_s=3.0)


@pytest.mark.parametrize("failure", [None, storage.NotFound("already gone")])
def test_delete_object_once_is_bounded_and_missing_is_complete(failure):  # noqa: ANN001
    blob = MagicMock()
    if failure is not None:
        blob.delete.side_effect = failure
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    with patch.object(storage, "_get_client", return_value=client):
        assert storage.delete_object_once("jobs/j/output.poster.jpg", timeout_s=3.0)

    blob.delete.assert_called_once_with(timeout=3.0, retry=None)


def test_delete_object_once_propagates_storage_outage():
    blob = MagicMock()
    blob.delete.side_effect = RuntimeError("delete unavailable")
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    with (
        patch.object(storage, "_get_client", return_value=client),
        pytest.raises(RuntimeError, match="delete unavailable"),
    ):
        storage.delete_object_once("jobs/j/output.poster.jpg", timeout_s=3.0)


def test_delete_object_generation_pins_exact_generation():
    blob = MagicMock()
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    with patch.object(storage, "_get_client", return_value=client):
        storage.delete_object_generation("users/u/plan/i/pool/x", generation="42")

    bucket.blob.assert_called_once_with("users/u/plan/i/pool/x", generation=42)
    blob.delete.assert_called_once_with()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(None, True), (storage.NotFound("gone"), True), (RuntimeError("private"), False)],
)
def test_delete_object_generation_best_effort_is_exact_and_idempotent(
    failure,
    expected,  # noqa: ANN001
):
    blob = MagicMock()
    if failure is not None:
        blob.delete.side_effect = failure
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    with patch.object(storage, "_get_client", return_value=client):
        assert (
            storage.delete_object_generation_best_effort("users/u/plan/i/pool/x", generation="42")
            is expected
        )

    bucket.blob.assert_called_once_with("users/u/plan/i/pool/x", generation=42)


def test_copy_object_generation_pins_source_and_returns_destination_metadata():
    source = MagicMock()
    destination = MagicMock(
        generation=84,
        etag="etag",
        size=123,
        content_type="image/png",
    )
    bucket = MagicMock()
    bucket.blob.return_value = source
    bucket.get_blob.return_value = destination
    client = MagicMock()
    client.bucket.return_value = bucket

    with patch.object(storage, "_get_client", return_value=client):
        result = storage.copy_object_generation(
            "dev-user/u/staging/x",
            "users/u/plan/i/pool/x",
            source_generation="42",
        )

    bucket.blob.assert_called_once_with("dev-user/u/staging/x", generation=42)
    bucket.copy_blob.assert_called_once_with(
        source,
        bucket,
        "users/u/plan/i/pool/x",
        if_source_generation_match=42,
        if_generation_match=0,
    )
    assert result.generation == "84"
    assert result.path == "users/u/plan/i/pool/x"


def test_copy_object_generation_recovers_lost_response_idempotently():
    source = MagicMock()
    destination = MagicMock(
        generation=84,
        etag="etag",
        size=123,
        content_type="image/png",
    )
    bucket = MagicMock()
    bucket.blob.return_value = source
    bucket.copy_blob.side_effect = storage.PreconditionFailed("destination exists")
    bucket.get_blob.return_value = destination
    client = MagicMock()
    client.bucket.return_value = bucket

    with patch.object(storage, "_get_client", return_value=client):
        result = storage.copy_object_generation(
            "dev-user/u/staging/x",
            "users/u/plan/i/pool/x",
            source_generation="42",
        )

    assert result.generation == "84"
    bucket.copy_blob.assert_called_once_with(
        source,
        bucket,
        "users/u/plan/i/pool/x",
        if_source_generation_match=42,
        if_generation_match=0,
    )
