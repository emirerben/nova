"""Regression coverage for `compose_slide_post_draft` (KRI-33).

Locks the compose stage itself — separate from the render/build pipeline
(`tests/tasks/test_slide_post_render.py`) and the raw agent's parse-time
safety contract (`tests/agents/test_slide_post_composer.py`) — for BOTH
platform profiles, using each profile's actually-supported source media
(images for `tiktok_photo`, video for `instagram_carousel`; see
`app/pipeline/slide_post/profiles.py`). The compose step is deliberately
best-effort (never raises), so these drive the fallback path directly rather
than mocking a live model response.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.slide_post_compose import compose_slide_post_draft


def _asset(kind: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        kind=kind,
        analysis=None,
        user_context=None,
    )


def _item() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), theme="", idea="", notes="")


@pytest.fixture(autouse=True)
def _agent_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the best-effort fallback (pool order / first slide as cover /
    no caption) without a live model call — mirrors how `slide_post_compose`
    already treats any agent failure as non-fatal."""

    def _raise(*_args, **_kwargs):
        raise RuntimeError("model client unavailable in tests")

    monkeypatch.setattr("app.agents._model_client.default_client", _raise)


@pytest.mark.asyncio
async def test_compose_tiktok_photo_from_supported_images() -> None:
    assets = [_asset("image") for _ in range(4)]
    draft = await compose_slide_post_draft(
        item=_item(),
        assets=assets,
        platform_profile="tiktok_photo",
        previous_version=0,
    )
    assert draft.platform_profile == "tiktok_photo"
    assert [s.asset_id for s in draft.slides] == [a.id for a in assets]
    assert all(s.kind == "image" for s in draft.slides)
    assert draft.cover_index == 0
    assert draft.version == 1
    assert draft.user_edited is False


@pytest.mark.asyncio
async def test_compose_instagram_carousel_from_supported_videos() -> None:
    assets = [_asset("video") for _ in range(4)]
    draft = await compose_slide_post_draft(
        item=_item(),
        assets=assets,
        platform_profile="instagram_carousel",
        previous_version=0,
    )
    assert draft.platform_profile == "instagram_carousel"
    assert [s.asset_id for s in draft.slides] == [a.id for a in assets]
    assert all(s.kind == "video" for s in draft.slides)
    assert draft.cover_index == 0
    assert draft.version == 1
    assert draft.user_edited is False


@pytest.mark.asyncio
async def test_compose_bumps_version_on_retry_without_duplicating_slides() -> None:
    """Retries after a first compose must stay idempotent in shape: same
    asset set, ordered, with version monotonically advancing — never
    duplicate or drop a slide."""
    assets = [_asset("video") for _ in range(4)]
    first = await compose_slide_post_draft(
        item=_item(),
        assets=assets,
        platform_profile="instagram_carousel",
        previous_version=0,
    )
    second = await compose_slide_post_draft(
        item=_item(),
        assets=assets,
        platform_profile="instagram_carousel",
        previous_version=first.version,
    )
    assert second.version == first.version + 1
    assert len(second.slides) == len(assets)
    assert {s.asset_id for s in second.slides} == {a.id for a in assets}
