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

from app.agents.clip_metadata import ClipMetadataOutput


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
