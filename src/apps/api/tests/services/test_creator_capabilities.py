"""Tests for server-derived Main Creator Agent capabilities."""

from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, CreatorEditSnapshot
from app.services import creator_capabilities as capabilities
from app.services.creator_sessions import compile_active_plan


def _enable_guided(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "guided_edit_capability_enabled", True)
    for name in capabilities._FEATURE_SETTINGS.values():
        monkeypatch.setattr(capabilities.settings, name, True, raising=False)


def test_guided_policy_is_used_and_manifest_is_deterministic(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    kwargs = {
        "item_id": "item-1",
        "edit_format": "montage",
        "media": [{"media_id": "clip-1", "kind": "video", "duration_s": 3.0}],
        "catalog": [{"catalog_id": "song-1", "kind": "music"}],
    }
    first = capabilities.resolve_creator_manifest(**kwargs)
    second = capabilities.resolve_creator_manifest(**kwargs)

    assert first.render_program == "guided"
    assert first.capabilities[capabilities.CAPABILITY_GUIDED_STORY].available is True
    assert first.capabilities[capabilities.CAPABILITY_DRAFT_GUIDED_PROPOSAL].available is True
    assert first.context_hash == second.context_hash
    assert first.manifest_hash == second.manifest_hash


def test_audio_led_and_voiceover_items_are_native(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="talking_head",
        has_voiceover=False,
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    assert manifest.render_program == "native"
    guided = manifest.capabilities[capabilities.CAPABILITY_GUIDED_STORY]
    assert guided.available is False
    assert guided.reason_code == "native_render_required"


def test_manifest_reports_setting_and_state_reasons(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "guided_edit_capability_enabled", False)
    manifest = capabilities.resolve_creator_manifest(item_id="item-1", edit_format="montage")
    assert manifest.capabilities[capabilities.CAPABILITY_GUIDED_STORY].reason_code == (
        "disabled_by_setting"
    )
    assert manifest.capabilities[capabilities.CAPABILITY_NATIVE_RENDER].reason_code == (
        "no_native_clip"
    )
    assert manifest.capabilities[capabilities.CAPABILITY_SELECT_READY_VARIANT].reason_code == (
        "no_ready_variant"
    )

    ready = capabilities.resolve_creator_manifest(
        item_id="item-1",
        current_edit=CreatorEditSnapshot(status="ready", variant_id="variant-1"),
    )
    assert ready.capabilities[capabilities.CAPABILITY_SELECT_READY_VARIANT].available is True


def test_day_vlog_manifest_is_explicitly_unavailable_while_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "edit_format_day_vlog_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="day_vlog",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    capability = manifest.capabilities["edit_format:day_vlog"]
    assert capability.available is False
    assert capability.reason_code == "disabled_by_setting"
    assert "EDIT_FORMAT_DAY_VLOG_ENABLED" in (capability.reason or "")


def test_day_vlog_manifest_can_advertise_guided_renderer_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "edit_format_day_vlog_enabled", True)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="day_vlog",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    assert manifest.capabilities["edit_format:day_vlog"].available is True


def test_single_hero_manifest_explains_flag_and_advertises_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "edit_format_single_hero_enabled", False)
    unavailable = capabilities.resolve_creator_manifest(
        item_id="item-1", edit_format="single_hero", media=[{"media_id": "clip-1", "kind": "video"}]
    )
    capability = unavailable.capabilities["edit_format:single_hero"]
    assert capability.reason_code == "disabled_by_setting"
    assert "EDIT_FORMAT_SINGLE_HERO_ENABLED" in (capability.reason or "")

    monkeypatch.setattr(capabilities.settings, "edit_format_single_hero_enabled", True)
    available = capabilities.resolve_creator_manifest(
        item_id="item-1", edit_format="single_hero", media=[{"media_id": "clip-1", "kind": "video"}]
    )
    assert available.capabilities["edit_format:single_hero"].available is True


def test_compile_strategy_uses_only_available_commands_and_never_guided_for_voiceover(
    monkeypatch,
) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        has_voiceover=True,
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            audio_strategy="voiceover",
            render_program="guided",
            selected_media_ids=["clip-1"],
        ),
    )
    assert [command.command for command in plan.commands] == [
        "set_item_intent",
        "dispatch_render",
    ]
    assert plan.strategy.render_program == "native"


