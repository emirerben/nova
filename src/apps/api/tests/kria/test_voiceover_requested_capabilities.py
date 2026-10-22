"""Phone Voiceover additions are real capabilities, never unconditional promises."""

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    check_requirement,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.kria.strategy_policy import CheckedStrategy, RefusedStrategy, check_strategy_for_runtime_v2
from app.schemas.edit_proposal import MontageAudioPlan
from app.services import creator_capabilities as capabilities
from app.services.client_protocol import clear_request_context, set_client_protocol
from tests.kria.test_strategy_policy import CLIPS, _narrated, _narrated_manifest


@pytest.mark.parametrize(
    "protocol,enabled,allowed", [(13, True, False), (14, False, False), (14, True, True)]
)
def test_selected_audio_requires_new_app_and_flag(
    prod_profile, monkeypatch, protocol, enabled, allowed
):
    monkeypatch.setattr(capabilities.settings, "phone_voiceover_source_audio_enabled", enabled)
    set_client_protocol(protocol)
    try:
        manifest = _narrated_manifest()
        strategy = _narrated(montage_audio=MontageAudioPlan(source_media_ids=[CLIPS[1]]))
        result = check_strategy_for_runtime_v2(manifest, strategy)
        assert isinstance(result, CheckedStrategy if allowed else RefusedStrategy)
        if allowed:
            assert result.strategy.montage_audio.source_media_ids == [CLIPS[1]]
    finally:
        clear_request_context()


def test_spoken_text_draft_no_longer_claims_impossible(prod_profile, monkeypatch):
    monkeypatch.setattr(capabilities.settings, "phone_voiceover_spoken_text_enabled", True)
    set_client_protocol(14)
    try:
        facts = plan_facts_from_strategy(
            _narrated().model_dump(mode="json"), manifest=_narrated_manifest()
        )
        req = BriefRequirement(
            id="r6",
            kind="text",
            scope="global",
            literal="20 dk",
            description="when I say yirmi dakika",
            facts={"triggers": ["yirmi dakika"]},
        )
        receipt = check_requirement(req, facts)
        assert receipt.status == "partial"
        assert "matching words" in receipt.reason
        assert "can't" not in receipt.reason
    finally:
        clear_request_context()


_SPOKEN_WORD_REQUEST = BriefRequirement(
    id="r6",
    kind="text",
    scope="global",
    literal="20 dk",
    description="when I say yirmi dakika",
    facts={"triggers": ["yirmi dakika"]},
)


def _rendered_voiceover_variant(receipt=None) -> dict:
    variant = {
        "render_destination": "device",
        "resolved_archetype": "narrated",
        # Matching caption copy must never certify a word-timed text request.
        "caption_cues": [{"text": "20 dk", "start_s": 1.0, "end_s": 2.0}],
        "text_elements": [{"id": "title", "text": "20 dk", "start_s": 0.0, "end_s": 2.0}],
    }
    if receipt is not None:
        variant["spoken_text_placement"] = {"version": 1, "rows": [receipt]}
    return variant


def test_rendered_voiceover_spoken_word_requires_a_placement_receipt():
    facts = plan_facts_from_phone_variant(_rendered_voiceover_variant())

    assert facts.rendered_variant is True
    assert facts.phone_voiceover_spoken_text is False
    assert check_requirement(_SPOKEN_WORD_REQUEST, facts).status == "partial"


def test_rendered_voiceover_nonplaced_spoken_word_receipt_blocks_matching_copy():
    facts = plan_facts_from_phone_variant(
        _rendered_voiceover_variant(
            {"requirement_id": "r6", "status": "not_heard", "text": "20 dk"}
        )
    )

    assert check_requirement(_SPOKEN_WORD_REQUEST, facts).status == "partial"


def test_rendered_voiceover_placed_receipt_must_name_the_requested_copy():
    wrong_content = plan_facts_from_phone_variant(
        _rendered_voiceover_variant({"requirement_id": "r6", "status": "placed", "text": "30 dk"})
    )
    placed = plan_facts_from_phone_variant(
        _rendered_voiceover_variant({"requirement_id": "r6", "status": "placed", "text": "20 dk"})
    )

    # The caption/title say "20 dk" in both variants. Only the per-request
    # placement receipt can prove that it appeared at the spoken cue.
    assert check_requirement(_SPOKEN_WORD_REQUEST, wrong_content).status == "partial"
    assert check_requirement(_SPOKEN_WORD_REQUEST, placed).status == "met"


def test_placed_spoken_text_does_not_certify_an_extra_duration_request():
    facts = plan_facts_from_phone_variant(
        _rendered_voiceover_variant({"requirement_id": "r6", "status": "placed", "text": "20 dk"})
    )
    req = _SPOKEN_WORD_REQUEST.model_copy(
        update={"facts": {"triggers": ["yirmi dakika"], "duration_s": 10}}
    )
    assert check_requirement(req, facts).status == "partial"
