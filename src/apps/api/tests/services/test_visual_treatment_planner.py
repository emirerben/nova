from __future__ import annotations

from app.agents.visual_treatment_planner import (
    RawVisualTreatment,
    VisualTreatmentPlannerInput,
)
from app.services.visual_treatment_planner import (
    build_visual_treatments,
    infer_structured_section_treatments,
)


def _assets(count: int = 5) -> dict[str, dict]:
    return {
        f"asset-{index}": {
            "id": f"asset-{index}",
            "gcs_path": f"users/u/plan/p/pool/{index}.jpg",
            "kind": "image",
        }
        for index in range(count)
    }


def test_materializes_montage_and_transcript_card_as_editable_objects() -> None:
    proposals = [
        RawVisualTreatment(
            kind="montage",
            purpose="hook",
            start_s=0,
            end_s=3,
            asset_ids=list(_assets()),
            rationale="Hook evidence",
            confidence="high",
        ),
        RawVisualTreatment(
            kind="text_card",
            purpose="key_argument",
            start_s=8,
            end_s=10,
            text="Talent alone was not enough.",
            rationale="Isolate the argument",
            confidence="high",
        ),
    ]
    blocks, elements = build_visual_treatments(
        proposals,
        assets_by_id=_assets(),
        duration_s=30,
        transcript_text="Talent alone was not enough. The work mattered too.",
    )
    assert [block["kind"] for block in blocks] == ["montage", "text_card"]
    assert blocks[0]["origin"] == "ai"
    assert len(blocks[0]["shots"]) == 5
    assert elements[0]["visual_block_id"] == blocks[1]["id"]
    assert elements[0]["text"] == "Talent alone was not enough."


def test_insufficient_assets_drops_montage() -> None:
    proposal = RawVisualTreatment(
        kind="montage",
        purpose="context",
        start_s=0,
        end_s=3,
        asset_ids=["asset-0", "asset-1"],
        confidence="high",
    )
    blocks, elements = build_visual_treatments([proposal], assets_by_id=_assets(2), duration_s=20)
    assert blocks == []
    assert elements == []


def test_caps_repetitive_text_cards() -> None:
    proposals = [
        RawVisualTreatment(
            kind="text_card",
            purpose="transition",
            start_s=float(index * 4),
            end_s=float(index * 4 + 1),
            text=f"Card {index}",
            confidence="high",
        )
        for index in range(8)
    ]
    blocks, _elements = build_visual_treatments(
        proposals,
        assets_by_id={},
        duration_s=60,
        transcript_text=" ".join(f"Card {index}" for index in range(8)),
    )
    assert len(blocks) <= 4
    assert sum(block["end_s"] - block["start_s"] for block in blocks) <= 60 * 0.18


def test_drops_text_card_copy_not_supported_by_transcript() -> None:
    proposal = RawVisualTreatment(
        kind="text_card",
        purpose="statistic",
        start_s=2,
        end_s=4,
        text="Revenue grew by 200 percent",
        confidence="high",
    )
    blocks, elements = build_visual_treatments(
        [proposal],
        assets_by_id={},
        duration_s=20,
        transcript_text="Revenue grew, but no exact figure was provided.",
    )
    assert blocks == []
    assert elements == []


def test_accepts_concise_numeric_formatting_of_spoken_statistic() -> None:
    proposal = RawVisualTreatment(
        kind="text_card",
        purpose="statistic",
        start_s=2,
        end_s=4,
        text="Retention rose 30%",
        confidence="high",
    )
    blocks, _elements = build_visual_treatments(
        [proposal],
        assets_by_id={},
        duration_s=20,
        transcript_text="Retention rose by thirty percent.",
    )
    assert len(blocks) == 1


