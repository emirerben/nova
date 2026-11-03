"""The meaning check that decides which of the creator's sentences a clip is about (parent
KRI-1151, insert at the sentence end).

Only recorded responses are replayed here: no network and no paid call, so these tests prove the
prompt carries what the model needs, that a well-formed answer parses into picks, that a
malformed or hostile one cannot place a clip, and that any failure leaves the caller with
``None`` (its signal to fall back to keywords). They do NOT prove the real model picks the right
sentence.
"""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import ModelClient, ModelInvocation, TransientError
from app.agents.talking_insert_sentence import (
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
RECORDED = {
    "picks": [
        {"clip": "c1", "first": 3, "last": 3, "confidence": "high", "reason": "alcohol ban"},
        {"clip": "c2", "first": None, "last": None, "confidence": "high", "reason": "not said"},
    ]
}


class _Recorded(ModelClient):
    """Replays one recorded response, or raises."""

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


def test_a_recorded_answer_becomes_picks_keyed_by_the_clips_media_id():
    client = _Recorded(RECORDED)
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
    client = _Recorded(RECORDED)
    assert pick_topic_sentences({}, SENTENCES, language="tr", client=client) is None
    assert pick_topic_sentences({"m": CLIPS[0]}, [], language="tr", client=client) is None
    too_long = [Sentence(i, "bir iki.", float(i), i + 0.5) for i in range(1, MAX_SENTENCES + 2)]
    assert pick_topic_sentences({"m": CLIPS[0]}, too_long, language="tr", client=client) is None
    assert client.prompts == []
