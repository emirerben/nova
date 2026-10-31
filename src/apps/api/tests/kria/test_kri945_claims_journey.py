"""KRI-945: what the approved hook, the render repair and the research may claim.

Same replay as ``test_kri945_global_text_journey.py``: ``submit_turn`` + ``run_kria_turn`` over
scratch Postgres with the recorded provider transport, approval readiness on. Each test pins
one way the fixes could tell the creator something the video will not show:

1. An approval answers only the hook ask it was given: after "Start fresh", a new hook ask
   never inherits the old words.
2. A hook literal the server copied from an approval follows the approval: skipping the
   wording withdraws it, approving new wording replaces it.
3. Typed words that wait for a spoken word are not promised by the opening repair, which
   puts whole-video text at the start.
4. Research done for one ask never proves another; a research limit holds the draft with its
   precise reason; with research preview switched off a researched ask stays held as before.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio

from app.agents._runtime import ModelInvocation
from app.config import settings
from app.database import engine as async_engine
from app.kria import research_preview
from app.services.creative_copy_decisions import APPROVED_COPY_FACT
from tests._prod_profile import apply_prod_profile
from tests.kria import test_kri945_global_text_journey as journey
from tests.kria.test_kri945_global_text_journey import (
    APPROVED,
    CASE,
    FACTS_TEXT,
    LITERAL_MISS,
    MESSAGES,
    T1,
    T2,
    T3_ALIGNED,
    Chat,
    _ask,
    _dismiss_card,
    _propose,
    _seed,
    _wire,
)

NEW_HOOK = "Cunda after dark: sunsets over the Aegean"
NOT_RENDERED = "I can't confirm this draft does it yet."
CHECK_AFTER_RENDER = "I can't check this before the video is made; I'll point it out after."


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    apply_prod_profile(monkeypatch)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_research_preview_enabled", True)
    memory = journey._MemoryRedis()
    monkeypatch.setattr(research_preview, "_redis", lambda: memory)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


async def _approved(monkeypatch: pytest.MonkeyPatch, t1: dict[str, Any] = T1) -> Chat:
    user_id, thread_id = _seed()
    chat = Chat(user_id, thread_id, _wire(monkeypatch))
    await chat.send(MESSAGES["t1"], t1)
    await chat.send(MESSAGES["t2"], T2, option="revise")
    await chat.send(MESSAGES["t3"], T3_ALIGNED, option="approve")
    return chat


def _title(chat: Chat) -> Any:
    return next(req for req in chat.brief().live() if req.kind == "text" and req.scope == "title")


def _plain_draft(**kwargs: Any) -> dict[str, Any]:
    return {"main_creator": _propose(intents=[], **kwargs), "clip_intent_planner": []}


async def test_start_fresh_never_hands_the_old_hook_to_a_new_hook_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = await _approved(monkeypatch)
    assert _title(chat).literal == APPROVED
    fresh = _plain_draft(
        updates=[
            {
                "operation": "add",
                "kind": "text",
                "scope": "title",
                "literal": None,
                "description": "a hook about the evening light",
            }
        ]
    )
    fresh["main_creator"]["starts_fresh"] = True
    await _dismiss_card(chat)
    await chat.send("Start fresh: only the sunsets, with a hook about the evening light", fresh)
    await _dismiss_card(chat)
    await chat.send("Make it about 20 seconds", _plain_draft())
    title = _title(chat)
    assert (title.description, title.literal) == ("a hook about the evening light", None)
    # Still no inherited words; on the phone unified montage the ask is approvable and named for
    # a look after render (route disclosure: it used to hold the draft as NOT_RENDERED).
    assert chat.unready().get(title.id) == ("unverified", CHECK_AFTER_RENDER)


@pytest.mark.parametrize(
    ("option", "draft", "literal"),
    [
        pytest.param("approve", {"opening_title": NEW_HOOK}, NEW_HOOK, id="new-wording-approved"),
        pytest.param("cancel", {"opening_title": None}, None, id="wording-skipped"),
    ],
)
async def test_the_copied_hook_follows_the_approval(
    monkeypatch: pytest.MonkeyPatch, option: str, draft: dict[str, Any], literal: str | None
) -> None:
    chat = await _approved(monkeypatch)
    await _dismiss_card(chat)
    asked = await chat.send(
        "Change the hook to something about the sunsets",
        {"main_creator": _ask(NEW_HOOK, f"How about “{NEW_HOOK}”?", [])},
    )
    assert (asked.payload or {})["choice_question"]["candidate"] == NEW_HOOK
    await chat.send("ok", _plain_draft(**draft), option=option)
    title = _title(chat)
    assert (title.literal, (title.facts.get(APPROVED_COPY_FACT) or {}).get("text")) == (
        literal,
        literal,
    )


async def test_typed_words_waiting_for_a_spoken_word_are_not_promised_by_the_opening_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = await _approved(monkeypatch)
    await _dismiss_card(chat)
    cued = {**CASE["brief_updates"]["t5"][0], "facts": {"triggers": ["Cunda"]}}
    await chat.send(
        f"{FACTS_TEXT}. Show it when I say Cunda",
        _plain_draft(updates=[cued], opening_title=APPROVED),
    )
    assert chat.unready().get("r1") == ("needs_choice", LITERAL_MISS)


def _review_rejects(reason: str):  # noqa: ANN202
    invoke = journey._Models.invoke

    def rejecting(self, *, model: str, prompt: str, **kwargs: Any) -> ModelInvocation:  # noqa: ANN001
        if prompt.startswith("Review proposed on-screen captions"):
            self.calls["review"] += 1
            return ModelInvocation(raw_text=json.dumps({"approved": False, "reason": reason}))
        return invoke(self, model=model, prompt=prompt, **kwargs)

    return rejecting


async def test_research_for_one_ask_never_proves_another(monkeypatch: pytest.MonkeyPatch) -> None:
    second = {
        "operation": "add",
        "kind": "text",
        "scope": "global",
        "literal": None,
        "description": "Where to eat in Cunda, with sources",
        "facts": {"research": {"topic": "Where to eat in Cunda", "request_quote": "great food"}},
    }
    t1 = {**T1, "main_creator": {**T1["main_creator"]}}
    t1["main_creator"]["brief_updates"] = [*CASE["brief_updates"]["t1"], second]
    chat = await _approved(monkeypatch, t1)
    assert chat.models.calls["search"] == 1
    assert chat.unready() == {"r3": ("evidence_missing", NOT_RENDERED)}


async def test_a_research_limit_holds_the_draft_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = "The causeway claim is not on the cited page."
    monkeypatch.setattr(journey._Models, "invoke", _review_rejects(reason))
    chat = await _approved(monkeypatch)
    assert (chat.models.calls["search"], chat.readiness().get("ready")) == (1, False)
    assert chat.unready() == {
        "r1": (
            "unsupported",
            f"Sources could not be confirmed before approval: Research needs correction: {reason}",
        )
    }


async def test_research_preview_off_keeps_the_researched_ask_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "kria_research_preview_enabled", False)
    chat = await _approved(monkeypatch)
    assert chat.models.calls["search"] == 0
    assert chat.unready() == {"r1": ("evidence_missing", NOT_RENDERED)}


async def test_speaking_clip_research_is_a_precise_limit_not_missing_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = await journey._approve_hook(
        monkeypatch,
        {
            "main_creator": _propose(intents=[], montage_audio=journey._CAMERA_VOICE),
            "clip_intent_planner": [],
        },
        speaking=True,
    )
    disposition, reason = chat.unready()["r1"]
    assert disposition == "unsupported"
    assert "research" in reason.casefold() and "montage" in reason.casefold()
    assert chat.models.calls["search"] == 0
    assert chat.readiness()["ready"] is False


async def test_a_researched_montage_can_recover_after_a_writer_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = await journey._approve_hook(
        monkeypatch,
        {
            "main_creator": _propose(intents=[], montage_audio=journey._CAMERA_VOICE),
            "clip_intent_planner": [],
        },
        speaking=True,
    )
    assert chat.unready()["r1"][0] == "unsupported"
    await _dismiss_card(chat)
    await chat.send(
        "Use a montage instead. Keep the approved hook and the sourced Cunda introduction.",
        {"main_creator": _propose(intents=[]), "clip_intent_planner": []},
    )
    assert chat.models.calls["search"] == 1
    assert chat.readiness()["ready"] is True
    assert chat.unready() == {}
    assert journey.SOURCED_TEXT in str(chat.replies[-1].content)


async def test_the_writer_limit_follows_the_creators_turkish_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = {
        "main_creator": _propose(intents=[], montage_audio=journey._CAMERA_VOICE),
        "clip_intent_planner": [],
    }
    chat = await journey._approve_hook(monkeypatch, answer, speaking=True)
    await chat.send(
        "Kaynaklı Cunda bilgilerini de istiyorum. Konuşmaları ve onayladığım başlığı koru.",
        answer,
    )
    disposition, reason = chat.unready()["r1"]
    assert disposition == "unsupported"
    assert "araştırma" in reason and "montaj" in reason
    assert "This edit format" not in reason
    assert chat.models.calls["search"] == 0
