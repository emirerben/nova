"""KRI-1560: "başlık koy" on a finished draft offers titles built from what she said.

Prod thread 1b8974a0 (Alanur, 2026-11-20): on a finished Talking draft she wrote "başlık koy".
The chat-edit model put its own "Renk Kavgası" on the video with no question, and the reply
said no title was added. Real submit -> worker -> planner on Postgres here; only the model
calls are replaced: the brief extractor reads the ask, and the opening-title model's raw answer
is scripted and parsed by the real ``OpeningTitleAgent`` (provider transport replayed).

Each test states the failure it would catch:

* the chat-edit model runs and applies a title of its own (the incident): the copilot is a
  tripwire and no draft may exist after the turn;
* a title stating something she did not say reaches her (the negative control): the scripted
  answer carries one naming "Paris", which her sentence never says, and it must be dropped;
* a title points at the wrong place: every offered title's line is her real sentence at its
  real time;
* her answer is not honoured: a tap on a title approves exactly its words, "No title" and the
  typed "başlık olmasın" skip it, and a typed title of her own needs no approval;
* a failed model call leaves her with nothing: the older "do you have an idea?" question.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents.opening_title import OpeningTitleAgent
from app.config import settings
from app.database import sync_session
from app.kria import planner
from app.kria.api_schemas import SubmitTurnBody
from app.kria.brief import BriefUpdate
from app.kria.brief_binding import snapshot_media
from app.kria.runtime import submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentSession,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services import opening_titles
from app.services.choice_questions import match_open_choice
from app.services.creative_copy_decisions import (
    creator_quoted,
    fold_creative_copy,
    media_digest,
)
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_creative_brief import _MANIFEST
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

pytestmark = pytest.mark.journey("kri-1560-opening-titles")

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures/kria_replay/kri1365_edit_route/fixture.json").read_text()
)
session_factory = async_sessionmaker(
    create_async_engine(settings.asyncpg_database_url, poolclass=NullPool), expire_on_commit=False
)

# Her talk, the scripted model answer and what she must be offered live in the fixture, apart
# from this test (test data in the incident's language, not the creator's real words).
TITLES = json.loads(
    (
        Path(__file__).parents[1] / "fixtures/kria_replay/kri1560_opening_titles/fixture.json"
    ).read_text(encoding="utf-8")
)
TALK = [tuple(row) for row in TITLES["tr"]["talk"]]
MODEL_ANSWER = TITLES["tr"]["model_answer"]
EXPECTED = TITLES["tr"]["expected_titles"]
GOOD_ONE, GOOD_TWO = (row["label"] for row in EXPECTED)
INVENTED = MODEL_ANSWER["candidates"][2]["text"]  # she never says Paris


def _job(user_id, item_id):  # noqa: ANN001, ANN202
    job_id = uuid.uuid4()
    variants = copy.deepcopy([FIXTURE["variant"]])
    # Her Talking draft had no title (the incident); a draft that has one keeps wordless title
    # asks with the editor ("show the title word by word", 29 Ekim T5 in
    # test_compose_unmet_with_ops).
    variants[0]["text_elements"] = []
    for slot in variants[0]["ai_timeline"]["slots"]:
        slot["source_gcs_path"] = slot["source_gcs_path"].replace(
            FIXTURE["ids"]["ready_job_id"], str(job_id)
        )
    return Job(
        id=job_id,
        user_id=user_id,
        status="variants_ready",
        mode="generative",
        raw_storage_path="",
        selected_platforms=["tiktok"],
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        all_candidates={"clip_paths": [clip["gcs_path"] for clip in FIXTURE["clips"]]},
        assembly_plan={"variants": variants},
    )


def _seed():  # noqa: ANN202
    user_id, thread_id, session_id = _seed_runtime_project()
    variant = FIXTURE["variant"]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**dict(thread.state or {}), "reply_language": "tr"}
        session = db.get(CreatorAgentSession, session_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.clip_assignments = [
            {
                "media_id": "take-1",
                "kind": "video",
                "analysis": {
                    "duration_s": 20.0,
                    "understanding": {
                        "speech": {
                            "has_speech": True,
                            "to_camera": True,
                            "transcript": " ".join(text for _s, _e, text in TALK),
                            "segments": [
                                {"start_s": start, "end_s": end, "text": text}
                                for start, end, text in TALK
                            ],
                            "language": "tr",
                        }
                    },
                },
            }
        ]
        ready = _job(user_id, item.id)
        db.add(ready)
        db.flush()
        item.current_job_id = ready.id
        session.target_job_id = ready.id
        session.target_variant_id = variant["variant_id"]
        session.target_generation_id = variant["render_generation_id"]
        session.manifest_hash = "a" * 64
        session.last_good = {
            "job_id": str(ready.id),
            "variant_id": variant["variant_id"],
            "render_generation_id": variant["render_generation_id"],
            "draft_id": None,
        }
        digest = media_digest(snapshot_media(item))
        db.commit()
        return SimpleNamespace(
            user_id=user_id,
            thread_id=thread_id,
            item_id=item.id,
            digest=digest,
        )


def _agents(monkeypatch, case, answer):  # noqa: ANN001, ANN202
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime.nudge_kria_observer", lambda *args: None)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(planner, "default_client", lambda: object())
    monkeypatch.setattr(
        planner,
        "resolve_item_creator_context",
        AsyncMock(return_value=(_MANIFEST.model_copy(update={"item_id": str(case.item_id)}), [])),
    )
    monkeypatch.setattr(planner, "creator_context", lambda *a: ("creator", "item"))
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=None))
    monkeypatch.setattr(planner, "_copilot_clip_context", AsyncMock(return_value=None))
    creator = Mock(side_effect=AssertionError("a title ask is answered before any re-plan"))
    monkeypatch.setattr(planner, "MainCreatorAgent", creator)
    copilot = AsyncMock(side_effect=AssertionError("the editor model must not write the title"))
    monkeypatch.setattr(planner, "run_copilot_turn", copilot)

    async def extractor(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return SimpleNamespace(
            brief_updates=[BriefUpdate(kind="text", scope="title", description="başlık koy")],
            request_scope="edit",
            clarification=None,
        )

    monkeypatch.setattr(planner, "_call_brief_extractor", extractor)
    prompts: list[str] = []

    def run(self, request, *, ctx=None):  # noqa: ANN001, ANN202
        prompts.append(self.render_prompt(request))
        if isinstance(answer, Exception):
            raise answer
        return self.parse(json.dumps(answer), request)

    monkeypatch.setattr(OpeningTitleAgent, "run", run)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())

    async def planned(_snapshot, message, **_kwargs):  # noqa: ANN001, ANN202
        async with session_factory() as db:
            return await planner.plan_live_turn(
                db,
                thread_id=case.thread_id,
                item_id=case.item_id,
                creator_id=case.user_id,
                user_message=message,
            )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    return SimpleNamespace(prompts=prompts, copilot=copilot, creator=creator)


async def _run(case, message):  # noqa: ANN001, ANN202
    async with session_factory() as db:
        expected = (await db.get(CreationThread, case.thread_id)).revision
        accepted, _ = await submit_turn(
            db,
            thread_id=case.thread_id,
            creator_id=case.user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=str(uuid.uuid4()),
                expected_thread_revision=expected,
            ),
        )
    return accepted, await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)


def _events(thread_id):  # noqa: ANN001, ANN202
    with sync_session() as db:
        rows = db.execute(
            select(CreationThreadEvent)
            .where(CreationThreadEvent.thread_id == thread_id)
            .order_by(CreationThreadEvent.sequence)
        ).scalars()
        return [(row.role, row.event_type, row.content, dict(row.payload or {})) for row in rows]


def _question(thread_id):  # noqa: ANN001, ANN202
    asked = [
        (content, payload["choice_question"])
        for role, _type, content, payload in _events(thread_id)
        if role == "assistant" and isinstance(payload.get("choice_question"), dict)
    ]
    assert asked, "the turn asked no question"
    return asked[-1]


def _no_draft(thread_id) -> bool:  # noqa: ANN001
    with sync_session() as db:
        return (
            db.scalar(select(CreatorEditDraft).where(CreatorEditDraft.thread_id == thread_id))
            is None
        )


@pytest.mark.asyncio
async def test_baslik_koy_offers_titles_from_her_words_and_puts_nothing_on_the_video(monkeypatch):
    case = _seed()
    agents = _agents(monkeypatch, case, MODEL_ANSWER)

    _accepted, result = await _run(case, TITLES["tr"]["message"])

    assert result["status"] == "completed", result
    agents.copilot.assert_not_called()
    agents.creator.assert_not_called()
    assert _no_draft(case.thread_id), "nothing is written on the video before she picks"
    message, question = _question(case.thread_id)
    assert question["kind"] == "creative_copy_candidates"
    titles = [o for o in question["options"] if o["key"].startswith("title_")]
    # Negative control: the invented "Paris" title is dropped; the two real ones stay.
    assert [o["label"] for o in titles] == [GOOD_ONE, GOOD_TWO]
    assert all(o["editable"] for o in titles)
    assert titles[0]["recommended"] is True
    # Each title shows where she said it: her real sentence at its real time.
    assert [o["description"] for o in titles] == [row["description"] for row in EXPECTED]
    evidence = {row["key"]: row["evidence"] for row in question["candidates"]}
    assert evidence["title_2"]["start_s"] == TALK[2][0]
    assert evidence["title_2"]["quote"] == TALK[2][2]
    # Turkish chat: Turkish reply and Turkish "No title".
    assert question["options"][-1] == {
        **question["options"][-1],
        "key": "cancel",
        "label": TITLES["tr"]["expected_no_title"],
        "recommended": False,
    }
    assert TITLES["tr"]["expected_reply_contains"] in message
    # The model was told her words are data, never instructions.
    assert "untrusted data" in agents.prompts[0]


@pytest.mark.asyncio
async def test_her_answer_ends_with_exactly_what_she_chose(monkeypatch):
    case = _seed()
    _agents(monkeypatch, case, MODEL_ANSWER)
    await _run(case, TITLES["tr"]["message"])
    _message, question = _question(case.thread_id)
    asked = ("assistant", {"choice_question": question})

    def fold(*answers):  # noqa: ANN002, ANN202
        return fold_creative_copy([asked, *answers], dependency_digest=case.digest)["opening_title"]

    tap = ("user", {"choice_selection": {"question_id": question["question_id"], "option_key": ""}})
    picked = copy.deepcopy(tap)
    picked[1]["choice_selection"]["option_key"] = "title_2"
    assert (fold(picked).status, fold(picked).approved) == ("approved", GOOD_TWO)
    skipped = copy.deepcopy(tap)
    skipped[1]["choice_selection"]["option_key"] = "cancel"
    assert fold(skipped).cancelled
    # Typed answers: her words of a title, its number, or a skip.
    assert match_open_choice(question, GOOD_ONE) == "title_1"
    assert match_open_choice(question, "başlık olmasın") == "cancel"
    # A tap on a title the server never offered approves nothing.
    forged = copy.deepcopy(tap)
    forged[1]["choice_selection"]["option_key"] = "title_3"
    assert fold(forged).approved is None
    # The pencil sends her edit quoted: her own wording, no approval question.
    edited = "Başlık: “Siyah herkese yakışmaz, bana da”"
    assert creator_quoted("Siyah herkese yakışmaz, bana da", [edited])
    # The server accepts the tap as an answer to the open question.
    async with session_factory() as db:
        expected = (await db.get(CreationThread, case.thread_id)).revision
        accepted, _ = await submit_turn(
            db,
            thread_id=case.thread_id,
            creator_id=case.user_id,
            body=SubmitTurnBody(
                message=GOOD_TWO,
                client_event_id=str(uuid.uuid4()),
                expected_thread_revision=expected,
                choice_selection={"question_id": question["question_id"], "option_key": "title_2"},
            ),
        )
    assert accepted.turn_id


@pytest.mark.asyncio
async def test_a_failed_title_model_falls_back_to_the_older_question(monkeypatch):
    case = _seed()
    agents = _agents(monkeypatch, case, RuntimeError("provider down"))

    _accepted, result = await _run(case, TITLES["tr"]["message"])

    assert result["status"] == "completed", result
    agents.copilot.assert_not_called()
    assert _no_draft(case.thread_id)
    _message, question = _question(case.thread_id)
    assert question["kind"] == "creative_copy_authorship"


@pytest.mark.asyncio
async def test_an_opening_that_already_hooks_recommends_no_title(monkeypatch):
    case = _seed()
    strong = {**MODEL_ANSWER, "opening_already_strong": True}
    _agents(monkeypatch, case, strong)

    await _run(case, TITLES["tr"]["message"])

    message, question = _question(case.thread_id)
    titles = [o for o in question["options"] if o["key"].startswith("title_")]
    assert len(titles) <= 2 and not any(o["recommended"] for o in titles)
    assert question["options"][-1]["key"] == "cancel"
    assert question["options"][-1]["recommended"] is True
    assert TALK[0][2] in message


def test_the_check_drops_what_she_did_not_say() -> None:
    """The evidence check alone, on the failure modes it exists for (`opening_titles`)."""
    sentences = opening_titles.talk_sentences(
        [
            (
                "take-1",
                {
                    "speech": {
                        "has_speech": True,
                        "segments": [{"start_s": s, "end_s": e, "text": t} for s, e, t in TALK],
                    }
                },
            )
        ]
    )

    def reason(text, first, last=None, language="tr"):  # noqa: ANN001, ANN202
        idea = opening_titles.TitleIdea(text=text, first=first, last=last or first)
        return opening_titles.check_title(idea, sentences, language=language)[1]

    assert reason(GOOD_TWO, 3) is None
    assert reason("Bir bej için yirmi dakika", 3) is None  # her number, in her words
    assert reason("Bir bej için 20 dakika", 3) == "number_not_said"
    assert reason("3 renk asla giymem", 5) == "number_not_said"
    assert reason(INVENTED, 4) == "name_not_said"
    assert reason("Bej, Zara'da hep ucuz", 4) == "name_not_said"
    assert reason("Zara'da bej almayı bırak", 4) == "name_not_said"
    assert reason("İşimi bıraktım", 3) == "not_about_that_line"
    assert reason(GOOD_ONE, 9) == "bad_span"
    assert reason(GOOD_ONE, 1, 4) == "bad_span"
    assert reason("“Siyah herkese yakışmaz”", 1) == "quote"
    assert reason("Siyah herkese yakışmaz ve bunu sana bugün burada kanıtlayacağım", 1) == "words"
    assert reason("Siyah yakışmaz takip et", 1) == "bait"
    assert reason("Siyah is not for you", 1) == "language"


def _reasons(talk: list[str], language: str):  # noqa: ANN202
    sentences = opening_titles.talk_sentences(
        [
            (
                "take-1",
                {
                    "speech": {
                        "has_speech": True,
                        "segments": [
                            {"start_s": 2.0 * i, "end_s": 2.0 * i + 1.8, "text": line}
                            for i, line in enumerate(talk)
                        ],
                    }
                },
            )
        ]
    )

    def reason(text: str, first: int) -> str | None:
        idea = opening_titles.TitleIdea(text=text, first=first, last=first)
        return opening_titles.check_title(idea, sentences, language=language)[1]

    return reason


def test_a_bare_name_opening_a_title_must_be_in_her_line() -> None:
    """Tadic's hold on #2455: a title may not open with a name her cited line never says.

    Ways this check can fail, each pinned below:
    * a bare invented name as the first word passes because the first word of a sentence-case
      title is always capitalised ("Paris changed my plans", "Zara bej satıyor");
    * a name she says in ANOTHER sentence passes for this one;
    * the fix over-reaches and drops an ordinary first word: one from her line in another form
      ("Changed" for "changed", "Burnt" for "burned"), a small word ("My", "Why", "Bu"), or a
      word she says in lower case elsewhere in her talk.
    """
    english = _reasons(
        [
            "My plans changed completely this year.",
            "I burned the first cake and nobody noticed.",
            "We moved to Paris in the spring.",
            "Honestly, lessons like that stay with you.",
        ],
        "en",
    )
    assert english("Paris changed my plans", 1) == "name_not_said"
    assert english("Paris changed my plans", 3) is None
    assert english("Changed plans, changed year", 1) is None
    assert english("My plans changed overnight", 1) is None
    assert english("Why my plans changed", 1) is None
    assert english("Burnt cake, nobody noticed", 2) is None
    assert english("Lessons from a burned cake", 2) is None

    turkish = _reasons(
        ["Bej her şeye uyuyor.", "Zara'da her sezon bej var."],
        "tr",
    )
    assert turkish("Zara bej satıyor", 1) == "name_not_said"
    assert turkish("Bej her şeye uyuyor mu", 1) is None
    assert turkish("Bu bej her şeye uyuyor", 1) is None


def test_german_capitalised_nouns_are_not_taken_for_names() -> None:
    """German writes every noun with a capital, so a capital is no sign of a name there.

    A paraphrased noun the line does not literally say ("Pläne" for "geplant") passes; a name
    the line never says still fails, at the start of the title or after a preposition.
    """
    german = _reasons(
        [
            "Ich hatte für den Montag so viel geplant, und dann ist alles ausgefallen.",
            "Am Abend waren wir dann noch in München.",
        ],
        "de",
    )
    assert german("Zu viele Pläne für einen Montag", 1) is None
    assert german("Mein Montag voller Pläne", 1) is None
    assert german("Der Montag, an dem alles ausfiel", 1) is None
    assert german("Berlin hat meinen Montag ruiniert", 1) == "name_not_said"
    assert german("Ein Montag in Berlin", 1) == "name_not_said"
    assert german("Ein Abend in München", 2) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "talk", "answer", "titles", "no_title", "says"),
    [
        (
            language,
            [tuple(row) for row in TITLES[language]["talk"]],
            TITLES[language]["model_answer"],
            [row["label"] for row in TITLES[language]["expected_titles"]],
            TITLES[language]["expected_no_title"],
            TITLES[language]["expected_reply_contains"],
        )
        for language in ("tr", "en")
    ],
)
async def test_a_title_asked_in_her_first_message_is_asked_before_the_draft(
    monkeypatch, language, talk, answer, titles, no_title, says
):
    """Emir's 3B: a title asked for in the first message is asked first; the draft waits.

    The planner's own wording ("Renk Kavgası", the incident's) is never what she is offered:
    the titles question replaces it, so nothing model-written is drafted.
    """
    from app.agents.main_creator import CreativeCopyDecision
    from app.kria.reply_language import bind_reply_language, release_reply_language

    def run(self, request, *, ctx=None):  # noqa: ANN001, ANN202
        return self.parse(json.dumps(answer), request)

    monkeypatch.setattr(OpeningTitleAgent, "run", run)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    sentences = opening_titles.talk_sentences(
        [
            (
                "take-1",
                {
                    "speech": {
                        "has_speech": True,
                        "segments": [{"start_s": s, "end_s": e, "text": t} for s, e, t in talk],
                    }
                },
            )
        ]
    )
    token = bind_reply_language(language)
    try:
        turn = await planner._copy_question_turn(
            CreativeCopyDecision(
                target="opening_title",
                status="delegated",
                proposed_text="Renk Kavgası",
                language=language,
            ),
            inputs=SimpleNamespace(
                creative_copy_state={}, creative_copy_digest="d" * 24, talk=tuple(sentences)
            ),
            creator_request=TITLES[language]["message"],
            manifest=SimpleNamespace(manifest_hash="a" * 64, context_hash="b" * 64),
        )
    finally:
        release_reply_language(token)
    assert turn is not None
    assert turn.plan.mode == "respond" and turn.plan.turn_value == "question"
    question = turn.plan.choice_question
    assert question["kind"] == "creative_copy_candidates"
    assert [o["label"] for o in question["options"][:-1]] == titles
    assert "Renk Kavgası" not in json.dumps(question, ensure_ascii=False)
    assert question["options"][-1]["label"] == no_title
    assert says in turn.plan.response


@pytest.mark.asyncio
async def test_the_kill_switch_asks_the_older_question_without_a_model_call(monkeypatch):
    case = _seed()
    agents = _agents(monkeypatch, case, MODEL_ANSWER)
    monkeypatch.setattr(settings, "kria_opening_title_options_enabled", False)

    _accepted, result = await _run(case, TITLES["tr"]["message"])

    assert result["status"] == "completed", result
    assert agents.prompts == []
    agents.copilot.assert_not_called()
    assert _no_draft(case.thread_id)
    assert _question(case.thread_id)[1]["kind"] == "creative_copy_authorship"


@pytest.mark.asyncio
async def test_with_the_brief_off_an_editor_written_title_becomes_the_question(monkeypatch):
    """Review finding: with the brief off, the chat goes straight to the editor model. A title
    it wrote itself on a draft with no title is never applied; she gets the titles question."""
    case = _seed()
    agents = _agents(monkeypatch, case, MODEL_ANSWER)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", False)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", False)
    seen: list[str] = []

    async def copilot(body, **_kwargs):  # noqa: ANN001, ANN202
        seen.append(body.message)
        return SimpleNamespace(
            ops=[{"op": "set_title", "title": "Renk Kavgası"}],
            outcome="proposed",
            reply="Done.",
            intent="edit",
            rejection_reasons=[],
            unmet_requests=[],
            reply_notes=None,
        )

    monkeypatch.setattr(planner, "run_copilot_turn", copilot)

    _accepted, result = await _run(case, TITLES["tr"]["message"])

    assert result["status"] == "completed", result
    assert seen, "the brief-off path reaches the editor model"
    assert _no_draft(case.thread_id), "its own title never reaches the video"
    question = _question(case.thread_id)[1]
    assert question["kind"] == "creative_copy_candidates"
    assert "Renk Kavgası" not in json.dumps(question, ensure_ascii=False)
    assert agents.prompts, "the titles came from her words"


def test_you_choose_on_the_titles_question_takes_the_recommended_title() -> None:
    """Review finding: a typed "sen seç" was a delegated answer the fold ignored, so the same
    titles question came back. The card shows every title's words; it takes the recommended."""
    question = {
        "version": 1,
        "question_id": "q",
        "conflict": "creative_copy",
        "kind": "creative_copy_candidates",
        "creative_target": "opening_title",
        "dependency_digest": "d",
        "candidates": [
            {
                "key": "title_1",
                "text": GOOD_ONE,
                "digest": opening_titles.candidate_digest(GOOD_ONE),
            }
        ],
        "options": [
            {"key": "title_1", "label": GOOD_ONE, "recommended": True},
            {"key": "cancel", "label": "Başlık olmasın", "recommended": False},
        ],
    }
    delegated = {"question_id": "q", "option_key": "title_1", "delegated": True}
    state = fold_creative_copy(
        [("assistant", {"choice_question": question}), ("user", {"choice_selection": delegated})],
        dependency_digest="d",
    )["opening_title"]
    assert (state.status, state.approved) == ("approved", GOOD_ONE)
