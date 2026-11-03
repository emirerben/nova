"""Run the sentence meaning check for a Talking edit's inserted clips (parent KRI-1151).

The only model call of the insert-at-the-sentence path: one request carries every clip and the
creator's numbered sentences. Failure-isolated by contract: any provider error, a malformed or
empty answer, or an input outside the bounds returns ``None``, which the caller reads as "the
check could not run" and answers with its keyword fallback and an honest receipt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import ModelClient, RunContext
from app.agents.talking_insert_sentence import (
    MAX_CLIPS,
    MAX_SENTENCES,
    SentenceChoiceInput,
    SentenceClip,
    TalkingInsertSentenceAgent,
)
from app.pipeline.phone_talking_insert_topics import Sentence, TopicPick

log = structlog.get_logger()


def pick_topic_sentences(
    clips: Mapping[str, SentenceClip],
    sentences: Sequence[Sentence],
    *,
    language: str,
    job_id: str | None = None,
    client: ModelClient | None = None,
) -> dict[str, TopicPick] | None:
    """``{media_id: TopicPick}`` from the model, or ``None`` when the check did not run.

    ``clips`` maps a media id to what the clip shows and says; a clip the model did not answer
    for is simply absent from the result (the caller reads that as "nothing is about it")."""
    if not clips or not sentences or len(clips) > MAX_CLIPS or len(sentences) > MAX_SENTENCES:
        return None
    key_to_media = {f"c{i}": media_id for i, media_id in enumerate(clips, start=1)}
    request = SentenceChoiceInput(
        clips=[
            clip.model_copy(update={"key": key})
            for key, clip in zip(key_to_media, clips.values(), strict=True)
        ],
        sentences=[{"n": s.index, "text": s.text} for s in sentences],
        language=language,
    )
    try:
        output = TalkingInsertSentenceAgent(client or default_client()).run(
            request, ctx=RunContext(job_id=job_id) if job_id else None
        )
    except Exception as exc:  # noqa: BLE001 - any failure means "fall back to keywords"
        log.warning("talking_insert_sentence_failed", error_type=type(exc).__name__)
        return None
    return {
        key_to_media[pick.clip]: TopicPick(
            key_to_media[pick.clip], pick.first, pick.last, pick.confidence
        )
        for pick in output.picks
        if pick.clip in key_to_media
    }
