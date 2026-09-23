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

from app.schemas.slide_post import (
    SlideEdits,
    SlidePostDraft,
    SlideRef,
    TextOverlay,
    bump_slide_post_version,
)
from app.services.slide_post_compose import compose_slide_post_draft, propose_slide_post_draft


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


@pytest.mark.asyncio
async def test_proposal_fallback_discloses_failure_and_preserves_manual_slide_state() -> None:
    assets = [_asset("image") for _ in range(2)]
    current = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(
                id="stable-slide",
                asset_id=assets[0].id,
                kind="image",
                edits=SlideEdits(text=TextOverlay(content="hello", position="top")),
            )
        ],
        version=4,
        user_edited=True,
    )
    result = await propose_slide_post_draft(
        item=_item(),
        assets=assets,
        platform_profile="tiktok_photo",
        previous_version=4,
        current_draft=current,
        instruction="Lead with the most colorful photo.",
    )
    assert result.fallback_used is True
    assert result.draft.version == 5
    assert result.draft.slides[0].id == "stable-slide"
    assert result.draft.slides[0].edits == current.slides[0].edits


@pytest.mark.asyncio
async def test_proposal_fallback_keeps_existing_caption_and_cover_asset() -> None:
    assets = [_asset("image") for _ in range(2)]
    current = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(id="custom-a", asset_id=assets[0].id, kind="image"),
            SlideRef(id="custom-cover", asset_id=assets[1].id, kind="image"),
        ],
        cover_index=1,
        caption="Keep this caption.",
        version=2,
    )
    result = await propose_slide_post_draft(
        item=_item(),
        assets=assets,
        platform_profile="tiktok_photo",
        previous_version=2,
        current_draft=current,
        instruction="Try a different order",
    )
    assert result.fallback_used is True
    assert result.draft.caption == "Keep this caption."
    assert result.draft.slides[result.draft.cover_index].asset_id == assets[1].id


@pytest.mark.asyncio
async def test_proposal_uses_agent_cover_asset_when_client_slide_id_is_custom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = [_asset("image") for _ in range(2)]
    current = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(id="custom-a", asset_id=assets[0].id, kind="image"),
            SlideRef(id="custom-b", asset_id=assets[1].id, kind="image"),
        ],
    )

    class Agent:
        def __init__(self, _client):
            pass

        def run(self, *_args, **_kwargs):
            return SimpleNamespace(
                order=[str(assets[1].id), str(assets[0].id)],
                cover_id=str(assets[0].id),
                caption="new",
                alt_text={},
            )

    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr("app.services.slide_post_compose.SlidePostComposerAgent", Agent)
    result = await propose_slide_post_draft(
        item=_item(),
        assets=assets,
        platform_profile="tiktok_photo",
        previous_version=1,
        current_draft=current,
    )
    assert result.fallback_used is False
    assert result.draft.slides[result.draft.cover_index].asset_id == assets[0].id


def test_bump_revalidates_client_supplied_cover_index() -> None:
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="one", asset_id=uuid.uuid4(), kind="image")],
    )
    with pytest.raises(ValueError, match="cover_index out of range"):
        bump_slide_post_version(draft, cover_index=1)