def test_drops_card_copy_assembled_from_distant_transcript_windows() -> None:
    proposal = RawVisualTreatment(
        kind="text_card",
        purpose="quote",
        start_s=8,
        end_s=10,
        text="Talent changed everything",
        confidence="high",
    )
    words = [
        {"word": "Talent", "start_s": 1.0, "end_s": 1.3},
        {"word": "takes", "start_s": 8.0, "end_s": 8.3},
        {"word": "practice", "start_s": 8.3, "end_s": 8.8},
        {"word": "changed", "start_s": 17.0, "end_s": 17.3},
        {"word": "everything", "start_s": 17.3, "end_s": 17.8},
    ]
    blocks, elements = build_visual_treatments(
        [proposal],
        assets_by_id={},
        duration_s=20,
        transcript_text="Talent takes practice changed everything",
        transcript_words=words,
    )
    assert blocks == []
    assert elements == []


def test_accepts_condensed_card_copy_within_timed_window() -> None:
    proposal = RawVisualTreatment(
        kind="text_card",
        purpose="statistic",
        start_s=8,
        end_s=10,
        text="Retention rose 30%",
        confidence="high",
    )
    words = [
        {"word": "Retention", "start_s": 8.0, "end_s": 8.3},
        {"word": "rose", "start_s": 8.3, "end_s": 8.6},
        {"word": "by", "start_s": 8.6, "end_s": 8.7},
        {"word": "thirty", "start_s": 8.7, "end_s": 9.0},
        {"word": "percent", "start_s": 9.0, "end_s": 9.3},
    ]
    blocks, _elements = build_visual_treatments(
        [proposal],
        assets_by_id={},
        duration_s=20,
        transcript_words=words,
    )
    assert len(blocks) == 1


def test_section_items_align_to_real_turkish_title_words_after_sixty_seconds() -> None:
    proposals = [
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=24.0,
            end_s=30.0,
            text="1. Somutlaştırma",
            confidence="high",
        ),
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=55.0,
            end_s=62.0,
            text="2. Pester Power",
            confidence="high",
        ),
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=79.0,
            end_s=84.0,
            text="3. Hatırlanabilirlik",
            confidence="high",
        ),
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=101.0,
            end_s=106.0,
            text="4. Storytelling",
            confidence="high",
        ),
    ]
    words = [
        {"word": "Dört", "start_s": 23.40, "end_s": 23.77},
        {"word": "ana başlıkta anlatayım.", "start_s": 23.77, "end_s": 25.73},
        {"word": "Birinci,", "start_s": 25.83, "end_s": 26.36},
        {"word": "somutlaştırma.", "start_s": 26.42, "end_s": 27.69},
        {"word": "Markalar aslında soğuk yapılardır.", "start_s": 28.08, "end_s": 31.74},
        {"word": "İki,", "start_s": 56.28, "end_s": 56.89},
        {"word": "Pester", "start_s": 56.89, "end_s": 57.61},
        {"word": "Power.", "start_s": 57.61, "end_s": 58.14},
        {"word": "Yani çocukların ikna gücü.", "start_s": 58.32, "end_s": 61.32},
        {"word": "Üç,", "start_s": 79.88, "end_s": 80.62},
        {"word": "hatırlanabilirlik.", "start_s": 80.78, "end_s": 82.92},
        {"word": "Reklam kuşağında onlarca reklam dönerken", "start_s": 82.92, "end_s": 90.76},
        {"word": "Dört,", "start_s": 101.68, "end_s": 102.28},
        {"word": "Storytelling.", "start_s": 102.58, "end_s": 103.42},
        {"word": "Yani hikâye anlatıcılığı.", "start_s": 103.70, "end_s": 105.32},
    ]

    blocks, elements = build_visual_treatments(
        proposals,
        assets_by_id={},
        duration_s=134.442,
        transcript_words=words,
    )

    assert [element["text"] for element in elements] == [
        "1. Somutlaştırma",
        "2. Pester Power",
        "3. Hatırlanabilirlik",
        "4. Storytelling",
    ]
    assert [(block["start_s"], block["end_s"]) for block in blocks] == [
        (25.83, 27.69),
        (56.28, 58.14),
        (79.88, 82.92),
        (101.68, 103.42),
    ]


