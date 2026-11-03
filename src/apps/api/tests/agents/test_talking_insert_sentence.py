"""The meaning check that decides which of the creator's sentences a clip is about (parent
KRI-1151, insert at the sentence end).

Only HAND-AUTHORED responses are replayed here (none is a recording of the real model): no
network and no paid call, so these tests prove the prompt carries what the model needs, that a
well-formed answer parses into picks, that a malformed or hostile one cannot place a clip, that
a clip with no usable answer is asked once more, that the answer is kept for a retry of the
render, and that a failure leaves the caller with ``None`` (its signal to fall back to keywords).
They do NOT prove the real model picks the right sentence; no live eval of this agent exists.
"""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import ModelClient, ModelInvocation, TransientError
from app.agents.talking_insert_sentence import (
    MAX_CLIPS,
    MAX_SENTENCES,
    SentenceChoiceInput,
    SentenceClip,
    TalkingInsertSentenceAgent,
)
from app.pipeline.phone_talking_insert_topics import Sentence, TopicPick
from app.services.phone_insert_topic_picker import pick_topic_sentences

SENTENCES = [
    Sentence(1, "Fenerbahçe'ye yeni bir stat teklif edildi.", 1.0, 3.2),
    Sentence(2, "Euro2032 için planlanan stat Fenerbahçe'ye verilecekmiş.", 16.1, 20.8),
    Sentence(3, "Kadıköy'deki tekellere alkol satışı yasağı getirildi.", 45.1, 49.9),
]
CLIPS = [
    SentenceClip(
        key="c1",
        summary="A man announces that alcohol sales are banned around the stadium on match days.",
        transcript="",
        subject="man speaking",
        activity="giving a speech",
        hook_text="",
        brands=["FBTV"],
        props=[],
    ),
    SentenceClip(
        key="c2",
        summary="A man in a suit proposes to allocate a new stadium to the club.",
        transcript="Eğer arzu ederseniz bu statı size tahsis edelim.",
        subject="man at a podium",
        activity="giving a speech",
        hook_text="",
        brands=[],
        props=[],
    ),
]
HAND_AUTHORED = {
    "picks": [
        {"clip": "c1", "first": 3, "last": 3, "confidence": "high", "reason": "alcohol ban"},
        {"clip": "c2", "first": None, "last": None, "confidence": "high", "reason": "not said"},
    ]
}


class _Recorded(ModelClient):
    """Replays one hand-authored response, or raises."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.prompts: list[str] = []

    def invoke(self, *, model: str, prompt: str, **kwargs) -> ModelInvocation:  # noqa: ANN003
        self.prompts.append(prompt)
        if isinstance(self.response, BaseException):
            raise self.response
        text = self.response if isinstance(self.response, str) else json.dumps(self.response)
        return ModelInvocation(raw_text=text, tokens_in=10, tokens_out=20)


def _input() -> SentenceChoiceInput:
    return SentenceChoiceInput(
        clips=CLIPS,
        sentences=[{"n": s.index, "text": s.text} for s in SENTENCES],
        language="tr",
    )


def test_the_prompt_numbers_the_sentences_and_carries_the_clip_facts_in_their_own_language():
    prompt = TalkingInsertSentenceAgent(None).render_prompt(_input())  # type: ignore[arg-type]
    assert "Euro2032 için planlanan stat" in prompt
    assert '"n": 2' in prompt
    assert "alcohol sales are banned" in prompt
    assert "Eğer arzu ederseniz" in prompt
    # The model is told to compare meaning across languages and that a miss is a valid answer.
    assert "different language" in prompt
    assert "null" in prompt
    assert "untrusted" in prompt


def test_a_hand_authored_answer_becomes_picks_keyed_by_the_clips_media_id():
    client = _Recorded(HAND_AUTHORED)
    picks = pick_topic_sentences(
        {"m-alcohol": CLIPS[0], "m-allocate": CLIPS[1]}, SENTENCES, language="tr", client=client
    )
    assert picks == {
        "m-alcohol": TopicPick("m-alcohol", first=3, last=3, confidence="high"),
        "m-allocate": TopicPick("m-allocate", first=None, last=None, confidence="high"),
    }
    assert len(client.prompts) == 1


def test_an_answer_naming_a_clip_that_was_not_asked_about_is_ignored():
    hostile = {"picks": [{"clip": "c9", "first": 1, "last": 1, "confidence": "high"}]}
    picks = pick_topic_sentences(
        {"m-alcohol": CLIPS[0]}, SENTENCES, language="tr", client=_Recorded(hostile)
    )
    assert picks == {}


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        {"picks": "none"},
        TransientError("provider down"),
    ],
)
def test_a_failed_or_malformed_check_is_none_so_the_caller_falls_back(response):
    picks = pick_topic_sentences(
        {"m-alcohol": CLIPS[0]}, SENTENCES, language="tr", client=_Recorded(response)
    )
    assert picks is None


def test_nothing_is_asked_when_there_is_nothing_to_choose_between():
    client = _Recorded(HAND_AUTHORED)
    assert pick_topic_sentences({}, SENTENCES, language="tr", client=client) is None
    assert pick_topic_sentences({"m": CLIPS[0]}, [], language="tr", client=client) is None
    too_long = [Sentence(i, "bir iki.", float(i), i + 0.5) for i in range(1, MAX_SENTENCES + 2)]
    assert pick_topic_sentences({"m": CLIPS[0]}, too_long, language="tr", client=client) is None
    assert client.prompts == []


# --- one more ask for a clip with no usable answer; the answer kept for a retry ---------------


class _Script(ModelClient):
    """Answers each call with the next scripted response (the last one repeats)."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.contexts: list[object] = []

    def invoke(self, *, model: str, prompt: str, **kwargs) -> ModelInvocation:  # noqa: ANN003
        self.prompts.append(prompt)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, BaseException):
            raise response
        text = response if isinstance(response, str) else json.dumps(response)
        return ModelInvocation(raw_text=text, tokens_in=10, tokens_out=20)


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def get(self, key):
        return self.rows.get(key)

    def setex(self, key, ttl, value):
        self.rows[key] = value


