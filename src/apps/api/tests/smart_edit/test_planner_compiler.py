from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from app.agents.smart_edit_planner import (
    SmartEditPlannerAgent,
    SmartEditPlannerInput,
    SmartPlannerCandidate,
    SmartPlannerProposal,
)
from app.smart_edit.compiler import compile_smart_plan, resolve_sfx_placements
from app.smart_edit.planner import _merge_agent_with_guards, plan_smart_captions
from app.smart_edit.presets import load_preset
from app.smart_edit.schemas import (
    SMART_EDIT_SCHEMA_VERSION,
    SMART_EDIT_SCHEMA_VERSION_V2,
    SmartEditPlanDocument,
)


def _cue(text: str, start: float, end: float) -> dict:
    tokens = text.split()
    step = (end - start) / len(tokens)
    return {
        "text": text,
        "start_s": start,
        "end_s": end,
        "words": [
            {
                "text": token,
                "start_s": round(start + index * step, 3),
                "end_s": round(start + (index + 1) * step, 3),
            }
            for index, token in enumerate(tokens)
        ],
    }


def test_turkish_talking_head_compiles_semantic_caption_number_title_and_sfx_lanes() -> None:
    cues = [
        _cue("Peki o dönem neden markalar maskot kullanıyordu?", 0.0, 2.8),
        _cue("Birincisi Selocanlar markayı hatırlatıyordu.", 4.0, 6.2),
        _cue("Şimdi başka bir konuya geçelim.", 8.0, 10.0),
        _cue("İkincisi Çelik ve Çeliknaz güven veriyordu.", 12.0, 14.5),
        _cue("Örneğin Selpak fili çok güçlüydü.", 17.0, 19.0),
        _cue("Sonuç olarak karakter markayı akılda tutuyordu.", 22.0, 24.2),
        _cue("Takip et, devamını birlikte inceleyelim.", 27.0, 29.0),
    ]

    planned = plan_smart_captions(cues, preset_version="cigdem-v1", language="tr")

    assert planned is not None
    assert [event.role for event in planned.document.events] == [
        "hook",
        "list_item",
        "context_shift",
        "list_item",
        "example",
        "payoff",
        "cta",
    ]
    assert planned.validation_receipt["normalized_word_count"] == sum(
        len(cue["words"]) for cue in cues
    )

    compiled = compile_smart_plan(planned.document, planned.caption_cues)

    # The context-title span becomes authored text and is removed from the
    # baseline caption lane, preventing two copies of the same words.
    assert [cue.get("smart_style") for cue in compiled.caption_cues] == [
        "hook",
        "list_item",
        "list_item",
        "example",
        "payoff",
        "cta",
    ]
    assert [element["text"] for element in compiled.text_elements] == [
        "1",
        "Selocanlar",
        "Şimdi başka bir konuya geçelim.",
        "2",
        "Çelik",
    ]
    numbers = [element for element in compiled.text_elements if element["text"].isdigit()]
    assert all(element["y_frac"] == 0.085 for element in numbers)
    keywords = [element for element in compiled.text_elements if not element["text"].isdigit()]
    chapter_keywords = [
        element for element in keywords if element["text"] in {"Selocanlar", "Çelik"}
    ]
    assert all(element["y_frac"] == 0.62 for element in chapter_keywords)
    assert all(
        element["font_family"].startswith("Montserrat") for element in compiled.text_elements
    )
    assert [(intent["role"], intent["at_s"]) for intent in compiled.sfx_intents] == [
        ("chapter_number_pop", 3.95),
        ("keyword_typewriter_tick", 4.25),
        ("transition_whip", 8.2),
        ("chapter_number_pop", 11.95),
        ("keyword_typewriter_tick", 12.25),
        ("cta_click", 27.0),
    ]
    assert compiled.boundary_effects == [
        {
            "event_id": planned.document.events[2].event_id,
            "effect": "horizontal_motion_blur",
            "at_s": 8.0,
            "duration_s": 0.42,
            "blur_sigma": 44.0,
            "intensity": 1.0,
        }
    ]


