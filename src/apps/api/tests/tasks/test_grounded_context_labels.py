"""KRI-127: worker-edge grounding fence for open-vocabulary context labels.

These are the replacement for the closed sport-allowlist sentinel
(``TestNoGeminiTextLeaks``): every case a stored resolved clip intent could
try to sneak untrusted text onto the screen, and the confirmation that the
legacy allowlist lane is byte-identical when the new lane isn't engaged.
"""

from types import SimpleNamespace

import pytest

from app.pipeline.agents.gemini_analyzer import ClipMeta
from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent
from app.tasks.generative_build import (
    _context_sport_text_elements,
    _grounded_context_labels,
    _resolve_clip_id_for_media_id,
)


def _meta(clip_id: str, **kwargs) -> ClipMeta:
    defaults = dict(
        clip_id=clip_id,
        transcript="",
        hook_text="",
        hook_score=7.0,
        best_moments=[],
    )
    defaults.update(kwargs)
    return ClipMeta(**defaults)


def _intent(
    *,
    intent_id: str = "i1",
    op: str = "label",
    attribute: str = "sport",
    status: str = "resolved",
    creator_text: str | None = None,
    assignments: list[ClipAssignment],
) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id=intent_id,
        op=op,
        attribute=attribute,
        status=status,
        creator_text=creator_text,
        assignments=assignments,
    )


# ---------------------------------------------------------------------------
# media_id -> clip_id mapping
# ---------------------------------------------------------------------------


def test_media_id_maps_directly_in_the_guided_lane_and_via_gcs_path_in_the_classic_lane():
    # Guided lane: media_id IS the clip_id (matcher_clip_metas mints
    # ClipMeta(clip_id=ref.media_id, ...) off the same snapshot.media).
    assert _resolve_clip_id_for_media_id("clip-a", {"clip-a": "gs://a.mp4"}) == "clip-a"
    # Classic lane: clip_id is a positional "clip_0" minted fresh per render;
    # the only identifier stable across the chat turn and this render is the
    # clip's GCS path.
    assert _resolve_clip_id_for_media_id("gs://a.mp4", {"clip_0": "gs://a.mp4"}) == "clip_0"
    assert _resolve_clip_id_for_media_id("unknown", {"clip_0": "gs://a.mp4"}) is None


@pytest.mark.parametrize("clip_ids", [("clip_0", "clip_1"), ("clip_1", "clip_0")])
def test_classic_media_id_rejects_duplicate_paths_regardless_of_clip_order(clip_ids):
    clip_id_to_gcs = dict.fromkeys(clip_ids, "gs://shared.mp4")
    assert _resolve_clip_id_for_media_id("gs://shared.mp4", clip_id_to_gcs) is None


@pytest.mark.parametrize("reverse_order", [False, True])
@pytest.mark.parametrize("creator_request", ["", "label the basketball clips Basketball"])
def test_classic_ambiguous_labels_never_render_but_unique_paths_still_do(
    reverse_order, creator_request
):
    # Both occurrences can independently ground the label. That must not let
    # the path-only assignment pick whichever clip happens to come first.
    entries = [
        ("clip_0", "gs://shared.mp4"),
        ("clip_1", "gs://unique.mp4"),
        ("clip_2", "gs://shared.mp4"),
    ]
    clip_id_to_gcs = dict(reversed(entries) if reverse_order else entries)
    metas = [
        _meta(clip_id, clip_summary="a basketball game at the park")
        for clip_id in ("clip_0", "clip_2")
    ] + [_meta("clip_1", clip_summary="cooking paella in a pan")]
    intent = _intent(
        creator_text="Basketball",
        assignments=[
            ClipAssignment(media_id="gs://shared.mp4", value="Basketball", confidence=0.95),
            ClipAssignment(media_id="gs://unique.mp4", value="Paella", confidence=0.95),
        ],
    )
    rows = _grounded_context_labels(
        [intent], clip_id_to_gcs, metas, creator_request=creator_request
    )
    assert [(row["clip_id"], row["sport"]) for row in rows] == [("clip_1", "Paella")]

    elements = _context_sport_text_elements(
        rows,
        steps=[
            SimpleNamespace(clip_id=clip_id, slot={"transition_in": "cut"})
            for clip_id in clip_id_to_gcs
        ],
        resolved_plans=[{"duration_s": 2.0}] * 3,
        video_duration_s=6.0,
    )
    assert [(e["text"], e["start_s"], e["end_s"]) for e in elements] == [("Paella", 2.0, 4.0)]


