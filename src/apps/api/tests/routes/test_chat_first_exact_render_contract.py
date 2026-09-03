"""Adversarial contract tests for chat-first exact render intent.

These tests deliberately sit at the boundary between the chat API and the
normal content-plan Job builder.  A Creator strategy is an explanation of a
direction unless its exact fields are typed and consumed by the worker.  The
exact request fixture below therefore requires the bounded title/font/colour
fields to survive on the minted Job alongside the strategy metadata.

The exact-request test is expected to fail on the pre-fix implementation.  It
is intentionally kept separate from the broad route tests so the failure is a
useful, one-command signal while the production contract is being implemented.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.requests import Request

from app.agents._schemas.creator_agent import CreativeStrategy
from app.models import Job
from app.routes.creation_threads import AttachBody, MediaInput, attach_media
from app.services.creator_capabilities import compile_strategy_to_plan, resolve_creator_manifest
from app.services.generative_jobs import (
    CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
    build_generative_job,
)

USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
THREAD_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ITEM_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _owned_path(media_id: str) -> str:
    return f"users/{USER_ID}/creation-threads/{THREAD_ID}/{media_id}.mp4"


def _exact_request() -> str:
    return (
        "Create an edit of the best moments. Add the title ‘Emir Olympics’ using Rascal "
        "font. Make the text yellow. Add a small text of the name of the sport being "
        "played on the bottom right"
    )


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _exact_strategy_payload() -> dict:
    """The user-authored constraints from the production repro, verbatim."""

    return {
        "direction": "fast_montage",
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "render_program": "guided",
        "selected_media_ids": ["clip-1", "clip-2", "clip-3", "clip-4"],
        # These are typed/executable fields, not prose strategy metadata.  The
        # worker validates them again before projecting them into its title
        # overlay/style inputs.  “Best moments” is the normal montage source
        # selection policy and is represented by the bounded direction/goal.
        "opening_title": "Emir Olympics",
        "font_family": "Rascal",
        "text_color": "yellow",
        "context_label": {
            "kind": "sport",
            "source": "clip_metadata",
            "placement": "bottom_right",
            "size": "small",
            "per_clip": True,
        },
        "story_structure": ["Use the best moments"],
    }


def test_exact_chat_request_is_promoted_to_typed_creator_fields() -> None:
    """The natural-language repro cannot degrade to advisory ``intro_hook``."""

    from app.routes.creator_agent import _apply_explicit_render_intent

    parsed = _apply_explicit_render_intent(CreativeStrategy(), _exact_request())

    assert parsed.opening_title == "Emir Olympics"
    assert parsed.font_family == "Rascal"
    assert parsed.text_color == "#FFD24A"
    assert parsed.context_label is not None
    assert parsed.context_label.model_dump(mode="json") == {
        "kind": "sport",
        "source": "clip_metadata",
        "placement": "bottom_right",
        "size": "small",
        "per_clip": True,
    }


def test_context_label_is_not_invented_from_unrelated_request() -> None:
    from app.routes.creator_agent import _apply_explicit_render_intent

    parsed = _apply_explicit_render_intent(
        CreativeStrategy(context_label={"kind": "sport"}),
        "Use the strongest moments and keep the energy up.",
    )

    assert parsed.context_label is None


def test_production_repro_exact_copy_overrides_model_authored_style() -> None:
    """Only the creator's explicit words may become burned title pixels."""

    from app.routes.creator_agent import _apply_explicit_render_intent

    parsed = _apply_explicit_render_intent(
        CreativeStrategy(
            opening_title="A plausible hallucinated title",
            font_family="Inter",
            text_color="#FFFFFF",
        ),
        (
            "Create an edit of the best moments. add the title 'Emir Olympics' "
            "using rascal font. Make the text yellow."
        ),
    )

    assert parsed.opening_title == "Emir Olympics"
    assert parsed.font_family == "Rascal"
    assert parsed.text_color == "#FFD24A"