def test_smart_sfx_never_falls_back_to_unrelated_voice_clip() -> None:
    intents = [
        {
            "event_id": "0" * 24,
            "role": "chapter_number_pop",
            "at_s": 5.0,
        }
    ]
    female_voice_only = [
        {
            "id": "voice",
            "name": "woman says wow",
            "audio_gcs_path": "sound-effects/voice/audio.mp3",
            "duration_s": 1.0,
        }
    ]
    assert resolve_sfx_placements(intents, female_voice_only) == []

    misleading_voice_match = [
        {
            "id": "voice-pop",
            "name": "female pop voice",
            "audio_gcs_path": "sound-effects/voice-pop/audio.mp3",
            "duration_s": 0.4,
        }
    ]
    assert resolve_sfx_placements(intents, misleading_voice_match) == []

    clean_pop = female_voice_only + [
        {
            "id": "pop",
            "name": "clean soft pop",
            "audio_gcs_path": "sound-effects/pop/audio.wav",
            "duration_s": 0.3,
            "role_tags": ["chapter_number_pop"],
            "contains_voice": False,
            "vocal_probability": 0.0,
            "manual_audit_status": "approved",
        }
    ]
    resolved = resolve_sfx_placements(intents, clean_pop)
    assert len(resolved) == 1
    assert resolved[0]["sound_effect_id"] == "pop"
    assert resolved[0]["gain"] == 0.48


def test_planner_returns_none_for_empty_caption_input() -> None:
    assert plan_smart_captions([], preset_version="cigdem-v1", language="tr") is None


def test_agent_parser_rejects_valid_word_ids_that_are_not_candidate_grounded() -> None:
    planner_input = SmartEditPlannerInput(
        words=[
            {"word_id": "w000001", "text": "Selocanlar", "normalized": "selocanlar"},
            {"word_id": "w000002", "text": "markayı", "normalized": "markayi"},
            {"word_id": "w000003", "text": "hatırlatır", "normalized": "hatirlatir"},
        ],
        candidates=[
            SmartPlannerCandidate(
                candidate_id="hook-w000001-0",
                role="hook",
                start_word_id="w000001",
                end_word_id="w000002",
                anchor_word_id="w000001",
                evidence="Selocanlar markayı",
            )
        ],
        assets=[],
        preset_id="cigdem",
        preset_version="v1",
        language="tr",
    )
    raw = json.dumps(
        {
            "proposals": [
                {
                    "role": "example",
                    "start_word_id": "w000002",
                    "end_word_id": "w000003",
                    "anchor_word_id": "w000002",
                    "rationale": "hallucinated smartphone danger",
                }
            ]
        }
    )

    parsed = SmartEditPlannerAgent(None).parse(raw, planner_input)

    assert parsed.proposals == []


def test_agent_merge_cannot_drop_grounded_visuals_or_required_sfx() -> None:
    fallback = SmartPlannerProposal(
        role="hook",
        start_word_id="w000001",
        end_word_id="w000002",
        anchor_word_id="w000001",
        confidence_tier="high",
        scene_token="hook_accumulation",
        visual_asset_ids=["selocan", "robots"],
        visual_anchor_word_ids={"selocan": "w000001", "robots": "w000002"},
        sfx_roles=["visual_enter_accent"],
        rationale="deterministic",
    )
    sparse_agent = fallback.model_copy(
        update={
            "scene_token": None,
            "visual_asset_ids": ["selocan"],
            "visual_anchor_word_ids": {"selocan": "w000001"},
            "sfx_roles": [],
            "rationale": "agent omitted decoration",
        }
    )

    merged = _merge_agent_with_guards([sparse_agent], [fallback])

    assert len(merged) == 1
    assert merged[0].scene_token == "hook_accumulation"
    assert merged[0].visual_asset_ids == ["selocan", "robots"]
    assert merged[0].visual_anchor_word_ids == {
        "selocan": "w000001",
        "robots": "w000002",
    }
    assert merged[0].sfx_roles == ["visual_enter_accent"]