def test_guided_media_ids_keep_labels_on_their_own_occurrences_of_a_shared_path():
    clip_id_to_gcs = {"clip-a": "gs://shared.mp4", "clip-b": "gs://shared.mp4"}
    for clip_id in clip_id_to_gcs:
        assert _resolve_clip_id_for_media_id(clip_id, clip_id_to_gcs) == clip_id
    intent = _intent(
        assignments=[
            ClipAssignment(media_id="clip-a", value="Basketball", confidence=0.95),
            ClipAssignment(media_id="clip-b", value="Paella", confidence=0.95),
        ]
    )
    rows = _grounded_context_labels(
        [intent],
        clip_id_to_gcs,
        [
            _meta("clip-a", clip_summary="a basketball game at the park"),
            _meta("clip-b", clip_summary="cooking paella in a pan"),
        ],
    )
    assert [(row["clip_id"], row["sport"]) for row in rows] == [
        ("clip-a", "Basketball"),
        ("clip-b", "Paella"),
    ]


# ---------------------------------------------------------------------------
# Sentinel tests: everything that must NOT render
# ---------------------------------------------------------------------------


def test_made_up_value_with_high_confidence_and_no_record_evidence_does_not_render():
    meta = _meta("clip-a", clip_summary="a person walking on a street")
    intent = _intent(
        assignments=[ClipAssignment(media_id="clip-a", value="Quidditch", confidence=0.99)]
    )
    assert _grounded_context_labels([intent], {"clip-a": "a.mp4"}, [meta]) == []


def test_stored_vision_verified_claim_with_no_cached_answer_does_not_render():
    meta = _meta("clip-a", clip_summary="a person walking on a street")
    intent = _intent(
        assignments=[
            ClipAssignment(
                media_id="clip-a",
                value="Basketball",
                confidence=0.95,
                grounding="vision_verified",
            )
        ]
    )
    # The stored `grounding` tag is never trusted -- with no cached answer and
    # no matching record evidence, this must still be omitted.
    assert _grounded_context_labels([intent], {"clip-a": "a.mp4"}, [meta]) == []


def test_value_grounded_in_another_clips_record_does_not_render():
    meta_a = _meta("clip-a", clip_summary="a person walking on a street")
    meta_b = _meta("clip-b", clip_summary="a basketball game at the park")
    intent = _intent(
        assignments=[ClipAssignment(media_id="clip-a", value="Basketball", confidence=0.9)]
    )
    rows = _grounded_context_labels(
        [intent], {"clip-a": "a.mp4", "clip-b": "b.mp4"}, [meta_a, meta_b]
    )
    assert rows == []


def test_spoken_transcript_only_evidence_does_not_render():
    meta = _meta("clip-a", transcript="we are playing basketball today")
    intent = _intent(
        assignments=[ClipAssignment(media_id="clip-a", value="Basketball", confidence=0.9)]
    )
    assert _grounded_context_labels([intent], {"clip-a": "a.mp4"}, [meta]) == []


def test_oversized_and_unsafe_charset_values_never_render():
    meta = _meta("clip-a", clip_summary="a basketball game at the park with basketball hoops")
    too_many_words = _intent(
        intent_id="i1",
        assignments=[
            ClipAssignment(
                media_id="clip-a",
                value="This Label Has Way Too Many Words",
                confidence=0.99,
            )
        ],
    )
    unsafe_charset = _intent(
        intent_id="i2",
        assignments=[ClipAssignment(media_id="clip-a", value="Basket@ball!!", confidence=0.99)],
    )
    assert _grounded_context_labels([too_many_words], {"clip-a": "a.mp4"}, [meta]) == []
    assert _grounded_context_labels([unsafe_charset], {"clip-a": "a.mp4"}, [meta]) == []


def test_non_label_ops_are_never_consulted():
    meta = _meta("clip-a", clip_summary="a basketball game at the park")
    intent = _intent(
        op="group",
        assignments=[ClipAssignment(media_id="clip-a", value="Basketball", confidence=0.95)],
    )
    assert _grounded_context_labels([intent], {"clip-a": "a.mp4"}, [meta]) == []


def test_needs_creator_status_intents_are_never_consulted():
    meta = _meta("clip-a", clip_summary="a basketball game at the park")
    intent = _intent(
        status="needs_creator",
        assignments=[ClipAssignment(media_id="clip-a", value="Basketball", confidence=0.95)],
    )
    assert _grounded_context_labels([intent], {"clip-a": "a.mp4"}, [meta]) == []


