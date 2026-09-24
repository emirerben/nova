"""End-to-end lane check for KRI-178: real phrase matching + real geometry
arbitration -> real `compile_phone_subtitled_plan` -> real
`validate_phone_pilot_recipe`, on the KRI-172 football scenario.

The per-module tests stub the neighbours; this one proves the lane models
the grounding module emits are exactly what the compiler accepts, and that
the compiled recipe passes the same pilot validation the worker runs.
"""

from __future__ import annotations

import app.services.phone_overlay_grounding as pg
import app.services.phone_reaction_grounding as rg
from app.config import settings
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneVisualBinding
from app.services.sfx_catalog import SfxEntry
from tests.pipeline.test_phone_subtitled_plan import _binding, _resolved_sfx

_IDS = {
    "rank3": "11111111-1111-1111-1111-111111111111",
    "greenwood": "22222222-2222-2222-2222-222222222222",
    "reject_x": "33333333-3333-3333-3333-333333333333",
    "leao": "44444444-4444-4444-4444-444444444444",
    "check": "55555555-5555-5555-5555-555555555555",
    "salah": "66666666-6666-6666-6666-666666666666",
    "goat": "77777777-7777-7777-7777-777777777777",
}


def _asset(key: str) -> dict:
    return {
        "id": _IDS[key],
        "gcs_path": f"users/u1/plan/item1/pool/{key}.png",
        "gcs_generation": "9",
        "kind": "image",
        "source_filename": f"{key}.png",
        "duration_s": None,
        "aspect": 1.0,
        "user_context": "",
        "analysis": {},
    }


def _visual(key: str) -> PhoneVisualBinding:
    return PhoneVisualBinding(
        media_id=_IDS[key],
        gcs_path=f"users/u1/plan/item1/pool/{key}.png",
        generation="9",
        sha256="b" * 64,
        byte_count=2048,
    )


def _words() -> list[dict]:
    spoken = (
        "number three mason greenwood no no way rafael leão yes number one mohamed salah the goat"
    ).split()
    return [
        {"text": w, "start_s": 0.5 * i, "end_s": 0.5 * i + 0.4, "confidence": 1.0}
        for i, w in enumerate(spoken)
    ]


_BEATS = [
    {"beat_id": "rank-three", "trigger": "number three", "visual_id": f"asset-{_IDS['rank3']}"},
    {
        "beat_id": "greenwood-photo",
        "trigger": "Mason Greenwood",
        "visual_id": f"asset-{_IDS['greenwood']}",
        "visual_role": "photo",
    },
    {
        "beat_id": "greenwood-no",
        "trigger": "no",
        "after": "Mason Greenwood",
        "visual_id": f"asset-{_IDS['reject_x']}",
        "sound": "buzzer",
    },
    {
        "beat_id": "leao-check",
        "trigger": "Rafael Leão",
        "visual_id": f"asset-{_IDS['check']}",
        "sound": "ding",
    },
    {
        "beat_id": "salah-photo",
        "trigger": "Salah",
        "visual_id": f"asset-{_IDS['salah']}",
        "visual_role": "photo",
    },
    {"beat_id": "icardi", "trigger": "Icardi", "visual_id": f"asset-{_IDS['goat']}"},
]

_CLOSING = {
    "visual_id": f"asset-{_IDS['salah']}",
    "badge_visual_id": f"asset-{_IDS['goat']}",
    "from_trigger": "Salah",
}


def test_football_beats_ground_compile_and_validate(monkeypatch):
    duration_s = 8.0
    monkeypatch.setattr(rg, "_load_ready_pool_assets", lambda *a, **k: [_asset(k) for k in _IDS])
    monkeypatch.setattr(
        rg,
        "_load_sfx_entries",
        lambda *a, **k: [
            SfxEntry(id="sfx-buzzer", name="Wrong buzzer", category=None, search_terms=("buzzer",)),
            SfxEntry(id="sfx-ding", name="Correct ding", category=None, search_terms=("ding",)),
        ],
    )
    # Real caption-band + face arbitration, with no faces detected.
    monkeypatch.setattr(pg, "sample_face_regions", lambda *a, **k: ([], {}))

    grounded = rg.ground_phone_reaction_beats(
        lambda: None,
        job_id="job-1",
        beats=_BEATS,
        closing=_CLOSING,
        words=_words(),
        duration_s=duration_s,
        clip_path="/tmp/clip.mp4",
    )

    receipt = grounded.receipt
    placed_ids = {entry["beat_id"] for entry in receipt["placed"]}
    assert {
        "rank-three",
        "greenwood-photo",
        "greenwood-no",
        "leao-check",
        "salah-photo",
    } <= placed_ids
    assert receipt["unplaced"] == [
        {"beat_id": "icardi", "trigger": "Icardi", "reason": "never_heard"}
    ]
    assert receipt["closing"]["status"] == "placed"
    assert receipt["closing"]["badge"] == "placed"
    # "no" after Mason Greenwood is the first "no" AFTER the name (t=2.0), not any earlier word.
    greenwood_no = next(e for e in receipt["placed"] if e["beat_id"] == "greenwood-no")
    assert greenwood_no["at_s"] >= 2.0
    assert {sfx.catalog_id for sfx in grounded.sound_effects} == {"sfx-buzzer", "sfx-ding"}
    # The Salah photo beat is merged into the closing shot: held to the clip end.
    salah_cards = [c for c in grounded.cards if c.media_id == _IDS["salah"]]
    assert len(salah_cards) == 1 and salah_cards[0].end_s == duration_s
    # Creator-safe: no storage identity ever leaves the receipt.
    assert "gcs_path" not in str(receipt) and "generation" not in str(receipt)

    # Same models straight into the real compiler + the worker's pilot validation.
    referenced = {c.media_id for c in grounded.cards}
    visuals = tuple(_visual(k) for k in _IDS if _IDS[k] in referenced)
    resolved_sfx = [_resolved_sfx(request=req) for req in grounded.sound_effects]
    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=duration_s),),
        caption_cues=[{"text": "number three", "start_s": 0.0, "end_s": 1.0}],
        visuals=visuals,
        lanes=PhoneSubtitledLanes(overlays=grounded.cards, sound_effects=resolved_sfx),
    )
    overlay_track = next(t for t in recipe.tracks if t.id == "subtitled-overlays")
    assert len(overlay_track.clips) == len(grounded.cards)
    assert next(t for t in recipe.tracks if t.id == "sfx").clips
    monkeypatch.setattr(settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe, allow_editor_media=True)
