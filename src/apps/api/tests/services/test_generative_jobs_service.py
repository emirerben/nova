"""Unit tests for the shared build_generative_job service (plan T4, no DB).

Locks the single-source-of-truth Job shape + clip-path validation that both the
public generative route and the content-plan per-item task depend on.
"""

from __future__ import annotations

import uuid

import pytest

from app.kria.media_sources import OriginalMediaDescriptor
from app.schemas.montage_preset import coerce_montage_preset
from app.services.generative_jobs import (
    CONTENT_PLAN_ORIGINAL_VARIANT_POLICY,
    CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
    build_generative_job,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding


def test_build_default_generative_job() -> None:
    uid = uuid.uuid4()
    job = build_generative_job(user_id=uid, clip_paths=["slot-uploads/a.mp4"])
    assert job.user_id == uid
    assert job.job_type == "generative"
    assert job.mode == "generative"
    assert job.status == "queued"
    assert job.raw_storage_path == "slot-uploads/a.mp4"
    assert job.all_candidates["clip_paths"] == ["slot-uploads/a.mp4"]
    assert job.all_candidates["language"] == "en"
    assert job.content_plan_item_id is None


def test_content_plan_mode_sets_reverse_link() -> None:
    item_id = uuid.uuid4()
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        mode="content_plan",
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=3,
        variant_policy=CONTENT_PLAN_PRIMARY_VARIANT_POLICY,
    )
    assert job.mode == "content_plan"
    assert job.content_plan_item_id == item_id
    assert job.content_plan_ownership_epoch == 3
    assert job.all_candidates["variant_policy"] == CONTENT_PLAN_PRIMARY_VARIANT_POLICY


def test_public_generative_job_omits_variant_policy() -> None:
    job = build_generative_job(user_id=uuid.uuid4(), clip_paths=["slot-uploads/a.mp4"])
    assert "variant_policy" not in job.all_candidates


def test_confirmed_creator_strategy_is_schema_bounded_and_persisted() -> None:
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        creator_strategy={
            "direction": "native",
            "edit_format": "talking_head",
            "audio_strategy": "original_audio",
            "pacing": "fast",
            "render_program": "native",
            "selected_media_ids": ["clip-1"],
            "intro_hook": "The one thing I learned",
        },
    )
    assert job.all_candidates["creator_strategy"]["intro_hook"] == ("The one thing I learned")
    assert job.all_candidates["creator_render_contract_version"] == "2026-09-03-v1"

    with pytest.raises(ValueError):
        build_generative_job(
            user_id=uuid.uuid4(),
            clip_paths=["users/u/plan/i/a.mp4"],
            creator_strategy={"intro_hook": "hello", "gcs_path": "users/private.mp4"},
        )


@pytest.mark.parametrize("repeat", [200, 2000])
def test_creator_request_is_bounded_and_persisted_for_retries(repeat: int) -> None:
    request = "  Match the voiceover to the clips. " + ("Add score text. " * repeat)
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        mode="content_plan",
        content_plan_item_id=uuid.uuid4(),
        content_plan_ownership_epoch=0,
        creator_request=request,
    )

    assert len(job.all_candidates["creator_request"]) == min(len(request.strip()), 12000)
    assert job.all_candidates["creator_request"] == request.strip()[:12000]


def test_content_plan_original_audio_policy_is_persisted() -> None:
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        mode="content_plan",
        content_plan_item_id=uuid.uuid4(),
        content_plan_ownership_epoch=0,
        variant_policy=CONTENT_PLAN_ORIGINAL_VARIANT_POLICY,
    )
    assert job.all_candidates["variant_policy"] == CONTENT_PLAN_ORIGINAL_VARIANT_POLICY


def test_public_job_omits_persona_key() -> None:
    # No persona args → the key must be ABSENT (not an empty dict) so the public
    # job's all_candidates shape is byte-identical to pre-persona behavior.
    job = build_generative_job(user_id=uuid.uuid4(), clip_paths=["slot-uploads/a.mp4"])
    assert "persona" not in job.all_candidates


def test_persona_stashed_in_all_candidates() -> None:
    item_id = uuid.uuid4()
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        mode="content_plan",
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        persona_tone="no-excuses gym motivation",
        persona_pillars=["morning routines", "discipline"],
        item_theme="first 5am workout",
        item_idea="film the dark early start",
    )
    persona = job.all_candidates["persona"]
    assert persona["tone"] == "no-excuses gym motivation"
    assert persona["content_pillars"] == ["morning routines", "discipline"]
    assert persona["theme"] == "first 5am workout"
    assert persona["idea"] == "film the dark early start"


