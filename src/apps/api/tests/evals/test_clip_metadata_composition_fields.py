"""Structural eval coverage for the composition fields added to
nova.video.clip_metadata (text_safe_zone / visual_density / composition_note),
consumed by overlay_sizing for the agent-decided generative intro size.

Replay-safe + offline: this asserts the agent's OUTPUT CONTRACT (additive,
tolerant, defaulted) and that the fields actually drive the sizer — it does not
call the model, so it runs in CI without Gemini. The live-eval pass for scoring
drift on the richer prompt is a separate manual gate
(`NOVA_EVAL_MODE=live ... --with-judge`), per the CLAUDE.md prompt-change rule.
"""

from __future__ import annotations

import json

from app.agents.clip_metadata import (
    ClipMetadataAgent,
    ClipMetadataInput,
    ClipMetadataOutput,
)


def test_parse_threads_composition_fields_through():
    # REGRESSION (inert-feature bug, caught by local-render): parse() rebuilds the
    # output field-by-field, so the composition fields must be passed explicitly or
    # the model's real values are silently dropped before the sizer sees them.
    agent = ClipMetadataAgent(model_client=None)
    raw = json.dumps(
        {
            "hook_text": "x",
            "hook_score": 1,
            "detected_subject": "sunset over bridge",
            "text_safe_zone": {"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.5},
            "visual_density": 2,
            "composition_note": "horizon line low, open sky above",
            "best_moments": [
                {"start_s": 0.0, "end_s": 5.0, "energy": 1, "description": "static sunset"}
            ],
        }
    )
    out = agent.parse(raw, ClipMetadataInput(file_uri="x"))
    assert out.text_safe_zone == {"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.5}
    assert out.visual_density == 2.0
    assert out.composition_note == "horizon line low, open sky above"


def test_parse_tolerates_missing_or_junk_composition():
    agent = ClipMetadataAgent(model_client=None)
    raw = json.dumps(
        {
            "hook_text": "x",
            "hook_score": 1,
            "text_safe_zone": "not-a-dict",
            "visual_density": "NaN",
            "best_moments": [{"start_s": 0, "end_s": 2, "energy": 1, "description": "d"}],
        }
    )
    out = agent.parse(raw, ClipMetadataInput(file_uri="x"))
    assert out.text_safe_zone is None
    assert out.visual_density == 5.0
    assert out.composition_note == ""


def test_composition_fields_default_when_absent():
    # The fields are additive: a model that omits them (or a pre-bump replay
    # fixture) must still parse, with safe defaults — generative never hard-fails.
    out = ClipMetadataOutput(hook_text="x", hook_score=5)
    assert out.text_safe_zone is None
    assert out.visual_density == 5.0
    assert out.composition_note == ""


def test_composition_fields_round_trip():
    out = ClipMetadataOutput(
        hook_text="x",
        hook_score=5,
        text_safe_zone={"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.4},
        visual_density=2.0,
        composition_note="open sky above",
    )
    assert out.text_safe_zone["w"] == 0.8
    assert out.visual_density == 2.0
    assert out.composition_note == "open sky above"


# NOTE: the agent-fields → sizer → in-envelope-px integration is asserted in
# tests/pipeline/test_overlay_sizing.py, not here. That path lazy-imports skia,
# which the lightweight "Structural evals" CI job (tests/evals/ only) runs WITHOUT
# system GL libs (libEGL). Keep this eval file skia-free: it covers the agent's
# OUTPUT CONTRACT (schema), which is the eval-layer concern.
