"""Regression guards for speech subtitles versus authored on-screen copy.

These are prompt-contract and offline parser/verifier checks.  They deliberately
do not claim that a provider generated the replay payload semantically.
"""

from __future__ import annotations

import json

import pytest

from app.agents._schemas.brief_extractor import BriefExtractionInput
from app.agents.brief_extractor import BriefExtractorAgent
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.agents.main_creator import MainCreatorAgent
from app.kria.brief import CreativeBrief, apply_updates
from app.kria.brief_checks import PlanFacts, asks_caption_language, build_receipts
from tests.agents.test_main_creator_agent import _input as main_creator_input
from tests.test_edit_copilot import _snapshot as editor_snapshot

_SPEECH_LANE_RULE = "Captions and subtitles are the speech lane"
_TITLE = "Tigre’de bir gün"


def _brief_replay(message: str) -> str:
    """Saved-shape provider replay, parsed through the production brief parser."""
    restriction = (
        "Başlık dışında ek yazı olmasın."
        if "Türkçe" in message or "Başlık" in message
        else "No added text except the title."
    )
    return json.dumps(
        {
            "brief_updates": [
                {
                    "operation": "add",
                    "kind": "text",
                    "scope": "title",
                    "literal": _TITLE,
                    "description": "title only",
                    "facts": {},
                },
                {
                    "operation": "add",
                    "kind": "style",
                    "scope": "global",
                    "literal": None,
                    "description": "English subtitles",
                    "facts": {"captions": "on", "caption_language": "en"},
                },
                {
                    "operation": "add",
                    "kind": "style",
                    "scope": "global",
                    "literal": None,
                    "description": restriction,
                    "facts": {},
                },
            ]
        },
        ensure_ascii=False,
    )


def _assert_title_and_speech_caption_acceptance(brief: CreativeBrief, facts: PlanFacts) -> None:
    """Incident acceptance: title plus English speech lane, with no decorative copy."""
    title, captions, _authored_text_restriction = brief.live()
    receipts = build_receipts((title, captions), facts)
    assert {receipt.requirement_id: receipt.status for receipt in receipts} == {
        title.id: "met",
        captions.id: "met",
    }
    assert facts.caption_language == "en"
    assert facts.texts == ()


def test_prompt_contract_keeps_speech_subtitles_separate_from_decorative_text() -> None:
    """Every planner surface teaches the same speech lane, including brief-off."""
    brief_on = MainCreatorAgent(None).render_prompt(  # type: ignore[arg-type]
        main_creator_input().model_copy(update={"brief_enabled": True})
    )
    brief_off = MainCreatorAgent(None).render_prompt(  # type: ignore[arg-type]
        main_creator_input().model_copy(update={"brief_enabled": False})
    )
    narrow = BriefExtractorAgent(None).render_prompt(  # type: ignore[arg-type]
        BriefExtractionInput(creator_request="", user_message="x", conversation=[])
    )

    assert _SPEECH_LANE_RULE in brief_on
    assert _SPEECH_LANE_RULE in brief_off
    assert _SPEECH_LANE_RULE in narrow


@pytest.mark.parametrize(
    "message",
    [
        (
            "My voice is Turkish but make the subtitles English. "
            'Title: "Tigre’de bir gün". No other text.'
        ),
        (
            "Seslendirmem Türkçe ama altyazılar İngilizce olsun. "
            'Başlık: "Tigre’de bir gün". Başlık dışında başka yazı olmasın.'
        ),
    ],
)
def test_title_only_and_english_speech_subtitles_are_independent_requirements(message: str) -> None:
    """The EN/TR incident shape has a title plus English speech captions, not a conflict."""
    parsed = BriefExtractorAgent(None).parse(  # type: ignore[arg-type]
        _brief_replay(message),
        BriefExtractionInput(creator_request="", user_message=message, conversation=[]),
    )
    brief = apply_updates(CreativeBrief(), parsed.brief_updates, source_turn_id=None)
    title, captions, restriction = brief.live()

    assert (title.kind, title.scope, title.literal) == ("text", "title", _TITLE)
    assert (captions.kind, captions.scope, captions.literal) == ("style", "global", None)
    assert captions.facts == {"captions": "on", "caption_language": "en"}
    assert (restriction.kind, restriction.literal, restriction.facts) == ("style", None, {})
    assert restriction.description in {
        "No added text except the title.",
        "Başlık dışında ek yazı olmasın.",
    }
    assert asks_caption_language(captions)

    _assert_title_and_speech_caption_acceptance(
        brief, PlanFacts(title=_TITLE, title_source="brief", caption_language="en")
    )


def test_title_caption_acceptance_rejects_wrong_or_missing_subtitles_and_extra_label() -> None:
    """Negative controls: a title cannot hide wrong/missing subtitles or an extra label."""
    parsed = BriefExtractorAgent(None).parse(  # type: ignore[arg-type]
        _brief_replay('Title: "Tigre’de bir gün"; English subtitles only.'),
        BriefExtractionInput(
            creator_request="",
            user_message='Title: "Tigre’de bir gün"; English subtitles only.',
            conversation=[],
        ),
    )
    brief = apply_updates(CreativeBrief(), parsed.brief_updates, source_turn_id=None)

    with pytest.raises(AssertionError):
        _assert_title_and_speech_caption_acceptance(
            brief, PlanFacts(title=_TITLE, title_source="brief", caption_language="tr")
        )
    with pytest.raises(AssertionError):
        _assert_title_and_speech_caption_acceptance(
            brief,
            PlanFacts(
                title=_TITLE,
                title_source="brief",
                caption_style="none",
                edit_format="subtitled",
            ),
        )
    with pytest.raises(AssertionError):
        _assert_title_and_speech_caption_acceptance(
            brief,
            PlanFacts(
                title=_TITLE,
                title_source="brief",
                caption_language="en",
                texts=("EXTRA DECORATIVE LABEL",),
            ),
        )


def test_no_captions_control_does_not_remove_a_title() -> None:
    """A caption-off fact controls only the speech lane; an authored title still remains."""
    raw = json.loads(_brief_replay("No captions; keep the title."))
    raw["brief_updates"][1]["facts"] = {"captions": "off"}
    raw["brief_updates"][1]["description"] = "No captions; keep the title."
    parsed = BriefExtractorAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw, ensure_ascii=False),
        BriefExtractionInput(
            creator_request="", user_message="No captions; keep the title.", conversation=[]
        ),
    )
    brief = apply_updates(CreativeBrief(), parsed.brief_updates, source_turn_id=None)
    title, captions, _authored_text_restriction = brief.live()

    assert title.literal == _TITLE
    assert captions.facts == {"captions": "off"}
    receipts = build_receipts(
        brief.live(),
        PlanFacts(
            title=_TITLE,
            title_source="brief",
            caption_style="none",
            edit_format="subtitled",
        ),
    )
    assert {receipt.requirement_id: receipt.status for receipt in receipts} == {
        title.id: "met",
        captions.id: "met",
    }


def test_edit_copilot_prompt_preserves_speech_subtitles_when_removing_extra_text() -> None:
    """The removal instruction must not turn a title-only ask into caption removal."""
    prompt = EditCopilotAgent(None).render_prompt(  # type: ignore[arg-type]
        EditCopilotInput(
            utterance="Keep the title and remove every other decorative word.",
            variant_snapshot=editor_snapshot(),
        )
    )

    assert _SPEECH_LANE_RULE in prompt