def test_section_item_copy_must_be_grounded_in_its_local_occurrence() -> None:
    proposal = RawVisualTreatment(
        kind="text_card",
        purpose="section_item",
        start_s=40.0,
        end_s=44.0,
        text="1. Somutlaştırma",
        confidence="high",
    )
    words = [
        {"word": "Birinci somutlaştırma", "start_s": 10.0, "end_s": 12.0},
        {"word": "İkinci hatırlanabilirlik", "start_s": 40.0, "end_s": 42.0},
    ]

    blocks, elements = build_visual_treatments(
        [proposal], assets_by_id={}, duration_s=60.0, transcript_words=words
    )

    assert blocks == []
    assert elements == []


def test_section_item_prefers_local_heading_over_preview_inside_same_window() -> None:
    proposal = RawVisualTreatment(
        kind="text_card",
        purpose="section_item",
        start_s=5.0,
        end_s=22.0,
        text="1. Somutlaştırma",
        confidence="high",
    )
    words = [
        {"word": "Birinci Somutlaştırma", "start_s": 10.0, "end_s": 11.0},
        {"word": "Şimdi başlayalım", "start_s": 11.0, "end_s": 19.5},
        {"word": "Birinci Somutlaştırma", "start_s": 20.0, "end_s": 21.0},
    ]

    blocks, _elements = build_visual_treatments(
        [proposal], assets_by_id={}, duration_s=30.0, transcript_words=words
    )

    assert [(block["start_s"], block["end_s"]) for block in blocks] == [(20.0, 21.0)]


def test_short_section_title_keeps_exact_spoken_window() -> None:
    proposal = RawVisualTreatment(
        kind="text_card",
        purpose="section_item",
        start_s=10.0,
        end_s=11.0,
        text="1. Fiyat",
        confidence="high",
    )
    words = [
        {"word": "Birinci", "start_s": 10.0, "end_s": 10.2},
        {"word": "Fiyat", "start_s": 10.2, "end_s": 10.55},
        {"word": "Ürün pahalıydı", "start_s": 10.55, "end_s": 12.0},
    ]

    blocks, _elements = build_visual_treatments(
        [proposal], assets_by_id={}, duration_s=30.0, transcript_words=words
    )

    assert [(block["start_s"], block["end_s"]) for block in blocks] == [(10.0, 10.55)]


def test_section_items_require_full_talking_head_gap() -> None:
    proposals = [
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=10.0,
            end_s=11.0,
            text="1. First",
            confidence="high",
        ),
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=11.74,
            end_s=12.74,
            text="2. Second",
            confidence="high",
        ),
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=13.49,
            end_s=14.49,
            text="3. Third",
            confidence="high",
        ),
    ]
    words = [
        {"word": "One First", "start_s": 10.0, "end_s": 11.0},
        {"word": "Two Second", "start_s": 11.74, "end_s": 12.74},
        {"word": "Three Third", "start_s": 13.49, "end_s": 14.49},
    ]

    _blocks, elements = build_visual_treatments(
        proposals, assets_by_id={}, duration_s=30.0, transcript_words=words
    )

    # The 0.74s gap is rejected; the next candidate is exactly 0.75s after
    # the last accepted title and survives.
    assert [element["text"] for element in elements] == ["1. First", "3. Third"]


def test_section_items_cap_at_first_eight_valid_items() -> None:
    proposals: list[RawVisualTreatment] = []
    words: list[dict] = []
    for index in range(10):
        start = 5.0 + index * 5.0
        proposals.append(
            RawVisualTreatment(
                kind="text_card",
                purpose="section_item",
                start_s=start,
                end_s=start + 2.0,
                text=f"{index + 1}. Item {index + 1}",
                confidence="high",
            )
        )
        words.extend(
            [
                {"word": str(index + 1), "start_s": start, "end_s": start + 0.3},
                {"word": "Item", "start_s": start + 0.3, "end_s": start + 0.8},
                {"word": str(index + 1), "start_s": start + 0.8, "end_s": start + 1.2},
            ]
        )

    _blocks, elements = build_visual_treatments(
        proposals, assets_by_id={}, duration_s=60.0, transcript_words=words
    )

    assert len(elements) == 8
    assert elements[0]["text"] == "1. Item 1"
    assert elements[-1]["text"] == "8. Item 8"