def test_persona_partial_fields_still_stashed() -> None:
    # Theme-only is enough to warrant the key (the hook can still cohere to it).
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        item_theme="cheap rooftop nobody posts about",
    )
    assert job.all_candidates["persona"]["theme"] == "cheap rooftop nobody posts about"
    assert job.all_candidates["persona"]["content_pillars"] == []


def test_persona_all_empty_omits_key() -> None:
    # Empty strings / empty list collapse to "no persona" → key omitted.
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        persona_tone="   ",
        persona_pillars=["", "  "],
        item_theme="",
        item_idea="",
    )
    assert "persona" not in job.all_candidates


def test_persona_pillars_capped_and_trimmed() -> None:
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        persona_pillars=[f"  pillar{i}  " for i in range(20)],
    )
    pillars = job.all_candidates["persona"]["content_pillars"]
    assert len(pillars) == 8  # _MAX_PERSONA_PILLARS
    assert pillars[0] == "pillar0"  # trimmed


def test_edit_format_defaults_to_montage() -> None:
    # Public job with no declared format → montage (today's behavior), always present.
    job = build_generative_job(user_id=uuid.uuid4(), clip_paths=["slot-uploads/a.mp4"])
    assert job.all_candidates["edit_format"] == "montage"


def test_edit_format_passthrough_and_coercion() -> None:
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        edit_format="talking_head",
    )
    assert job.all_candidates["edit_format"] == "talking_head"
    # An unknown/legacy value must coerce to montage, never reach the render path raw.
    job2 = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        edit_format="cinematic-banger",
    )
    assert job2.all_candidates["edit_format"] == "montage"
    # Preserve the raw intent separately so a mixed API/worker deployment cannot
    # mistake an unknown future audio-led format for an intentional montage.
    assert job2.all_candidates["declared_edit_format"] == "cinematic_banger"


def test_day_vlog_jobs_pin_renderer_version_for_mixed_workers() -> None:
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4", "users/u/plan/i/b.mp4"],
        edit_format="day_vlog",
    )
    assert job.all_candidates["edit_format"] == "day_vlog"
    assert job.all_candidates["day_vlog_renderer_version"] == 1


def test_single_hero_jobs_pin_renderer_version_for_mixed_workers() -> None:
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4", "users/u/plan/i/b.mp4"],
        edit_format="single_hero",
    )
    assert job.all_candidates["edit_format"] == "single_hero"
    assert job.all_candidates["single_hero_renderer_version"] == 1


def test_smart_captions_context_is_pinned_only_for_subtitled_jobs() -> None:
    context = {"preset_id": "cigdem", "preset_version": "v1"}
    smart = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        edit_format="subtitled",
        smart_captions=context,
    )
    assert smart.all_candidates["smart_captions"] == {
        **context,
        "sound_design": "auto",
    }

    montage = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        edit_format="montage",
        smart_captions=context,
    )
    assert "smart_captions" not in montage.all_candidates

    invalid = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        edit_format="subtitled",
        smart_captions={"preset_id": "../../cigdem", "preset_version": "v1"},
    )
    assert "smart_captions" not in invalid.all_candidates


def test_smart_captions_shadow_context_survives_only_as_a_complete_safe_pair() -> None:
    base = {"preset_id": "cigdem", "preset_version": "v1"}
    smart = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        edit_format="subtitled",
        smart_captions={
            **base,
            "shadow_preset_id": "cigdem",
            "shadow_preset_version": "v2",
        },
    )
    assert smart.all_candidates["smart_captions"] == {
        **base,
        "shadow_preset_id": "cigdem",
        "shadow_preset_version": "v2",
        "sound_design": "auto",
    }

    for invalid_shadow in (
        {"shadow_preset_id": "cigdem"},
        {"shadow_preset_id": "../../cigdem", "shadow_preset_version": "v2"},
    ):
        job = build_generative_job(
            user_id=uuid.uuid4(),
            clip_paths=["users/u/plan/i/a.mp4"],
            edit_format="subtitled",
            smart_captions={**base, **invalid_shadow},
        )
        assert job.all_candidates["smart_captions"] == {
            **base,
            "sound_design": "auto",
        }


def test_montage_preset_omits_classic_and_stashes_collage_presets() -> None:
    assert coerce_montage_preset("polaroid_wall") == "polaroid_wall"

    classic = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        montage_preset="classic",
    )
    assert "montage_preset" not in classic.all_candidates

    for preset in ("masonry", "polaroid_wall"):
        collage = build_generative_job(
            user_id=uuid.uuid4(),
            clip_paths=["users/u/plan/i/a.mp4"],
            montage_preset=preset,
        )
        assert collage.all_candidates["montage_preset"] == preset

    unknown = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        montage_preset="collage-but-wrong",
    )
    assert "montage_preset" not in unknown.all_candidates


