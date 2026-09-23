from __future__ import annotations

import pytest

from app.pipeline.narration_labels import TOPIC_SENTENCE_REJECTION, materialize_narration_labels

# Synthetic stand-in for a creator's spoken script line (13 words, the widest
# span `_annotation_bounds` accepts). Never copy real creator text here.
SPOKEN_LINE = "The old bridge opened before the railway came and it still carries traffic"


def _words(*texts: str) -> list[dict]:
    rows = []
    for index, text in enumerate(texts):
        rows.append(
            {
                "word_id": f"w{index:06d}",
                "text": text,
                "start_s": float(index),
                "end_s": float(index + 1),
            }
        )
    return rows


def _timeline(*rows: tuple[str, str, float, float, bool | None]) -> list[dict]:
    return [
        {
            "timeline_id": timeline_id,
            "asset_id": asset_id,
            "start_s": start_s,
            "end_s": end_s,
            **({"single_subject": single_subject} if single_subject is not None else {}),
        }
        for timeline_id, asset_id, start_s, end_s, single_subject in rows
    ]


def test_hybrid_scores_are_transcript_grounded_without_keyword_window() -> None:
    transcript = _words(
        "with",
        "about",
        "10",
        "minutes",
        "left",
        "Team",
        "Juni",
        "went",
        "1-0",
        "up",
        "and",
        "now",
        "it",
        "was",
        "1-1.",
    )
    result = materialize_narration_labels(
        transcript,
        _timeline(("shot-0", "asset-0", 0.0, 16.0, None)),
        [{"asset_id": "asset-0", "subject_count": 2}],
        {"score_labels": True},
    )

    assert [item["text"] for item in result.elements] == ["1-0", "1-1."]
    assert [item["source_params"]["source_word_ids"] for item in result.elements] == [
        ["w000008"],
        ["w000014"],
    ]
    assert not result.rejected


def test_incidental_numbers_are_rejected_even_when_request_mentions_scores() -> None:
    result = materialize_narration_labels(
        _words("about", "10", "minutes", "left", "and", "one", "game", "left"),
        _timeline(("shot-0", "asset-0", 0.0, 8.0, None)),
        [],
        {"score_labels": True},
    )

    assert result.elements == ()


def test_spoken_score_requires_an_exact_semantic_anchor() -> None:
    transcript = _words("the", "score", "was", "one", "nil", "and", "one", "game", "left")
    result = materialize_narration_labels(
        transcript,
        _timeline(("shot-0", "asset-0", 0.0, 9.0, None)),
        [],
        {"score_labels": True},
        semantic_annotations=[
            {
                "kind": "score",
                "start_word_id": "w000003",
                "end_word_id": "w000004",
            },
            {
                "kind": "score",
                "start_word_id": "w000006",
                "end_word_id": "w000007",
            },
        ],
    )

    assert [item["text"] for item in result.elements] == ["one nil"]
    assert len(result.rejected) == 1
    assert "exact score phrase" in result.rejected[0].reason


def test_score_text_ignores_model_copy_and_uses_the_transcript() -> None:
    result = materialize_narration_labels(
        _words("Team", "Juni", "went", "1-0", "up"),
        _timeline(("shot-0", "asset-0", 0.0, 5.0, None)),
        [],
        {"score_labels": True},
        semantic_annotations=[
            {
                "kind": "score",
                "start_word_id": "w000003",
                "end_word_id": "w000003",
                "text": "99-99",
            }
        ],
    )

    assert [item["text"] for item in result.elements] == ["1-0"]


