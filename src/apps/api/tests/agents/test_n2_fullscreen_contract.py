"""N2 planner-only contract for full-screen Voiceover reaction beats."""

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.reaction_beats import ReactionBeat
from app.services import creator_capabilities as capabilities
from app.services.phone_rollout import (
    PHONE_SUBTITLED_OVERLAY_FEATURES,
    PHONE_SUBTITLED_SFX_FEATURES,
    PHONE_VOICEOVER_EDITOR_FEATURES,
)


def _voiceover_manifest(monkeypatch):
    monkeypatch.setattr(capabilities.settings, "narrated_archetype_enabled", True, raising=False)
    monkeypatch.setattr(capabilities.settings, "phone_narrated_rendering_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_narration_rendering_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_voiceover_editor_lanes_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_editor_lanes_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_reaction_beats_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_voiceover_reaction_beats_enabled", True)
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_fullscreen_overlays_enabled", True)
    monkeypatch.setattr(capabilities.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(capabilities.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(
        capabilities.settings,
        "phone_render_verified_features",
        list(
            set(PHONE_SUBTITLED_OVERLAY_FEATURES)
            | set(PHONE_SUBTITLED_SFX_FEATURES)
            | set(PHONE_VOICEOVER_EDITOR_FEATURES)
            | {"narrationAudio"}
        ),
    )
    return capabilities.resolve_creator_manifest(
        item_id="item-n2",
        edit_format="narrated_planned",
        has_voiceover=True,
        narration={
            "gcs_path": "users/u/voice.m4a",
            "generation": "123",
            "duration_s": 20,
        },
        phone_source_media_ids=["phone-river", "phone-deniz"],
        phone_rendering_allowed=True,
        runs_guided_voiceover=False,
        media=[
            {"media_id": "phone-river", "kind": "video", "label": "River clip"},
            {"media_id": "phone-deniz", "kind": "video", "label": "Deniz at laptop"},
            {"media_id": "asset-river-photo", "kind": "image", "label": "River photo"},
            {"media_id": "asset-river-video", "kind": "video", "label": "River Visual"},
        ],
    )


def _strategy(*, selected_media_ids=None, **beat):
    return CreativeStrategy(
        edit_format="narrated_planned",
        audio_strategy="voiceover",
        render_program="native",
        selected_media_ids=selected_media_ids or ["phone-river", "phone-deniz"],
        reaction_beats=[
            {
                "beat_id": "river",
                "trigger": "river",
                "visual_id": "River photo",
                "display_mode": "fullscreen",
                "following_clip_id": "River clip",
                **beat,
            }
        ],
    )


def test_voiceover_fullscreen_follow_is_canonical_and_native_only(monkeypatch):
    manifest = _voiceover_manifest(monkeypatch)
    assert manifest.capabilities[capabilities.CAPABILITY_MEDIA_OVERLAY_FULLSCREEN].available

    plan = capabilities.compile_strategy_to_plan(manifest, _strategy())
    beat = plan.strategy.reaction_beats[0]
    assert beat.display_mode == "fullscreen"
    assert beat.following_clip_id == "phone-river"


def test_voiceover_all_scope_resolves_follow_against_every_own_video(monkeypatch):
    manifest = _voiceover_manifest(monkeypatch)
    strategy = _strategy(selected_media_ids=[]).model_copy(update={"media_scope": "all"})

    plan = capabilities.compile_strategy_to_plan(manifest, strategy)

    assert plan.strategy.selected_media_ids == ["phone-river", "phone-deniz"]
    assert plan.strategy.reaction_beats[0].following_clip_id == "phone-river"


def test_voiceover_all_scope_still_rejects_visuals_video_follow(monkeypatch):
    manifest = _voiceover_manifest(monkeypatch)
    strategy = _strategy(
        selected_media_ids=[], following_clip_id="asset-river-video"
    ).model_copy(update={"media_scope": "all"})

    plan = capabilities.compile_strategy_to_plan(manifest, strategy)

    assert plan.strategy.reaction_beats is None
    assert any("Couldn't find the selected original" in notice for notice in plan.notices)


def test_voiceover_follow_rejects_unselected_foreign_and_nonvideo_targets(monkeypatch):
    manifest = _voiceover_manifest(monkeypatch)
    cases = (
        ("phone-deniz", ["phone-river"], "not selected"),
        (
            "asset-river-video",
            ["phone-river", "phone-deniz"],
            "Couldn't find the selected original",
        ),
        (
            "asset-river-photo",
            ["phone-river", "phone-deniz"],
            "Couldn't find the selected original",
        ),
        ("missing", ["phone-river", "phone-deniz"], "Couldn't find the selected original"),
    )
    for following_clip_id, selected, notice in cases:
        strategy = _strategy(following_clip_id=following_clip_id, selected_media_ids=selected)
        plan = capabilities.compile_strategy_to_plan(manifest, strategy)
        assert plan.strategy.reaction_beats is None
        assert any(notice in item for item in plan.notices)


def test_follow_needs_first_fullscreen_visual_and_explicit_pip_stays_pip(monkeypatch):
    manifest = _voiceover_manifest(monkeypatch)
    pip = capabilities.compile_strategy_to_plan(
        manifest, _strategy(display_mode="pip", following_clip_id=None)
    ).strategy.reaction_beats[0]
    assert pip.display_mode == "pip"
    assert pip.following_clip_id is None

    every = capabilities.compile_strategy_to_plan(manifest, _strategy(occurrence="every"))
    assert every.strategy.reaction_beats is None


def test_fullscreen_sequence_is_native_voiceover_only(monkeypatch):
    manifest = _voiceover_manifest(monkeypatch)
    non_native = _strategy().model_copy(update={"render_program": "guided"})
    plan = capabilities.compile_strategy_to_plan(manifest, non_native)
    assert plan.strategy.reaction_beats is None
    assert any("photo-then-video sequence" in notice for notice in plan.notices)


def test_default_new_reaction_fields_are_not_serialized():
    beat = ReactionBeat(beat_id="legacy", trigger="river", visual_id="asset-river-photo")
    dumped = beat.model_dump(mode="json")
    assert "display_mode" not in dumped
    assert "following_clip_id" not in dumped