def test_agent_merge_restores_omitted_grounded_candidates() -> None:
    hook = SmartPlannerProposal(
        role="hook",
        start_word_id="w000001",
        end_word_id="w000002",
        anchor_word_id="w000001",
        confidence_tier="high",
    )
    visual = SmartPlannerProposal(
        role="example",
        start_word_id="w000003",
        end_word_id="w000004",
        anchor_word_id="w000003",
        confidence_tier="high",
        scene_token="single_example",
        visual_asset_ids=["bear"],
        sfx_roles=["visual_enter_soft"],
    )

    merged = _merge_agent_with_guards([hook], [hook, visual])

    assert [(event.role, event.visual_asset_ids) for event in merged] == [
        ("hook", []),
        ("example", ["bear"]),
    ]


def test_chapter_marker_inside_declaration_cue_anchors_number_and_sfx_to_marker() -> None:
    cues = [
        _cue("Peki neden maskot kullanıyorlardı?", 0.0, 2.0),
        _cue("4 tane başlıkta anlatayım: 1.", 4.0, 7.0),
        _cue("Somutlaştırma. Markalar karakterle bağ kurar.", 7.05, 9.0),
        _cue("2. Pester Power yani ikna gücü.", 10.0, 13.0),
    ]

    planned = plan_smart_captions(
        cues,
        preset_version="v1",
        language="tr",
        use_agent=False,
    )

    assert planned is not None
    list_events = [event for event in planned.document.events if event.role == "list_item"]
    assert [event.anchor.word_id for event in list_events] == ["w000009", "w000015"]
    compiled = compile_smart_plan(planned.document, planned.caption_cues)
    numbers = [element for element in compiled.text_elements if element["text"].isdigit()]
    assert [element["text"] for element in numbers] == ["1", "2"]
    assert 6.3 < numbers[0]["start_s"] < 6.5
    assert numbers[1]["start_s"] == 10.0
    assert [element["text"] for element in compiled.text_elements if not element["text"].isdigit()][
        :1
    ] == ["Somutlaştırma"]
    chapter_pop_times = [
        intent["at_s"] for intent in compiled.sfx_intents if intent["role"] == "chapter_number_pop"
    ]
    assert 6.3 < chapter_pop_times[0] < 6.5
    assert chapter_pop_times[1] == 9.95


def test_visual_matching_handles_conservative_turkish_plural_suffixes() -> None:
    cues = [_cue("Selocanlar markayı hatırlatıyordu.", 0.0, 2.0)]
    assets = [
        {
            "id": "selocan-family",
            "kind": "image",
            "source_filename": "selocan_family.png",
            "analysis": {"subject": "Selocan family mascot"},
        }
    ]

    planned = plan_smart_captions(
        cues,
        preset_version="v1",
        language="tr",
        assets=assets,
        use_agent=False,
    )

    assert planned is not None
    visual_ids = [
        lane.asset_id
        for event in planned.document.events
        for lane in event.lanes
        if getattr(lane, "kind", None) == "visual"
    ]
    assert visual_ids == ["selocan-family"]


