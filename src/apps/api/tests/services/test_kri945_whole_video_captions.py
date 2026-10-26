"""KRI-945: a whole-video text the brief gives its own lane never becomes a clip caption.

The creator asked for "some facts about Cunda ... an introductory explanation". The brief
held it as a researched global text, but the inventory planner minted a footage caption for
it, footage can never ground researched facts, and every turn ended on "What should the
caption on the Cunda Island introductory explanation say?". A researched text is written by
the research lane and a hook by the title lane, so a caption the resolver would author for
the same ask is dropped before any clip question.

Ways the drop could wrongly lose a real caption, each pinned below:
1. A per-clip caption in the creator's own words ("label the butcher shot ...").
2. A chapter caption in the creator's words whose brief text was scoped global (prod briefs
   scope chapter captions and spoken-word texts global).
3. A footage caption the creator described, recorded as a global text with no research.
4. A separate caption in the same message as a research ask, sharing a few words with it.
5. A separate caption next to a hook ask.
6. A research or title requirement the creator has since replaced.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.kria.brief import BriefRequirement, CreativeBrief
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentResolution

REQUEST = (
    "We went to Cunda Island and we should include some facts about Cunda to show why people "
    "should go there, an introductory explanation. Add a hook about it being a must visit spot "
    "with great food, town and sea"
)
RESEARCH = BriefRequirement(
    id="r1",
    kind="text",
    scope="global",
    description="An introductory explanation with facts about Cunda that show why people go",
    facts={
        "research": {
            "topic": "Reasons to visit Cunda Island, Turkey",
            "request_quote": "include some facts about Cunda to show why people should go "
            "there, an introductory explanation",
        }
    },
)
HOOK = BriefRequirement(
    id="r2",
    kind="text",
    scope="title",
    description="a hook about it being a must visit spot with great food, town and sea",
)


def _caption(quote: str, *, attribute: str, creator_text: str | None = None) -> PlannedClipIntent:
    return PlannedClipIntent(
        intent_id="caption",
        op="caption",
        attribute=attribute,
        creator_text=creator_text,
        caption_attribute=None if creator_text else "what the clips show",
        source_quote=quote,
    )


async def _requested(
    monkeypatch: pytest.MonkeyPatch,
    intent: PlannedClipIntent,
    *requirements: BriefRequirement,
    request: str = REQUEST,
) -> list[str]:
    """The intent ids the inventory hands on (resolver input and the strategy's intents)."""
    agent = MagicMock()
    agent.run.return_value = ClipIntentPlannerOutput(intents=[intent])
    monkeypatch.setattr(service, "default_client", MagicMock())
    monkeypatch.setattr(service, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    resolver = AsyncMock(return_value=IntentResolution())
    monkeypatch.setattr(service, "resolve_clip_intents_for_turn", resolver)
    brief = CreativeBrief(version=1, requirements=list(requirements))
    result = await service.plan_and_resolve_clip_intents(
        creator_request=request,
        latest_user_message="Use this wording",
        candidate_intents=None,
        clips=[],
        run_context=RunContext(),
        whole_video_texts=service.whole_video_text_asks(brief),
    )
    requested = [i.intent_id for i in result.requested_intents]
    resolved = [i.intent_id for i in resolver.await_args.kwargs["intents"]] if requested else []
    assert resolved == requested
    return requested


@pytest.mark.parametrize(
    "quote",
    [
        pytest.param(
            "include some facts about Cunda to show why people should go there, an introductory "
            "explanation",
            id="the-research-ask",
        ),
        pytest.param("an introductory explanation", id="part-of-the-research-ask"),
        pytest.param(
            "we should include some facts about Cunda to show why people should go there",
            id="research-ask-with-lead-in",
        ),
    ],
)
async def test_a_caption_for_the_researched_explanation_is_dropped(
    monkeypatch: pytest.MonkeyPatch, quote: str
) -> None:
    intent = _caption(quote, attribute="the Cunda Island introductory explanation")
    assert await _requested(monkeypatch, intent, RESEARCH, HOOK) == []


async def test_a_caption_for_the_hook_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    intent = _caption(
        "Add a hook about it being a must visit spot with great food, town and sea",
        attribute="the opening shot",
    )
    assert await _requested(monkeypatch, intent, RESEARCH, HOOK) == []


@pytest.mark.parametrize(
    ("request_text", "intent", "requirement"),
    [
        pytest.param(
            "Make a montage of our Cunda trip and label the butcher shot 'The butcher's counter'",
            _caption(
                "label the butcher shot 'The butcher's counter'",
                attribute="the butcher shot",
                creator_text="The butcher's counter",
            ),
            BriefRequirement(
                id="r3",
                kind="text",
                scope="per_clip",
                literal="The butcher's counter",
                description="the butcher shot",
            ),
            id="per-clip-creator-words",
        ),
        pytest.param(
            'Put "MICHELIN 2024 · 2025" on the plaques shot',
            _caption(
                'Put "MICHELIN 2024 · 2025" on the plaques shot',
                attribute="the plaques shot",
                creator_text="MICHELIN 2024 · 2025",
            ),
            BriefRequirement(
                id="r3",
                kind="text",
                scope="global",
                literal="MICHELIN 2024 · 2025",
                description="on the plaques shot",
            ),
            id="global-scoped-chapter-caption-in-creator-words",
        ),
        pytest.param(
            "Add a caption about the weather on the park clips",
            _caption(
                "Add a caption about the weather on the park clips", attribute="the park clips"
            ),
            BriefRequirement(
                id="r3",
                kind="text",
                scope="global",
                description="a caption about the weather on the park clips",
            ),
            id="described-footage-caption-recorded-global",
        ),
        pytest.param(
            f"{REQUEST}. Add a caption about the food on the restaurant clips",
            _caption(
                "Add a caption about the food on the restaurant clips",
                attribute="the restaurant clips",
            ),
            RESEARCH,
            id="separate-caption-beside-the-research-ask",
        ),
        pytest.param(
            f"{REQUEST}. Caption the sea clips about the beaches",
            _caption("Caption the sea clips about the beaches", attribute="the sea clips"),
            HOOK,
            id="separate-caption-beside-the-hook-ask",
        ),
        pytest.param(
            REQUEST,
            _caption("an introductory explanation", attribute="the arrival clips"),
            RESEARCH.model_copy(update={"status": "superseded"}),
            id="replaced-research-ask",
        ),
    ],
)
async def test_a_real_clip_caption_is_kept(
    monkeypatch: pytest.MonkeyPatch,
    request_text: str,
    intent: PlannedClipIntent,
    requirement: BriefRequirement,
) -> None:
    kept = await _requested(monkeypatch, intent, requirement, request=request_text)
    assert kept == ["caption"]