def test_quote_before_title_noun_is_promoted_to_exact_copy() -> None:
    """The later photo-heavy repro says ``'copy' title``, not ``title 'copy'``."""

    from app.routes.creator_agent import _apply_explicit_render_intent

    parsed = _apply_explicit_render_intent(
        CreativeStrategy(opening_title="Invented words"),
        (
            "Amongst the videos, add groups of photos that transition quickly. "
            "In the intro, add 'Emir Olympics' title. Group content by sport."
        ),
    )

    assert parsed.opening_title == "Emir Olympics"


def test_subsecond_photo_groups_promote_existing_mixed_media_contract() -> None:
    """A numeric fast-photo request must include pool images in the guided edit."""

    from app.routes.creator_agent import _apply_explicit_render_intent

    parsed = _apply_explicit_render_intent(
        CreativeStrategy(),
        (
            "Amongst the videos, add groups of photos that transition in 0.1 seconds. "
            "The photo sections should feel like a video due to this fast change."
        ),
    )

    assert parsed.direction == "fast_montage"
    assert parsed.mixed_media_timing is not None
    assert parsed.mixed_media_timing.model_dump(mode="json") == {
        "image_hold": "very_fast",
        "image_hold_s": 0.1,
        "video_hold": "longer",
        "boundary_style": "cut",
        "image_grouping": "runs",
    }


def test_latest_chat_corrections_become_executable_timing_and_photo_layout() -> None:
    """The production follow-up must not survive only in assistant prose."""

    from app.routes.creator_agent import _apply_explicit_render_intent

    parsed = _apply_explicit_render_intent(
        CreativeStrategy(),
        (
            "Use photos at 0.1 seconds and let the videos hold longer. "
            "Make the images stay 0.2 seconds instead of 0.1. "
            "Add text to indicate the sport. Don't make the images fill the screen as well."
        ),
    )

    assert parsed.mixed_media_timing is not None
    assert parsed.mixed_media_timing.image_hold_s == pytest.approx(0.2)
    assert parsed.image_layout == "supporting_card"
    assert parsed.context_label is not None