def test_hook_visual_pool_enters_on_each_spoken_name_and_holds_the_completed_collage() -> None:
    cues = [
        _cue("Selocanlar, Çelik Çelik, Naz, Pınar Beyaz, Bağırsak,", 1.24, 5.316),
        _cue("Selpak Fil, Coca-Cola Ayıcıkları...", 5.316, 7.645),
        _cue("Bunları hatırlıyor musunuz?", 7.645, 9.598),
    ]
    assets = [
        {
            "id": "asset-selocan",
            "kind": "image",
            "gcs_path": "users/u/plan/i/pool/selocan.png",
            "source_filename": "selocan.png",
            "analysis": {"subject": "Selocan family characters"},
        },
        {
            "id": "asset-robots",
            "kind": "image",
            "gcs_path": "users/u/plan/i/pool/robots.png",
            "source_filename": "robots_wedding.png",
            "analysis": {"subject": "Robot wedding couple"},
        },
        {
            "id": "asset-pinar",
            "kind": "image",
            "gcs_path": "users/u/plan/i/pool/pinar.png",
            "source_filename": "pinar_beyaz_sheep.png",
            "analysis": {"subject": "White sheep mascot"},
        },
        {
            "id": "asset-selpak",
            "kind": "image",
            "gcs_path": "users/u/plan/i/pool/selpak.png",
            "source_filename": "selpak-fil.png",
            "analysis": {"subject": "Selpak elephant character"},
        },
        {
            "id": "asset-cocacola",
            "kind": "image",
            "gcs_path": "users/u/plan/i/pool/cocacola.png",
            "source_filename": "cocacola_bear.png",
            "analysis": {"subject": "Polar bear", "brands": ["Coca-Cola"]},
        },
        {
            "id": "asset-unmentioned",
            "kind": "image",
            "gcs_path": "users/u/plan/i/pool/emocan.png",
            "source_filename": "emocan_single.png",
            "analysis": {"subject": "Yellow mascot with glasses"},
        },
    ]

    planned = plan_smart_captions(
        cues,
        preset_version="v1",
        language="tr",
        assets=assets,
        use_agent=False,
    )

    assert planned is not None
    compiled = compile_smart_plan(
        planned.document,
        planned.caption_cues,
        assets_by_id={asset["id"]: asset for asset in assets},
    )
    assert [overlay["src_gcs_path"] for overlay in compiled.media_overlays] == [
        "users/u/plan/i/pool/selocan.png",
        "users/u/plan/i/pool/robots.png",
        "users/u/plan/i/pool/pinar.png",
        "users/u/plan/i/pool/selpak.png",
        "users/u/plan/i/pool/cocacola.png",
    ]
    assert {overlay["composition_group_id"] for overlay in compiled.media_overlays} == {
        "hook_accumulation"
    }
    assert [overlay["start_s"] for overlay in compiled.media_overlays] == [
        1.24,
        1.822,
        3.569,
        5.316,
        6.48,
    ]
    assert {overlay["end_s"] for overlay in compiled.media_overlays} == {8.48}
    assert len({(overlay["x_frac"], overlay["y_frac"]) for overlay in compiled.media_overlays}) == 5
    assert all(overlay["entrance_token"] == "pop_in" for overlay in compiled.media_overlays)


def test_hook_zone_allocation_does_not_reset_across_separate_candidates() -> None:
    cues = [
        _cue("Selocanlar markanın ilk maskotlarıydı.", 0.0, 2.0),
        _cue("Coca-Cola ayıcıkları da hemen gelirdi.", 2.2, 4.8),
    ]
    assets = [
        {
            "id": "asset-selocan",
            "kind": "image",
            "gcs_path": "users/u/plan/i/pool/selocan.png",
            "source_filename": "selocan.png",
            "analysis": {"subject": "Selocanlar mascot"},
        },
        {
            "id": "asset-cocacola",
            "kind": "image",
            "gcs_path": "users/u/plan/i/pool/cocacola.png",
            "source_filename": "cocacola-bear.png",
            "analysis": {"subject": "Coca-Cola bear"},
        },
    ]

    planned = plan_smart_captions(
        cues,
        preset_version="v1",
        language="tr",
        assets=assets,
        use_agent=False,
    )

    assert planned is not None
    compiled = compile_smart_plan(
        planned.document,
        planned.caption_cues,
        assets_by_id={asset["id"]: asset for asset in assets},
    )
    hook_overlays = [
        overlay
        for overlay in compiled.media_overlays
        if overlay.get("composition_group_id") == "hook_accumulation"
    ]
    assert len(hook_overlays) == 2
    assert [(overlay["x_frac"], overlay["y_frac"]) for overlay in hook_overlays] == [
        (0.2, 0.12),
        (0.81, 0.81),
    ]
    assert [overlay["z"] for overlay in hook_overlays] == [10, 51]