def test_section_item_cap_backfills_after_ungrounded_candidate() -> None:
    proposals: list[RawVisualTreatment] = []
    words: list[dict] = []
    for index in range(10):
        start = 5.0 + index * 5.0
        copy = "3. Hallucinated" if index == 2 else f"{index + 1}. Item {index + 1}"
        proposals.append(
            RawVisualTreatment(
                kind="text_card",
                purpose="section_item",
                start_s=start,
                end_s=start + 2.0,
                text=copy,
                confidence="high",
            )
        )
        words.append(
            {
                "word": f"{index + 1} Item {index + 1}",
                "start_s": start,
                "end_s": start + 1.2,
            }
        )

    _blocks, elements = build_visual_treatments(
        proposals, assets_by_id={}, duration_s=60.0, transcript_words=words
    )

    assert len(elements) == 8
    assert "3. Hallucinated" not in [element["text"] for element in elements]
    assert elements[-1]["text"] == "9. Item 9"


def test_section_item_span_is_clamped_and_zero_length_is_rejected() -> None:
    proposals = [
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=9.7,
            end_s=10.0,
            text="1. Ending",
            confidence="high",
        ),
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=5.0,
            end_s=6.0,
            text="2. Empty",
            confidence="high",
        ),
    ]
    words = [
        {"word": "One Ending", "start_s": 9.8, "end_s": 10.2},
        {"word": "Two Empty", "start_s": 5.5, "end_s": 5.5},
    ]

    blocks, elements = build_visual_treatments(
        proposals, assets_by_id={}, duration_s=10.0, transcript_words=words
    )

    assert [(block["start_s"], block["end_s"]) for block in blocks] == [(9.8, 10.0)]
    assert [element["text"] for element in elements] == ["1. Ending"]


def test_section_items_do_not_change_generic_card_spacing() -> None:
    proposals = [
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=10.0,
            end_s=11.0,
            text="1. First",
            confidence="high",
        ),
        RawVisualTreatment(
            kind="text_card",
            purpose="statistic",
            start_s=12.0,
            end_s=13.0,
            text="Sales doubled",
            confidence="high",
        ),
    ]
    words = [
        {"word": "One First", "start_s": 10.0, "end_s": 11.0},
        {"word": "Sales doubled", "start_s": 12.0, "end_s": 13.0},
    ]

    _blocks, elements = build_visual_treatments(
        proposals, assets_by_id={}, duration_s=30.0, transcript_words=words
    )

    assert [element["text"] for element in elements] == ["1. First", "Sales doubled"]


def test_section_gap_counts_only_uncovered_talking_head_footage() -> None:
    proposals = [
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=10.0,
            end_s=11.0,
            text="1. First",
            confidence="high",
        ),
        RawVisualTreatment(
            kind="text_card",
            purpose="quote",
            start_s=11.1,
            end_s=11.9,
            text="Brief quote",
            confidence="high",
        ),
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=12.0,
            end_s=13.0,
            text="2. Second",
            confidence="high",
        ),
    ]
    words = [
        {"word": "One First", "start_s": 10.0, "end_s": 11.0},
        {"word": "Brief quote", "start_s": 11.1, "end_s": 11.9},
        {"word": "Two Second", "start_s": 12.0, "end_s": 13.0},
    ]

    _blocks, elements = build_visual_treatments(
        proposals, assets_by_id={}, duration_s=30.0, transcript_words=words
    )

    assert [element["text"] for element in elements] == ["1. First", "Brief quote"]


def test_section_ordinal_equivalence_does_not_change_generic_grounding() -> None:
    proposal = RawVisualTreatment(
        kind="text_card",
        purpose="quote",
        start_s=10.0,
        end_s=11.0,
        text="First priority",
        confidence="high",
    )

    blocks, elements = build_visual_treatments(
        [proposal],
        assets_by_id={},
        duration_s=30.0,
        transcript_words=[{"word": "one priority", "start_s": 10.0, "end_s": 11.0}],
    )

    assert blocks == []
    assert elements == []