def test_users_prefix_is_allowlisted() -> None:
    # The whole point of the Phase 5 allowlist change — plan uploads must pass.
    job = build_generative_job(user_id=uuid.uuid4(), clip_paths=["users/u/plan/i/clip.mp4"])
    assert job.raw_storage_path.startswith("users/")


def test_owned_direct_generative_prefix_is_allowlisted() -> None:
    user_id = uuid.uuid4()
    path = f"users/{user_id}/generative/abc123def456/clip.mov"
    job = build_generative_job(user_id=user_id, clip_paths=[path])
    assert job.raw_storage_path == path


def test_legacy_owned_direct_generative_prefix_remains_readable() -> None:
    user_id = uuid.uuid4()
    path = f"dev-user/{user_id}/generative/abc123def456/clip.mov"
    job = build_generative_job(user_id=user_id, clip_paths=[path])
    assert job.raw_storage_path == path


def test_foreign_direct_generative_prefix_is_rejected() -> None:
    with pytest.raises(ValueError, match="owner mismatch"):
        build_generative_job(
            user_id=uuid.uuid4(),
            clip_paths=[f"users/{uuid.uuid4()}/generative/abc123def456/clip.mov"],
        )


def test_rejects_unallowlisted_prefix() -> None:
    with pytest.raises(ValueError):
        build_generative_job(user_id=uuid.uuid4(), clip_paths=["secret-bucket/key.mp4"])


def test_rejects_empty_clip_list() -> None:
    with pytest.raises(ValueError):
        build_generative_job(user_id=uuid.uuid4(), clip_paths=[])


def test_rejects_path_traversal() -> None:
    with pytest.raises(ValueError):
        build_generative_job(user_id=uuid.uuid4(), clip_paths=["users/../../etc/passwd"])


def test_content_plan_accepts_owner_scoped_creation_thread_voiceover() -> None:
    user_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    voiceover_path = f"users/{user_id}/creation-threads/{thread_id}/voice.webm"

    job = build_generative_job(
        user_id=user_id,
        clip_paths=[f"users/{user_id}/creation-threads/{thread_id}/clip.mp4"],
        mode="content_plan",
        content_plan_item_id=uuid.uuid4(),
        content_plan_ownership_epoch=0,
        voiceover_gcs_path=voiceover_path,
    )

    assert job.all_candidates["voiceover_gcs_path"] == voiceover_path


@pytest.mark.parametrize(
    "voiceover_path",
    [
        "users/{other_user}/creation-threads/thread/voice.webm",
        "users/{user_id}/creation-threads/../voice.webm",
        "/users/{user_id}/creation-threads/thread/voice.webm",
    ],
)
def test_content_plan_rejects_unowned_or_unsafe_creation_thread_voiceover(
    voiceover_path: str,
) -> None:
    user_id = uuid.uuid4()
    resolved_path = voiceover_path.format(user_id=user_id, other_user=uuid.uuid4())

    with pytest.raises(ValueError):
        build_generative_job(
            user_id=user_id,
            clip_paths=[f"users/{user_id}/plan/item/clip.mp4"],
            mode="content_plan",
            content_plan_item_id=uuid.uuid4(),
            content_plan_ownership_epoch=0,
            voiceover_gcs_path=resolved_path,
        )


def test_public_job_rejects_creation_thread_voiceover() -> None:
    user_id = uuid.uuid4()
    voiceover_path = f"users/{user_id}/creation-threads/thread/voice.webm"

    with pytest.raises(ValueError):
        build_generative_job(
            user_id=user_id,
            clip_paths=["slot-uploads/clip.mp4"],
            voiceover_gcs_path=voiceover_path,
        )


def _phone_binding(user_id: uuid.UUID, thread_id: uuid.UUID) -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id="phone-source",
        proxy_path=f"users/{user_id}/creation-threads/{thread_id}/analysis-proxy-source.mp4",
        generation="17",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=12,
            width=1920,
            height=1080,
            has_audio=True,
        ),
    )


def _phone_voiceover_args(user_id: uuid.UUID, *, edit_format: str = "montage") -> dict:
    thread_id = uuid.uuid4()
    binding = _phone_binding(user_id, thread_id)
    return {
        "user_id": user_id,
        "clip_paths": [binding.proxy_path],
        "mode": "content_plan",
        "content_plan_item_id": uuid.uuid4(),
        "content_plan_ownership_epoch": 0,
        "edit_format": edit_format,
        "voiceover_gcs_path": f"users/{user_id}/creation-threads/{thread_id}/voice.m4a",
        "phone_sources": (binding,),
    }


