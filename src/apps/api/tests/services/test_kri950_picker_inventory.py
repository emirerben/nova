"""A picker answer resumes the same inventory and settled photo copy.

Failure modes: another planner invents a restatement, previously resolved photo
copy changes, a changed source reuses old evidence, or a later typed request
silently resumes an old inventory.
"""

import pytest

from app.agents._runtime import RunContext
from app.services import clip_intent_planning as planning
from app.services.clip_selection import fold_clip_selections
from tests.services.test_kri779_single_take_moments import (
    PHOTO,
    TAKE,
    _patch,
    _photo,
    _take,
)

REQUEST = 'Include the queue. Put "Ojo de bife 🥩" on the ribeye photo.'


@pytest.mark.asyncio
async def test_a_picker_answer_keeps_the_validated_inventory_and_photo_caption(monkeypatch):
    raw = {
        "intents": [
            {
                "intent_id": "include_queue",
                "op": "include",
                "attribute": "the queue",
                "source_quote": "Include the queue",
            },
            {
                "intent_id": "caption_ribeye",
                "op": "caption",
                "attribute": "the ribeye photo",
                "source_quote": 'Put "Ojo de bife 🥩" on the ribeye photo',
                "creator_text": "Ojo de bife 🥩",
                "assignments": [{"media": "m002", "confidence": 0.95, "evidence": "ribeye photo"}],
            },
        ]
    }
    transport = _patch(monkeypatch, raw, vision={})
    monkeypatch.setattr(planning, "default_client", lambda: transport)
    clips = [_take(), _photo({"subject": "ribeye photo", "description": "ribeye steak"})]
    kwargs = dict(
        creator_request=REQUEST,
        latest_user_message=REQUEST,
        candidate_intents=None,
        clips=clips,
        run_context=RunContext(),
        max_vision_requeries=0,
    )
    first = await planning.plan_and_resolve_clip_intents(**kwargs)
    assert first.resolution.clip_question is not None
    question = first.resolution.clip_question
    assert "inventory" in question
    from app.services.clip_picker_inventory import answered_inventory

    events = [
        ("assistant", {"clip_question": question}),
        (
            "user",
            {
                "clip_selection": {
                    "question_id": question["question_id"],
                    "answers": [{"key": "include:the-queue", "media_ids": [TAKE]}],
                }
            },
        ),
    ]
    inventory = answered_inventory(events, clips)
    assert inventory is not None

    def no_more_models(**kwargs):
        raise AssertionError("a pure picker answer must not re-plan settled instructions")

    monkeypatch.setattr(transport, "invoke", no_more_models)
    resumed = await planning.plan_and_resolve_clip_intents(
        **kwargs,
        clip_selections=fold_clip_selections(events),
        picker_inventory=inventory,
    )
    assert resumed.resolution.status == "resolved"
    by_id = {i.intent_id: i for i in resumed.resolution.intents}
    assert by_id["include_queue"].media_ids() == [TAKE]
    assert by_id["caption_ribeye"].media_ids() == [PHOTO]
    assert by_id["caption_ribeye"].caption_text == "Ojo de bife 🥩"

    assert answered_inventory([*events, ("user", {"text": "change the caption"})], clips) is None
    changed = [*clips[:-1], type(clips[-1])(**{**clips[-1].__dict__, "generation": "new"})]
    assert answered_inventory(events, changed) is None