def test_transcript_label_intent_cannot_enter_the_visual_grounding_fence():
    meta = _meta("clip-a", clip_summary="a basketball game at the park")
    # A forged resolved assignment has sufficient visual evidence, but the
    # transcript source is a hard boundary before evidence is even consulted.
    transcript_intent = SimpleNamespace(
        intent_id="transcript-1",
        op="label",
        status="resolved",
        label_source="transcript",
        transcript_kind="participant",
        creator_text=None,
        assignments=[ClipAssignment(media_id="clip-a", value="Basketball", confidence=0.99)],
    )
    assert _grounded_context_labels([transcript_intent], {"clip-a": "a.mp4"}, [meta]) == []


# ---------------------------------------------------------------------------
# Positive grounding paths
# ---------------------------------------------------------------------------


def test_record_span_grounded_value_renders_with_provenance():
    meta = _meta("clip-a", clip_summary="a basketball game at the park")
    intent = _intent(
        assignments=[ClipAssignment(media_id="clip-a", value="Basketball", confidence=0.9)]
    )
    rows = _grounded_context_labels([intent], {"clip-a": "a.mp4"}, [meta])
    assert rows == [
        {
            "clip_id": "clip-a",
            "sport": "Basketball",
            "source": "grounded_label",
            "grounding": "record_span",
            "confidence": 0.9,
            "intent_id": "i1",
        }
    ]


def test_open_vocabulary_value_never_coded_for_renders_with_no_code_change():
    # A dish and a city: neither is in any allowlist, and none needs to exist
    # for these to render -- that is the whole point of KRI-127.
    dish_meta = _meta("clip-a", clip_summary="cooking paella in a big pan at the market")
    dish_intent = _intent(
        intent_id="dish-1",
        attribute="dish",
        assignments=[ClipAssignment(media_id="clip-a", value="Paella", confidence=0.85)],
    )
    dish_rows = _grounded_context_labels([dish_intent], {"clip-a": "a.mp4"}, [dish_meta])
    assert dish_rows[0]["sport"] == "Paella"
    assert dish_rows[0]["grounding"] == "record_span"

    city_meta = _meta("clip-b", clip_summary="a rooftop view over Istanbul at sunset")
    city_intent = _intent(
        intent_id="city-1",
        attribute="city",
        assignments=[ClipAssignment(media_id="clip-b", value="Istanbul", confidence=0.85)],
    )
    city_rows = _grounded_context_labels([city_intent], {"clip-b": "b.mp4"}, [city_meta])
    assert city_rows[0]["sport"] == "Istanbul"
    assert city_rows[0]["grounding"] == "record_span"


def test_creator_text_label_renders():
    meta = _meta("clip-a", clip_summary="a scenic view")
    intent = _intent(
        creator_text="Sunset Beach",
        assignments=[ClipAssignment(media_id="clip-a", value="Sunset Beach", confidence=0.3)],
    )
    rows = _grounded_context_labels(
        [intent],
        {"clip-a": "a.mp4"},
        [meta],
        creator_request="please label the sunset beach clip up front",
    )
    assert rows[0]["grounding"] == "creator_text"
    assert rows[0]["confidence"] == 1.0
    assert rows[0]["sport"] == "Sunset Beach"


def test_classic_lane_cached_vision_answer_grounds_only_above_threshold():
    above = _meta("clip-a", clip_summary="a quiet street")
    above.answers = {"what sport is this": {"answer": "basketball", "confidence": 0.85}}
    intent_above = _intent(
        assignments=[ClipAssignment(media_id="clip-a", value="Basketball", confidence=0.4)]
    )
    rows_above = _grounded_context_labels([intent_above], {"clip-a": "a.mp4"}, [above])
    assert rows_above[0]["grounding"] == "vision_verified"
    assert rows_above[0]["confidence"] == 0.85

    below = _meta("clip-b", clip_summary="a quiet street")
    below.answers = {"what sport is this": {"answer": "basketball", "confidence": 0.5}}
    intent_below = _intent(
        intent_id="i2",
        assignments=[ClipAssignment(media_id="clip-b", value="Basketball", confidence=0.4)],
    )
    assert _grounded_context_labels([intent_below], {"clip-b": "b.mp4"}, [below]) == []


def test_media_refs_use_the_real_stored_analysis_and_its_cached_answers():
    # Guided lane: `media_refs` (EditProposalSnapshot.media) wins over
    # `clip_metas` and carries the clip's REAL persisted analysis + answers.
    media_ref = SimpleNamespace(
        media_id="clip-a",
        kind="video",
        analysis={
            "understanding": {"kind": "video", "summary": "a quiet street", "subject": ""},
            "answers": {"what sport is being played": {"answer": "basketball", "confidence": 0.85}},
        },
    )
    intent = _intent(
        assignments=[
            ClipAssignment(
                media_id="clip-a",
                value="Basketball",
                confidence=0.4,
                grounding="vision_verified",
            )
        ]
    )
    rows = _grounded_context_labels([intent], {"clip-a": "gs://a.mp4"}, [], media_refs=[media_ref])
    assert rows[0]["grounding"] == "vision_verified"
    assert rows[0]["confidence"] == 0.85


