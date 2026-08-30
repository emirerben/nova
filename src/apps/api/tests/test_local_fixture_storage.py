from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from google.api_core.exceptions import PreconditionFailed

from app import storage
from app.config import settings
from app.main import app
from app.routes import dev_qa_storage


@pytest.fixture
def local_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(settings, "storage_provider", "local")
    monkeypatch.setattr(settings, "e2e_fixtures", True)
    monkeypatch.setattr(settings, "local_storage_root", str(tmp_path))
    monkeypatch.setattr(
        settings,
        "local_storage_base_url",
        "http://127.0.0.1:8000/dev-qa/storage",
    )
    return tmp_path


def test_local_fixture_storage_round_trip(local_storage: Path, tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture-video")

    url = storage.upload_public_read(str(source), "fixtures/item/source.mp4")
    metadata = storage.object_metadata("fixtures/item/source.mp4")

    assert url == "http://127.0.0.1:8000/dev-qa/storage/fixtures/item/source.mp4"
    assert metadata.size == len(b"fixture-video")
    assert metadata.generation
    assert storage.object_exists("fixtures/item/source.mp4") is True

    downloaded = tmp_path / "downloaded.mp4"
    storage.download_generation_to_file(
        "fixtures/item/source.mp4",
        str(downloaded),
        generation=metadata.generation,
    )
    assert downloaded.read_bytes() == b"fixture-video"


def test_local_fixture_generation_checks_reject_replaced_bytes(
    local_storage: Path, tmp_path: Path
) -> None:
    object_path = "fixtures/item/source.mp4"
    fixture = local_storage / object_path
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"original")
    stale_generation = storage.object_metadata(object_path).generation
    fixture.write_bytes(b"replacement-with-a-different-size")
    assert storage.object_metadata(object_path).generation != stale_generation

    with pytest.raises(FileNotFoundError):
        storage.download_generation_to_file(
            object_path,
            str(tmp_path / "download.mp4"),
            generation=stale_generation,
        )
    with pytest.raises(PreconditionFailed):
        storage.delete_object_generation(object_path, generation=stale_generation)
    with pytest.raises(PreconditionFailed):
        storage.copy_object_generation(
            object_path,
            "fixtures/item/copy.mp4",
            source_generation=stale_generation,
        )

    assert fixture.read_bytes() == b"replacement-with-a-different-size"
    assert not (local_storage / "fixtures/item/copy.mp4").exists()


def test_local_fixture_storage_rejects_traversal(local_storage: Path) -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        storage.local_object_path("../outside.mp4")


def test_local_fixture_route_serves_media(
    local_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = local_storage / "fixtures" / "image.jpg"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"fixture-image")

    monkeypatch.setattr(dev_qa_storage, "_is_loopback_client", lambda _request: True)
    response = TestClient(app).get("/dev-qa/storage/fixtures/image.jpg")

    assert response.status_code == 200
    assert response.content == b"fixture-image"
    assert response.headers["content-type"] == "image/jpeg"


def test_local_fixture_route_rejects_non_loopback_clients(
    local_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = local_storage / "fixtures" / "image.jpg"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"fixture-image")
    monkeypatch.setattr(dev_qa_storage, "_is_loopback_client", lambda _request: False)

    response = TestClient(app).get("/dev-qa/storage/fixtures/image.jpg")

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", True), ("::1", True), ("10.0.0.8", False), ("testclient", False)],
)
def test_local_fixture_loopback_fence(host: str, expected: bool) -> None:
    request = cast(Request, SimpleNamespace(client=SimpleNamespace(host=host)))
    assert dev_qa_storage._is_loopback_client(request) is expected


@pytest.mark.parametrize(
    ("provider", "fixtures_enabled"),
    [("gcs", True), ("local", False)],
)
def test_local_fixture_route_requires_both_storage_gates(
    local_storage: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    fixtures_enabled: bool,
) -> None:
    fixture = local_storage / "fixtures" / "image.jpg"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"fixture-image")
    monkeypatch.setattr(settings, "storage_provider", provider)
    monkeypatch.setattr(settings, "e2e_fixtures", fixtures_enabled)
    monkeypatch.setattr(dev_qa_storage, "_is_loopback_client", lambda _request: True)

    response = TestClient(app).get("/dev-qa/storage/fixtures/image.jpg")

    assert response.status_code == 404


def test_local_fixture_storage_requires_fixture_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "storage_provider", "local")
    monkeypatch.setattr(settings, "e2e_fixtures", False)
    monkeypatch.setattr(settings, "local_storage_root", str(tmp_path))

    with pytest.raises(RuntimeError, match="E2E_FIXTURES"):
        storage.local_object_path("fixtures/source.mp4")


def test_local_fixture_storage_rejects_browser_signed_uploads(local_storage: Path) -> None:
    with pytest.raises(RuntimeError, match="does not support browser-signed uploads"):
        storage.signed_put_url("fixtures/upload.mp4", "video/mp4", 100)