def test_subsecond_photo_groups_compile_every_pool_image_into_guided_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact repro must not leave images in an advisory-only visual pool."""

    from app.routes.creator_agent import _apply_explicit_render_intent
    from app.services import creator_capabilities as capabilities

    monkeypatch.setattr(capabilities.settings, "guided_edit_capability_enabled", True)
    for setting_name in capabilities._FEATURE_SETTINGS.values():
        monkeypatch.setattr(capabilities.settings, setting_name, True, raising=False)
    manifest = resolve_creator_manifest(
        item_id="item-photo-repro",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-photo-1", "kind": "image"},
            {"media_id": "asset-photo-2", "kind": "image"},
        ],
    )
    strategy = _apply_explicit_render_intent(
        CreativeStrategy(
            edit_format="montage",
            render_program="native",
            selected_media_ids=["clip-1"],
        ),
        (
            "Amongst the videos, add groups of photos that transition in 0.1 seconds. "
            "In the intro, add 'Emir Olympics' title."
        ),
    )

    plan = compile_strategy_to_plan(manifest, strategy)

    assert plan.strategy.render_program == "guided"
    assert plan.strategy.selected_media_ids == ["clip-1", "asset-photo-1", "asset-photo-2"]
    assert "draft_guided_proposal" in [command.command for command in plan.commands]


def test_model_cannot_invent_exact_render_fields_without_creator_wording() -> None:
    from app.routes.creator_agent import _apply_explicit_render_intent

    parsed = _apply_explicit_render_intent(
        CreativeStrategy(
            opening_title="Invented title",
            font_family="Rascal",
            text_color="yellow",
        ),
        "Use the strongest moments and keep the energy up.",
    )

    assert parsed.opening_title is None
    assert parsed.font_family is None
    assert parsed.text_color is None


@pytest.mark.asyncio
async def test_chat_first_can_attach_four_owned_clips_before_exact_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repro starts with four direct uploads, all in one owned namespace."""

    user = SimpleNamespace(id=USER_ID)
    thread = SimpleNamespace(
        id=THREAD_ID,
        creator_id=USER_ID,
        status="active",
        revision=0,
        active_job_id=None,
        active_plan_item_id=ITEM_ID,
        active_creator_agent_session_id=None,
        state={"edit_format": "montage", "media": [], "media_count": 0},
    )
    item = SimpleNamespace(
        clip_gcs_paths=[],
        clip_assignments=[],
        voiceover_gcs_path=None,
        audio_mode="kria",
    )
    db = Mock()
    db.get = AsyncMock(return_value=item)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    import app.routes.creation_threads as routes

    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_append", AsyncMock())
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _path: SimpleNamespace(size=100, content_type="video/mp4"),
    )

    for index in range(1, 5):
        media_id = f"clip-{index}.mp4"
        await attach_media(
            _request(),
            str(THREAD_ID),
            AttachBody(
                media=[MediaInput(media_id=media_id, kind="video", filename=f"{media_id}")],
                client_event_id=f"attach-{index}",
                expected_revision=thread.revision,
            ),
            user,
            db,
        )

    assert [row["media_id"] for row in item.clip_assignments] == [
        "clip-1.mp4",
        "clip-2.mp4",
        "clip-3.mp4",
        "clip-4.mp4",
    ]
    assert item.clip_gcs_paths == [_owned_path(f"clip-{index}") for index in range(1, 5)]
    assert thread.state["media_count"] == 4

    # Keep the exact natural-language request at the API boundary as well as
    # in the Job assertion below.  The Planner may normalize it, but it must
    # not silently drop a clause before the typed contract is compiled.
    planner = AsyncMock(return_value=thread)
    monkeypatch.setattr(routes, "_agent_message", planner)
    await routes.message_thread(
        _request(),
        str(THREAD_ID),
        routes.MessageBody(
            message=_exact_request(),
            client_event_id="exact-request",
            expected_revision=thread.revision,
        ),
        user,
        db,
    )
    planner.assert_awaited_once()
    assert planner.await_args.args[2].message == _exact_request()


def test_exact_chat_request_mints_normal_content_plan_job_with_executable_constraints() -> None:
    """Exact user constraints survive confirmation into the normal Job shape.

    This is the regression that fails before the fix: ``build_generative_job``
    currently validates/stashes only advisory strategy fields, and the
    renderer receives no typed title/font/colour contract.
    """

    strategy = CreativeStrategy.model_validate(_exact_strategy_payload())
    job = build_generative_job(
        user_id=USER_ID,
        clip_paths=[_owned_path(f"clip-{index}") for index in range(1, 5)],
        mode="content_plan",
        content_plan_item_id=ITEM_ID,
        content_plan_ownership_epoch=0,
        variant_policy=CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
        creator_strategy=strategy.model_dump(mode="json", exclude_none=True),
    )

    assert isinstance(job, Job)
    assert job.mode == "content_plan"
    assert job.content_plan_item_id == ITEM_ID
    assert job.all_candidates["clip_paths"] == [
        _owned_path(f"clip-{index}") for index in range(1, 5)
    ]
    assert job.all_candidates["creator_strategy"] == strategy.model_dump(
        mode="json", exclude_none=True
    )

    # The typed strategy is itself the worker-facing contract.  Checking every
    # exact field prevents a partial fix that merely copies the prose request
    # or only persists the title.  ``yellow`` is normalized by the schema to
    # the canonical hex value before it reaches Job JSONB.
    persisted = job.all_candidates["creator_strategy"]
    assert persisted["opening_title"] == "Emir Olympics"
    assert persisted["font_family"] == "Rascal"
    assert persisted["text_color"] == "#FFD24A"
    assert persisted["context_label"] == {
        "kind": "sport",
        "source": "clip_metadata",
        "placement": "bottom_right",
        "size": "small",
        "per_clip": True,
    }
    assert persisted["story_structure"] == ["Use the best moments"]


