"""KRI-1307: "zoom into the recordings so the text is readable" is judged on its own.

Q2 run 1 dropped the zoom ask from the receipt while "full screen" said Done. Failure modes pinned:

 1. an aimed (zoomed) render says so and stays "met";
 2. a render the worker did NOT aim is "partial" and says small text may stay small;
 3. a draft (nothing recorded yet) promises nothing it cannot confirm and keeps its status;
 4. the creator's words alone never trigger it (KRI-667: only the typed fact counts);
 5. an editor Save's own document decides (focus present = aimed).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from app.kria.brief import STICKY_FACTS, BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    SpeakerBackgroundFact,
    build_receipts,
    plan_facts_from_phone_variant,
)

ZOOM = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description="Recordings full screen, zoomed so the text is readable",
    facts={"speaker_background": True, "background_zoom": True},
)
WORDS_ONLY = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description="Recordings full screen, zoomed so the text is readable",
    facts={"speaker_background": True},
)


def _variant(aimed: bool | None) -> dict[str, Any]:
    background: dict[str, Any] = {"windows": 2, "media": 2, "speaker_frame": None}
    if aimed is not None:
        background["aimed"] = aimed
    return {
        "variant_id": "subtitled",
        "render_destination": "device",
        "resolved_archetype": "subtitled",
        "edit_format": "subtitled",
        "caption_cues": [{"text": "Hi.", "start_s": 0.0, "end_s": 1.0}],
        "text_elements": [],
        "phone_lane_receipt": {"applied": ["background"], "dropped": [], "background": background},
    }


def _receipt(req: BriefRequirement, facts: PlanFacts):
    (receipt,) = build_receipts([req], facts, include_unchecked=True)
    return receipt


def test_aimed_render_is_met_and_says_zoomed():
    receipt = _receipt(ZOOM, plan_facts_from_phone_variant(_variant(True)))
    assert receipt.status == "met"
    assert "zoomed in" in receipt.reason


def test_unaimed_render_is_partial_not_a_quiet_met():
    receipt = _receipt(ZOOM, plan_facts_from_phone_variant(_variant(False)))
    assert receipt.status == "partial"
    assert "small text may stay small" in receipt.reason


def test_draft_does_not_claim_the_zoom():
    fact = SpeakerBackgroundFact(
        "planned", pool_count=2, fill_id="a", window_count=2, media_count=2, windows_open=True
    )
    facts = dataclasses.replace(
        PlanFacts(), edit_format="subtitled", phone_talking=True, speaker_background=fact
    )
    receipt = _receipt(ZOOM, facts)
    assert receipt.status == "met"
    assert "can't confirm" in receipt.reason


def test_words_alone_do_not_trigger_it():
    receipt = _receipt(WORDS_ONLY, plan_facts_from_phone_variant(_variant(False)))
    assert receipt.status == "met"
    assert "zoom" not in receipt.reason.lower()


def test_editor_document_decides():
    variant = _variant(False)
    variant["speaker_background"] = {
        "media_id": "m1",
        "windows": [{"media_id": "m1", "focus": {"x": 0.0, "y": 0.5, "zoom": 1.5}}],
    }
    receipt = _receipt(ZOOM, plan_facts_from_phone_variant(variant))
    assert "zoomed in" in receipt.reason


def test_fact_survives_a_reworded_requirement():
    assert "background_zoom" in STICKY_FACTS
