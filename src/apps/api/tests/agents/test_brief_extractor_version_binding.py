"""A model interprets requirements; the server owns concurrency metadata.

KRI-524 post-deploy capture: a correct style correction failed three times
because Gemini supplied version 1, a description string, then version 1 for
the current v4 brief. These cases retain that output shape with authored text.
"""

import json

import pytest

from app.agents._schemas.brief_extractor import BriefExtractionInput
from app.agents.brief_extractor import BriefExtractorAgent
from app.kria.brief import (
    BriefRequirement,
    BriefUpdateBatchError,
    CreativeBrief,
    apply_updates,
)


@pytest.mark.parametrize("model_version", [None, 1, "the previous animation request"])
@pytest.mark.parametrize("kind", ["style", "text", "audio"])
def test_model_correction_binds_to_input_version_and_still_rejects_newer_state(
    model_version: object, kind: str
) -> None:
    brief = CreativeBrief(
        version=4,
        requirements=[BriefRequirement(id="r7", kind=kind, scope="title", description="Before")],
    )
    update = {
        "operation": "change",
        "target_requirement_id": "r7",
        "kind": kind,
        "scope": "title",
        "description": "After",
    }
    if model_version is not None:
        update["expected_version"] = model_version
    result = BriefExtractorAgent(None).parse(
        json.dumps({"brief_updates": [update]}),
        BriefExtractionInput(user_message="Change it", current_brief=brief),
    )
    assert result.brief_updates[0].expected_version == 4
    assert (
        apply_updates(brief, result.brief_updates, source_turn_id=None).live()[0].description
        == "After"
    )
    # Binding to the observed input is NOT rebinding to the database's latest
    # version after inference. A concurrent edit must still make this stale.
    with pytest.raises(BriefUpdateBatchError, match="exact current brief version"):
        apply_updates(
            brief.model_copy(update={"version": 5}), result.brief_updates, source_turn_id=None
        )


def test_removal_binds_version_without_losing_target_validation() -> None:
    brief = CreativeBrief(
        version=8,
        requirements=[BriefRequirement(id="r2", kind="style", scope="title", description="Before")],
    )
    result = BriefExtractorAgent(None).parse(
        json.dumps({"brief_updates": [{"operation": "remove", "target_requirement_id": "r2"}]}),
        BriefExtractionInput(user_message="Remove that requirement", current_brief=brief),
    )
    assert result.brief_updates[0].expected_version == 8
    assert apply_updates(brief, result.brief_updates, source_turn_id=None).live() == []
