"""KRI-1611: an intro scene through the REAL `_run_phone_subtitled_job` and the approval check.

Round 2 of the review found what the row-level tests could not see: the phone draws a caps face
(`grotesk_hand`, `grotesk_pen`) in capitals, so the compiled layer text is "BU YILIN" while her
approved title is "bu yılın", and `verify_phone_recipe` refused the edit instead of falling back.
These run the real worker and the real recipe compile; only the transcript and the face sampler
are stubbed (the harness of `test_title_look_worker.py`).

Ways it could fail (written before the fix):
W1 a font pair that draws capitals makes the approval check refuse her title (EN and TR);
W2 the same check wrongly accepts a changed, missing or reordered word once case is ignored;
W3 a scene drawn for one title passes as another title;
W4 a creator's whole-title rewrite leaves old scene lines, or repeats the new title per line.
"""

from __future__ import annotations

import pytest

from app.pipeline import intro_scene as scene_mod
from app.pipeline import intro_scene_pick as pick
from app.services.creator_render_contract import (
    CreatorRenderContract,
    TextRequirement,
    verify_phone_recipe,
)
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled
from tests.tasks.test_phone_subtitled_title_worker import _faces

EN = "the biggest design trend right now"
TR = "bu yılın en büyük tasarım trendi şu an"


def _scene_job(monkeypatch, title: str, font_pair: str, language: str):
    _faces(monkeypatch)
    monkeypatch.setattr(gb.settings, "title_look_enabled", True)
    job, *_ = _setup_subtitled(monkeypatch)
    job.all_candidates["language"] = language
    job.all_candidates["creator_strategy"] = {"opening_title": title}
    menu = scene_mod.issue_menu(title)
    assert font_pair in menu.font_pairs
    job.assembly_plan = {
        **job.assembly_plan,
        pick.INTRO_SCENE_ASK_FIELD: {
            "version": pick.INTRO_SCENE_VERSION,
            "seed": "w",
            "inks": list(scene_mod.INKS),
            "candidates": [
                {
                    "voices": list(pick.accent_splits(title)[0]),
                    "font_pair": font_pair,
                    "entrance": menu.entrances_for(font_pair)[0],
                    "zones": ["above_head", "top_left", "mid_frame"],
                }
            ],
        },
    }
    return job


def _title_rows(job):
    return [
        row
        for row in job.assembly_plan["variants"][0]["text_elements"]
        if row["id"].startswith("opening-title")
    ]


def _check(job, title: str):
    recipe = device_status(job, "subtitled").request.recipe
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="opening", text=title),)
    )
    return verify_phone_recipe(contract, recipe)


@pytest.mark.parametrize("font_pair", sorted(scene_mod.FONT_PAIRS))
@pytest.mark.parametrize(("title", "language"), [(EN, "en"), (TR, "tr")], ids=["en", "tr"])
def test_every_issued_pair_draws_a_title_the_approval_check_accepts(
    monkeypatch, font_pair, title, language
):
    if font_pair not in scene_mod.issue_menu(title).font_pairs:
        pytest.skip("this pair cannot draw the title; the menu never issues it")
    job = _scene_job(monkeypatch, title, font_pair, language)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device", job.error_detail
    rows = _title_rows(job)
    assert len(rows) >= 2, "the scene was drawn, not today's single row"
    assert " ".join(row["text"] for row in rows) == title  # her words, never re-cased in storage
    _check(job, title)  # raises the typed decline when her words are not found


@pytest.mark.parametrize("font_pair", ["grotesk_hand", "grotesk_pen"])
@pytest.mark.parametrize(("title", "language"), [(EN, "en"), (TR, "tr")], ids=["en", "tr"])
def test_a_caps_pair_still_refuses_a_different_title(monkeypatch, font_pair, title, language):
    if font_pair not in scene_mod.issue_menu(title).font_pairs:
        pytest.skip("this pair cannot draw the title")
    job = _scene_job(monkeypatch, title, font_pair, language)
    gb._run_generative_job(str(job.id))
    words = title.split()
    for wrong in (
        " ".join(words[:-1]),  # a word missing
        " ".join([*words[:-1], "later"]),  # a word changed
        " ".join(reversed(words)),  # reordered
    ):
        with pytest.raises(Exception):  # noqa: B017 - the typed decline
            _check(job, wrong)