def test_participant_labels_require_typed_single_subject_analysis_and_keep_asset_identity() -> None:
    timeline = _timeline(
        ("shot-a-1", "asset-a", 0.0, 2.0, None),
        ("shot-group", "asset-group", 2.0, 4.0, None),
        ("shot-unknown", "asset-unknown", 4.0, 6.0, None),
        ("shot-a-2", "asset-a", 6.0, 8.0, None),
    )
    analysis = [
        {"asset_id": "asset-a", "subject_focus": "single_subject"},
        {"asset_id": "asset-group", "subject_count": 3},
        {"asset_id": "asset-unknown", "detected_subject": "a young woman"},
    ]
    result = materialize_narration_labels(
        _words("visual", "placeholder"),
        timeline,
        analysis,
        {"participant_labels": "single_subject"},
    )

    assert [item["text"] for item in result.elements] == ["PLAYER 1", "PLAYER 1"]
    assert [item["start_s"] for item in result.elements] == [0.0, 6.0]
    assert [item["source_params"]["participant_key"] for item in result.elements] == [
        "asset:asset-a",
        "asset:asset-a",
    ]
    assert all(item["source_params"]["source_asset_id"] == "asset-a" for item in result.elements)
    assert all(item["position"] == "custom" for item in result.elements)
    assert all(item["x_frac"] == 0.08 for item in result.elements)
    assert all(item["y_frac"] == 0.76 for item in result.elements)
    assert all(item["alignment"] == "left" for item in result.elements)
    assert all(item["effect"] == "static" for item in result.elements)


def test_timeline_level_typed_evidence_can_qualify_a_single_shot() -> None:
    result = materialize_narration_labels(
        _words("one", "shot"),
        _timeline(("shot-0", "asset-0", 4.25, 5.75, True)),
        [{"asset_id": "asset-0", "detected_subject": "person"}],
        {"participant_labels": "single_subject"},
    )

    assert result.elements[0]["start_s"] == 4.25
    assert result.elements[0]["end_s"] == 5.75


def test_nested_media_ref_analysis_uses_only_explicit_typed_focus() -> None:
    result = materialize_narration_labels(
        _words("one", "shot"),
        _timeline(("shot-0", "asset-0", 0.0, 2.0, None)),
        [
            {
                "asset_id": "asset-0",
                "analysis": {"visible_subject_count": 1, "description": "a player"},
            }
        ],
        {"participant_labels": "single_subject"},
    )

    assert [item["text"] for item in result.elements] == ["PLAYER 1"]


def test_free_form_subject_description_cannot_qualify_participant_label() -> None:
    result = materialize_narration_labels(
        _words("one", "shot"),
        _timeline(("shot-0", "asset-0", 0.0, 2.0, None)),
        [{"asset_id": "asset-0", "description": "one player fills the frame"}],
        {"participant_labels": "single_subject"},
    )

    assert result.elements == ()


def test_single_primary_focus_with_background_people_qualifies() -> None:
    result = materialize_narration_labels(
        _words("one", "shot"),
        _timeline(("shot-0", "asset-0", 0.0, 2.0, None)),
        [
            {
                "asset_id": "asset-0",
                "focus_mode": "single_subject",
                "primary_subject_count": 1,
                "visible_subject_count": 3,
            }
        ],
        {"participant_labels": "single_subject"},
    )

    assert [item["text"] for item in result.elements] == ["PLAYER 1"]


def test_topic_labels_are_generic_and_transcript_grounded() -> None:
    result = materialize_narration_labels(
        _words("first", "sport", "was", "football", "then", "we", "visited", "Rome"),
        _timeline(("shot-0", "asset-0", 0.0, 8.0, None)),
        [],
        {"context_labels": ["sport"]},
        semantic_annotations=[
            {
                "kind": "topic",
                "text": "football",
                "start_word_id": "w000001",
                "end_word_id": "w000003",
            }
        ],
    )

    assert [item["text"] for item in result.elements] == ["football"]
    assert result.elements[0]["source_params"]["source_word_ids"] == ["w000003"]


def test_score_and_topic_use_distinct_top_lanes() -> None:
    result = materialize_narration_labels(
        _words("score", "1-0", "football"),
        _timeline(("shot-0", "asset-0", 0.0, 3.0, None)),
        [],
        {"score_labels": True, "context_labels": ["sport"]},
        semantic_annotations=[
            {"kind": "topic", "text": "football", "start_word_id": "w000002"},
        ],
    )

    score, topic = result.elements
    assert (score["x_frac"], score["alignment"]) == (0.92, "right")
    assert (topic["x_frac"], topic["alignment"]) == (0.08, "left")
    assert score["y_frac"] == topic["y_frac"] == 0.12


