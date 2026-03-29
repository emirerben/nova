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

    with patch.object(storage.settings, "google_application_credentials", ""), \
         patch.object(storage.settings, "google_service_account_json", json.dumps(_FAKE_SA_INFO)), \
         patch.object(storage.settings, "gcloud_project", "nova-test"):
        storage._get_client()

    mock_from_info.assert_called_once_with(_FAKE_SA_INFO)
    mock_gcs_client.assert_called_once_with(project="nova-test", credentials=fake_creds)


@patch("app.storage.gcs.Client")
@patch("app.storage.service_account.Credentials.from_service_account_file")
def test_file_credentials_take_priority_over_json(mock_from_file, mock_gcs_client):
    """Tier 1 wins: file path credentials take priority when both are set."""
    fake_creds = MagicMock()
    mock_from_file.return_value = fake_creds

    with patch.object(storage.settings, "google_application_credentials", "/path/to/sa.json"), \
         patch.object(storage.settings, "google_service_account_json", json.dumps(_FAKE_SA_INFO)), \
         patch.object(storage.settings, "gcloud_project", "nova-test"):
        storage._get_client()

    mock_from_file.assert_called_once_with("/path/to/sa.json")
    mock_gcs_client.assert_called_once_with(project="nova-test", credentials=fake_creds)


def test_malformed_json_raises_runtime_error():
    """Tier 2 with bad JSON raises RuntimeError with an actionable message."""
    with patch.object(storage.settings, "google_application_credentials", ""), \
         patch.object(storage.settings, "google_service_account_json", "not-valid-json{"), \
         patch.object(storage.settings, "gcloud_project", ""):
        with pytest.raises(RuntimeError, match="invalid JSON"):
            storage._get_client()


@patch("app.storage.gcs.Client")
def test_adc_fallback_when_neither_set(mock_gcs_client):
    """Tier 3: ADC fallback when no explicit credentials are configured."""
    with patch.object(storage.settings, "google_application_credentials", ""), \
         patch.object(storage.settings, "google_service_account_json", ""), \
         patch.object(storage.settings, "gcloud_project", ""):
        storage._get_client()

    mock_gcs_client.assert_called_once_with(project=None, credentials=None)


def test_invalid_sa_structure_raises_runtime_error():
    """Tier 2: valid JSON but not a valid service account key structure."""
    with patch.object(storage.settings, "google_application_credentials", ""), \
         patch.object(storage.settings, "google_service_account_json", '{"foo": "bar"}'), \
         patch.object(storage.settings, "gcloud_project", ""):
        with pytest.raises(RuntimeError, match="missing required fields"):
            storage._get_client()


@patch("app.storage.gcs.Client")
def test_whitespace_only_json_falls_through_to_adc(mock_gcs_client):
    """Whitespace-only GOOGLE_SERVICE_ACCOUNT_JSON is treated as unset (ADC fallback)."""
    with patch.object(storage.settings, "google_application_credentials", ""), \
         patch.object(storage.settings, "google_service_account_json", "  \n  "), \
         patch.object(storage.settings, "gcloud_project", ""):
        storage._get_client()

    mock_gcs_client.assert_called_once_with(project=None, credentials=None)