@pytest.mark.parametrize(
    ("edit_format", "creator_strategy"),
    [
        ("montage", None),
        ("narrated_planned", None),
        (
            "narrated_planned",
            {
                "execution_contract": "guided_voiceover_v1",
                "render_program": "guided",
                "audio_strategy": "voiceover",
            },
        ),
    ],
    ids=["montage_voiceover", "narrated_voiceover", "guided_voiceover"],
)
def test_phone_voiceover_constructor_accepts_live_supported_lanes(
    monkeypatch: pytest.MonkeyPatch, edit_format: str, creator_strategy: dict | None
) -> None:
    """The constructor must agree with dispatch on every live narration lane."""
    from app.config import settings

    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_narration_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_guided_narration_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_narrated_rendering_enabled", True)
    monkeypatch.setattr(settings, "narrated_archetype_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["narrationAudio"])
    user_id = uuid.uuid4()

    job = build_generative_job(
        **_phone_voiceover_args(user_id, edit_format=edit_format),
        creator_strategy=creator_strategy,
    )

    assert job.all_candidates["voiceover_gcs_path"].endswith("/voice.m4a")
    assert job.assembly_plan[PHONE_SOURCES_FIELD][0]["media_id"] == "phone-source"


def test_phone_voiceover_constructor_preserves_rollout_and_source_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    user_id = uuid.uuid4()
    args = _phone_voiceover_args(user_id)
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_narration_rendering_enabled", False)
    monkeypatch.setattr(settings, "phone_render_verified_features", [])

    with pytest.raises(ValueError, match="phone planning is unavailable"):
        build_generative_job(**args)

    monkeypatch.setattr(settings, "phone_narration_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["narrationAudio"])
    invalid_source_args = dict(args)
    invalid_source_args["clip_paths"] = ["slot-uploads/not-the-bound-proxy.mp4"]
    with pytest.raises(ValueError, match="exactly bind"):
        build_generative_job(**invalid_source_args)

    invalid_voiceover_args = dict(args)
    invalid_voiceover_args["voiceover_gcs_path"] = (
        f"users/{uuid.uuid4()}/creation-threads/{uuid.uuid4()}/voice.m4a"
    )
    with pytest.raises(ValueError, match="owner mismatch"):
        build_generative_job(**invalid_voiceover_args)

    proxy_voiceover_args = dict(args)
    proxy_voiceover_args["voiceover_gcs_path"] = args["clip_paths"][0]
    with pytest.raises(ValueError, match="analysis proxies"):
        build_generative_job(**proxy_voiceover_args)


@pytest.mark.parametrize(
    ("edit_format", "creator_strategy", "disable_guided"),
    [
        (
            "narrated_planned",
            {
                "execution_contract": "guided_voiceover_v1",
                "render_program": "guided",
                "audio_strategy": "voiceover",
            },
            True,
        ),
        ("talking_head", None, False),
    ],
    ids=["guided_rollout_disabled", "unsupported_format"],
)
def test_phone_voiceover_constructor_rejects_unavailable_lane(
    monkeypatch: pytest.MonkeyPatch,
    edit_format: str,
    creator_strategy: dict | None,
    disable_guided: bool,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_narration_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_guided_narration_rendering_enabled", not disable_guided)
    monkeypatch.setattr(settings, "phone_narrated_rendering_enabled", True)
    monkeypatch.setattr(settings, "narrated_archetype_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["narrationAudio"])

    with pytest.raises(ValueError, match="phone planning is unavailable"):
        build_generative_job(
            **_phone_voiceover_args(uuid.uuid4(), edit_format=edit_format),
            creator_strategy=creator_strategy,
        )


# ── T1: topic/intent passthrough ─────────────────────────────────────────────


def test_topic_and_intent_passed_as_item_theme_and_idea() -> None:
    """topic/intent from the onboarding fork must arrive as persona.theme/idea."""
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["music-uploads/a.mp4"],
        item_theme="travel cooking",
        item_idea="inspire wanderlust",
    )
    persona = job.all_candidates["persona"]
    assert persona["theme"] == "travel cooking"
    assert persona["idea"] == "inspire wanderlust"


def test_empty_topic_intent_omits_persona_key_unchanged() -> None:
    """Empty/blank topic+intent → persona key absent (same as test_public_job_omits_persona_key)."""
    job = build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["music-uploads/a.mp4"],
        item_theme="",
        item_idea="",
    )
    assert "persona" not in job.all_candidates
