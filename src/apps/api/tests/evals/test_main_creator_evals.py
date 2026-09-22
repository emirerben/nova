"""Replay/live eval gate for nova.creator.main."""

from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "main_creator"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_main_creator_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    judge = judge_for(fixture.agent) if with_judge else None
    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )
    assert result.passed, f"{result.summary()}: {result.structural_failures}"

    if fixture.meta.get("phone_original_audio"):
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        assert action["strategy"]["render_program"] == "guided"
        assert action["strategy"]["montage_audio"]["preserve_source_audio"] is True
        assert action["strategy"]["montage_audio"]["source_media_ids"] == []

    if fixture.meta.get("manual_visual_removal"):
        assert result.output is not None
        assert result.output["action"]["kind"] == "ask_user", (
            "A strategy cannot remove manual media layers; it must not clear text instead"
        )

    expected = fixture.meta.get("text_intent")
    if expected:
        from app.agents._schemas.creator_agent import (
            CreativeStrategy,
            CreatorRenderIntentEvidence,
        )
        from app.routes.creator_agent import _apply_explicit_render_intent

        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy["opening_title"] == expected["title"]
        assert strategy["text_color"] == expected["color"]
        assert strategy["target_duration_s"] <= 12
        evidence = CreatorRenderIntentEvidence.model_validate(action["render_intent_evidence"])
        validated = _apply_explicit_render_intent(
            CreativeStrategy.model_validate(strategy),
            fixture.input["creator_request"],
            render_intent_evidence=evidence,
        )
        assert validated.opening_title == expected["title"]
        assert validated.text_color == expected["color"]

    clip_intents_meta = fixture.meta.get("clip_intents")
    if clip_intents_meta:
        # Structural-only: the eval cassette ignores the rendered prompt (see
        # CassetteModelClient), so this fixture does not exercise
        # `settings.clip_intents_enabled` -- it pins that a model response
        # using the new open-vocabulary `clip_intents` shape (for a request
        # the coded sport/participant/score fields cannot express) still
        # parses under the current `CreativeStrategy` contract, and that no
        # per-clip answer or resolved assignment ever appears in raw model
        # output.
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy.get("resolved_clip_intents") is None
        intents = strategy.get("clip_intents") or []
        assert sorted(intent["op"] for intent in intents) == sorted(clip_intents_meta["ops"])
        if eval_mode == "live":
            # Open vocabulary: the live model's wording varies. Live mode needs
            # `CLIP_INTENTS_ENABLED=true` in the env (the prompt teaches the
            # field only when the flag is on; off, the model asks instead).
            keywords = clip_intents_meta.get("live_keywords") or []
            for intent, keyword in zip(intents, keywords, strict=True):
                assert keyword.casefold() in intent["attribute"].casefold(), (
                    keyword,
                    intent["attribute"],
                )
        else:
            assert [intent["attribute"] for intent in intents] == clip_intents_meta["attributes"]
        for intent in intents:
            assert "assignments" not in intent
            assert "media_id" not in intent

    transcript_kinds = fixture.meta.get("transcript_label_kinds")
    if transcript_kinds:
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy["execution_contract"] == "guided_voiceover_v1"
        intents = strategy.get("clip_intents") or []
        assert {intent.get("transcript_kind") for intent in intents} == set(transcript_kinds)
        assert all(intent.get("label_source") == "transcript" for intent in intents)
        assert all(intent["op"] == "label" and not intent.get("creator_text") for intent in intents)
        assert strategy.get("resolved_clip_intents") is None

    exact_copy = fixture.meta.get("exact_copy_intent")
    if exact_copy:
        from app.agents._schemas.creator_agent import (
            CreativeStrategy,
            CreatorRenderIntentEvidence,
        )
        from app.routes.creator_agent import _apply_explicit_render_intent

        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        evidence = CreatorRenderIntentEvidence.model_validate(action["render_intent_evidence"])
        grounded = _apply_explicit_render_intent(
            CreativeStrategy.model_validate(action["strategy"]),
            fixture.input["creator_request"],
            render_intent_evidence=evidence,
        )
        # Every exact copy field must survive the verbatim-evidence boundary.
        for field, value in exact_copy.items():
            assert getattr(grounded, field) == value, field