def test_negative_and_disabled_requirements_materialize_no_labels() -> None:
    result = materialize_narration_labels(
        _words("score", "1-0", "football"),
        _timeline(("shot-0", "asset-0", 0.0, 3.0, True)),
        [{"asset_id": "asset-0", "single_subject": True}],
        {
            "participant_labels": "single_subject",
            "score_labels": True,
            "sport_labels": True,
            "negative_kinds": ["participant", "score", "topic"],
        },
    )

    assert result.elements == ()


def test_source_instance_id_is_the_asset_local_identity_boundary() -> None:
    result = materialize_narration_labels(
        _words("shot"),
        [
            {
                "timeline_id": "shot-0",
                "asset_id": "same-path",
                "source_instance_id": "upload-a",
                "start_s": 0.0,
                "end_s": 1.0,
            },
            {
                "timeline_id": "shot-1",
                "asset_id": "same-path",
                "source_instance_id": "upload-b",
                "start_s": 1.0,
                "end_s": 2.0,
            },
        ],
        [
            {"asset_id": "same-path", "source_instance_id": "upload-a", "single_subject": True},
            {"asset_id": "same-path", "source_instance_id": "upload-b", "single_subject": False},
        ],
        {"participant_labels": "single_subject"},
    )

    assert [item["text"] for item in result.elements] == ["PLAYER 1"]
    assert [item["source_params"]["participant_key"] for item in result.elements] == [
        "asset:upload-a"
    ]


def test_topic_spoken_sentence_is_rejected_with_receipt_reason() -> None:
    """Prod shape (2026-09-23): a request pairing shots with spoken lines was
    read as topic intents and the annotation agent echoed the first voiceover
    sentence twice. That sentence is exactly copied from its anchor, so the
    grounding check alone accepted it and it rendered once (deduped) as a
    pop-in label that duplicated the caption. It is script, not a label."""

    tokens = SPOKEN_LINE.split()
    assert len(tokens) == 13
    sentence_topic = {
        "kind": "topic",
        "text": SPOKEN_LINE,
        "start_word_id": "w000000",
        "end_word_id": f"w{len(tokens) - 1:06d}",
        "confidence": "high",
    }
    result = materialize_narration_labels(
        _words(*tokens),
        _timeline(("shot-0", "asset-0", 0.0, 13.0, None)),
        [],
        {"context_labels": ["topic"]},
        semantic_annotations=[sentence_topic, dict(sentence_topic)],
    )

    assert result.elements == ()
    assert result.accepted == ()
    assert [row.reason for row in result.rejected] == [TOPIC_SENTENCE_REJECTION] * 2
    assert {(row.kind, row.start_word_id, row.end_word_id) for row in result.rejected} == {
        ("topic", "w000000", "w000012")
    }