def test_five_minute_input_collection_bounds_cover_dense_sources() -> None:
    planner_input = VisualTreatmentPlannerInput(
        duration_s=300.0,
        words=[{"word": "word", "start_s": 0.0, "end_s": 0.1}] * 1201,
        beat_timestamps_s=[index / 2 for index in range(600)],
        clip_plan=[{"start_s": index} for index in range(61)],
    )

    assert len(planner_input.words) == 1201
    assert len(planner_input.beat_timestamps_s) == 600
    assert len(planner_input.clip_plan) == 61


def test_section_item_longer_than_four_seconds_is_rejected() -> None:
    proposal = RawVisualTreatment(
        kind="text_card",
        purpose="section_item",
        start_s=10.0,
        end_s=16.0,
        text="1. An exceptionally long section heading",
        confidence="high",
    )
    words = [
        {"word": "Birinci", "start_s": 10.0, "end_s": 10.5},
        {
            "word": "An exceptionally long section heading",
            "start_s": 10.5,
            "end_s": 15.0,
        },
    ]

    blocks, _elements = build_visual_treatments(
        [proposal], assets_by_id={}, duration_s=30.0, transcript_words=words
    )

    assert blocks == []


def test_unannounced_numbered_hook_is_not_inferred_as_sections() -> None:
    words = [
        {"word": "1.", "start_s": 0.0, "end_s": 0.2},
        {"word": "Selocanlar", "start_s": 0.2, "end_s": 0.8},
        {"word": "2.", "start_s": 1.5, "end_s": 1.7},
        {"word": "Çelik", "start_s": 1.7, "end_s": 2.0},
        {"word": "Çelik", "start_s": 2.0, "end_s": 2.3},
        {"word": "3.", "start_s": 3.0, "end_s": 3.2},
        {"word": "Naz", "start_s": 3.2, "end_s": 3.5},
    ]

    assert infer_structured_section_treatments(words) == []


def test_inferred_turkish_titles_use_locale_correct_dotted_i() -> None:
    words = [
        {"word": "Üç", "start_s": 0.0, "end_s": 0.2},
        {"word": "ana", "start_s": 0.2, "end_s": 0.4},
        {"word": "başlık", "start_s": 0.4, "end_s": 0.8},
        {"word": "Birinci", "start_s": 2.0, "end_s": 2.2},
        {"word": "iletişim", "start_s": 2.2, "end_s": 2.6},
        {"word": "İkinci", "start_s": 3.5, "end_s": 3.7},
        {"word": "içerik", "start_s": 3.7, "end_s": 4.1},
        {"word": "Üçüncü", "start_s": 5.0, "end_s": 5.2},
        {"word": "işbirliği", "start_s": 5.2, "end_s": 5.7},
    ]

    treatments = infer_structured_section_treatments(words)

    assert [treatment.text for treatment in treatments] == [
        "1. İletişim",
        "2. İçerik",
        "3. İşbirliği",
    ]


def test_inferred_english_titles_keep_ascii_i() -> None:
    words = [
        {"word": "Üç", "start_s": 0.0, "end_s": 0.2},
        {"word": "ana", "start_s": 0.2, "end_s": 0.4},
        {"word": "başlık", "start_s": 0.4, "end_s": 0.8},
        {"word": "First", "start_s": 2.0, "end_s": 2.2},
        {"word": "impact", "start_s": 2.2, "end_s": 2.6},
        {"word": "Second", "start_s": 3.5, "end_s": 3.7},
        {"word": "insight", "start_s": 3.7, "end_s": 4.1},
        {"word": "Third", "start_s": 5.0, "end_s": 5.2},
        {"word": "intent", "start_s": 5.2, "end_s": 5.7},
    ]

    treatments = infer_structured_section_treatments(words)

    assert [treatment.text for treatment in treatments] == [
        "1. Impact",
        "2. Insight",
        "3. Intent",
    ]


