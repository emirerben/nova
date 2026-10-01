"""KRI-238: "Add captions" on a one-clip iPhone Talking project ended in
"I couldn't finish that step" (prod thread 467a02c4, 2026-10-01).

The Main Creator answered with `media_scope: "all"` for the single clip. "all"
needs guided proposals, which a phone Talking edit never has, so every attempt
was refused as a schema error and the turn failed with
`RuntimeError: Kria could not produce a reliable editorial plan`. Runs against
the test Postgres with production flags, the prod clip receipt, and the prod
thread events; only the model is stubbed, with the answer it gave.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agents._runtime import ModelInvocation
from app.agents.main_creator import MAIN_CREATOR_CONVERSATION_MAX
from app.kria import planner
from tests.kria.talking_thread import MEDIA_ID, plan_add_captions_turn, seed_talking_thread

# The answer the live Main Creator gave on a local replay of this turn.
_ADD_CAPTIONS_ANSWER = {
    "action": {
        "kind": "propose_strategy",
        "strategy": {
            "direction": "native",
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "media_scope": "all",
            "caption_style": "auto",
            "render_program": "native",
            "selected_media_ids": [MEDIA_ID],
            "target_duration_s": 14.8,
            "rationale": "Play the whole clip so every spoken line gets a caption.",
        },
        "summary": "I'll add captions to your clip and keep its original audio.",
    },
    "brief_updates": [
        {"kind": "style", "scope": "global", "literal": None, "description": "Add captions"}
    ],
}


class _SameAnswerClient:
    """Gives the same answer on every attempt, as the model did in prod."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, **kwargs: Any) -> ModelInvocation:
        self.prompts.append(kwargs["prompt"])
        return ModelInvocation(
            raw_text=json.dumps(_ADD_CAPTIONS_ANSWER), tokens_in=10, tokens_out=20
        )


@pytest.mark.asyncio
async def test_add_captions_on_one_clip_phone_talking_project_proposes_captions(
    monkeypatch: pytest.MonkeyPatch, prod_profile
) -> None:
    client = _SameAnswerClient()
    monkeypatch.setattr(planner, "default_client", lambda: client)
    user_id, thread_id, item_id = seed_talking_thread()

    result = await plan_add_captions_turn(user_id, thread_id, item_id)

    assert result.plan.mode == "act", result.plan.response
    assert len(client.prompts) == 1
    strategy = result.plan.intents[0].arguments["strategy"]
    assert strategy["edit_format"] == "subtitled"
    assert strategy["selected_media_ids"] == [MEDIA_ID]
    assert strategy["render_program"] == "native"


@pytest.mark.asyncio
async def test_long_thread_sends_the_main_creator_its_bounded_latest_history(
    monkeypatch: pytest.MonkeyPatch, prod_profile
) -> None:
    # Prod thread e798bda2 failed every turn once it held more than 20 chat
    # messages: the planner loaded 24 and the Main Creator input allows 20.
    seen: list = []

    async def creator(inputs, **_kw):  # noqa: ANN001, ANN202
        seen.append(inputs.agent_input.conversation)
        return planner.MainCreatorOutput.model_validate(
            {"action": {"kind": "ask_user", "question": "Which part?", "reason_code": "x"}}
        )

    monkeypatch.setattr(planner, "_call_main_creator", creator)
    history = [
        ("user" if i % 2 else "assistant", f"message {i}")
        for i in range(MAIN_CREATOR_CONVERSATION_MAX + 10)
    ]
    user_id, thread_id, item_id = seed_talking_thread(history=history)

    result = await plan_add_captions_turn(user_id, thread_id, item_id)

    assert result.plan.mode == "respond"
    (conversation,) = seen
    assert len(conversation) == MAIN_CREATOR_CONVERSATION_MAX
    assert conversation[-1] == {"role": "user", "content": "Add captions"}
