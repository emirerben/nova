"""KRI-189: the Main Creator prompt explains clip `facts` only when a clip carries them."""

from __future__ import annotations

import json

from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from tests.agents.test_main_creator_agent import _input


def _with_media_context(media_context: list[dict]) -> MainCreatorInput:
    return _input().model_copy(update={"media_context": media_context})


def _prompt(agent_input: MainCreatorInput) -> str:
    return MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]


_FACT = {"kind": "landmark", "value": "Rumeli Hisarı", "provenance": "inferred", "confidence": 0.8}


def test_prompt_explains_facts_and_provenance_when_a_clip_has_them() -> None:
    prompt = _prompt(
        _with_media_context([{"media_id": "clip-1", "kind": "video", "facts": [_FACT]}])
    )
    assert "Each item may also carry `facts`" in prompt
    assert "`inferred` is a best guess" in prompt
    assert "$clip_facts_note" not in prompt
    # The fact itself reaches the model beside (not inside) the AI evidence.
    assert json.dumps(_FACT, ensure_ascii=False) in prompt


def test_prompt_is_unchanged_when_no_clip_has_facts() -> None:
    plain = _prompt(_with_media_context([{"media_id": "clip-1", "kind": "video"}]))
    assert "may also carry `facts`" not in plain
    assert "$clip_facts_note" not in plain
    # Empty facts lists are "no facts" too.
    empty = _prompt(_with_media_context([{"media_id": "clip-1", "kind": "video", "facts": []}]))
    assert "may also carry `facts`" not in empty
