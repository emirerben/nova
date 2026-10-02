"""Talking edits never send "Add captions" through clip-intent planning.

With CLIP_INTENTS_ENABLED on, runtime-v2 inventoried clip operations on every
proposed strategy. On a one-clip iPhone Talking project the clip-intent
planner read "Add captions" as a chapter `caption` op (gemini-2.5-flash,
3 local live runs, 2026-10-01: two schema failures, one caption op) and the
turn ended in a question instead of the captioned edit. Talking renderers draw
no clip intents and their captions are what the creator says, so planning
skips those formats and the strategy compile strips any footage intents the
Main Creator proposed, with a notice.

The planner cases run against the test Postgres with production flags (plus
CLIP_INTENTS_ENABLED) and a prod-shaped phone Talking thread; only the models
are stubbed.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from app.agents._runtime import ModelInvocation
from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.edit_format import CLIP_INTENT_FREE_EDIT_FORMATS
from app.config import settings
from app.kria import planner
from app.kria.strategy_policy import CheckedStrategy, RefusedStrategy, check_strategy_for_runtime_v2
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning
from app.services import creator_capabilities as capabilities
from app.services.creator_capabilities import TALKING_CLIP_INTENTS_DROPPED_NOTICE
from app.tasks import generative_build
from tests.kria.talking_thread import MEDIA_ID, plan_add_captions_turn, seed_talking_thread

CLIPS = ["analysis-proxy-ios-1.mp4", "analysis-proxy-ios-2.mp4"]
# What a model hears in "Add captions" when it reads it as a clip operation.
CHAPTER_CAPTION = ClipIntent(
    intent_id="captions",
    op="caption",
    attribute="default",
    caption_attribute="authored_from_footage",
)
CITY_LABEL = ClipIntent(intent_id="city", op="label", attribute="the city shown in the clip")


def test_talking_renderers_take_no_clip_intents() -> None:
    # Skipping the inventory is only safe while these renderers ignore clip
    # intents. If one starts drawing them, remove its format from
    # CLIP_INTENT_FREE_EDIT_FORMATS.
    assert CLIP_INTENT_FREE_EDIT_FORMATS == {"subtitled", "talking_head"}
    for renderer in (
        generative_build._render_subtitled_variant,
        generative_build._render_talking_head_variant,
        generative_build._run_phone_subtitled_job,
    ):
        assert "clip_intent" not in inspect.getsource(renderer), renderer.__name__
    # Control: the montage-family renderer does receive them.
    assert "creator_resolved_clip_intents" in (
        inspect.signature(generative_build._process_generative_variant).parameters
    )


def _phone_talking_manifest():
    return capabilities.resolve_creator_manifest(
        item_id="item-talking",
        edit_format="subtitled",
        media=[{"media_id": CLIPS[0], "kind": "video", "duration_s": 40.0}],
        phone_source_media_ids=[CLIPS[0]],
        phone_rendering_allowed=True,
    )


def _cloud_talking_head_manifest():
    return capabilities.resolve_creator_manifest(
        item_id="item-talking-head",
        edit_format="talking_head",
        media=[{"media_id": media_id, "kind": "video"} for media_id in CLIPS],
    )


@pytest.mark.parametrize(
    ("manifest_factory", "edit_format", "media_ids"),
    [
        (_phone_talking_manifest, "subtitled", CLIPS[:1]),
        (_cloud_talking_head_manifest, "talking_head", CLIPS),
    ],
)
@pytest.mark.parametrize("intent", [CHAPTER_CAPTION, CITY_LABEL], ids=["caption", "label"])
def test_talking_strategy_drops_footage_intents_with_a_notice(
    monkeypatch: pytest.MonkeyPatch,
    prod_profile,
    manifest_factory,
    edit_format: str,
    media_ids: list[str],
    intent: ClipIntent,
) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    strategy = CreativeStrategy(
        edit_format=edit_format,
        audio_strategy="original_audio",
        render_program="native",
        selected_media_ids=media_ids,
        clip_intents=[intent],
    )

    checked = check_strategy_for_runtime_v2(manifest_factory(), strategy)

    assert isinstance(checked, CheckedStrategy), checked
    assert checked.strategy.clip_intents is None
    assert checked.strategy.resolved_clip_intents is None
    assert checked.notices == (TALKING_CLIP_INTENTS_DROPPED_NOTICE,)


def test_talking_strategy_without_footage_intents_has_no_notice(
    monkeypatch: pytest.MonkeyPatch, prod_profile
) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    strategy = CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        render_program="native",
        selected_media_ids=CLIPS[:1],
    )

    checked = check_strategy_for_runtime_v2(_phone_talking_manifest(), strategy)

    assert isinstance(checked, CheckedStrategy)
    assert checked.notices == ()


def test_talking_transcript_labels_still_ask_for_a_voiceover(
    monkeypatch: pytest.MonkeyPatch, prod_profile
) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    strategy = CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        render_program="native",
        selected_media_ids=CLIPS[:1],
        clip_intents=[
            ClipIntent(
                intent_id="score",
                op="label",
                attribute="the spoken score",
                label_source="transcript",
                transcript_kind="score",
            ),
            CITY_LABEL,
        ],
    )

    refused = check_strategy_for_runtime_v2(_phone_talking_manifest(), strategy)

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "transcript_labels_unavailable"


def test_montage_keeps_footage_intents_for_the_inventory(
    monkeypatch: pytest.MonkeyPatch, prod_profile
) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-montage",
        edit_format="montage",
        media=[{"media_id": media_id, "kind": "video"} for media_id in CLIPS],
    )
    strategy = CreativeStrategy(
        edit_format="montage",
        render_program="native",
        selected_media_ids=CLIPS,
        clip_intents=[CITY_LABEL],
    )

    checked = check_strategy_for_runtime_v2(manifest, strategy)

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.clip_intents == [CITY_LABEL]
    assert TALKING_CLIP_INTENTS_DROPPED_NOTICE not in checked.notices


def test_flag_off_talking_compile_is_unchanged(monkeypatch: pytest.MonkeyPatch, prod_profile):
    # Flag off, the model boundary already discards footage intents; the
    # compile adds no notice of its own (production today).
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    strategy = CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        render_program="native",
        selected_media_ids=CLIPS[:1],
        clip_intents=[CHAPTER_CAPTION],
    )

    checked = check_strategy_for_runtime_v2(_phone_talking_manifest(), strategy)

    assert isinstance(checked, CheckedStrategy)
    assert checked.notices == ()
    assert checked.strategy.clip_intents == [CHAPTER_CAPTION]


# --- runtime-v2 planner over a real, prod-shaped phone Talking thread --------


def _creator_answer(clip_intents: list[dict] | None) -> dict:
    return {
        "action": {
            "kind": "propose_strategy",
            "strategy": {
                "direction": "native",
                "edit_format": "subtitled",
                "audio_strategy": "original_audio",
                "media_scope": "selected",
                "caption_style": "auto",
                "render_program": "native",
                "selected_media_ids": [MEDIA_ID],
                "target_duration_s": 14.8,
                "rationale": "Play the whole clip so every spoken line gets a caption.",
                **({"clip_intents": clip_intents} if clip_intents else {}),
            },
            "summary": "I'll add captions to your clip and keep its original audio.",
        },
        "brief_updates": [
            {"kind": "style", "scope": "global", "literal": None, "description": "Add captions"}
        ],
    }


# The clip-intent planner's live answer to "Add captions" (2026-10-01 repro).
_CHAPTER_CAPTION_INVENTORY = {
    "intents": [
        {
            "intent_id": "captions",
            "op": "caption",
            "attribute": "default",
            "caption_attribute": "authored_from_footage",
            "source_quote": "Add captions",
        }
    ],
    "question": None,
}


class _Models:
    """Main Creator gives `answer`; the clip-intent planner, if ever asked,
    reads the request as a chapter caption, as gemini-2.5-flash did."""

    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.creator_prompts: list[str] = []
        self.inventory_prompts: list[str] = []

    def invoke(self, **kwargs: Any) -> ModelInvocation:
        prompt = kwargs["prompt"]
        if "COMPLETE inventory of clip operations" in prompt:
            self.inventory_prompts.append(prompt)
            raw = _CHAPTER_CAPTION_INVENTORY
        else:
            self.creator_prompts.append(prompt)
            raw = self.answer
        return ModelInvocation(raw_text=json.dumps(raw), tokens_in=10, tokens_out=20)


async def _plan(monkeypatch: pytest.MonkeyPatch, models: _Models):  # noqa: ANN202
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(planner, "default_client", lambda: models)
    monkeypatch.setattr(clip_intent_planning, "default_client", lambda: models)
    return await plan_add_captions_turn(*seed_talking_thread())


@pytest.mark.asyncio
async def test_add_captions_on_phone_talking_never_runs_the_clip_inventory(
    monkeypatch: pytest.MonkeyPatch, prod_profile
) -> None:
    models = _Models(_creator_answer(None))

    result = await _plan(monkeypatch, models)

    assert result.plan.mode == "act", result.plan.response
    assert len(models.creator_prompts) == 1
    assert models.inventory_prompts == []
    apply, render = result.plan.intents
    assert apply.tool_name == "draft.apply_strategy"
    assert render.tool_name == "render.request"
    strategy = apply.arguments["strategy"]
    assert strategy["edit_format"] == "subtitled"
    assert strategy["selected_media_ids"] == [MEDIA_ID]
    assert "clip_intents" not in strategy
    assert TALKING_CLIP_INTENTS_DROPPED_NOTICE not in apply.arguments["summary"]


@pytest.mark.asyncio
async def test_main_creator_chapter_caption_on_phone_talking_is_dropped_with_a_notice(
    monkeypatch: pytest.MonkeyPatch, prod_profile
) -> None:
    models = _Models(_creator_answer([CHAPTER_CAPTION.model_dump(mode="json")]))

    result = await _plan(monkeypatch, models)

    assert result.plan.mode == "act", result.plan.response
    assert models.inventory_prompts == []
    apply = result.plan.intents[0]
    assert "clip_intents" not in apply.arguments["strategy"]
    assert TALKING_CLIP_INTENTS_DROPPED_NOTICE in apply.arguments["summary"]
    assert result.policy_notices == (TALKING_CLIP_INTENTS_DROPPED_NOTICE,)
