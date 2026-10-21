"""KRI-759: a policy refusal says what it dropped, has buttons, and "yes" drafts it.

Incident (prod thread 91256BF1, Merope café M1, 2026-10-19): the server's own check capped a
20 s once-only montage at 15 s (the clip scope had narrowed the pick to one 15.8 s clip).
The creator got only "Bu düzenleme istediklerinin bu birleşimini aynen yapamıyor. Daha sade
bir sürüm yapayım mı?" with no reason and no buttons; "Evet daha sade surumu hazirla" was a
new message, the Main Creator replanned, and the same refusal came back, every time.

Ways it could go wrong, written down before the code (each has a case below):

1. The refusal still says the generic sentence for a pure length cap.
2. It names the wrong clips (off by one, a Visuals ``asset-*`` counted as a clip) or hides
   that only some clips are in the edit.
3. The reason is in the wrong language for the chat.
4. The planner's refusal carries no ``choice_question`` -> a tapped answer is "stale".
5. A tapped / typed "simpler version" replans into the same refusal (the loop).
6. Typed "Evet daha sade surumu hazirla" is not recognised as the answer.
7. Consent is sticky: one earlier "yes" waives every later refusal in the thread.
8. The receipts path's question (same conflict id) waives a policy refusal it never described,
   or a refusal about MORE than was offered is waived.
9. "Try a different approach" is taken for consent.
10. The drafted strategy is not a fixed point of the check (the post-planner re-check refuses).
11. Flag off (choice questions): the refusal stays text-only, byte-identical to before.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import planner
from app.kria.api_schemas import SubmitTurnBody
from app.kria.reply_language import reply_language_for
from app.kria.runtime import submit_turn
from app.kria.strategy_policy import (
    CheckedStrategy,
    RefusedStrategy,
    _lost_in_words,
    check_strategy_for_runtime_v2,
)
from app.models import CreationThread, CreationThreadEvent
from app.services import creator_capabilities as capabilities
from app.services.choice_questions import (
    SIMPLER_VERSION_REASON,
    match_open_choice,
    simpler_version_accepted,
    simpler_version_question,
)
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentResolution
from app.tasks.kria_runtime import _append_sync_event
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

# The M1 take: twelve clips in upload order, one of them (the last) the long one.
DURATIONS = [3.0, 2.2, 4.1, 6.4, 5.0, 3.3, 2.8, 4.4, 3.9, 2.5, 3.1, 15.8]
CLIPS = [f"analysis-proxy-ios-{n:02d}.mp4" for n in range(1, 13)]
LONG_CLIP = CLIPS[-1]


def _manifest(item_id: str = "item-m1"):
    return capabilities.resolve_creator_manifest(
        item_id=item_id,
        edit_format="montage",
        media=[
            {"media_id": clip, "kind": "video", "duration_s": seconds}
            for clip, seconds in zip(CLIPS, DURATIONS, strict=True)
        ],
        phone_source_media_ids=CLIPS,
        phone_rendering_allowed=True,
    )


def _strategy(*, selected: list[str] | None = None, seconds: int = 20) -> CreativeStrategy:
    """What the scope step left the policy: one clip picked, 20 s asked."""
    return CreativeStrategy(
        edit_format="montage",
        audio_strategy="original_audio",
        selected_media_ids=selected if selected is not None else [LONG_CLIP],
        media_scope="selected",
        target_duration_s=seconds,
        video_reuse_policy="once",
    )


def _check(strategy: CreativeStrategy, language: str = "en"):
    with reply_language_for(language):
        return check_strategy_for_runtime_v2(
            _manifest(),
            strategy,
            ask_before_simplifying=True,
            ask_about_stated_settings=True,
        )


# -- 1-3: the refusal names what it dropped, in the chat's language ---------------------


def test_length_cap_refusal_names_the_clip_the_lengths_and_why(prod_profile) -> None:
    refused = _check(_strategy())

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == SIMPLER_VERSION_REASON
    assert refused.fields == ("target_duration_s",)
    assert "cannot carry out that exact combination" not in refused.question
    # Which clip is in (the app's picker number), how long it runs, what was asked.
    assert "clip 12" in refused.question
    assert "15 seconds" in refused.question and "20 seconds" in refused.question
    assert refused.question.endswith("Should I make a simpler version?")
    assert refused.agreed == "I made it 15 seconds instead of 20."


def test_the_same_refusal_in_a_turkish_chat_is_turkish(prod_profile) -> None:
    refused = _check(_strategy(), "tr")

    assert isinstance(refused, RefusedStrategy)
    assert refused.question == (
        "Bu düzenlemede yalnızca 12. klip var ve yaklaşık 15 saniye sürüyor, bu yüzden video "
        "20 saniye olamıyor. Daha sade bir sürüm yapayım mı?"
    )
    assert refused.agreed == "20 saniye yerine 15 saniyelik yaptım."


def test_every_clip_in_the_edit_says_the_clips_add_up_not_which_are_in(prod_profile) -> None:
    refused = _check(_strategy(selected=[], seconds=60))

    assert isinstance(refused, RefusedStrategy)
    assert refused.question.startswith("Your clips add up to about 56 seconds")


def test_a_pick_that_checks_clean_is_not_asked_about(prod_profile) -> None:
    checked = _check(_strategy(seconds=15))

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.target_duration_s == 15


def test_left_out_clips_are_named_by_the_number_the_picker_shows(prod_profile) -> None:
    manifest = _manifest()
    before = _strategy(selected=[CLIPS[2], CLIPS[4], CLIPS[8]])
    after = _strategy(selected=[CLIPS[8]])

    reason, agreed = _lost_in_words(manifest, before, after, ("selected_media_ids",))

    assert reason == "Clips 3 and 5 can't be in this edit."
    assert agreed == "I left out clips 3 and 5."
    with reply_language_for("tr"):
        reason, agreed = _lost_in_words(manifest, before, after, ("selected_media_ids",))
    assert reason == "3 ve 5. klipler bu düzenlemeye giremiyor."
    assert agreed == "3 ve 5. klipleri dışarıda bıraktım."


def test_a_visual_is_never_counted_as_a_clip(prod_profile) -> None:
    manifest = _manifest()
    before = _strategy(selected=[CLIPS[0], "asset-photo-1"])
    after = _strategy(selected=[CLIPS[0]])

    reason, _ = _lost_in_words(manifest, before, after, ("selected_media_ids",))

    assert "clip" not in reason.lower() or "1" not in reason
    assert reason == "This edit can't keep: the media you picked."


def test_a_field_with_no_plain_name_keeps_the_generic_sentence(prod_profile) -> None:
    assert _lost_in_words(_manifest(), _strategy(), _strategy(), ("something_new",)) == ("", "")


# -- 6, 8, 9: the answer is exact, per question, and never the receipts path's ---------


def test_a_policy_refusals_question_takes_a_yes_the_receipts_question_does_not() -> None:
    # The labels follow the chat's language, so this is the question a Turkish chat is shown.
    with reply_language_for("tr"):
        refusal = simpler_version_question(("target_duration_s",))
        receipts = simpler_version_question()

    for typed in ("Evet daha sade surumu hazirla", "Evet", "yes", "Daha basit sürümü hazırla"):
        assert match_open_choice(refusal, typed) == "simpler_version", typed
    assert match_open_choice(refusal, "Farklı bir yol dene") == "different_approach"
    # Nothing else is consent, and the two-way receipts question never reads a bare yes.
    assert match_open_choice(refusal, "Hayır") is None
    assert match_open_choice(refusal, "20 saniye olsun") is None
    assert match_open_choice(receipts, "yes") is None
    assert receipts.get("kind") is None and "dropped_fields" not in receipts
    assert refusal["kind"] == "strategy_refusal"
    assert refusal["dropped_fields"] == ["target_duration_s"]


def _asked(question: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return "assistant", {"turn_value": "question", "choice_question": question}


def _answer(question: dict[str, Any], option: str = "simpler_version") -> tuple[str, dict]:
    return "user", {
        "choice_selection": {"question_id": question["question_id"], "option_key": option}
    }


def test_acceptance_is_this_turns_answer_to_the_open_policy_question() -> None:
    question = simpler_version_question(("target_duration_s", "selected_media_ids"))
    fields = ("target_duration_s",)

    assert simpler_version_accepted([_asked(question), _answer(question)], fields)
    # 9: the other button is not consent.
    assert not simpler_version_accepted(
        [_asked(question), _answer(question, "different_approach")], fields
    )
    # No answer yet: the creator's last message is a plain one.
    assert not simpler_version_accepted([_asked(question), ("user", {})], fields)
    assert not simpler_version_accepted([_asked(question)], fields)


def test_acceptance_is_not_sticky_or_borrowed() -> None:
    question = simpler_version_question(("target_duration_s",))
    fields = ("target_duration_s",)
    accepted = [_asked(question), _answer(question)]

    # 7: a later plain message (a new request) is not covered by the old "yes".
    assert not simpler_version_accepted([*accepted, ("assistant", {}), ("user", {})], fields)
    # 7: nor is a later question's turn, once the answered question has closed.
    other = simpler_version_question(("target_duration_s",))
    assert not simpler_version_accepted([*accepted, _asked(other), ("user", {})], fields)
    # 8: the receipts path's question (same conflict id, no marker) is not a policy consent.
    receipts = simpler_version_question()
    assert not simpler_version_accepted([_asked(receipts), _answer(receipts)], fields)
    # 8: a refusal that drops MORE than was offered is asked again.
    assert not simpler_version_accepted(accepted, ("target_duration_s", "pinned_texts"))
    # An answer to a question that is not the open one.
    assert not simpler_version_accepted(
        [_asked(question), _answer({**question, "question_id": str(uuid.uuid4())})], fields
    )
    # Nothing dropped, nothing to accept.
    assert not simpler_version_accepted(accepted, ())


# -- 4, 5, 6, 10, 11: the planner, against a real thread -----------------------------------


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    # No model call: the production flag profile turns clip intents on, so stand in for the
    # planner's clip-intent step with "the creator asked for none".
    monkeypatch.setattr(
        planner,
        "plan_and_resolve_clip_intents",
        AsyncMock(return_value=PlannedIntentResolution([], IntentResolution(intents=[]))),
    )
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())


async def _plan(thread_id: uuid.UUID, item_id: uuid.UUID, message: str, *, language: str = "en"):
    """The REAL planner step (policy check + refusal/acceptance) for one proposed strategy."""
    manifest = _manifest(str(item_id))
    action = ProposeStrategy(kind="propose_strategy", strategy=_strategy(), summary="A montage.")
    async with AsyncSessionLocal() as db:
        with reply_language_for(language):
            return await planner._plan_from_creator_output(
                db,
                thread_id=thread_id,
                item_id=item_id,
                creator_id=uuid.uuid4(),
                user_message=message,
                manifest=manifest,
                inputs=planner._CreatorInputs(
                    agent_input=SimpleNamespace(), intent_clips=[], creator_request=message
                ),
                output=SimpleNamespace(action=action),
            )


def _persist_assistant_question(thread_id: uuid.UUID, planned) -> dict[str, Any]:  # noqa: ANN001
    """Store the refusal the way the turn worker stores a question reply."""
    plan = planned.plan
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content=plan.response,
            payload={"turn_value": "question", "choice_question": plan.choice_question},
        )
        db.commit()
    return plan.choice_question


async def _send(user_id, thread_id, message, selection=None):  # noqa: ANN001, ANN202
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"k759-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
                choice_selection=selection,
            ),
        )
    return accepted


def _stored_selection(thread_id) -> dict | None:  # noqa: ANN001
    with sync_session() as db:
        payloads = (
            db.execute(
                select(CreationThreadEvent.payload)
                .where(
                    CreationThreadEvent.thread_id == thread_id, CreationThreadEvent.role == "user"
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .all()
        )
    return next((p["choice_selection"] for p in payloads if p and "choice_selection" in p), None)


def _drafted(planned) -> dict[str, Any]:  # noqa: ANN001
    assert planned.plan.mode == "act", planned.plan.response
    intent = planned.plan.intents[0]
    assert intent.tool_name == "draft.apply_strategy"
    return intent.arguments


@pytest.mark.asyncio
async def test_the_m1_refusal_names_the_cap_and_carries_the_buttons(prod_profile) -> None:
    user_id, thread_id, item_id = _seed_runtime_project()
    try:
        planned = await _plan(thread_id, item_id, "Caro'nun kısa klibini kullanma. 20 saniye.")
        plan = planned.plan
        assert plan.mode == "respond" and plan.turn_value == "question"
        assert "clip 12" in plan.response and "20 seconds" in plan.response
        question = plan.choice_question
        assert question["conflict"] == SIMPLER_VERSION_REASON
        assert [o["key"] for o in question["options"]] == ["different_approach", "simpler_version"]
        assert question["dropped_fields"] == ["target_duration_s"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_flag_off_the_refusal_stays_a_text_question(prod_profile, monkeypatch) -> None:
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", False)
    user_id, thread_id, item_id = _seed_runtime_project()
    try:
        plan = (await _plan(thread_id, item_id, "20 saniye")).plan
        assert plan.mode == "respond" and plan.choice_question is None
        assert "clip 12" in plan.response
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_tapping_the_simpler_version_drafts_it_instead_of_refusing_again(
    prod_profile,
) -> None:
    user_id, thread_id, item_id = _seed_runtime_project()
    try:
        question = _persist_assistant_question(
            thread_id, await _plan(thread_id, item_id, "20 saniye olsun")
        )
        # 4: the app's tap is a valid answer to the stored question.
        selection = {"question_id": question["question_id"], "option_key": "simpler_version"}
        await _send(user_id, thread_id, "Daha basit sürümü hazırla", selection)
        assert _stored_selection(thread_id)["option_key"] == "simpler_version"

        planned = await _plan(thread_id, item_id, "Daha basit sürümü hazırla")

        # 5: the same strategy is now drafted as the check keeps it, and the reply says so.
        arguments = _drafted(planned)
        assert arguments["strategy"]["target_duration_s"] == 15
        assert arguments["strategy"]["selected_media_ids"] == [LONG_CLIP]
        assert "I made it 15 seconds instead of 20." in arguments["summary"]
        # 10: nothing left for the post-planner re-check to refuse.
        again = check_strategy_for_runtime_v2(
            _manifest(str(item_id)),
            CreativeStrategy.model_validate(arguments["strategy"]),
            ask_before_simplifying=True,
            ask_about_stated_settings=True,
        )
        assert isinstance(again, CheckedStrategy)
        assert again.strategy.target_duration_s == 15
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_typing_yes_in_the_creators_words_drafts_it_too(prod_profile) -> None:
    """The words Yasin typed in the M1 run, in a Turkish chat."""
    user_id, thread_id, item_id = _seed_runtime_project()
    try:
        refused = await _plan(thread_id, item_id, "20 saniye olsun", language="tr")
        assert refused.plan.response.startswith("Bu düzenlemede yalnızca 12. klip var")
        _persist_assistant_question(thread_id, refused)
        # 6: no tap, only text. The runtime turns an exact answer into a stored selection.
        await _send(user_id, thread_id, "Evet daha sade surumu hazirla")
        assert _stored_selection(thread_id)["option_key"] == "simpler_version"

        planned = await _plan(thread_id, item_id, "Evet daha sade surumu hazirla", language="tr")

        arguments = _drafted(planned)
        assert arguments["strategy"]["target_duration_s"] == 15
        assert "20 saniye yerine 15 saniyelik yaptım." in arguments["summary"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_the_other_button_asks_again_with_a_new_question(prod_profile) -> None:
    user_id, thread_id, item_id = _seed_runtime_project()
    try:
        question = _persist_assistant_question(
            thread_id, await _plan(thread_id, item_id, "20 saniye olsun")
        )
        selection = {"question_id": question["question_id"], "option_key": "different_approach"}
        await _send(user_id, thread_id, "Farklı bir yol dene", selection)

        planned = await _plan(thread_id, item_id, "Farklı bir yol dene")

        assert planned.plan.mode == "respond"
        assert planned.plan.choice_question["question_id"] != question["question_id"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_an_old_yes_does_not_cover_the_next_request(prod_profile) -> None:
    user_id, thread_id, item_id = _seed_runtime_project()
    try:
        question = _persist_assistant_question(
            thread_id, await _plan(thread_id, item_id, "20 saniye olsun")
        )
        selection = {"question_id": question["question_id"], "option_key": "simpler_version"}
        await _send(user_id, thread_id, "Daha basit sürümü hazırla", selection)
        _drafted(await _plan(thread_id, item_id, "Daha basit sürümü hazırla"))
        # Later the creator asks for something new that hits the same cap.
        with sync_session() as db:
            thread = db.get(CreationThread, thread_id, with_for_update=True)
            _append_sync_event(
                db,
                thread,
                role="assistant",
                event_type="assistant_response",
                content="Here is the simpler version.",
                payload={"turn_value": "act"},
            )
            _append_sync_event(
                db,
                thread,
                role="user",
                event_type="user_message",
                content="Make it 30 seconds instead",
                payload={},
            )
            db.commit()

        planned = await _plan(thread_id, item_id, "Make it 30 seconds instead")

        assert planned.plan.mode == "respond"
        assert planned.plan.choice_question is not None
    finally:
        await async_engine.dispose()