def _stable_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _reference_hook_assets() -> list[dict]:
    return [
        {
            "id": "asset-selocan",
            "kind": "image",
            "gcs_path": "users/u/pool/selocan.png",
            "source_filename": "selocan.png",
            "analysis": {"subject": "Selocan family"},
        },
        {
            "id": "asset-robots",
            "kind": "image",
            "gcs_path": "users/u/pool/robots.png",
            "source_filename": "robots_wedding.png",
            "analysis": {"subject": "Robot wedding couple"},
        },
        {
            "id": "asset-pinar",
            "kind": "image",
            "gcs_path": "users/u/pool/pinar.png",
            "source_filename": "pinar_beyaz.png",
            "analysis": {"subject": "White sheep mascot"},
        },
        {
            "id": "asset-selpak",
            "kind": "image",
            "gcs_path": "users/u/pool/selpak.png",
            "source_filename": "selpak-fil.png",
            "analysis": {"subject": "Selpak elephant character"},
        },
        {
            "id": "asset-cocacola",
            "kind": "image",
            "gcs_path": "users/u/pool/cocacola.png",
            "source_filename": "cocacola-bear.png",
            "analysis": {"subject": "Polar bear", "brands": ["Coca-Cola"]},
        },
    ]


def test_v1_plan_and_compiled_patch_remain_byte_stable() -> None:
    cues = [
        _cue("Peki neden maskot kullanıyorlardı?", 0.0, 2.0),
        _cue("Birincisi somutlaştırma önemlidir.", 4.0, 6.0),
        _cue("Takip et şimdi.", 8.0, 9.0),
    ]

    planned = plan_smart_captions(
        cues,
        preset_version="v1",
        language="tr",
        use_agent=False,
    )

    assert planned is not None
    compiled = compile_smart_plan(planned.document, planned.caption_cues)
    assert planned.document.schema_version == SMART_EDIT_SCHEMA_VERSION
    assert _stable_digest(planned.document.model_dump(mode="json")) == (
        "333ca9f41e701cd433740a928546d3e4c3adcc2c651bd879d6230c9e717ab028"
    )
    assert _stable_digest(planned.caption_cues) == (
        "a91017032494269d6fdc4e19239162cbe9b6a27c927a27feda6232988af87666"
    )
    assert _stable_digest(compiled.compiled_patch) == (
        "b22ed7db36892cefe756f44d1d89b806d263f0ab74696bfb108304fba4660148"
    )


def test_v2_schema_accepts_typed_camera_and_audio_lanes_but_v1_rejects_them() -> None:
    event_id = "0" * 24
    payload = {
        "schema_version": SMART_EDIT_SCHEMA_VERSION_V2,
        "preset_id": "cigdem",
        "preset_version": "v2",
        "baseline_captions": [
            {
                "cue_id": "cue-1",
                "word_ids": ["w000001"],
                "display_text": "Peki",
            }
        ],
        "events": [
            {
                "event_id": event_id,
                "role": "hook",
                "start_word_id": "w000001",
                "end_word_id": "w000001",
                "anchor": {"word_id": "w000001"},
                "active_start_ms": 0,
                "active_end_ms": 500,
                "confidence_tier": "high",
                "lanes": [
                    {
                        "kind": "camera",
                        "token": "semantic_crop_pulse",
                        "intensity_token": "subtle",
                    },
                    {
                        "kind": "audio_treatment",
                        "token": "voice_safe_music_bed",
                        "selection_token": "licensed_published_match",
                        "gain_token": "preset",
                    },
                ],
            }
        ],
    }

    plan = SmartEditPlanDocument.model_validate(payload)
    assert [lane.kind for lane in plan.events[0].lanes] == ["camera", "audio_treatment"]

    payload["schema_version"] = SMART_EDIT_SCHEMA_VERSION
    with pytest.raises(ValidationError, match="schema v1"):
        SmartEditPlanDocument.model_validate(payload)


