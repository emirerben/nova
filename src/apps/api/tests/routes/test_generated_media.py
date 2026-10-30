"""Native and rollout fences run before generated provider work."""

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import get_current_user, is_native_client, kria_client_protocol
from app.config import settings
from app.models import User
from app.routes.generated_media import router


def test_disabled_and_non_native_requests_never_enter_service(monkeypatch):
    app = FastAPI()
    app.include_router(router, prefix="/plan-items")
    user = User(id=uuid.uuid4(), email="offline@example.test")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[is_native_client] = lambda: True
    app.dependency_overrides[kria_client_protocol] = lambda: 99
    monkeypatch.setattr(settings, "generated_images_enabled", False)
    client = TestClient(app)
    body = {"client_event_id": str(uuid.uuid4()), "capability": "image", "prompt": "Background"}
    assert client.post(f"/plan-items/{uuid.uuid4()}/generated-media", json=body).status_code == 404
    app.dependency_overrides[is_native_client] = lambda: False
    assert client.post(f"/plan-items/{uuid.uuid4()}/generated-media", json=body).status_code == 404


def test_request_rejects_unbounded_or_client_selected_model():
    import pytest
    from pydantic import ValidationError

    from app.services.generated_media import GeneratedMediaRequest

    base = {"client_event_id": str(uuid.uuid4()), "capability": "image", "prompt": "Background"}
    for extra in (
        {"model": "expensive"},
        {"n": 100},
        {"prompt": "x" * 1501},
        {"source_url": "https://foreign.test/image"},
    ):
        with pytest.raises(ValidationError):
            GeneratedMediaRequest(**{**base, **extra})


def test_capability_inputs_cannot_mix_source_and_script_contracts():
    import pytest
    from pydantic import ValidationError

    from app.services.generated_media import GeneratedMediaRequest

    base = {"client_event_id": str(uuid.uuid4()), "prompt": "Background"}
    invalid = [
        {"capability": "image", "script_version": 1},
        {"capability": "image_edit"},
        {"capability": "narration"},
        {"capability": "narration", "source_asset_id": str(uuid.uuid4())},
    ]
    for row in invalid:
        with pytest.raises(ValidationError):
            GeneratedMediaRequest(**base, **row)