def test_creator_capability_manifest_cannot_promise_unadvertised_exact_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A planner may only compile commands for capabilities it was shown."""

    from app.services import creator_capabilities as capabilities

    monkeypatch.setattr(capabilities.settings, "guided_edit_capability_enabled", True)
    for name in capabilities._FEATURE_SETTINGS.values():
        monkeypatch.setattr(capabilities.settings, name, True, raising=False)
    # Exact style/title fields are intentionally validated by the typed
    # strategy rather than implied by free-form ``creator_strategy`` metadata.
    manifest = resolve_creator_manifest(
        item_id="item-exact",
        edit_format="montage",
        media=[{"media_id": f"clip-{i}", "kind": "video"} for i in range(1, 5)],
    )
    strategy = CreativeStrategy.model_validate(_exact_strategy_payload())
    plan = compile_strategy_to_plan(manifest, strategy)

    assert plan.strategy.opening_title == "Emir Olympics"
    assert plan.strategy.font_family == "Rascal"
    assert plan.strategy.text_color == "#FFD24A"
    assert plan.strategy.context_label is not None
    assert plan.strategy.context_label.placement == "bottom_right"
    assert plan.strategy.story_structure == ["Use the best moments"]
    # No unadvertised command family may be synthesized from exact fields.
    assert {command.command for command in plan.commands} <= {
        "set_item_intent",
        "draft_guided_proposal",
        "dispatch_render",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [("font_family", "NotARegisteredFont"), ("text_color", "chartreuse")],
)
def test_creator_exact_style_promise_rejects_unsupported_values(field: str, value: str) -> None:
    """The assistant cannot claim an exact style the renderer cannot honor."""

    payload = _exact_strategy_payload()
    payload[field] = value
    with pytest.raises(ValueError):
        CreativeStrategy.model_validate(payload)


def test_stale_content_plan_worker_cannot_publish_over_new_generation(monkeypatch) -> None:
    """A late worker for a prior generation is reconciled as stale."""

    from app.tasks.generative_build import _stale_render_discarded

    class FakeJob:
        mode = "content_plan"
        status = "variants_ready"
        all_candidates = {"clip_paths": [_owned_path("clip-1")]}
        assembly_plan = {
            "creator_generation_id": "creator-new",
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_generation_id": "render-new",
                    "render_status": "rendering",
                }
            ],
        }

    job = FakeJob()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, model, _job_id, **_kwargs):
            # The guard only needs the Job row; avoiding a DB keeps this test a
            # deterministic worker-boundary contract test.
            return job

    monkeypatch.setattr("app.tasks.generative_build._sync_session", lambda: Session())

    assert (
        _stale_render_discarded(
            str(uuid.uuid4()),
            "original_text",
            "render-old",
            outcome="content_plan_terminal",
        )
        is True
    )
    assert job.assembly_plan["creator_generation_id"] == "creator-new"
    assert job.assembly_plan["variants"][0]["render_generation_id"] == "render-new"


def test_legacy_content_plan_job_has_no_creator_exact_contract() -> None:
    """Pre-chat PlanItems remain byte-compatible when no Creator plan exists."""

    job = build_generative_job(
        user_id=USER_ID,
        clip_paths=["users/legacy-plan/clip.mp4"],
        mode="content_plan",
        content_plan_item_id=ITEM_ID,
        content_plan_ownership_epoch=0,
        variant_policy=CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
    )

    assert "creator_strategy" not in job.all_candidates
    assert "render_intent" not in job.all_candidates
    assert job.all_candidates["clip_paths"] == ["users/legacy-plan/clip.mp4"]
