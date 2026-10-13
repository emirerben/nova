"""KRI-651: real copy choice, montage, composition, reload and phone compiler boundaries."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from app.agents._runtime import ModelInvocation, RunContext
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.reply_language import reply_language_for
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_proposal_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import (
    UnifiedClip,
    brief_view,
    plan_unified_montage,
    skia_font_covers,
)
from app.schemas.edit_proposal import EditProposalSnapshot
from app.services.creation_text_composition import (
    CreationTextCompositionError,
    compose_creation_text,
)
from app.services.creative_copy_decisions import fold_creative_copy, media_digest, wording_question
from app.services.creative_copy_gate import creative_copy_problem
from app.services.phone_sources import PhoneSourceBinding

CASE = json.loads(
    (Path(__file__).parents[2] / "fixtures/prompt_coverage/kri651_approved_hook.json").read_text()
)
CTX = RunContext(extra={"skip_agent_run_persist": True})


def approved_plan(
    *, extra_requirements=(), closing=None, context_caption=True, font_covers=skia_font_covers
):
    media = {"clip_assignments": [{"media_id": "clip", "generation": "1"}]}
    digest = media_digest(media)
    question = wording_question(
        target="opening_title", candidate=CASE["candidate"], dependency_digest=digest
    )
    events = [
        ("assistant", {"choice_question": question}),
        (
            "user",
            {"choice_selection": {"question_id": question["question_id"], "option_key": "approve"}},
        ),
    ]
    approved = fold_creative_copy(events, dependency_digest=digest)["opening_title"].approved
    strategy = {"opening_title": approved, "closing_title": closing}
    assert creative_copy_problem(events, media, strategy=strategy) is None
    if context_caption:
        strategy["resolved_clip_intents"] = [
            {
                "op": "caption",
                "status": "resolved",
                "intent_id": "context",
                "attribute": "hook clip",
                "creator_text": CASE["context"],
                "caption_text": CASE["context"],
                "caption_grounding": "creator_text",
                "assignments": [{"media_id": "clip", "confidence": 0.9}],
            }
        ]
    brief = CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="title", literal=approved),
            *extra_requirements,
        ],
    )
    clip = UnifiedClip(
        media_id="clip",
        proxy_path="users/test/analysis-proxy-clip.mp4",
        generation="1",
        duration_s=CASE["duration_s"],
        width=1920,
        height=1080,
        orientation_degrees=90,
    )
    return (
        plan_unified_montage(
            [clip],
            brief_view(brief),
            strategy=strategy,
            clip_intents_enabled=True,
            font_covers=font_covers,
        ),
        brief,
        clip,
    )


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return ModelInvocation(raw_text=json.dumps(self.response))


def transport(monkeypatch, response):
    client = Transport(response)
    monkeypatch.setattr("app.services.creation_text_composition.default_client", lambda: client)
    return client


def test_approved_unicode_hook_survives_generation_reload_and_retry_without_text_model(monkeypatch):
    client = transport(monkeypatch, CASE["rewrite_response"])
    plan, brief, clip = approved_plan()
    assert plan.title == CASE["candidate"]
    assert plan.record()["labels"] == []
    for request in [
        CASE["context"] + "\nUse this wording",
        CASE["context"] + "\nTry generating this edit again",
    ]:
        plan.snapshot = compose_creation_text(
            plan.snapshot, creator_request=request, text_requirements=brief.live(), ctx=CTX
        )
        plan.snapshot = EditProposalSnapshot.model_validate_json(plan.snapshot.model_dump_json())
        assert plan.record()["title"] == CASE["candidate"]
        assert all(
            r.status == "met"
            for r in build_receipts(brief.live(), plan_facts_from_unified_montage(plan.record()))
        )
        execution = GuidedStoryExecutionPlan.model_validate(
            compile_proposal_execution_plan(plan.snapshot)
        )
        binding = PhoneSourceBinding(
            media_id=clip.media_id,
            proxy_path=clip.proxy_path,
            generation=clip.generation,
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=clip.duration_s,
                width=1920,
                height=1080,
                has_audio=True,
                orientation_degrees=90,
            ),
        )
        recipe = compile_phone_guided_plan(execution, (binding,))
        assert (
            " ".join(run.text for layer in recipe.text_layers for run in layer.runs)
            == CASE["candidate"]
        )
        assert any(run.shaped for layer in recipe.text_layers for run in layer.runs)
    assert client.calls == []


@pytest.mark.parametrize("response_key", ["rewrite_response", "invalid_response"])
def test_composer_cannot_replace_approved_copy_or_accept_invalid_segments(
    monkeypatch, response_key
):
    transport(monkeypatch, CASE[response_key])
    plan, _, _ = approved_plan(context_caption=False, font_covers=None)
    with pytest.raises(CreationTextCompositionError):
        compose_creation_text(
            plan.snapshot,
            creator_request="Show each word separately",
            copy_constraints={"guided-title": CASE["candidate"]},
            ctx=CTX,
        )


def test_legitimate_sequence_preserves_copy_and_constraints_after_reload(monkeypatch):
    words = CASE["candidate"].split()
    transport(
        monkeypatch,
        {
            "intent": "edit",
            "confidence": 1,
            "reply": "Prepared",
            "ops": [
                {"op": "replace_text_sequence", "selector": {"group": "title"}, "segments": words}
            ],
        },
    )
    plan, _, _ = approved_plan(context_caption=False, font_covers=None)
    plan.snapshot = compose_creation_text(
        plan.snapshot,
        creator_request="Show each word separately",
        copy_constraints={"guided-title": CASE["candidate"]},
        ctx=CTX,
    )
    restored = EditProposalSnapshot.model_validate_json(plan.snapshot.model_dump_json())
    assert restored.text_composition.copy_constraints == {"guided-title": CASE["candidate"]}
    assert (
        " ".join(row["text"] for row in compile_proposal_execution_plan(restored)["text_elements"])
        == CASE["candidate"]
    )
    restored.text_composition.operations[0]["segments"][-1] = "wrong"
    with pytest.raises(CreationTextCompositionError):
        compile_proposal_execution_plan(restored)


@pytest.mark.parametrize("requests_caption", [False, True])
def test_bound_styling_composition_adds_text_only_when_live_brief_requests_it(
    monkeypatch, requests_caption
):
    requirements = [
        BriefRequirement(id="style", kind="style", scope="title", description="Style the hook")
    ]
    if requests_caption:
        requirements.append(
            BriefRequirement(
                id="caption", kind="text", scope="per_clip", description="Add captions"
            )
        )
    plan, brief, _ = approved_plan(extra_requirements=requirements, context_caption=False)
    client = transport(
        monkeypatch,
        {
            "intent": "edit",
            "confidence": 1,
            "reply": "Prepared",
            "ops": [{"op": "add_text", "text": CASE["context"], "start_s": 0, "end_s": 2}],
        },
    )
    if requests_caption:
        result = compose_creation_text(
            plan.snapshot, creator_request=CASE["context"], text_requirements=brief.live(), ctx=CTX
        )
        assert any(
            row["text"] == CASE["context"]
            for row in compile_proposal_execution_plan(result)["text_elements"]
        )
        restored = EditProposalSnapshot.model_validate_json(result.model_dump_json())
        with pytest.raises(CreationTextCompositionError, match="unrequested text"):
            compose_creation_text(
                restored,
                creator_request="Style the hook",
                text_requirements=[req for req in brief.live() if req.id != "caption"],
                ctx=CTX,
            )
    else:
        with pytest.raises(CreationTextCompositionError, match="unrequested text"):
            compose_creation_text(
                plan.snapshot,
                creator_request=CASE["context"],
                text_requirements=brief.live(),
                ctx=CTX,
            )
    assert len(client.calls) == 1


def test_separately_requested_caption_is_kept_and_unicode_is_not_truncated():
    req = BriefRequirement(id="r2", kind="text", scope="clip:clip", literal="Çağrı 😤")
    plan, _, _ = approved_plan(extra_requirements=[req])
    assert plan.record()["labels"][0]["text"] == req.literal
    long_title = "é" * 281
    with pytest.raises(ValueError):
        plan_unified_montage(
            [UnifiedClip(media_id="c", proxy_path="c", generation="1", duration_s=5)],
            strategy={"opening_title": long_title},
        )


def test_closing_copy_and_mixed_emoji_symbols_remain_authoritative(monkeypatch):
    closing = "Çağrı → açaí 😤"
    plan, _, _ = approved_plan(context_caption=False, closing=closing)
    assert plan.snapshot.closing_title == closing
    assert skia_font_covers(plan.snapshot.font_family or "Fraunces", "Çağrı → açaí")
    transport(
        monkeypatch,
        {
            "intent": "edit",
            "confidence": 1,
            "reply": "Prepared",
            "ops": [
                {
                    "op": "rewrite_text",
                    "selector": {"ids": ["guided-closing-title"]},
                    "text": "Wrong closing",
                }
            ],
        },
    )
    with pytest.raises(CreationTextCompositionError):
        compose_creation_text(
            plan.snapshot,
            creator_request="Style the closing",
            copy_constraints={"guided-closing-title": closing},
            ctx=CTX,
        )


def test_constraints_allow_only_nfc_and_whitespace_normalization(monkeypatch):
    plan, _, _ = approved_plan(context_caption=False, font_covers=None)
    value = unicodedata.normalize("NFD", CASE["candidate"]).replace(" ", "\n")
    transport(
        monkeypatch,
        {
            "intent": "edit",
            "confidence": 1,
            "reply": "Prepared",
            "ops": [{"op": "rewrite_text", "selector": {"group": "title"}, "text": value}],
        },
    )
    result = compose_creation_text(
        plan.snapshot,
        creator_request="Format the text",
        copy_constraints={"guided-title": CASE["candidate"]},
        ctx=CTX,
    )
    assert (
        unicodedata.normalize(
            "NFC",
            " ".join(compile_proposal_execution_plan(result)["text_elements"][0]["text"].split()),
        )
        == CASE["candidate"]
    )


@pytest.mark.parametrize("language", ["en", "tr"])
def test_text_preparation_failure_is_actionable_and_never_blames_device(language):
    from app.tasks.kria_runtime import _job_failure_copy

    with reply_language_for(language):
        message = _job_failure_copy("creation_text_failed", "PRIVATE DIAGNOSTICS")
    assert message and "iPhone" not in message and "PRIVATE" not in message
    assert ("wording" if language == "en" else "yazı") in message
