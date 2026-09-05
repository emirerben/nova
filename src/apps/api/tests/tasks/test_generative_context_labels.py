"""Focused contracts for confirmation-gated per-clip context labels."""

from types import SimpleNamespace

from app.agents._schemas.text_element import merge_projected_text_elements_for_variant
from app.tasks.generative_build import (
    _canonical_context_sport_labels,
    _context_output_slot_windows,
    _context_sport_burn_dicts,
    _context_sport_text_elements,
)


def test_context_sport_labels_are_allowlisted_and_confidence_gated():
    labels = _canonical_context_sport_labels(
        [
            {
                "source": "detected_sport",
                "clip_index": 0,
                "sport": "basketball",
                "position": "bottom_right",
                "size": "small",
                "confidence": 0.96,
            },
            {
                "source": "detected_sport",
                "clip_index": 1,
                "sport": "tennis",
                "position": "bottom_right",
                "size": "small",
                "confidence": 0.79,
            },
            {
                "source": "detected_sport",
                "clip_index": 1,
                "sport": "a made-up sport from clip prose",
                "position": "bottom_right",
                "size": "small",
                "confidence": 0.99,
            },
        ],
        {"clip-a": "users/a.mp4", "clip-b": "users/b.mp4"},
    )

    assert labels == [
        {
            "clip_id": "clip-a",
            "sport": "Basketball",
            "source": "detected_sport",
            "position": "bottom_right",
            "size": "small",
            "confidence": 0.96,
        }
    ]


def test_context_sport_elements_follow_resolved_slots_without_timeline_ripple():
    steps = [
        SimpleNamespace(clip_id="clip-a"),
        SimpleNamespace(clip_id="clip-b"),
        SimpleNamespace(clip_id="clip-a"),
    ]
    plans = [{"duration_s": 2.0}, {"duration_s": 3.0}, {"duration_s": 1.0}]
    labels = [
        {
            "source": "detected_sport",
            "clip_index": 0,
            "sport": "basketball",
            "position": "bottom_right",
            "size": "small",
            "confidence": 0.96,
        },
        {
            "source": "detected_sport",
            "clip_id": "clip-b",
            "sport": "tennis",
            "position": "bottom_right",
            "size": "small",
        },
    ]

    elements = _context_sport_text_elements(
        labels,
        steps=steps,
        resolved_plans=plans,
        clip_id_to_gcs={"clip-a": "users/a.mp4", "clip-b": "users/b.mp4"},
        video_duration_s=6.0,
    )

    assert [(e["text"], e["start_s"], e["end_s"]) for e in elements] == [
        ("Basketball", 0.0, 2.0),
        ("Tennis", 2.0, 5.0),
        ("Basketball", 5.0, 6.0),
    ]
    assert all(
        (e["position"], e["x_frac"], e["y_frac"], e["size_class"], e["alignment"])
        == ("custom", 0.86, 0.86, "small", "right")
        for e in elements
    )
    assert all(e["role"] == "generative_sequence" for e in elements)
    assert all(e["source_params"]["identity"].startswith("context_sport:") for e in elements)

    burn = _context_sport_burn_dicts(elements, video_duration_s=6.0)
    assert [(o["text"], o["start_s"], o["end_s"]) for o in burn] == [
        ("Basketball", 0.0, 2.0),
        ("Tennis", 2.0, 5.0),
        ("Basketball", 5.0, 6.0),
    ]
    assert all(
        (o["position_x_frac"], o["position_y_frac"], o["text_anchor"]) == (0.86, 0.86, "right")
        for o in burn
    )

    projected = merge_projected_text_elements_for_variant(
        {
            "text_mode": "agent_text",
            "intro_text": "Emir Olympics",
            "context_label_text_elements": elements,
        }
    )
    assert projected is not None
    assert {row["text"] for row in projected} == {"Emir Olympics", "Basketball", "Tennis"}


def test_typed_context_label_intent_resolves_only_structured_clip_sports():
    intent = {
        "kind": "sport",
        "source": "clip_metadata",
        "placement": "bottom_right",
        "size": "small",
        "per_clip": True,
    }
    metas = [
        SimpleNamespace(clip_id="clip-a", detected_subject="basketball player on court"),
        SimpleNamespace(clip_id="clip-b", detected_subject="a person outdoors"),
    ]

    labels = _canonical_context_sport_labels(
        intent,
        {"clip-a": "users/a.mp4", "clip-b": "users/b.mp4"},
        metas,
    )

    assert [(row["clip_id"], row["sport"]) for row in labels] == [("clip-a", "Basketball")]


def test_context_label_windows_mirror_crossfade_output_clock():
    steps = [
        SimpleNamespace(clip_id="clip-a", slot={"transition_in": "cut"}),
        SimpleNamespace(
            clip_id="clip-b",
            slot={"transition_in": "crossfade", "transition_duration_s": 0.4},
        ),
    ]
    windows = _context_output_slot_windows(
        steps,
        [{"duration_s": 2.0}, {"duration_s": 3.0}],
    )

    assert [(round(start, 3), round(end, 3)) for _, _, start, end in windows] == [
        (0.0, 2.0),
        (1.6, 4.6),
    ]
