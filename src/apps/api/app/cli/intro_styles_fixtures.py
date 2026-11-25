"""KRI-1633: synthetic phone jobs for the intro-styles checks, built with the REAL phone builders.

One Talking, one narrated and one guided/montage job, each as the worker leaves it: a pinned
device recipe (the real `compile_phone_*_plan`), the variant row with its title row(s), and for
Talking the real `place_talking_title` receipt (a saved face band). Used by the verify CLI and by
the tests (the prod image has no `tests/`, so the fixtures live in `app/`). No network, no DB.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from typing import Any

from app.kria.device_render import make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint
from app.pipeline.canvas import PORTRAIT
from app.services.device_render import pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding

USER_ID = "6f1c0d52-4a4e-4f6e-9b7c-0d6a3c5f1a11"
EN_TITLE = "the biggest design trend right now"
TR_TITLE = "bu yılın en büyük tasarım trendi şu an"
ROUTES = ("talking", "narrated", "montage")
VARIANT_IDS = {"talking": "subtitled", "narrated": "narrated", "montage": "guided_story"}
VERIFIED_FEATURES = [
    "basicComposition",
    "local1080Export",
    "positionedText",
    "animatedText",
    "audioMix",
    "audioDucking",
    "narrationAudio",
    "variableSpeed",
    "soundEffects",
    "visualBlocks",
    "alphaOverlay",
    "stillImages",
    "visualVideos",
]
POLICY = {
    "version": 1,
    "preferences": {"all_text": {}},
    "requested": {},
    "informative_text_designs": True,
    "informative_text_shapes": True,
}
CUES = [
    {"text": "Hello everyone", "start_s": 0.0, "end_s": 1.5},
    {"text": "welcome back", "start_s": 1.5, "end_s": 3.0},
]
# A speaker standing a little left of centre, head high: the saved Talking face band.
FACE = (0.30, 0.30, 0.62, 0.62)


def flag_overrides() -> dict[str, Any]:
    """Settings the phone title lanes need (the tests' monkeypatches, the CLI's assignments)."""
    return {
        "phone_rendering_enabled": True,
        "phone_subtitled_title_enabled": True,
        "phone_narrated_title_edits_enabled": True,
        "phone_narrated_caption_edits_enabled": True,
        "phone_subtitled_editor_lanes_enabled": True,
        "phone_subtitled_media_lanes_enabled": True,
        "sound_effects_enabled": True,
        "media_overlays_enabled": True,
        "title_look_enabled": True,
        "intro_scene_enabled": True,
        "intro_scene_user_ids": [USER_ID],
        "phone_render_verified_features": list(VERIFIED_FEATURES),
    }


def binding(media_id: str, *, duration_s: float = 12.0) -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"user/analysis-proxy-{media_id}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=duration_s,
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )


def _face_sampler(face: tuple[float, float, float, float] | None):
    from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion  # noqa: PLC0415

    def sample(_path, anchors, **_kwargs):  # noqa: ANN001, ANN202
        regions = [ProtectedRegion(0.0, 1e6, NormalizedBox(*face), kind="face")] if face else []
        return regions, {
            "attempted": len(anchors),
            "decoded": len(anchors),
            "detected": len(regions),
        }

    return sample


def _job(variant: dict[str, Any], extra: dict[str, Any], recipe: Any, bindings: tuple) -> Any:
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=USER_ID,
        status="awaiting_device",
        current_phase=None,
        all_candidates={},
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            "phone_text_style": copy.deepcopy(POLICY),
            "variants": [variant],
            **extra,
        },
    )
    pin_device_request(
        job,
        make_device_request(
            job_id=job.id, variant_id=variant["variant_id"], revision=1, recipe=recipe
        ),
        base_generation="first",
    )
    return job


def talking_job(title: str = EN_TITLE, *, face: tuple | None = FACE) -> tuple[Any, str, list[dict]]:
    """``(job, variant_id, title_rows)`` for a phone Talking edit with a plain one-row title."""
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan  # noqa: PLC0415
    from app.pipeline.phone_subtitled_title import (  # noqa: PLC0415
        place_talking_title,
        talking_title_element,
    )

    bindings = (binding("clip-0", duration_s=10.0),)
    row = talking_title_element(
        title, duration_s=2.5, first_word_end_s=1.0, timeline_duration_s=10.0, canvas=PORTRAIT
    )
    placed, receipt = place_talking_title(
        row,
        clip_path="take.mp4",
        keep_segments=[(0.0, 10.0)],
        display_width=PORTRAIT.width,
        display_height=PORTRAIT.height,
        canvas=PORTRAIT,
        sample=_face_sampler(face),
    )
    recipe = compile_phone_subtitled_plan(bindings, caption_cues=CUES, text_elements=[placed])
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "render_status": "awaiting_device",
        "render_generation_id": "first",
        "duration_s": recipe.duration,
        "caption_cues": copy.deepcopy(CUES),
        "voiceover_caption_style": "sentence",
        "text_elements": [copy.deepcopy(placed)],
        "text_elements_user_edited": True,
        "opening_title_placement": receipt,
    }
    return _job(variant, {}, recipe, bindings), "subtitled", [copy.deepcopy(placed)]


def narrated_job(title: str = EN_TITLE) -> tuple[Any, str, list[dict]]:
    from app.pipeline.phone_narrated_plan import (  # noqa: PLC0415
        NarratedPhoneStep,
        compile_phone_narrated_plan,
        narrated_title_element,
    )
    from app.pipeline.phone_recipe_shared import PhoneNarrationBed  # noqa: PLC0415

    bindings = (binding("c0"), binding("c1"), binding("c2"))
    steps = [
        NarratedPhoneStep(
            step_id=f"s{i}",
            media_id=f"c{i}",
            source_start_s=0.0,
            start_s=4.0 * i,
            end_s=4.0 * (i + 1),
        )
        for i in range(3)
    ]
    narration = PhoneNarrationBed(
        plan_item_id="item-1",
        generation="9",
        fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=999),
        duration_s=12.0,
    )
    recipe = compile_phone_narrated_plan(
        steps,
        bindings,
        narration,
        voiceover_duration_s=12.0,
        mix=0.7,
        caption_cues=[
            {"text": "First we pack", "start_s": 0.0, "end_s": 2.0},
            {"text": "then we drive", "start_s": 4.5, "end_s": 6.0},
        ],
        caption_style="sentence",
        target_lufs=-14.0,
        duck_footage_bed=True,
        opening_title=title,
        opening_title_end_s=2.5,
    )
    element = narrated_title_element(title, end_s=2.5, timeline_duration_s=12.0, canvas=PORTRAIT)
    row = element.model_dump(mode="json", exclude_none=True)
    variant = {
        "variant_id": "narrated",
        "rank": 1,
        "resolved_archetype": "narrated",
        "render_destination": "device",
        "render_status": "awaiting_device",
        "render_generation_id": "first",
        "duration_s": recipe.duration,
        "caption_cues": [
            {"text": "First we pack", "start_s": 0.0, "end_s": 2.0},
            {"text": "then we drive", "start_s": 4.5, "end_s": 6.0},
        ],
        "voiceover_caption_style": "sentence",
        "caption_language": "en",
        "voiceover_bed_level": 0.3,
        "ok": False,
    }
    return _job(variant, {}, recipe, bindings), "narrated", [row]


def montage_job(title: str = EN_TITLE) -> tuple[Any, str, list[dict]]:
    from app.agents._schemas.text_element import TextElement  # noqa: PLC0415
    from app.pipeline.guided_story import GuidedStoryExecutionPlan  # noqa: PLC0415
    from app.pipeline.intro_styles import _guided_plain_title  # noqa: PLC0415
    from app.pipeline.phone_guided_plan import compile_phone_guided_plan  # noqa: PLC0415

    bind = binding("source", duration_s=10.0)
    plan = GuidedStoryExecutionPlan.model_validate(
        {
            "compiler_version": 5,
            "proposal_version": 1,
            "media_digest": "b" * 64,
            "direction": "guided_story",
            "goal": "Show the scene",
            "pace": "balanced",
            "approved_duration_s": 3,
            "resolved_duration_s": 3,
            "selected_media_ids": ["source"],
            "editor_revision_number": 1,
            "story_timeline": [
                {
                    "moment_id": "moment",
                    "beat_id": "beat",
                    "topic": "Scene",
                    "media_id": "source",
                    "lane": "clip",
                    "kind": "video",
                    "gcs_path": bind.proxy_path,
                    "generation": "1",
                    "layout": "fullscreen",
                    "source_start_s": 2,
                    "source_end_s": 5,
                    "output_start_s": 0,
                    "output_end_s": 3,
                    "duration_s": 3,
                }
            ],
            "beat_windows": [
                {
                    "beat_id": "beat",
                    "approved_duration_s": 3,
                    "resolved_duration_s": 3,
                    "start_s": 0,
                    "end_s": 3,
                }
            ],
            "text_elements": [],
            "transition_policy": {"type": "none", "duration_s": 0},
            "typography": {"style_id": "guided_story_v2", "font": "Inter"},
        }
    )
    row = _guided_plain_title(title, 2.5, PORTRAIT)
    plan.text_elements = [TextElement.model_validate(row)]
    raw = plan.model_dump(mode="json")
    recipe = compile_phone_guided_plan(plan, (bind,), visuals=(), narration=None)
    variant = {
        "variant_id": "guided_story",
        "resolved_archetype": "guided_story",
        "render_destination": "device",
        "render_status": "awaiting_device",
        "render_generation_id": "first",
        "duration_s": raw["resolved_duration_s"],
        "text_mode": "agent_text",
        "intro_mode": "linear",
        "intro_layout": "linear",
        "text_elements": copy.deepcopy(raw["text_elements"]),
    }
    job = _job(variant, {"guided_story_execution_plan": raw}, recipe, (bind,))
    return job, "guided_story", copy.deepcopy(raw["text_elements"])


BUILDERS = {"talking": talking_job, "narrated": narrated_job, "montage": montage_job}
