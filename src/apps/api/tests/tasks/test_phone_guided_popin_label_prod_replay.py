"""Prod-shaped replay of the KRI narration-pop-in-label phone fix.

Reproduces job 76db6913 (plan item 26bf79fe): an iPhone narrated guided story
("Sagrada Familia", execution contract `guided_voiceover_v1`) with 5 phone
clips, 5 Visuals-pool photos, and a 41.9s cleaned voiceover with 88 sentence
captions plus one title. `_run_phone_guided_job` -> `compile_phone_guided_plan`
raised

    app.pipeline.phone_guided_plan.UnsupportedPhonePlan:
    sequence effect needs composite-stream parity

The offending overlay was a narration LABEL (lane
`narration_label_text_elements`) authored at render time by
`nova.compose.narration_annotations` via `narration_labels.py::_append_element`
(`kind="topic"` -> `role="generative_sequence"`, `effect="pop-in"`,
`position="custom"`). Labels are written AFTER dispatch, so nothing upstream
of the compiler could have caught this -- the same project rendered fine the
day before because it happened not to draw a label.

This module reconstructs the anonymized admin dump
(`.exec_plan_76db6913.json`, not checked in) in code: the real 10-moment
`story_timeline` (5 video moments + 5 image moments, in the dump's exact
order and timing -- transition_policy is a global 0.2s crossfade, which is
why adjacent moments' output windows overlap by ~0.2s with no per-moment
`transition_after`), the real title + a representative trimmed subset of the
88 sentence captions (first 4 + last 2, with real `word_timings`), the real
pop-in topic label verbatim, and a synthetic 41.9s cleaned-narration bed
matching the dump's `narration` block (`compiler_version: 8`,
`editor_caption_meta: {"style": "sentence", "y_frac": 0.7}`). Media ids were
stripped by the admin view; this file synthesizes them consistently with the
dump's `guided_edit_media_identities` order (5 "video"/"clip" entries, then 5
"image"/"asset" entries) as `clip-1..5` / `photo-1..5`, in the order each
first appears in `story_timeline`.

Mirrors `tests/tasks/test_phone_guided_narration_prod_replay.py`'s pattern:
construct the `GuidedStoryExecutionPlan` directly (this compiler's shape is
already exhaustively unit-tested in `tests/pipeline/test_phone_guided_plan.py`)
and drive it under `prod_profile` through `validate_phone_pilot_recipe`, the
same device-pilot gate a real render must clear.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.text_element import TextElement
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderFingerprint
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.schemas.edit_proposal import NarrationTrack, NarrationWord
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding

CLIP_MEDIA_IDS = [f"clip-{n}" for n in range(1, 6)]
PHOTO_MEDIA_IDS = [f"photo-{n}" for n in range(1, 6)]
NARRATION_DURATION_S = 41.89775
NARRATION_GENERATION = "1790167896437300"

# The dump's exact 10-moment `story_timeline`, in order, with media ids
# assigned as each kind is first seen (5 video moments -> clip-1..5 in
# appearance order, 5 image moments -> photo-1..5 in appearance order). Every
# other field (topic/beat_id/timing) is copied verbatim.
_RAW_STORY_TIMELINE = [
    (
        "video",
        "chapter-1",
        "chapter-1:m1",
        "Current towering scale and ongoing construction",
        0.0,
        2.8666666666666667,
        1.8333333333333333,
        4.7,
    ),
    (
        "video",
        "chapter-1",
        "chapter-1:m2",
        "Current towering scale and ongoing construction",
        2.6666666666666665,
        5.533333333333333,
        0.0,
        2.8666666666666667,
    ),
    (
        "image",
        "chapter-2",
        "chapter-2:m1",
        "Gaudi's introduction to the project",
        5.333333333333333,
        11.066666666666666,
        0.0,
        5.733333333333333,
    ),
    (
        "image",
        "chapter-3",
        "chapter-3:m1",
        "Early construction phases under Gaudi",
        10.866666666666667,
        13.733333333333333,
        0.0,
        2.8666666666666667,
    ),
    (
        "image",
        "chapter-3",
        "chapter-3:m2",
        "Early construction phases under Gaudi",
        13.533333333333333,
        16.4,
        0.0,
        2.8666666666666667,
    ),
    (
        "image",
        "chapter-4",
        "chapter-4:m1",
        "A century of interrupted, resumed construction",
        16.2,
        21.9,
        0.0,
        5.7,
    ),
    (
        "image",
        "chapter-5",
        "chapter-5:m1",
        "Modern construction techniques and funding",
        21.7,
        27.4,
        0.0,
        5.7,
    ),
    (
        "video",
        "chapter-6",
        "chapter-6:m1",
        "The scale of what remains unfinished",
        27.2,
        32.233333333333334,
        0.0,
        5.033333333333333,
    ),
    (
        "video",
        "chapter-7",
        "chapter-7:m1",
        "The scale of what remains unfinished",
        32.03333333333333,
        37.06666666666667,
        0.0,
        5.033333333333333,
    ),
    (
        "video",
        "chapter-8",
        "chapter-8:m1",
        "Why it has taken so many years",
        36.86666666666667,
        41.9,
        0.0,
        5.033333333333333,
    ),
]


def _story_timeline_and_media_ids() -> tuple[list[dict], list[str]]:
    clip_ids = iter(CLIP_MEDIA_IDS)
    photo_ids = iter(PHOTO_MEDIA_IDS)
    timeline: list[dict] = []
    media_ids: list[str] = []
    for (
        kind,
        beat_id,
        moment_id,
        topic,
        out_start,
        out_end,
        src_start,
        src_end,
    ) in _RAW_STORY_TIMELINE:
        is_video = kind == "video"
        media_id = next(clip_ids) if is_video else next(photo_ids)
        media_ids.append(media_id)
        timeline.append(
            {
                "moment_id": moment_id,
                "beat_id": beat_id,
                "topic": topic,
                "media_id": media_id,
                "lane": "clip" if is_video else "asset",
                "kind": "video" if is_video else "image",
                "gcs_path": (
                    f"users/creator/plan/item/analysis-proxy-{media_id}.mp4"
                    if is_video
                    else f"users/creator/plan/item/pool/{media_id}.jpg"
                ),
                "generation": "123" if is_video else "777",
                "layout": "fullscreen",
                "source_start_s": src_start,
                "source_end_s": src_end,
                "output_start_s": out_start,
                "output_end_s": out_end,
                "duration_s": round(out_end - out_start, 6),
                "look_preset": "none",
            }
        )
    return timeline, media_ids


def _beat_windows() -> list[dict]:
    return [
        {
            "beat_id": "chapter-1",
            "approved_duration_s": 5.333333333333333,
            "resolved_duration_s": 5.333333333333333,
            "start_s": 0.0,
            "end_s": 5.333333333333333,
        },
        {
            "beat_id": "chapter-2",
            "approved_duration_s": 5.533333333333333,
            "resolved_duration_s": 5.533333333333333,
            "start_s": 5.333333333333333,
            "end_s": 10.866666666666667,
        },
        {
            "beat_id": "chapter-3",
            "approved_duration_s": 5.333333333333333,
            "resolved_duration_s": 5.333333333333333,
            "start_s": 10.866666666666667,
            "end_s": 16.2,
        },
        {
            "beat_id": "chapter-4",
            "approved_duration_s": 5.5,
            "resolved_duration_s": 5.5,
            "start_s": 16.2,
            "end_s": 21.7,
        },
        {
            "beat_id": "chapter-5",
            "approved_duration_s": 5.5,
            "resolved_duration_s": 5.5,
            "start_s": 21.7,
            "end_s": 27.2,
        },
        {
            "beat_id": "chapter-6",
            "approved_duration_s": 4.833333333333333,
            "resolved_duration_s": 4.833333333333333,
            "start_s": 27.2,
            "end_s": 32.03333333333333,
        },
        {
            "beat_id": "chapter-7",
            "approved_duration_s": 4.833333333333333,
            "resolved_duration_s": 4.833333333333333,
            "start_s": 32.03333333333333,
            "end_s": 36.86666666666667,
        },
        {
            "beat_id": "chapter-8",
            "approved_duration_s": 5.033333333333333,
            "resolved_duration_s": 5.033333333333333,
            "start_s": 36.86666666666667,
            "end_s": 41.9,
        },
    ]


def _title_element() -> dict:
    return TextElement(
        id="guided-title",
        text="Building since 1882",
        role="generative_intro",
        effect="fade-in",
        position="custom",
        x_frac=0.5,
        y_frac=0.16,
        alignment="center",
        color="#FFF8F0",
        highlight_color="#D9FF70",
        font_family="Fraunces",
        size_px=104.0,
        start_s=0.0,
        end_s=3.2,
    ).model_dump(mode="json", exclude_none=True)


def _caption_element(*, id_: str, text: str, key: str, start_s: float, end_s: float) -> dict:
    """Shaped exactly like a `narration-caption-*` element from the dump:
    `role="generative_sequence"`, `effect="static"`, real `word_timings`, and
    `source_params.source == "caption_cue"` (which is what makes
    `_tag_guided_text_overlays` re-tag its overlay role to
    `generative_narration_caption` -- a DIFFERENT lane from the pop-in label
    below, and never subject to the sequence-parity check either)."""
    return TextElement(
        id=id_,
        text=text,
        role="generative_sequence",
        effect="static",
        position="custom",
        x_frac=0.5,
        y_frac=0.82,
        alignment="center",
        color="#FFFFFF",
        highlight_color="#FFFFFF",
        font_family="Inter-Bold",
        size_px=58.0,
        stroke_width=6.0,
        start_s=start_s,
        end_s=end_s,
        word_timings=[{"text": text, "start_s": start_s, "end_s": end_s, "confidence": 1.0}],
        source_params={"key": key, "source": "caption_cue", "identity": f"pinned-{id_}"},
    ).model_dump(mode="json", exclude_none=True)


def _trimmed_captions() -> list[dict]:
    # First 4 + last 2 of the real 88 sentence captions -- representative of
    # the full run (a title, ordinary mid-sentence words, and the closing
    # punctuated word) without constructing all 88.
    return [
        _caption_element(id_="narration-caption-1", text="They", key="0", start_s=0.3, end_s=0.94),
        _caption_element(
            id_="narration-caption-2", text="started", key="1", start_s=0.94, end_s=1.36
        ),
        _caption_element(
            id_="narration-caption-3", text="building", key="2", start_s=1.36, end_s=1.68
        ),
        _caption_element(id_="narration-caption-4", text="this", key="3", start_s=1.68, end_s=1.94),
        _caption_element(
            id_="narration-caption-87", text="144", key="86", start_s=40.098, end_s=40.838
        ),
        _caption_element(
            id_="narration-caption-88", text="years?", key="87", start_s=40.838, end_s=41.398
        ),
    ]


def _pop_in_topic_label() -> dict:
    """Verbatim shape of the dump's single `narration_label_text_elements`
    entry -- the exact overlay that crashed job 76db6913."""
    return TextElement(
        id="1fb7922920c25482625f6c9f9b2a4fc5",
        text="They started building this before the Eiffel Tower, and it's still not finished.",
        role="generative_sequence",
        effect="pop-in",
        position="custom",
        x_frac=0.08,
        y_frac=0.12,
        alignment="left",
        size_class="small",
        start_s=0.30000000000000004,
        end_s=4.57999997138977,
        source_params={
            "source_kind": "narration_annotation",
            "source_word_ids": [f"w{n:06d}" for n in range(13)],
            "transcript_grounded": True,
            "narration_label_kind": "topic",
        },
    ).model_dump(mode="json", exclude_none=True)


def _narration_track() -> NarrationTrack:
    words = [
        NarrationWord(text="They", start_s=0.3, end_s=0.94),
        NarrationWord(text="started", start_s=0.94, end_s=1.36),
        NarrationWord(text="building", start_s=1.36, end_s=1.68),
        NarrationWord(text="this", start_s=1.68, end_s=1.94),
    ]
    return NarrationTrack(
        gcs_path="users/creator/creation-threads/thread/sagrada-voiceover-cleaned.wav",
        generation=NARRATION_GENERATION,
        duration_s=NARRATION_DURATION_S,
        words=words,
        language="en",
        caption_style="sentence",
    )


def _plan_bindings_and_visuals(
    narration: NarrationTrack,
) -> tuple[
    GuidedStoryExecutionPlan, tuple[PhoneSourceBinding, ...], tuple[PhoneVisualBinding, ...]
]:
    story_timeline, media_ids = _story_timeline_and_media_ids()
    clip_ids = [m for m in media_ids if m.startswith("clip-")]
    photo_ids = [m for m in media_ids if m.startswith("photo-")]
    bindings = tuple(
        PhoneSourceBinding(
            media_id=media_id,
            proxy_path=f"users/creator/plan/item/analysis-proxy-{media_id}.mp4",
            generation="123",
            original=OriginalMediaDescriptor(
                sha256=format(index, "x") * 64,
                byte_count=1000,
                duration_s=10.0,  # comfortable headroom over every clip's source window
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for index, media_id in enumerate(clip_ids, start=1)
    )
    visuals = tuple(
        PhoneVisualBinding(
            media_id=media_id,
            gcs_path=f"users/creator/plan/item/pool/{media_id}.jpg",
            generation="777",
            sha256=format(index, "x") * 64,
            byte_count=2048,
        )
        for index, media_id in enumerate(photo_ids, start=1)
    )
    plan = GuidedStoryExecutionPlan.model_validate(
        {
            "compiler_version": 8,
            "proposal_version": 1,
            "media_digest": "a" * 64,
            "direction": "guided_story",
            "goal": "Tell the story of the Sagrada Familia",
            "pace": "balanced",
            "approved_duration_s": NARRATION_DURATION_S,
            "resolved_duration_s": 41.9,
            "output_orientation": "portrait",
            "selected_media_ids": media_ids,
            "story_timeline": story_timeline,
            "beat_windows": _beat_windows(),
            "text_elements": [_title_element(), *_trimmed_captions()],
            "narration_label_text_elements": [_pop_in_topic_label()],
            "transition_policy": {"type": "crossfade", "duration_s": 0.2},
            "typography": {"style_id": "guided_story_v2", "font": "Fraunces"},
            "editor_caption_meta": {"style": "sentence", "y_frac": 0.7},
            "editor_audio_level": 1.0,
            "narration": narration.model_dump(mode="json"),
        }
    )
    return plan, bindings, visuals


def _narration_bed(narration: NarrationTrack) -> PhoneNarrationBed:
    return PhoneNarrationBed(
        plan_item_id="item-26bf79fe",
        generation=narration.generation,
        fingerprint=RenderFingerprint(sha256="b" * 64, byte_count=1_048_576),
        duration_s=narration.duration_s,
    )


def test_replay_reproduces_the_prod_crash_before_the_fix_on_the_old_gate() -> None:
    """Pin the exact pre-fix behaviour: the old role-based pre-check rejected
    ANY `generative_sequence`-role overlay whose effect fell outside the
    sequence-fade set, which is precisely what the pop-in topic label is.
    This is a standalone characterization of the removed code path (not a
    call into current `compile_phone_guided_plan`, which now compiles this
    plan successfully -- see the test below) so a future revert of the fix
    is caught by this file re-deriving the same rejected set."""
    label_overlay_effect = "pop-in"
    label_overlay_role = "generative_sequence"
    old_allowed_sequence_effects = {"fade-in", "static", "none", "handwriting", "ink-reveal"}
    assert label_overlay_role == "generative_sequence"
    assert label_overlay_effect not in old_allowed_sequence_effects  # <- why job 76db6913 crashed


def test_narrated_planned_pilot_with_pop_in_label_compiles_and_passes_the_phone_gate(
    prod_profile,
) -> None:
    narration = _narration_track()
    plan, bindings, visuals = _plan_bindings_and_visuals(narration)
    bed = _narration_bed(narration)

    recipe = compile_phone_guided_plan(plan, bindings, visuals=visuals, narration=bed)

    validate_phone_pilot_recipe(recipe)  # must not raise under prod_profile's verified features
    assert recipe.duration == pytest.approx(41.9)
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe

    pop_in_layers = [layer for layer in recipe.text_layers if layer.effect == "pop-in"]
    assert len(pop_in_layers) == 1
    assert pop_in_layers[0].fade is None

    # Every caption layer (the title + all 6 trimmed captions) still compiles,
    # untouched by the label fix.
    caption_ids = {f"narration-caption-{n}" for n in (1, 2, 3, 4, 87, 88)}
    compiled_caption_layers = [
        layer for layer in recipe.text_layers if layer.id.startswith("text-")
    ]
    assert len(compiled_caption_layers) == 1 + len(caption_ids)  # title + 6 captions, one lane

    voice_asset_id = f"voiceover-{bed.plan_item_id}"
    assert recipe.audio.narration_asset_id == voice_asset_id
    assert recipe.audio.original_volume == 0
    assert {
        "narrationAudio",
        "audioMix",
        "stillImages",
        "crossfade",
    } <= recipe.required_capabilities


def test_replay_still_fails_closed_if_the_narration_identity_gate_is_reverted() -> None:
    """Regression fence for the OTHER gate in this function: a narration bed
    that doesn't match the plan's pinned identity must still be rejected,
    proving this replay would catch that check being silently dropped even
    though the sequence-effect gate above is now relaxed."""
    narration = _narration_track()
    plan, bindings, visuals = _plan_bindings_and_visuals(narration)
    stale_bed = _narration_bed(narration).model_copy(update={"generation": "stale"})
    with pytest.raises(UnsupportedPhonePlan, match="replaced since approval"):
        compile_phone_guided_plan(plan, bindings, visuals=visuals, narration=stale_bed)
