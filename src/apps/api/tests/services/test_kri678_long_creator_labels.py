"""KRI-678: a creator's 4-word place name looped the clip picker forever.

"Lunapark klibine "Parque de la Costa" etiketi koy" planned a label whose text could
never ground (the old 3-word cap), so a tapped clip never settled and the same
"which clips show Lunapark klibi?" picker came back every turn.

Ways this can fail, written down before the fix:

1. A creator's own 4-word label never grounds, so the tapped clip never settles and
   the picker is re-asked (the reported loop).
2. Widening the cap for the creator also widens it for model or vision copy, letting a
   longer invented phrase onto the video (the provenance fence must stay at 3 words).
3. Widening the cap past the 24-character width bound lets a label overflow the frame.
4. A creator label that can never be printed (too long, too many words, or characters
   a label cannot hold) still reaches the picker, so the picker loops again.
5. The plain "too long" reply is only given when there is no tapped answer, so a tapped
   answer for an unusable label still re-asks the picker.
6. The render lane re-cleans the creator's text with the model bound and drops it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.schemas.clip_intents import (
    CREATOR_LABEL_MAX_WORDS,
    LABEL_MAX_CHARS,
    LABEL_MAX_WORDS,
    ClipUnderstanding,
    clean_label_text,
    creator_label_problem,
    ground_label,
)
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentClip, picker_eligible
from app.services.clip_selection import fold_clip_selections

REQUEST = 'Lunapark klibine "Parque de la Costa" etiketi koy.'


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=f"m{i}",
            kind="video",
            analysis={"subject": "amusement park rides at night", "description": "rides"},
            gcs_path=f"users/u/clips/m{i}.mp4",
        )
        for i in range(1, 4)
    ]


def _label(text: str) -> PlannedClipIntent:
    return PlannedClipIntent(
        intent_id="l1",
        op="label",
        attribute="Lunapark klibi",
        creator_text=text,
        source_quote=text,
    )


def _wire(monkeypatch, intents: list[PlannedClipIntent]) -> AsyncMock:
    agent = MagicMock()
    agent.run.return_value = ClipIntentPlannerOutput(intents=intents)
    monkeypatch.setattr(service, "default_client", MagicMock())
    monkeypatch.setattr(service, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    resolver = AsyncMock(return_value=service.IntentResolution())
    monkeypatch.setattr(service, "resolve_clip_intents_for_turn", resolver)
    return resolver


def _tapped(media_id: str = "m2"):
    return fold_clip_selections(
        [
            (
                "assistant",
                {
                    "clip_question": {
                        "version": 1,
                        "question_id": "q1",
                        "allow_none": True,
                        "categories": [
                            {
                                "key": "label:lunapark-klibi",
                                "label": "Lunapark klibi",
                                "op": "label",
                                "candidate_media_ids": ["m1", "m2", "m3"],
                                "suggested_media_ids": ["m2"],
                            }
                        ],
                    }
                },
            ),
            (
                "user",
                {
                    "clip_selection": {
                        "question_id": "q1",
                        "answers": [{"key": "label:lunapark-klibi", "media_ids": [media_id]}],
                    }
                },
            ),
        ]
    )


async def _plan(request: str, selections=None):
    return await service.plan_and_resolve_clip_intents(
        creator_request=request,
        latest_user_message=None,
        candidate_intents=None,
        clips=_clips(),
        run_context=RunContext(),
        clip_selections=selections,
    )


@pytest.mark.asyncio
async def test_tapped_clip_settles_a_four_word_creator_label(monkeypatch) -> None:
    """Failure 1: the M1 prompt renders the label after one picker answer."""
    resolver = _wire(monkeypatch, [_label("Parque de la Costa")])
    result = await _plan(REQUEST, _tapped("m2"))
    resolver.assert_not_called()
    assert not result.resolution.needs_creator
    (intent,) = result.resolution.intents
    assert intent.status == "resolved"
    assert [(a.media_id, a.value, a.grounding) for a in intent.assignments] == [
        ("m2", "Parque de la Costa", "creator_text")
    ]


def test_model_and_vision_copy_keep_the_three_word_fence() -> None:
    """Failure 2: only the creator's own words get the wider word cap."""
    record = ClipUnderstanding(subject="parque de la costa amusement park")
    assert (
        ground_label(
            media_id="m1",
            value="Parque de la Costa",
            confidence=1.0,
            creator_request="label the park clip",
            record=record,
            vision_answer="Parque de la Costa",
            vision_confidence=1.0,
        )
        is None
    )
    assert clean_label_text("Parque de la Costa") is None
    assert LABEL_MAX_WORDS == 3


def test_creator_cap_stays_inside_the_width_bound() -> None:
    """Failure 3: the 24-character width bound applies to creator labels too."""
    assert clean_label_text("Sagrada Família de Gaudí", max_words=CREATOR_LABEL_MAX_WORDS)
    assert (
        clean_label_text("Parque Nacional Tierra del Fuego", max_words=CREATOR_LABEL_MAX_WORDS)
        is None
    )
    assert LABEL_MAX_CHARS == 24


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("Parque de la Costa", None),
        ("Parque Nacional Tierra del Fuego", "too_long"),
        ("a b c d e f", "too_many_words"),
        ("Café #1", "characters"),
    ],
)
def test_creator_label_problem_names_why_text_cannot_print(text, problem) -> None:
    assert creator_label_problem(text) == problem


@pytest.mark.asyncio
@pytest.mark.parametrize("tapped", [False, True])
@pytest.mark.parametrize(
    ("text", "en_hint"),
    [
        ("Parque Nacional Tierra del Fuego", "too long"),
        ("Café #1", "letters"),
    ],
)
async def test_unusable_creator_label_gets_a_plain_reply_never_the_picker(
    monkeypatch, text, en_hint, tapped
) -> None:
    """Failures 4 + 5: no clip question, with or without a tapped answer."""
    resolver = _wire(monkeypatch, [_label(text)])
    result = await _plan(f'Lunapark klibine "{text}" etiketi koy.', _tapped() if tapped else None)
    resolver.assert_not_called()
    resolution = result.resolution
    assert resolution.needs_creator
    assert resolution.clip_question is None
    assert resolution.error_code == "label_text_unusable"
    assert text in (resolution.question or "")
    assert en_hint in (resolution.question or "")
    assert "which" not in (resolution.question or "").lower()


def test_unusable_creator_label_is_never_a_picker_category() -> None:
    assert picker_eligible(_label("Parque de la Costa"))
    assert not picker_eligible(_label("Parque Nacional Tierra del Fuego"))
