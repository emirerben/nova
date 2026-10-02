"""HTTP-level coverage for the iOS-only legacy creation admission fence."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    original_mode = settings.ios_device_only_mode
    settings.ios_device_only_mode = True
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        settings.ios_device_only_mode = original_mode


def test_flag_off_leaves_legacy_mutation_routing_unchanged() -> None:
    original_mode = settings.ios_device_only_mode
    settings.ios_device_only_mode = False
    try:
        response = TestClient(app, raise_server_exceptions=False).post("/uploads/not-a-route")
    finally:
        settings.ios_device_only_mode = original_mode

    assert response.status_code == 404


def test_get_legacy_creation_path_passes_unchanged(client: TestClient) -> None:
    response = client.get("/uploads/not-a-route")

    assert response.status_code == 404


def test_anonymous_and_web_proxy_legacy_mutations_are_retired(client: TestClient) -> None:
    anonymous = client.post("/uploads/not-a-route")
    web_proxy = client.post(
        "/uploads/not-a-route",
        headers={"Authorization": "Bearer internal-key", "X-User-Id": "user-from-web-proxy"},
    )

    for response in (anonymous, web_proxy):
        assert response.status_code == 410
        assert response.json()["problem"]["code"] == "web_creation_retired"


@pytest.mark.parametrize("protocol", [None, "malformed", "1", "-1"])
def test_old_or_malformed_native_protocol_requires_update(
    client: TestClient, protocol: str | None
) -> None:
    headers = {"Authorization": "Bearer not-a-real-jwt"}
    if protocol is not None:
        headers["X-Kria-Client-Protocol"] = protocol

    response = client.post("/uploads/not-a-route", headers=headers)

    assert response.status_code == 426
    assert response.json()["problem"]["code"] == "native_update_required"


def test_current_native_protocol_reaches_normal_auth(client: TestClient) -> None:
    response = client.post(
        "/creation-threads",
        json={},
        headers={
            "Authorization": "Bearer not-a-real-jwt",
            "X-Kria-Client-Protocol": str(settings.kria_minimum_client_protocol),
        },
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/content-plans",
        "/music-jobs",
        "/template-jobs",
        "/uploads/drive-import",
        "/generative-jobs",
        "/generative-jobs/job-id/variants/variant-id/retext",
        "/generative-jobs/job-id/clips",
        "/plan-items/item-id/slide-post/generate",
        "/plan-items/item-id/variants/variant-id/retext",
    ],
)
def test_current_native_cloud_only_creation_is_unsupported(client: TestClient, path: str) -> None:
    response = client.post(
        path,
        headers={
            "Authorization": "Bearer not-a-real-jwt",
            "X-Kria-Client-Protocol": str(settings.kria_minimum_client_protocol),
        },
    )

    assert response.status_code == 422
    assert response.json()["problem"]["code"] == "device_render_unsupported"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/generative-jobs/upload-url"),
        ("DELETE", "/generative-jobs/uploads/reservation-id"),
        ("POST", "/plan-items/item-id/variants/variant-id/editor-commit"),
        ("POST", "/plan-items/item-id/variants/variant-id/editor-sources"),
        ("POST", "/me/jobs/job-id/device-render/retry"),
    ],
)
def test_current_native_device_contracts_reach_normal_auth(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(
        method,
        path,
        headers={
            "Authorization": "Bearer not-a-real-jwt",
            "X-Kria-Client-Protocol": str(settings.kria_minimum_client_protocol),
        },
    )

    assert response.status_code not in {410, 426}
    assert response.json().get("problem", {}).get("code") != "device_render_unsupported"


def test_auth_and_me_mutations_are_not_creation_gated(client: TestClient) -> None:
    for path in ("/auth/not-a-route", "/me/not-a-route"):
        response = client.post(path)
        assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/admin/templates/template-id/test-job",
        "/admin/templates/template-id/rerender-job",
        "/admin/templates/template-id/text-preview",
        "/admin/overlay-preview",
        "/admin/music-tracks/track-id/lyrics-preview",
        "/admin/music-tracks/track-id/test-job",
        "/admin/music-tracks/track-id/rerender-job",
    ],
)
def test_admin_render_entrypoints_are_retired(client: TestClient, path: str) -> None:
    response = client.post(path)

    assert response.status_code == 410
    assert response.json()["problem"]["code"] == "web_creation_retired"


@pytest.mark.parametrize(
    "path",
    [
        "/admin/templates/template-id",
        "/admin/templates/template-id/test-job/extra",
        "/admin/music-tracks/track-id/test-jobs",
        "/admin/music-tracks/track-id/lyrics-preview-jobs",
        "/admin/overlay-preview/extra",
    ],
)
def test_admin_metadata_and_near_matches_are_not_creation_gated(
    client: TestClient, path: str
) -> None:
    response = client.post(path)

    assert response.status_code != 410
