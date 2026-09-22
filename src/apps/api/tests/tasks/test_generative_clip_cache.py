"""Generative candidate-cache round trips preserve clip understanding (KRI-157)."""

import json

import pytest

import app.tasks.generative_build as gb
from app.pipeline.agents.gemini_analyzer import ClipMeta
from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent


def _meta(**overrides) -> ClipMeta:
    return ClipMeta(
        clip_id="clip_0",
        transcript="hello",
        hook_text="Hook!",
        hook_score=8.5,
        best_moments=[],
        **overrides,
    )


def test_candidate_cache_round_trip_preserves_understanding_fields(monkeypatch):
    meta = _meta(
        detected_subject="friends",
        analysis_degraded=True,
        moments_synthetic=True,
        failed=True,
        clip_path="/tmp/clip.mp4",
        text_safe_zone={"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.2},
        visual_density=7.0,
        clip_summary="Two friends grill burgers in a backyard.",
        setting="backyard patio at dusk",
        activity="grilling burgers",
        people_count=2,
        speaks_to_camera=True,
        people_note="two men in aprons",
        clip_brands=["Weber"],
        clip_composition_note="subject centered, smoke drifting right",
        clip_content_type="action",
        clip_audio_type="dialogue",
    )
    stored = {}

    def persist(_job_id, patch):
        # Job.all_candidates is JSONB, so include the JSON serialization boundary.
        stored.update(json.loads(json.dumps(patch)))
        return True

    monkeypatch.setattr(gb, "_merge_all_candidates", persist)
    monkeypatch.setattr(gb, "_read_all_candidates", lambda _job_id: stored)

    gb._store_clip_metadata_cache("job-1", ["gcs/a.mp4"], [meta])
    restored = gb._load_clip_metadata_cache("job-1", ["gcs/a.mp4"])

    assert restored == [meta]
    assert restored[0] is not meta


def test_legacy_cache_uses_defaults_for_missing_understanding_fields():
    raw = {
        "clip_id": "clip_0",
        "transcript": "hello",
        "hook_text": "Hook!",
        "hook_score": 8.5,
        "best_moments": [],
    }

    assert gb._clip_meta_from_cache(raw) == _meta()


def test_cache_ignores_unknown_fields():
    meta = _meta(people_count=0, speaks_to_camera=False)
    raw = {**gb._clip_meta_to_cache(meta), "future_field": "ignore me"}

    assert gb._clip_meta_from_cache(raw) == meta


@pytest.mark.parametrize("evidence_field", ["clip_summary", "setting", "activity", "people_note"])
def test_classic_lane_record_span_label_survives_cache_round_trip(evidence_field):
    # The label's only visual evidence lives in a KRI-127 field; no persisted
    # guided-lane media record, detected_subject or best_moments can ground it.
    meta = _meta(**{evidence_field: "basketball at the park"})
    clip_id_to_gcs = {"clip_0": "gcs/a.mp4"}
    intent = ResolvedClipIntent(
        intent_id="sport-1",
        op="label",
        attribute="sport",
        status="resolved",
        assignments=[ClipAssignment(media_id="gcs/a.mp4", value="Basketball", confidence=0.9)],
    )
    expected = [
        {
            "clip_id": "clip_0",
            "sport": "Basketball",
            "source": "grounded_label",
            "grounding": "record_span",
            "confidence": 0.9,
            "intent_id": "sport-1",
        }
    ]
    assert gb._grounded_context_labels([intent], clip_id_to_gcs, [meta]) == expected

    restored = gb._clip_meta_from_cache(json.loads(json.dumps(gb._clip_meta_to_cache(meta))))

    assert gb._grounded_context_labels([intent], clip_id_to_gcs, [restored]) == expected