def test_v2_detects_all_production_chapters_before_caption_chunking() -> None:
    cues = [
        _cue("Selocanlar Çeliknaz Pınar Beyaz Selpak Fil Coca-Cola Ayıcıkları", 0.0, 7.8),
        _cue("Bunları hatırlıyor musunuz?", 7.8, 9.5),
        _cue("Peki bu karakterler neden bu kadar güçlüydü?", 9.6, 13.0),
        _cue("Dört başlıkta anlatayım: 1.", 13.0, 16.0),
        _cue("Somutlaştırma markayı akılda tutar.", 16.1, 18.0),
        _cue("Bu bölümün son sözü İki", 30.0, 33.0),
        _cue("Pester Power tüketiciyi etkiler.", 33.1, 36.0),
        _cue("Üç sosyal kanıt sağlar.", 45.0, 48.0),
        _cue("Dört nostalji yaratır.", 60.0, 63.0),
    ]

    planned = plan_smart_captions(
        cues,
        preset_version="v2",
        language="tr",
        assets=_reference_hook_assets(),
        use_agent=False,
    )

    assert planned is not None
    assert planned.document.schema_version == SMART_EDIT_SCHEMA_VERSION_V2
    assert planned.validation_receipt["detected_chapters"] == [1, 2, 3, 4]
    assert planned.validation_receipt["missing_chapters"] == []
    assert planned.validation_receipt["chapter_order_valid"] is True
    chapter_events = [event for event in planned.document.events if event.role == "list_item"]
    assert [
        next(lane.sequence_number for lane in event.lanes if lane.kind == "text")
        for event in chapter_events
    ] == [1, 2, 3, 4]
    assert all(len({cue["smart_role"]}) == 1 for cue in planned.caption_cues)
    flattened_words = [word for cue in planned.caption_cues for word in cue["words"]]
    assert all(
        float(left["start_s"]) <= float(left["end_s"]) <= float(right["start_s"])
        for left, right in zip(flattened_words, flattened_words[1:])
    )
    two_event = chapter_events[1]
    assert planned.normalized_words[int(two_event.start_word_id[1:]) - 1].display_text == "İki"

    compiled = compile_smart_plan(
        planned.document,
        planned.caption_cues,
        assets_by_id={asset["id"]: asset for asset in _reference_hook_assets()},
    )
    assert [element["text"] for element in compiled.text_elements if element["text"].isdigit()] == [
        "1",
        "2",
        "3",
        "4",
    ]
    assert "İki" not in " ".join(cue["text"] for cue in compiled.caption_cues)
    assert "Power tüketiciyi etkiler." in " ".join(cue["text"] for cue in compiled.caption_cues)


def test_v2_missing_chapter_does_not_poison_later_markers() -> None:
    cues = [
        _cue("Bunları hatırlıyor musunuz?", 0.0, 2.0),
        _cue("Dört başlıkta anlatayım: 1.", 3.0, 5.0),
        _cue("Somutlaştırma önemlidir.", 5.1, 6.5),
        _cue("Üç sosyal kanıt sağlar.", 12.0, 14.0),
        _cue("Dört nostalji yaratır.", 18.0, 20.0),
    ]

    planned = plan_smart_captions(
        cues,
        preset_version="v2",
        language="tr",
        use_agent=False,
    )

    assert planned is not None
    assert planned.validation_receipt["detected_chapters"] == [1, 3, 4]
    assert planned.validation_receipt["missing_chapters"] == [2]


