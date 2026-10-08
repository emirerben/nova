"""Word/phrase restructuring must survive the real phone Save recipe compiler."""

import pytest

from app.routes import generative_jobs as gj
from app.services.device_render import device_status
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from tests.routes.test_phone_editor_commit import phone_job
from tests.test_edit_copilot import _parse


@pytest.mark.parametrize(
    "segments", [["A", "quiet", "walk", "by", "the", "river"], ["A quiet walk", "by the river"]]
)
def test_sequence_save_and_later_style_edit_preserve_words_windows_and_footage(
    monkeypatch, segments
):
    job = phone_job(monkeypatch)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "authoredText"],
    )
    variant = job.assembly_plan["variants"][0]
    title = variant["text_elements"][0]
    title.update(text="A quiet walk by the river", role="generative_intro")
    job.assembly_plan["guided_story_execution_plan"]["text_elements"][0].update(title)
    before = device_status(job, "guided_story").request.recipe.model_dump(mode="json")
    snapshot = build_editor_snapshot(job, variant)
    parsed = _parse(
        [
            {
                "op": "replace_text_sequence",
                "selector": {"ids": ["title"]},
                "segments": segments,
                "patch": {"animation_phases": {"entrance": "fade", "exit": "fade", "loop": "none"}},
            }
        ],
        snapshot=snapshot,
    )
    assert len(parsed.ops) == 1
    compiled = compile_editor_ops(job, variant, parsed.ops)
    gj.prepare_editor_commit(job, "guided_story", compiled.payload)
    recipe = device_status(job, "guided_story").request.recipe
    assert [" ".join(r.text for r in layer.runs) for layer in recipe.text_layers] == segments
    assert all(
        layer.animation_phases.entrance == "fade" and layer.animation_phases.exit == "fade"
        for layer in recipe.text_layers
    )
    windows = [(layer.start, layer.end) for layer in recipe.text_layers]
    assert windows[0][0] == 0 and windows[-1][1] == 2
    assert all(a[1] <= b[0] for a, b in zip(windows, windows[1:]))
    assert recipe.model_dump(mode="json")["tracks"] == before["tracks"]

    variant = job.assembly_plan["variants"][0]
    parsed = _parse(
        [
            {
                "op": "patch_text",
                "selector": {"group": "title"},
                "patch": {"color": "#00FF00"},
            }
        ],
        snapshot=build_editor_snapshot(job, variant),
    )
    assert len(parsed.ops[0]["target_ids"]) == len(segments)
    compiled = compile_editor_ops(job, variant, parsed.ops)
    gj.prepare_editor_commit(job, "guided_story", compiled.payload)
    after = device_status(job, "guided_story").request.recipe
    assert [(layer.start, layer.end) for layer in after.text_layers] == windows
    assert [" ".join(r.text for r in layer.runs) for layer in after.text_layers] == segments
    assert after.model_dump(mode="json")["tracks"] == before["tracks"]
