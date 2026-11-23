"""KRI-1585 acceptance finding 1: A1's capitals follow the copy's language on the writer's rows.

The Kyoto research copy goes through the real writer path (`run_researched_revision`, the
captured A2 answer, whose heading and detail shape A1 shares), the real ops onto the Kyoto
slots, the real resolver, then the cloud burn's overlays and the phone compile. Nothing here
reads `designed_rows.json`, whose rows already carry `text_language`.

Ways this can fail, written before the code:
1. The writer's rows carry no language, so A1 upper-cases Turkish with English rules:
   "Hozukyo Geçidi" -> "HOZUKYO GEÇIDI" (the acceptance export at 28.2 s).
2. The language reaches fresh rows but not rows replayed from an approval
   (`pinned_research_operations`), so the approved render loses it.
3. Designed text layers (a translated or word-sequence ask) keep no language.
4. A legacy (no design) row or approval gains the key, so flag-off dumps and digests move.
"""

from __future__ import annotations

import json

from app.agents._schemas.text_element import TextElement
from app.config import settings
from app.pipeline.canvas import PORTRAIT
from app.pipeline.generative_overlays import build_overlays_from_text_elements
from app.pipeline.phone_text_style import resolve_phone_text_style
from app.pipeline.portable_text_layout import compile_text_overlay
from app.schemas.research import (
    PreparedResearch,
    PreparedResearchLayer,
    ReviewedClip,
)
from app.services.client_protocol import INFORMATIVE_TEXT_DESIGNS_PROTOCOL, set_client_protocol
from app.services.researched_text import (
    layer_operations,
    pinned_layer_operations,
    pinned_research_operations,
    prepared_evidence_facts,
    prepared_research_texts,
)
from tests.pipeline import test_kri1580_kyoto_research_copy as kyoto


def _a1_writer_rows(monkeypatch) -> tuple[list[dict], list[dict]]:
    kyoto._stub_agents(monkeypatch, [kyoto._designed("A2").model_dump()])
    ops = kyoto._run("A1").ops
    return ops, kyoto._rows(ops)


def _drawn(rows: list[dict]) -> tuple[list[str], list[str]]:
    """What the cloud burn and the phone compile draw for each row."""
    policy = kyoto._load("plan.json")["variant"]["phone_text_style"]
    styled = resolve_phone_text_style(
        rows,
        preferences=policy["preferences"],
        requested=policy["requested"],
        selected_fonts=policy["selected_fonts"],
        informative=True,
        shapes=True,
    )
    overlays = build_overlays_from_text_elements(
        [TextElement.model_validate(row) for row in styled],
        video_duration_s=60,
        independent_box_alignment=True,
    )
    burned = [str(overlay["text"]) for overlay in overlays]
    phone = [
        "".join(
            run.text
            for run in compile_text_overlay(overlay, layer_id=f"text-{i}", canvas=PORTRAIT)[0].runs
        )
        for i, overlay in enumerate(overlays)
    ]
    return burned, phone


def test_a1_turkish_headings_from_the_writer_cap_with_the_dotted_i(monkeypatch):
    monkeypatch.setattr(settings, "informative_text_designs_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "textDesigns"],
    )
    ops, rows = _a1_writer_rows(monkeypatch)

    set_client_protocol(INFORMATIVE_TEXT_DESIGNS_PROTOCOL)
    try:
        burned, phone = _drawn(rows)
    finally:
        set_client_protocol(None)
    for drawn in (burned, phone):
        joined = "\n".join(drawn).replace("\n", " ")
        assert "HOZUKYO GEÇİDİ" in joined and "7.3 KİLOMETRELİK HAT" in joined, drawn
        assert "GEÇIDI" not in joined and "KILOMETRELIK" not in joined
    assert {row.get("text_language") for row in rows} == {"tr"}

    approved = PreparedResearch(
        status="ready",
        requirement_id="research",
        topic="Sagano Romantik Treni",
        request_quote=kyoto._request()[:400],
        basis_digest="0" * 64,
        texts=prepared_research_texts(ops),
        retrieved_at=kyoto._evidence().retrieved_at,
        search_entry_html=kyoto._evidence().search_entry_html,
        evidence_facts=prepared_evidence_facts(ops),
    )
    replayed = pinned_research_operations(
        PreparedResearch.model_validate(approved.model_dump(mode="json")), kyoto._snapshot()
    )
    assert replayed == ops


def test_designed_text_layers_carry_their_language_and_legacy_layers_do_not():
    slots = kyoto._snapshot()["slots"]
    first = slots[0]
    clip = ReviewedClip(
        media_id=str(first["media_id"]),
        output_start_s=float(first["output_start_s"]),
        output_end_s=float(first["output_end_s"]),
    )
    facts = kyoto._evidence().facts[:1]
    layers = [
        PreparedResearchLayer(
            text=text,
            language=language,
            position="bottom",
            start_s=0.0,
            end_s=2.0,
            translation_of=translation_of,
            clips=[clip],
            sources=facts[0].sources,
            facts=facts,
        )
        for text, language, translation_of in (
            ("Hozukyo Geçidi boyunca giden hat", "tr", None),
            ("The line runs through the Hozukyo Gorge", "EN", 0),
        )
    ]
    designed = layer_operations(
        layers, slots, retrieved_at="", search_entry_html="", evidence_facts=facts
    )
    assert [op["research"].get("text_language") for op in designed] == ["tr", "en"]
    legacy = layer_operations(layers, slots, retrieved_at="", search_entry_html="")
    assert not [op for op in legacy if "text_language" in json.dumps(op)]
    approved = PreparedResearch(
        status="ready",
        requirement_id="research",
        topic="Sagano Romantik Treni",
        request_quote=kyoto._request()[:400],
        basis_digest="0" * 64,
        layers=layers,
        evidence_facts=facts,
    )
    snapshot = {"slots": slots, "total_duration_s": 60.0}
    assert pinned_layer_operations(approved, snapshot) == designed
