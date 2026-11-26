"""KRI-1633 F12: a picked intro look keeps the render contract's whole-title requirements.

`_text_requirements_for_save` maps requirements per row by id, so an approved opening requirement
"Bu yılın trendi" used to become line 1's "Bu" once a look set the title as several rows, and an
`any` requirement on her whole title failed because only `opening` fell back to the stacked-title
check. Guards, per route and language:

* the standard contract shape [opening X (2.0 s), any X] survives pick, Save and a second Save,
  still requires X as the opening, and is not declined;
* rows without a stack are byte-identical to before (a plain row, a typed " / " stack);
* a contract that cannot read the stack (the old `any` rule) refuses (negative control, N6).
"""

from __future__ import annotations

import copy

import pytest

from app.pipeline import intro_scene as scene_mod
from app.pipeline import intro_scene_pick as pick
from app.pipeline import intro_styles as styles
from app.pipeline.canvas import PORTRAIT
from app.pipeline.phone_title_look import intro_stack_as_one_row
from app.routes import generative_jobs as gj
from app.services.creator_render_contract import (
    CreatorRenderContract,
    TextRequirement,
    verify_phone_recipe,
)
from app.services.device_render import device_status
from app.services.phone_editor import _text_requirements_for_save
from tests.pipeline.test_intro_scene_pick import _place_with
from tests.routes import test_phone_narrated_editor_commit as nar

EN = "the biggest design trend right now"
TR = "bu yılın en büyük tasarım trendi şu an"


def stack(title: str, language: str | None = None, font_pair: str = "grotesk_hand") -> list[dict]:
    menu = scene_mod.issue_menu(title)
    ask = {
        "version": 1,
        "seed": "e",
        "inks": list(scene_mod.INKS),
        "candidates": [
            {
                "voices": list(pick.accent_splits(title)[0]),
                "font_pair": font_pair,
                "entrance": menu.entrances_for(font_pair)[0],
                "zones": ["above_head"],
            }
        ],
    }
    rows, _receipt, info = pick.fit_intro_scene(
        ask,
        title,
        canvas=PORTRAIT,
        timeline_duration_s=30.0,
        duration_s=None,
        language=language,
        place=_place_with(),
    )
    assert rows is not None, info
    return rows


def _contract(title: str) -> CreatorRenderContract:
    return CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(
            TextRequirement(role="opening", text=title, duration_s=2.0),
            TextRequirement(role="any", text=title),
        )
    )


@pytest.mark.parametrize(("title", "language"), [(EN, None), (TR, "tr")], ids=["en", "tr"])
def test_requirements_read_a_stack_as_one_text(title, language):
    rows = styles.remap_to_route(stack(title, language), "talking")
    assert len(rows) >= 2
    prior = (
        TextRequirement(role="opening", text=title, duration_s=2.0),
        TextRequirement(role="any", text=title),
    )
    plain = {"opening-title": title}
    got = _text_requirements_for_save(rows, prior, plain, keep_shot_roles=True)
    assert got == (
        TextRequirement(role="opening", text=title, duration_s=2.0),
        TextRequirement(role="any", text=title),
    )
    # a second Save: the prior rows are the stack itself (ids differ per line)
    again = _text_requirements_for_save(
        rows,
        got,
        {r["id"]: r["text"] for r in intro_stack_as_one_row(rows)},
        keep_shot_roles=True,
    )
    assert again == got


def test_rows_that_are_not_an_intro_stack_are_untouched():
    plain = [{"id": "opening-title", "text": "Hello world", "start_s": 0.0, "end_s": 2.0}]
    assert intro_stack_as_one_row(plain) == plain
    typed = styles.remap_to_route(stack(EN), "talking")
    for row in typed:  # a typed " / " stack carries `plain`/`typed` styles, never `intro_*`
        row["source_params"]["title_look"]["style"] = "typed"
    assert intro_stack_as_one_row(typed) == typed
    prior = (TextRequirement(role="opening", text="Hello world", duration_s=2.0),)
    assert _text_requirements_for_save(
        plain, prior, {"opening-title": "Hello world"}, keep_shot_roles=True
    ) == (TextRequirement(role="opening", text="Hello world", duration_s=2.0),)


@pytest.mark.parametrize(("title", "language"), [(EN, None), (TR, "tr")], ids=["en", "tr"])
def test_narrated_pick_save_and_second_save_keep_the_contract(monkeypatch, title, language):
    job = nar.phone_job(monkeypatch, title=title)
    monkeypatch.setattr(gj.settings, "phone_narrated_title_edits_enabled", True)
    rows = styles.remap_to_route(stack(title, language), "narrated")
    nar.save(job, text_elements=rows)
    recipe = device_status(job, "narrated").request.recipe
    verify_phone_recipe(_contract(title), recipe)  # opening + any both read the stack
    # a second, unrelated Save keeps the very same title layers and the contract holds
    before = [layer for layer in recipe.text_layers if layer.id.startswith("title-")]
    variant = job.assembly_plan["variants"][0]
    gj.prepare_editor_commit(
        job,
        "narrated",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(variant),
            caption_cues=copy.deepcopy(nar._CUES),
        ),
        user_id="owner",
        plan_item_id="item",
    )
    after_recipe = device_status(job, "narrated").request.recipe
    assert [layer for layer in after_recipe.text_layers if layer.id.startswith("title-")] == before
    verify_phone_recipe(_contract(title), after_recipe)


def test_the_old_any_rule_refuses_the_stack_negative_control():
    """N6: with `any` reading only whole rows (KRI-1611's rule) the stacked title is refused."""
    from app.pipeline.phone_title_look import stacked_title_matches
    from app.services.creator_render_contract import text_matches

    title = EN
    evidence = [
        {
            "element_id": f"text-{i}",
            "text": row["text"],
            "start_s": row["start_s"],
            "end_s": row["end_s"],
            "anchor_y": row["y_frac"],
        }
        for i, row in enumerate(stack(title))
    ]
    assert not text_matches(evidence, title, role="any", tolerance_s=0.05)  # the old `any` refuses
    assert stacked_title_matches(evidence, title, tolerance_s=0.05)  # the new fallback reads it