@pytest.fixture
def store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("app.services.insert_pick_checkpoint._redis", lambda: fake)
    return fake


def _unsure(clip):
    return {"clip": clip, "first": None, "last": None, "confidence": "low"}


def test_a_clip_with_no_usable_answer_is_asked_once_more_and_only_that_clip(store):
    client = _Script(
        {"picks": [HAND_AUTHORED["picks"][0], _unsure("c2")]},
        {"picks": [{"clip": "c2", "first": 2, "last": 2, "confidence": "medium"}]},
    )
    picks = pick_topic_sentences(
        {"m-alcohol": CLIPS[0], "m-allocate": CLIPS[1]}, SENTENCES, language="tr", client=client
    )
    assert len(client.prompts) == 2
    assert "Eğer arzu ederseniz" in client.prompts[1]  # the unsure clip
    assert "alcohol sales are banned" not in client.prompts[1].split("CLIPS:")[1]
    assert picks["m-alcohol"] == TopicPick("m-alcohol", 3, 3, "high")
    assert picks["m-allocate"] == TopicPick("m-allocate", 2, 2, "medium")


def test_a_sure_answer_costs_one_call_only(store):
    client = _Script(HAND_AUTHORED)
    pick_topic_sentences(
        {"m-alcohol": CLIPS[0], "m-allocate": CLIPS[1]}, SENTENCES, language="tr", client=client
    )
    assert len(client.prompts) == 1


def test_a_failed_first_call_is_tried_once_more_then_gives_up_with_none(store):
    client = _Script(TransientError("provider down"))
    assert (
        pick_topic_sentences({"m-alcohol": CLIPS[0]}, SENTENCES, language="tr", client=client)
        is None
    )
    assert len(client.prompts) >= 2


def test_the_first_answer_is_kept_and_a_retried_render_reuses_it_without_calling(store):
    first = _Script(HAND_AUTHORED)
    clips = {"m-alcohol": CLIPS[0], "m-allocate": CLIPS[1]}
    expected = pick_topic_sentences(clips, SENTENCES, language="tr", job_id="job-1", client=first)
    assert store.rows  # kept the moment it was known
    retry = _Script(TransientError("must not be called"))
    again = pick_topic_sentences(
        clips, SENTENCES, language="tr", job_id="job-1", client=retry, retry_scope="r1"
    )
    assert again == expected
    assert retry.prompts == []


def test_another_job_or_other_sentences_do_not_reuse_the_answer(store):
    clips = {"m-alcohol": CLIPS[0], "m-allocate": CLIPS[1]}
    pick_topic_sentences(
        clips, SENTENCES, language="tr", job_id="job-1", client=_Script(HAND_AUTHORED)
    )
    other_job = _Script(HAND_AUTHORED)
    pick_topic_sentences(clips, SENTENCES, language="tr", job_id="job-2", client=other_job)
    changed = _Script(HAND_AUTHORED)
    pick_topic_sentences(clips, SENTENCES[:2], language="tr", job_id="job-1", client=changed)
    assert len(other_job.prompts) == 1 and len(changed.prompts) == 1


def test_a_call_whose_outcome_is_unknown_is_not_read_as_unavailable(store):
    from app.agents._runtime import ProviderOutcomeUnknownError

    client = _Script(ProviderOutcomeUnknownError("paid call abc already settled"))
    with pytest.raises(ProviderOutcomeUnknownError):
        pick_topic_sentences({"m-alcohol": CLIPS[0]}, SENTENCES, language="tr", client=client)


def test_a_retry_keys_the_paid_call_apart_from_the_first_attempt(store, monkeypatch):
    ids: list[str | None] = []
    real = TalkingInsertSentenceAgent.run

    def spy(self, input, ctx=None):  # noqa: A002, ANN001
        ids.append(getattr(ctx, "request_id", None))
        return real(self, input, ctx=ctx)

    monkeypatch.setattr(TalkingInsertSentenceAgent, "run", spy)
    clips = {"m-alcohol": CLIPS[0]}
    sure = {"picks": [HAND_AUTHORED["picks"][0]]}
    for scope, job in (("", "job-a"), ("r1", "job-b")):
        pick_topic_sentences(
            clips,
            SENTENCES,
            language="tr",
            job_id=job,
            client=_Script(sure),
            retry_scope=scope,
            execution_id="task-9",
        )
    # The first attempt keeps the ledger's own key; a retry is a new logical call.
    assert ids == [None, "task-9:r1:p1"]


def test_an_input_the_schema_refuses_is_none_not_an_exception(store):
    too_many = {f"m{i}": CLIPS[0] for i in range(MAX_CLIPS + 1)}
    assert pick_topic_sentences(too_many, SENTENCES, language="tr", client=_Script({})) is None
    oversized = SentenceClip(key="c0", summary="x" * 600)
    assert (
        pick_topic_sentences({"m": oversized}, SENTENCES, language="x" * 50, client=_Script({}))
        is None
    )