def test_creator_text_fallback_requires_exact_match_key_when_creator_request_unreachable():
    """If a call site cannot reach a real creator_request, the ONLY allowed
    creator_text grounding is an EXACT match-key equality against the
    intent's own creator_text -- never a substring, unlike `ground_label`'s
    real creator_request branch."""
    meta = _meta("clip-a", clip_summary="a scenic view")
    exact = _intent(
        creator_text="Sunset Beach",
        assignments=[ClipAssignment(media_id="clip-a", value="Sunset Beach", confidence=0.4)],
    )
    rows = _grounded_context_labels([exact], {"clip-a": "a.mp4"}, [meta], creator_request="")
    assert rows[0]["grounding"] == "creator_text"

    partial = _intent(
        intent_id="i2",
        creator_text="Sunset Beach Bar",
        assignments=[ClipAssignment(media_id="clip-a", value="Sunset Beach", confidence=0.4)],
    )
    rows2 = _grounded_context_labels([partial], {"clip-a": "a.mp4"}, [meta], creator_request="")
    assert rows2 == []


def test_first_resolved_intent_claims_a_clip_even_when_it_fails_to_ground():
    meta = _meta("clip-a", clip_summary="a basketball game at the park")
    fails_to_ground = _intent(
        intent_id="i1",
        assignments=[ClipAssignment(media_id="clip-a", value="Quidditch", confidence=0.99)],
    )
    would_ground = _intent(
        intent_id="i2",
        assignments=[ClipAssignment(media_id="clip-a", value="Basketball", confidence=0.9)],
    )
    rows = _grounded_context_labels([fails_to_ground, would_ground], {"clip-a": "a.mp4"}, [meta])
    assert rows == []


# ---------------------------------------------------------------------------
# _context_sport_text_elements wiring: provenance + legacy byte-identity
# ---------------------------------------------------------------------------


def test_every_grounded_element_carries_provenance_in_source_params():
    steps = [
        SimpleNamespace(clip_id="clip-a", slot={"transition_in": "cut"}),
        SimpleNamespace(clip_id="clip-b", slot={"transition_in": "cut"}),
    ]
    plans = [{"duration_s": 2.0}, {"duration_s": 3.0}]
    grounded_rows = [
        {
            "clip_id": "clip-a",
            "sport": "Basketball",
            "source": "grounded_label",
            "grounding": "record_span",
            "confidence": 0.9,
            "intent_id": "i1",
        },
        {
            "clip_id": "clip-b",
            "sport": "Paella",
            "source": "grounded_label",
            "grounding": "creator_text",
            "confidence": 1.0,
            "intent_id": "i2",
        },
    ]
    elements = _context_sport_text_elements(
        grounded_rows,
        steps=steps,
        resolved_plans=plans,
        video_duration_s=5.0,
    )
    assert len(elements) == 2
    for element in elements:
        params = element["source_params"]
        assert params["source"] == "context_sport"
        assert params["grounding"] in {"creator_text", "record_span", "vision_verified"}
        assert isinstance(params["confidence"], float)
        assert params["intent_id"]
        # The legacy-only keys never leak into a grounded element.
        assert "context_label_source" not in params


def test_legacy_rows_cannot_fallback_into_the_visual_label_lane():
    steps = [
        SimpleNamespace(clip_id="clip-a", slot={"transition_in": "cut"}),
        SimpleNamespace(clip_id="clip-b", slot={"transition_in": "cut"}),
    ]
    plans = [{"duration_s": 2.0}, {"duration_s": 2.0}]
    legacy_rows = [
        {
            "source": "detected_sport",
            "clip_id": clip_id,
            "sport": "basketball",
            "position": "bottom_right",
            "size": "small",
            "confidence": 0.96,
        }
        for clip_id in ("clip-a", "clip-b")
    ]
    assert (
        _context_sport_text_elements(
            [],
            steps=steps,
            resolved_plans=plans,
            video_duration_s=4.0,
        )
        == []
    )
    # Passing an old receipt-shaped row as though it had reached the visual
    # lane does not turn it into output either: the worker only receives rows
    # returned by the grounding fence.
    assert (
        _context_sport_text_elements(
            legacy_rows,
            steps=steps,
            resolved_plans=plans,
            video_duration_s=4.0,
        )
        == []
    )