@pytest.mark.parametrize(
    "transcript,topic,keep",
    [
        (("we", "played", "football", "today"), "football", True),
        (("then", "Old", "Town", "at", "night"), "Old Town", True),
        (("into", "the", "Gothic", "Quarter", "again"), "the Gothic Quarter", True),
        # Punctuation is not a word: a comma-attached label is still one token.
        (("first", "Football,", "then", "lunch"), "Football,", True),
        (("in", "1998", "we", "moved"), "1998", True),
        # Proper names past the visual label fence (4-5 words or 25-32 chars)
        # are still labels, not script.
        (("we", "crossed", "Parc", "de", "la", "Ciutadella"), "Parc de la Ciutadella", True),
        (("then", "Palau", "de", "la", "Música", "Catalana"), "Palau de la Música Catalana", True),
        (("watching", "Brighton", "and", "Hove", "Albion"), "Brighton and Hove Albion", True),
        (("at", "Estadio", "Santiago", "Bernabéu", "tonight"), "Estadio Santiago Bernabéu", True),
        (("on", "the", "3rd", "of", "March", "2019", "we"), "the 3rd of March 2019", True),
        (
            ("then", "Parc", "del", "Laberint", "d'Horta", "after"),
            "Parc del Laberint d'Horta",
            True,
        ),
        (
            ("then", "Parc", "del", "Laberint", "d\N{RIGHT SINGLE QUOTATION MARK}Horta", "after"),
            "Parc del Laberint d\N{RIGHT SINGLE QUOTATION MARK}Horta",
            True,
        ),
        (("the", "Llanfairpwllgwyngyllgogerych", "sign"), "Llanfairpwllgwyngyllgogerych", True),
        # 24 displayed characters; casefolding "ß" to "ss" would make it 25.
        (("dann", "Kurfürstenstraße", "Potsdam", "entlang"), "Kurfürstenstraße Potsdam", True),
        # Spoken lines and over-long spans stay rejected.
        (("we", "walked", "the", "Gothic", "Quarter"), "walked the Gothic Quarter", False),
        (("so", "we", "got", "to", "Rome", "early"), "so we got to Rome", False),
        (("Then", "Barcelona", "won", "it", "all"), "Then Barcelona won it", False),
        (
            ("at", "Basilica", "di", "Santa", "Maria", "del", "Fiore"),
            "Basilica di Santa Maria del Fiore",
            False,
        ),
        (
            ("the", "Llanfairpwllgwyngyllgogerychwyrndrobwll", "sign"),
            "Llanfairpwllgwyngyllgogerychwyrndrobwll",
            False,
        ),
        # Caseless scripts cannot be name-shaped: the 3-word fence applies.
        (("今日", "は", "東京", "タワー", "に", "行った"), "は 東京 タワー に", False),
    ],
)
def test_topic_labels_must_be_label_shaped(
    transcript: tuple[str, ...], topic: str, keep: bool
) -> None:
    start = transcript.index(topic.split()[0])
    end = start + len(topic.split()) - 1
    result = materialize_narration_labels(
        _words(*transcript),
        _timeline(("shot-0", "asset-0", 0.0, float(len(transcript)), None)),
        [],
        {"context_labels": ["topic"]},
        semantic_annotations=[
            {
                "kind": "topic",
                "text": topic,
                "start_word_id": f"w{start:06d}",
                "end_word_id": f"w{end:06d}",
            }
        ],
    )

    if keep:
        assert [item["text"] for item in result.elements] == [topic]
        assert result.elements[0]["effect"] == "pop-in"
        assert not result.rejected
    else:
        assert result.elements == ()
        assert [row.reason for row in result.rejected] == [TOPIC_SENTENCE_REJECTION]


def test_sentence_shape_rule_is_scoped_to_topic_labels() -> None:
    """Explicit creator copy (the intro) and exact score spans are untouched by
    the topic shape rule, even alongside a rejected sentence-length topic."""

    tokens = SPOKEN_LINE.split()
    result = materialize_narration_labels(
        _words(*tokens, "and", "it", "finished", "2-1"),
        _timeline(("shot-0", "asset-0", 0.0, 17.0, None)),
        [],
        {"intro": True, "score_labels": True, "context_labels": ["topic"]},
        semantic_annotations=[
            {
                "kind": "topic",
                "text": SPOKEN_LINE,
                "start_word_id": "w000000",
                "end_word_id": "w000012",
            }
        ],
        explicit_intro_text="A walk across the old bridge before the railway came",
    )

    assert sorted(item["source_params"]["narration_label_kind"] for item in result.elements) == [
        "intro",
        "score",
    ]
    assert {item["text"] for item in result.elements} == {
        "A walk across the old bridge before the railway came",
        "2-1",
    }
    assert [row.reason for row in result.rejected] == [TOPIC_SENTENCE_REJECTION]
