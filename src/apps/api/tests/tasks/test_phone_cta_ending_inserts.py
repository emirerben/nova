"""Item 14's ending when a clip is INSERTED inside the last 3 s (Parent KRI-1151).

The take pauses for the inserted clip, so the ending's rows are split around it. The approved
contract must still find the creator's exact closing text, and the arrow must never play over
the inserted clip while the text is hidden there. Replays prod thread 6fa36e51's 58 s take
(`talking_show_when_mentioned_6fa36e51.json`) with the congress talk inserted after
"kötü sonuçlar" (ends 56.06 s, inside the last 3 s), through `_run_generative_job`.
"""

from __future__ import annotations

from app.services.creator_render_contract import (
    CreatorRenderContract,
    TextRequirement,
    verify_phone_recipe,
)
from tests.tasks.test_talking_show_when_mentioned import (
    _FEATURES,
    CONGRESS_TALK,
    CREATOR,
    _run,
    _tracks,
)

_CTA = "comment READ"


def test_an_insert_inside_the_ending_keeps_the_contract_and_the_arrow_off_the_clip(monkeypatch):
    overrides = {
        "phone_talking_insert_clips_enabled": True,
        "phone_render_verified_features": [*_FEATURES, "authoredText", "freehandDrawing"],
    }
    from app.tasks import generative_build as gb

    if "creator_cta_ending_enabled" in type(gb.settings).model_fields:
        overrides["creator_cta_ending_enabled"] = True
    _job, recipe, _variant = _run(
        monkeypatch,
        mention=True,
        strategy={
            "insert_clips": [
                {"media_id": CONGRESS_TALK, "at_phrase": "kötü sonuçlar", "keep_audio": True}
            ],
            "closing_title": _CTA,
        },
        settings_overrides=overrides,
        extra_candidates={"creator_client_protocol": 35},
    )
    main = _tracks(recipe)["subtitled"].clips
    assert [c.source_asset_id for c in main] == [CREATOR, CONGRESS_TALK, CREATOR]
    insert = main[1]
    contract = CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="closing", text=_CTA),)
    )
    assert verify_phone_recipe(contract, recipe)
    insert_end = insert.timeline_start + insert.source_duration
    for layer in recipe.drawing_layers:
        assert layer.end <= insert.timeline_start + 1e-3 or layer.start >= insert_end - 1e-3