def test_compile_drops_treatments_not_advertised_by_manifest(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "sound_effects_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )
    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            optional_treatments=["sfx", "transitions"],
        ),
    )
    assert plan.strategy.optional_treatments == ["transitions"]


def test_compile_rejects_media_ids_outside_manifest(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )

    with pytest.raises(ValueError, match="must reference manifest media"):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="montage",
                render_program="native",
                selected_media_ids=["clip-from-another-item"],
            ),
        )


def test_compile_preserves_explicit_native_program(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            render_program="native",
            selected_media_ids=["clip-1"],
        ),
    )

    assert plan.strategy.render_program == "native"
    assert "draft_guided_proposal" not in [command.command for command in plan.commands]


def test_guided_compile_leaves_exact_media_choice_to_the_specialist(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "clip-2", "kind": "video"},
        ],
    )

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            render_program="guided",
            selected_media_ids=["clip-1"],
        ),
    )

    assert plan.strategy.render_program == "guided"
    assert plan.strategy.selected_media_ids == ["clip-1", "clip-2"]


def test_native_compile_excludes_pool_assets_it_cannot_render(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[
            {"media_id": "clip-1", "kind": "video"},
            {"media_id": "asset-11111111-1111-1111-1111-111111111111", "kind": "image"},
        ],
    )

    plan = capabilities.compile_strategy_to_plan(
        manifest,
        CreativeStrategy(
            edit_format="montage",
            render_program="native",
            selected_media_ids=[
                "clip-1",
                "asset-11111111-1111-1111-1111-111111111111",
            ],
        ),
    )
    assert plan.strategy.selected_media_ids == ["clip-1"]

    with pytest.raises(ValueError, match="requires at least one attached clip"):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="montage",
                render_program="native",
                selected_media_ids=["asset-11111111-1111-1111-1111-111111111111"],
            ),
        )


def test_native_manifest_requires_an_attached_clip_not_only_pool_assets(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="talking_head",
        media=[{"media_id": "asset-11111111-1111-1111-1111-111111111111", "kind": "video"}],
    )

    assert manifest.render_program == "native"
    assert manifest.capabilities[capabilities.CAPABILITY_NATIVE_RENDER].available is False
    assert manifest.capabilities[capabilities.CAPABILITY_DISPATCH_RENDER].reason_code == (
        "no_native_clip"
    )


def test_assets_only_manifest_cannot_dispatch_when_guided_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.settings, "guided_edit_capability_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "asset-11111111-1111-1111-1111-111111111111", "kind": "image"}],
    )

    assert manifest.render_program == "guided"
    assert manifest.capabilities[capabilities.CAPABILITY_GUIDED_STORY].available is False
    assert manifest.capabilities[capabilities.CAPABILITY_DISPATCH_RENDER].available is False
    assert manifest.capabilities[capabilities.CAPABILITY_DISPATCH_RENDER].reason_code == (
        "no_native_clip"
    )


def test_compile_rejects_format_whose_renderer_flag_is_off(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    monkeypatch.setattr(capabilities.settings, "subtitled_archetype_enabled", False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )

    assert manifest.capabilities["edit_format:subtitled"].available is False
    with pytest.raises(ValueError, match="unavailable"):
        capabilities.compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                edit_format="subtitled",
                audio_strategy="original_audio",
                render_program="native",
                selected_media_ids=["clip-1"],
            ),
        )


def test_session_compiles_agent_strategy_through_capability_service(monkeypatch) -> None:
    _enable_guided(monkeypatch)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": "clip-1", "kind": "video"}],
    )

    receipt = compile_active_plan(
        SimpleNamespace(active_plan=None),
        manifest=manifest,
        strategy=CreativeStrategy(
            edit_format="montage",
            selected_media_ids=["clip-1"],
        ),
        summary="A fast, personal montage.",
    )

    assert receipt["version"] == 1
    assert receipt["edit_plan"]["commands"][-1]["command"] == "dispatch_render"
