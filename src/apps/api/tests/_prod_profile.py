"""Production-shaped settings for tests of the iPhone-rendering (pilot) path.

Why this exists: two KRI-132 fixes shipped green and failed on the first real
iPhone, because every unit test resolved its manifest under config DEFAULTS and
with no narration identity, while production runs with ~70 feature flags on.
The capability that broke (`guided_voiceover_executable`) is only ever True
when `creator_prompt_fidelity_enabled` and guided editing are both on AND the
voiceover has a resolved narration identity -- i.e. always in production, never
in the old tests.

`PROD_TRUE_FLAGS` was derived on 2026-09-22 from `fly secrets list --app
nova-video`: every secret sharing `PHONE_RENDERING_ENABLED`'s digest holds the
same value, and that one is known to be `true` (pilot renders work). No secret
VALUE is recorded here, only which boolean flags are on. Re-derive when a phone
gate changes behaviour in production but not in tests.

Use `apply_prod_profile(monkeypatch)` (or the `prod_profile` fixture in
conftest.py) at the top of any test that claims "this renders on the phone in
production".
"""

from __future__ import annotations

from app.config import settings

# Lower-cased `Settings` attribute names. Names that are not `Settings` fields
# (read straight from the environment elsewhere) are skipped by
# `apply_prod_profile`, so this list can stay a faithful copy of the secret list.
PROD_TRUE_FLAGS: tuple[str, ...] = (
    "carousel_auto_author_enabled",
    "carousel_effects_enabled",
    "conformance_feedback_enabled",
    "creator_memory_enabled",
    "creator_prompt_fidelity_enabled",
    "custom_effects_enabled",
    "edit_copilot_enabled",
    "edit_director_enabled",
    "edit_format_day_vlog_enabled",
    "edit_format_single_hero_enabled",
    "edit_format_talking_head_enabled",
    "edit_transitions_enabled",
    "edit_wide_looks_enabled",
    "evolving_type_enabled",
    "generative_direct_voiceover_strict_enabled",
    "guided_edit_capability_enabled",
    "guided_edit_conversation_enabled",
    "guided_edit_direction_confirmation_enabled",
    "guided_edit_enforcement_enabled",
    "guided_story_editor_v2_enabled",
    "guided_text_face_placement_enabled",
    "landscape_output_enabled",
    "lyrics_editor_enabled",
    "lyrics_optional_enabled",
    "main_creator_agent_auto_iteration_enabled",
    "main_creator_agent_enabled",
    "main_creator_agent_execution_enabled",
    "main_creator_agent_freeform_uploads_enabled",
    "main_creator_agent_quality_review_enabled",
    "main_creator_agent_review_enabled",
    "main_creator_agent_workspace_enabled",
    "matte_depth_occluder_enabled",
    "media_overlays_enabled",
    "media_overlay_alpha_enabled",
    "motion_scenes_enabled",
    "narrated_self_narration_enabled",
    "narrated_storyboard_enabled",
    "nova_steps_feed_enabled",
    "overlay_autoapply_enabled",
    "overlay_autoplace_enabled",
    "phone_rendering_enabled",
    "pool_asset_queued_status_enabled",
    "render_autostop_enabled",
    "retake_cut_enabled",
    "reviewer_login_enabled",
    "silence_cut_enabled",
    "smart_captions_enabled",
    "smart_caption_emphasis_cues_enabled",
    "smart_caption_face_placement_enabled",
    "smart_caption_layout_balance_enabled",
    "smart_caption_section_heading_enabled",
    "smart_caption_transcript_cache_enabled",
    "smart_music_bed_enabled",
    "smart_scene_matcher_enabled",
    "sound_effects_enabled",
    "style_agent_enabled",
    "subtitled_archetype_enabled",
    "subtitled_text_lane_enabled",
    "text_appearance_enabled",
    "text_behind_subject_enabled",
    "text_motion_v2_enabled",
    "tiktok_draft_upload_enabled",
    "tiktok_performance_sync_enabled",
    "tiktok_publishing_enabled",
    "tiktok_style_vision_enabled",
    "user_style_enabled",
    "visual_blocks_enabled",
    "visual_block_autoplan_enabled",
)

# `PHONE_RENDER_VERIFIED_FEATURES` as read from the production api machine on
# 2026-09-22 (a capability-name list, not a secret).
PROD_VERIFIED_FEATURES: tuple[str, ...] = (
    "alphaOverlay",
    "animatedText",
    "audioDucking",
    "audioMix",
    "authoredText",
    "basicComposition",
    "cameraEffects",
    "captions",
    "carouselEffects",
    "clipTransitions",
    "crossfade",
    "customEffects",
    "editorMedia",
    "goldenHourLook",
    "hdr",
    "hevcDecode",
    "local1080Export",
    "mediaCards",
    "motionPresets",
    "motionScenes",
    "musicBed",
    "narrationAudio",
    "positionedText",
    "semanticCamera",
    "slidePosts",
    "soundEffects",
    "stillImages",
    "variableSpeed",
    "visualBlocks",
    "visualVideos",
)

# A voiceover as production sees it: the upload has been probed, so the
# manifest carries a narration identity. Tests that omit this never exercise
# `guided_voiceover_executable`.
PROD_NARRATION_IDENTITY: dict[str, object] = {
    "gcs_path": "users/creator/creation-threads/thread/ios-voiceover.m4a",
    "generation": "1758493638000000",
    "duration_s": 48.0,
}


def apply_prod_profile(monkeypatch) -> list[str]:
    """Turn on every flag production has on; return the names actually applied."""

    applied: list[str] = []
    for name in PROD_TRUE_FLAGS:
        if hasattr(settings, name):
            monkeypatch.setattr(settings, name, True)
            applied.append(name)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    return applied
