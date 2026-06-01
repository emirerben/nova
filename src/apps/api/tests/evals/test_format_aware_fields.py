"""Structural eval coverage for the format-aware edit engine's new agent outputs
(Lane A): content_plan's `edit_format` and clip_metadata's `content_type` /
`audio_type` labels.

Replay-safe + offline: asserts the agents' OUTPUT CONTRACT (additive, defaulted,
and — critically — coercion-tolerant so a drifted LLM token degrades to the safe
default instead of dropping a plan item or breaking clip parsing). It does not
call the model. The live-eval pass for scoring drift on the richer prompts is a
separate manual gate (`NOVA_EVAL_MODE=live ... --with-judge`), per the CLAUDE.md
prompt-change rule. content_plan's live gate ran clean (3/3); clip_metadata's is
blocked by GC'd fixture source videos (see CLAUDE.md), so this output-contract
lock is the eval-layer guard for the clip labels until fixtures are regenerated.
"""

from __future__ import annotations

import json

from app.agents._schemas.content_plan import PlanItemSpec
from app.agents.clip_metadata import ClipMetadataAgent, ClipMetadataInput, ClipMetadataOutput


def test_content_plan_edit_format_defaults_and_coerces():
    # Absent → montage (today's behavior). Unknown → montage, never raises (one
    # drifted token must not 422 a whole plan).
    assert PlanItemSpec(day_index=1, theme="t", idea="i").edit_format == "montage"
    assert (
        PlanItemSpec(day_index=1, theme="t", idea="i", edit_format="vibey").edit_format == "montage"
    )
    assert (
        PlanItemSpec(day_index=1, theme="t", idea="i", edit_format="Talking-Head").edit_format
        == "talking_head"
    )


def test_clip_metadata_labels_default_when_absent():
    # Additive: a model that omits the labels (or a pre-bump replay fixture) parses
    # with neutral defaults that never falsely promote a clip to the talking spine.
    out = ClipMetadataOutput(hook_text="x", hook_score=5)
    assert out.content_type == "broll"
    assert out.audio_type == "ambient"


def test_clip_metadata_labels_round_trip_and_coerce():
    out = ClipMetadataOutput(
        hook_text="x", hook_score=5, content_type="talking_head", audio_type="dialogue"
    )
    assert (out.content_type, out.audio_type) == ("talking_head", "dialogue")
    # Unknown content_type must NOT coerce to talking_head (would wrongly win spine).
    bad = ClipMetadataOutput(hook_text="x", hook_score=5, content_type="vlog-face")
    assert bad.content_type == "broll"


def test_parse_threads_format_labels_through():
    # REGRESSION (same inert-feature trap as the composition fields): parse()
    # rebuilds the output field-by-field, so the labels must be threaded explicitly
    # or Gemini's real values are silently dropped before Lane C's dispatch sees them.
    agent = ClipMetadataAgent(model_client=None)
    raw = json.dumps(
        {
            "hook_text": "let me show you",
            "hook_score": 7,
            "content_type": "talking_head",
            "audio_type": "dialogue",
            "best_moments": [
                {"start_s": 0.0, "end_s": 5.0, "energy": 5, "description": "to camera"}
            ],
        }
    )
    out = agent.parse(raw, ClipMetadataInput(file_uri="x"))
    assert out.content_type == "talking_head"
    assert out.audio_type == "dialogue"


def test_parse_defaults_labels_when_absent():
    agent = ClipMetadataAgent(model_client=None)
    raw = json.dumps(
        {
            "hook_text": "x",
            "hook_score": 1,
            "best_moments": [{"start_s": 0, "end_s": 2, "energy": 1, "description": "d"}],
        }
    )
    out = agent.parse(raw, ClipMetadataInput(file_uri="x"))
    assert out.content_type == "broll"
    assert out.audio_type == "ambient"
