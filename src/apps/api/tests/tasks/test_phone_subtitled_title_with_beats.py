"""KRI-851: a phone Talking edit keeps the creator's hook title next to its reaction beats.

The Main Creator prompt's beats section used to say the edit "stays `subtitled` with no
`opening_title`" (a KRI-178 leftover from before the subtitled compiler drew titles), so a
hook title plus photos on spoken words ("POV: the waiter explains the sauces" + two photos)
could lose its title. The prompt now asks for both together
(``test_reaction_beats_prompt_keeps_the_creators_title``). No other test pairs
``opening_title`` with non-empty ``reaction_beats``, so these pin the rest of the path: the
runtime v2 policy lets both through untouched, and the phone worker draws the exact title
row beside both beat cards, which the recipe verifier accepts as the pinned opening text.
Only beat grounding (a model + transcript lookup) is stubbed, as in the KRI-178 / KRI-514
worker tests.
"""

from __future__ import annotations

import app.services.phone_overlay_grounding as phone_overlay_grounding_mod
import app.services.phone_reaction_grounding as phone_reaction_grounding_mod
import app.services.phone_visuals as phone_visuals_mod
from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.strategy_policy import CheckedStrategy, check_strategy_for_runtime_v2
from app.services import creator_capabilities as capabilities
from app.services.creator_render_contract import (
    CreatorRenderContract,
    TextRequirement,
    verify_phone_recipe,
)
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _basic_beat_receipt,
    _beat_dict,
    _beat_grounding_mock,
    _enable_beats,
    _grounding_mock,
    _make_fake_bind,
    _photo_card,
    _setup_subtitled,
)
from tests.tasks.test_phone_subtitled_title_worker import _faces

_TITLE = "POV: the waiter explains the sauces"
_CLIP = "analysis-proxy-ios-1.mp4"
_GRILL = "asset-grill-photo"
_CHIMICHURRI = "asset-chimichurri-photo"
_BEATS = [
    _beat_dict("argentinian", "Argentinian", visual_id=_GRILL, visual_role="photo"),
    _beat_dict("chimichurri", "chimichurri", visual_id=_CHIMICHURRI, visual_role="photo"),
]


def test_the_policy_keeps_the_hook_title_and_both_beats(prod_profile) -> None:
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-don-julio-t2",
        edit_format="subtitled",
        media=[
            {"media_id": _CLIP, "kind": "video", "duration_s": 12.0},
            {"media_id": _GRILL, "kind": "image"},
            {"media_id": _CHIMICHURRI, "kind": "image"},
        ],
        phone_source_media_ids=[_CLIP],
        phone_rendering_allowed=True,
    )
    strategy = CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        render_program="native",
        selected_media_ids=[_CLIP],
        opening_title=_TITLE,
        reaction_beats=_BEATS,
    )

    checked = check_strategy_for_runtime_v2(
        manifest, strategy, ask_before_simplifying=True, ask_about_stated_settings=True
    )

    assert isinstance(checked, CheckedStrategy), checked
    assert checked.notices == ()
    assert checked.strategy.opening_title == _TITLE
    assert [(b.beat_id, b.visual_id) for b in checked.strategy.reaction_beats or []] == [
        ("argentinian", _GRILL),
        ("chimichurri", _CHIMICHURRI),
    ]


def test_the_worker_draws_the_hook_title_beside_both_beat_cards(monkeypatch) -> None:
    _faces(monkeypatch)
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    _enable_beats(monkeypatch)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    cards = [
        _photo_card("beat-argentinian-1", _GRILL, start_s=0.8, end_s=2.4),
        _photo_card("beat-chimichurri-1", _CHIMICHURRI, start_s=5.0, end_s=7.0),
    ]
    receipt = _basic_beat_receipt(
        placed=[
            {
                "beat_id": "argentinian",
                "trigger": "Argentinian",
                "at_s": 0.8,
                "end_s": 2.4,
                "visual_label": "grill.jpg",
            },
            {
                "beat_id": "chimichurri",
                "trigger": "chimichurri",
                "at_s": 5.0,
                "end_s": 7.0,
                "visual_label": "chimichurri.jpg",
            },
        ]
    )
    beat_grounding = _beat_grounding_mock(cards, [], receipt)
    monkeypatch.setattr(phone_reaction_grounding_mod, "ground_phone_reaction_beats", beat_grounding)
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", _grounding_mock([])
    )
    job.all_candidates["creator_strategy"] = {"opening_title": _TITLE, "reaction_beats": _BEATS}

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    beat_grounding.assert_called_once()
    variant = job.assembly_plan["variants"][0]
    # The exact title survives as an editable row, and both beat lanes survived with it.
    rows = variant.get("text_elements", [])
    assert [r["text"] for r in rows if r["id"] == "opening-title"] == [_TITLE]
    assert variant["text_elements_materialized_from"] == "opening_title"
    assert variant["phone_lane_receipt"]["applied"] == ["overlays"]
    assert variant["phone_lane_receipt"]["dropped"] == []
    assert [e["beat_id"] for e in variant["phone_beat_receipt"]["placed"]] == [
        "argentinian",
        "chimichurri",
    ]
    # The compiled recipe carries the title from t=0 and both photo cards.
    recipe = device_status(job, "subtitled").request.recipe
    title_layers = [
        layer
        for layer in recipe.text_layers
        if " ".join(" ".join(run.text for run in layer.runs).split()) == _TITLE
    ]
    assert [layer.start for layer in title_layers] == [0.0]
    assert sum(len(t.clips) for t in recipe.tracks if t.id != "subtitled") == 2
    # What approval runs on the recipe: raises a decline if the pinned opening text is missing.
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="opening", text=_TITLE),)
    )
    verify_phone_recipe(contract, recipe)