def test_v2_semantic_hook_accumulates_all_visuals_and_suppresses_only_when_resolved() -> None:
    cues = [
        _cue("Selocanlar Çeliknaz Pınar Beyaz", 1.0, 5.0),
        _cue("Selpak Fil Coca-Cola Ayıcıkları", 5.0, 8.0),
        _cue("Bunları hatırlıyor musunuz?", 8.0, 10.0),
        _cue("Peki maskotlar neden işe yarıyordu?", 10.1, 13.0),
    ]
    assets = _reference_hook_assets()
    planned = plan_smart_captions(
        cues,
        preset_version="v2",
        language="tr",
        assets=assets,
        use_agent=False,
    )

    assert planned is not None
    hook_events = [
        event
        for event in planned.document.events
        if any(
            lane.kind == "visual" and lane.composition_group_id == "hook_accumulation"
            for lane in event.lanes
        )
    ]
    assert len(hook_events) == 5
    assert [event.active_start_ms for event in hook_events] == [1000, 2000, 3000, 5000, 6500]
    assert max(event.active_end_ms for event in hook_events) == 12000
    assert max(event.active_end_ms for event in hook_events) > 6000

    complete = compile_smart_plan(
        planned.document,
        planned.caption_cues,
        assets_by_id={asset["id"]: asset for asset in assets},
    )
    assert complete.compiled_patch["hook_caption_suppressed"] is True
    assert all(cue.get("smart_role") != "hook" for cue in complete.caption_cues)

    incomplete = compile_smart_plan(
        planned.document,
        planned.caption_cues,
        assets_by_id={asset["id"]: asset for asset in assets[:3]},
    )
    assert incomplete.compiled_patch["hook_caption_suppressed"] is False
    assert any(cue.get("smart_role") == "hook" for cue in incomplete.caption_cues)


def test_v2_scene_tokens_compile_exhaustively_and_emit_camera_audio_intents() -> None:
    cues = [
        _cue("Bunları hatırlıyor musunuz?", 0.0, 2.0),
        _cue("Peki örneklere bakalım.", 3.0, 4.5),
        _cue("Örneğin Selpak ile Coca-Cola birlikte çalışır.", 6.0, 9.0),
        _cue("Mesela demo videosu her şeyi gösterir.", 12.0, 15.0),
        _cue("Örneğin Teklogo tek başına görünür.", 18.0, 21.0),
    ]
    assets = [
        {
            "id": "selpak",
            "kind": "image",
            "gcs_path": "users/u/selpak.png",
            "source_filename": "selpak.png",
            "analysis": {"subject": "Selpak elephant"},
        },
        {
            "id": "coke",
            "kind": "image",
            "gcs_path": "users/u/coke.png",
            "source_filename": "cocacola.png",
            "analysis": {"subject": "Coca-Cola bear"},
        },
        {
            "id": "demo",
            "kind": "video",
            "gcs_path": "users/u/demo.mp4",
            "source_filename": "demo-video.mp4",
            "analysis": {"subject": "Demo video"},
        },
        {
            "id": "single",
            "kind": "image",
            "gcs_path": "users/u/single.png",
            "source_filename": "teklogo.png",
            "analysis": {"subject": "Teklogo"},
        },
        {
            "id": "badge",
            "kind": "image",
            "gcs_path": "users/u/badge.png",
            "source_filename": "skip-ad-badge.png",
            "analysis": {"subject": "Skip ad badge"},
        },
    ]

    planned = plan_smart_captions(
        cues,
        preset_version="v2",
        language="tr",
        assets=assets,
        use_agent=False,
    )

    assert planned is not None
    compiled = compile_smart_plan(
        planned.document,
        planned.caption_cues,
        assets_by_id={asset["id"]: asset for asset in assets},
    )
    preset = load_preset("cigdem", "v2")
    assert set(preset.scene_layouts) == {
        "hook_accumulation",
        "single_example",
        "example_pair",
        "fullscreen_cutaway",
        "persistent_badge",
    }
    overlays_by_path = {overlay["src_gcs_path"]: overlay for overlay in compiled.media_overlays}
    assert overlays_by_path["users/u/selpak.png"]["x_frac"] == 0.23
    assert overlays_by_path["users/u/coke.png"]["x_frac"] == 0.77
    assert overlays_by_path["users/u/demo.mp4"]["display_mode"] == "fullscreen"
    assert overlays_by_path["users/u/single.png"]["x_frac"] == 0.77
    assert overlays_by_path["users/u/badge.png"]["composition_group_id"] == "persistent_badge"
    assert compiled.audio_treatment_intents[0]["music_match_min_score"] == 7.0
    assert compiled.camera_intents
    assert any(intent["role"] == "keyword_typewriter_tick" for intent in compiled.sfx_intents)
