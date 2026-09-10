import pytest

from app.agents._schemas.text_element import TextElement
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.services.phone_sources import PhoneSourceBinding


def fixture():
    binding = PhoneSourceBinding(
        media_id="source",
        proxy_path="user/analysis-proxy-source.mp4",
        generation="123",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=10,
            width=1920,
            height=1080,
            has_audio=True,
        ),
    )
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
                    "gcs_path": binding.proxy_path,
                    "generation": "123",
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
    return plan, (binding,)


def test_shared_timing_and_original_metadata_preserved():
    plan, bindings = fixture()
    recipe = compile_phone_guided_plan(plan, bindings)
    assert recipe.duration == 3
    assert recipe.tracks[0].clips[0].source_start == 2
    assert recipe.assets[0].natural_size.width == 1920
    assert recipe.audio.original_volume == 0
    assert "analysis-proxy" not in recipe.model_dump_json()
    plan.montage_audio = {"preserve_source_audio": True}
    plan.editor_audio_level = 0.4
    assert compile_phone_guided_plan(plan, bindings).audio.original_volume == 0.4


def test_approved_static_text_uses_shared_layout_and_bound_font():
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(
            id="title",
            text="This view",
            start_s=0.5,
            end_s=2.5,
            font_family="Inter-Bold",
            effect="none",
            size_px=64,
        )
    ]
    recipe = compile_phone_guided_plan(plan, bindings)
    assert len(recipe.text_layers) == 1
    layer = recipe.text_layers[0]
    assert (layer.start, layer.end) == (0.5, 2.5)
    assert " ".join(run.text for run in layer.runs) == "This view"
    font = next(a for a in recipe.asset_manifest.assets if a.id == layer.runs[0].font_asset_id)
    assert font.kind == "library" and font.catalog == "font"
    assert font.fingerprint.byte_count > 1000


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", "124"),
        ("source_end_s", 11),
        ("output_start_s", 0.1),
        ("layout", "supporting_card"),
        ("look_preset", "warm"),
        ("transition_after", "crossfade"),
        ("kind", "image"),
    ],
)
def test_cannot_silently_drop_treatments_or_rebind_sources(field, value):
    plan, bindings = fixture()
    plan.story_timeline[0] = plan.story_timeline[0].model_copy(update={field: value})
    with pytest.raises(ValueError):
        compile_phone_guided_plan(plan, bindings)


@pytest.mark.parametrize(
    "lane",
    [
        "editor_sound_effects",
        "editor_media_overlays",
        "editor_visual_blocks",
        "editor_motion_scenes",
        "editor_custom_effects",
    ],
)
def test_editor_lanes_cannot_disappear(lane):
    plan, bindings = fixture()
    setattr(plan, lane, [{"id": "required"}])
    with pytest.raises(ValueError, match=lane):
        compile_phone_guided_plan(plan, bindings)


def test_dissolve_seed_follows_cloud_combined_lane_indices():
    plan, bindings = fixture()
    for index, lane in enumerate(
        ("text_elements", "context_label_text_elements", "narration_label_text_elements")
    ):
        setattr(
            plan,
            lane,
            [
                TextElement(
                    id=f"label-{index}",
                    text="This view",
                    start_s=0.5,
                    end_s=2.5,
                    font_family="Inter-Bold",
                    effect="dissolve-out",
                    size_px=64,
                )
            ],
        )
    recipe = compile_phone_guided_plan(plan, bindings)
    assert [layer.dissolve_seed for layer in recipe.text_layers] == [101, 138, 175]


def test_sequence_blocks_draw_above_other_lanes_without_changing_dissolve_seed():
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(
            id="seq1",
            text="First",
            role="generative_sequence",
            effect="fade-in",
            start_s=0,
            end_s=2,
            fade_out_ms=500,
        ),
        TextElement(
            id="seq2",
            text="Second",
            role="generative_sequence",
            effect="static",
            start_s=1,
            end_s=3,
            fade_out_ms=250,
        ),
    ]
    plan.context_label_text_elements = [
        TextElement(id="context", text="Context", effect="dissolve-out", start_s=0, end_s=3)
    ]
    recipe = compile_phone_guided_plan(plan, bindings)
    assert [layer.id for layer in recipe.text_layers] == ["context-0", "text-0", "text-1"]
    assert recipe.text_layers[0].dissolve_seed == 101
    assert [(layer.fade.kind, layer.fade.out_ms) for layer in recipe.text_layers[1:]] == [
        ("sequence", 500),
        ("sequence", 250),
    ]
