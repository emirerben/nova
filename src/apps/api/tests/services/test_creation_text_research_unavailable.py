"""Research that finds no usable source must not cost the creator the approved video.

Prod 2026-11-20, thread de471354 (jobs 2e76b62f, cc3e380b): a creator asked for informative
captions about the Kyoto Toroko train and a title. Search returned cited facts from the train's
operator, a city government and travel sites; the institutional-source filter dropped all of
them, and the render failed. A retry then sent the identical search under the identical paid-call
key and the ledger refused it, which chat reported as "the AI took too long".

Failure modes written down before the fix:
 1. no source passes the filter, a title is also asked: the title renders, the drop names the ask;
 2. no source passes and nothing else was asked: still a typed failure, with the research reason;
 3. a truncated or malformed model answer (a SchemaError that does not wrap ResearchUnavailable)
    is ours: it keeps failing, never reported as a source limit;
 4. a second compose of one run, or a worker retry, sends its research search under a new key;
 5. the first compose of the first delivery keeps the bare id, so nothing else changes.

The planner, brief, composer parse, filter and replay are real; the provider answer is the
recorded one, minus the search widget.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents._runtime import RunContext, SchemaError, TerminalSchemaError
from app.kria.brief import BriefRequirement, CreativeBrief
from app.pipeline.guided_story import compile_proposal_execution_plan
from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
from app.services import researched_text
from app.services.ai_cost_control import logical_call_id
from app.services.creation_text_composition import (
    CreationTextComposer,
    CreationTextCompositionError,
    compose_creation_text,
)
from app.tasks.generative_build import _creation_text_request_id

TITLE = "Kyoto toroko treni"
ASK = "Kyoto'daki Toroko treni hakkında bilgilendirici alt yazı istiyorum."
UTTERANCE = f"Başlık {TITLE} olsun. {ASK}"


def _source(host: str) -> dict:
    # Provider grounding redirects carry the real domain in the title.
    return {
        "title": host,
        "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ" + host,
    }


# The recorded answer: every host is the operator, a city government or a travel site.
RECORDED = {
    "facts": [
        {
            "id": "fact-0",
            "text": "The Sagano Scenic Railway began operating on April 27, 1991",
            "sources": [_source("sagano-kanko.co.jp"), _source("wikipedia.org")],
        },
        {
            "id": "fact-1",
            "text": "The route covers 7.3 kilometers along the Hozugawa River",
            "sources": [_source("city.kameoka.kyoto.jp"), _source("japan.travel")],
        },
        {
            "id": "fact-2",
            "text": "The journey typically lasts about 25 minutes",
            "sources": [_source("kyoto.travel"), _source("japan-guide.com")],
        },
    ],
    "retrieved_at": "2026-11-20T14:08:54.497068+00:00",
    "search_entry_html": "<div>Google Search</div>",
}


def _search_runs_like_the_runtime(raw: dict | str):
    """``GroundedResearchAgent.run`` as the runtime runs it: parse, and a SchemaError from
    ``parse`` is re-raised as ``TerminalSchemaError`` (``_runtime.py`` ``run``)."""

    def run(agent, request, *, ctx=None):  # noqa: ARG001
        try:
            return agent.parse(raw if isinstance(raw, str) else json.dumps(raw), request)
        except SchemaError as exc:
            raise TerminalSchemaError(f"{agent.spec.name}: schema — {exc}") from exc

    return run


def _composer_asks_for_research(agent, input, *, ctx=None):  # noqa: ARG001
    reply = {
        "intent": "describe",
        "confidence": 0.9,
        "reply": "This needs research.",
        "ops": [],
        "research_request": {"topic": "Kyoto Toroko train", "request_quote": ASK},
    }
    return agent.parse(json.dumps(reply), input)


def _plan_and_brief(*, with_title: bool):
    reqs = [
        BriefRequirement(
            id="r3",
            kind="text",
            scope="global",
            description="informative captions about the Kyoto Toroko train",
        )
    ]
    if with_title:
        reqs.append(BriefRequirement(id="r5", kind="text", scope="title", literal=TITLE))
    brief = CreativeBrief(requirements=reqs)
    clips = [
        UnifiedClip(
            media_id=f"train-{index}",
            proxy_path=f"private/train-{index}.mp4",
            generation="1",
            duration_s=seconds,
            analysis={"understanding": {"summary": "Passengers on a scenic train"}},
        )
        for index, seconds in enumerate([6, 5, 8, 7, 5, 6])
    ]
    return plan_unified_montage(clips, brief_view(brief)), brief


@pytest.fixture
def research_answers(monkeypatch):
    def install(raw: dict | str) -> None:
        monkeypatch.setattr(CreationTextComposer, "run", _composer_asks_for_research)
        monkeypatch.setattr(
            researched_text.GroundedResearchAgent, "run", _search_runs_like_the_runtime(raw)
        )

    return install


def test_the_recorded_answer_is_the_incident_shape() -> None:
    evidence = researched_text.ResearchEvidence.model_validate(RECORDED)
    with pytest.raises(researched_text.ResearchUnavailable, match="institutional"):
        researched_text.primary_evidence(evidence)


def test_no_usable_source_renders_the_title_and_names_the_unmet_ask(research_answers) -> None:
    research_answers(RECORDED)
    plan, brief = _plan_and_brief(with_title=True)
    drops: list = []

    composed = compose_creation_text(
        plan.snapshot,
        creator_request=UTTERANCE,
        text_requirements=brief.live(),
        drops=drops,
    )

    shown = [row["text"] for row in compile_proposal_execution_plan(composed)["text_elements"]]
    assert TITLE in shown
    assert [(drop.reason, drop.text) for drop in drops] == [("incomplete", ASK)]


def test_no_usable_source_and_nothing_else_asked_fails_with_the_research_reason(
    research_answers,
) -> None:
    research_answers(RECORDED)
    plan, brief = _plan_and_brief(with_title=False)

    with pytest.raises(CreationTextCompositionError, match="institutional sources"):
        compose_creation_text(
            plan.snapshot, creator_request=ASK, text_requirements=brief.live(), drops=[]
        )


def test_a_malformed_search_answer_is_still_a_failure(research_answers) -> None:
    research_answers("not json")
    plan, brief = _plan_and_brief(with_title=True)

    with pytest.raises(TerminalSchemaError):
        compose_creation_text(
            plan.snapshot,
            creator_request=UTTERANCE,
            text_requirements=brief.live(),
            drops=[],
        )


def _key(request_id: str) -> str:
    return logical_call_id(
        feature="nova.research.public_facts",
        model="gemini-2.5-flash",
        prompt="Research this public topic: Kyoto Toroko train",
        ctx=RunContext(job_id="job-1", request_id=request_id),
        attempt=1,
    )


def test_each_compose_of_a_run_and_each_worker_retry_pays_under_its_own_key(monkeypatch) -> None:
    first = _creation_text_request_id("gen-1", 0)
    assert first == "creation-text:gen-1"

    fallback = _creation_text_request_id("gen-1", 1)
    monkeypatch.setattr("celery.current_task", SimpleNamespace(request=SimpleNamespace(retries=1)))
    retried = _creation_text_request_id("gen-1", 0)
    retried_fallback = _creation_text_request_id("gen-1", 1)

    assert len({_key(i) for i in (first, fallback, retried, retried_fallback)}) == 4
    assert _creation_text_request_id("gen-1", 0) == retried