def test_empty_model_fallback_stops_before_english_definitions() -> None:
    words = [
        {"word": "Three main reasons.", "start_s": 0.0, "end_s": 1.0},
        {"word": "First", "start_s": 3.0, "end_s": 3.3},
        {"word": "personalization", "start_s": 3.3, "end_s": 3.8},
        {"word": "is when brands adapt.", "start_s": 3.8, "end_s": 5.2},
        {"word": "Second", "start_s": 7.0, "end_s": 7.3},
        {"word": "trust", "start_s": 7.3, "end_s": 7.7},
        {"word": "means customers believe you.", "start_s": 7.7, "end_s": 9.0},
        {"word": "Third", "start_s": 11.0, "end_s": 11.3},
        {"word": "memory", "start_s": 11.3, "end_s": 11.8},
        {"word": "is what makes it last.", "start_s": 11.8, "end_s": 13.0},
    ]

    treatments = infer_structured_section_treatments(words)

    assert [treatment.text for treatment in treatments] == [
        "1. Personalization",
        "2. Trust",
        "3. Memory",
    ]


def test_sentence_sized_transcript_rows_keep_distinct_card_timings() -> None:
    words = [
        {
            "word": (
                "Three main points. First focus. Explanation here. "
                "Second trust. Explanation here. Third memory."
            ),
            "start_s": 0.0,
            "end_s": 18.0,
        }
    ]

    blocks, elements = build_visual_treatments(
        [], assets_by_id={}, duration_s=40.0, transcript_words=words
    )

    assert [element["text"] for element in elements] == [
        "1. Focus",
        "2. Trust",
        "3. Memory",
    ]
    assert len({(block["start_s"], block["end_s"]) for block in blocks}) == 3


def test_complete_grounded_model_sections_are_not_replaced_by_broader_inference() -> None:
    words = [
        {"word": "There are three reasons.", "start_s": 0.0, "end_s": 1.0},
        {"word": "First", "start_s": 3.0, "end_s": 3.3},
        {"word": "personalization", "start_s": 3.3, "end_s": 3.8},
        {"word": "is when brands adapt.", "start_s": 3.8, "end_s": 5.2},
        {"word": "Second", "start_s": 7.0, "end_s": 7.3},
        {"word": "trust", "start_s": 7.3, "end_s": 7.7},
        {"word": "is built slowly.", "start_s": 7.7, "end_s": 9.0},
        {"word": "Third", "start_s": 11.0, "end_s": 11.3},
        {"word": "memory", "start_s": 11.3, "end_s": 11.8},
        {"word": "makes it last.", "start_s": 11.8, "end_s": 13.0},
    ]
    model = [
        RawVisualTreatment(
            kind="text_card",
            purpose="section_item",
            start_s=start,
            end_s=end,
            text=text,
            confidence="high",
        )
        for start, end, text in [
            (3.0, 3.8, "1. Personalization"),
            (7.0, 7.7, "2. Trust"),
            (11.0, 11.8, "3. Memory"),
        ]
    ]

    _blocks, elements = build_visual_treatments(
        model, assets_by_id={}, duration_s=30.0, transcript_words=words
    )

    assert [element["text"] for element in elements] == [
        "1. Personalization",
        "2. Trust",
        "3. Memory",
    ]


def test_announced_ten_item_list_degrades_to_first_eight_cards() -> None:
    words = [
        {"word": "Ten", "start_s": 0.0, "end_s": 0.2},
        {"word": "main", "start_s": 0.2, "end_s": 0.4},
        {"word": "items", "start_s": 0.4, "end_s": 0.8},
    ]
    for ordinal in range(1, 11):
        start = 2.0 + (ordinal - 1) * 1.5
        words.extend(
            [
                {"word": f"{ordinal}.", "start_s": start, "end_s": start + 0.2},
                {"word": f"item{ordinal}", "start_s": start + 0.2, "end_s": start + 0.6},
            ]
        )

    treatments = infer_structured_section_treatments(words)

    assert [treatment.text for treatment in treatments] == [
        f"{ordinal}. Item{ordinal}" for ordinal in range(1, 9)
    ]
