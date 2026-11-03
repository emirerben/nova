"""Run the sentence meaning check for a Talking edit's inserted clips (parent KRI-1151).

The only model call of the insert-at-the-sentence path: one request carries every clip and the
creator's numbered sentences. The check is an optional helper, so it never costs the creator
his video: a provider error, a malformed or empty answer, or an input outside the bounds
returns what is known (``None`` when nothing), which the caller answers with keywords and then
a labelled guess. Three rules keep the answer honest:

* a clip with no usable answer is asked ONCE more (a second, differently keyed paid call), so
  one bad answer does not decide the placement;
* the answer is stored the moment it is known (`app.services.insert_pick_checkpoint`), so a
  retry of the render task takes the first attempt's answer instead of being refused by the
  paid-call ledger as a replay and silently placing by keyword;
* ``ProviderOutcomeUnknownError`` is NOT "unavailable": the call may have been paid and
  answered, so it propagates to the task's retry machinery, which retries under a retry-scoped
  key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import ModelClient, ProviderOutcomeUnknownError, RunContext
from app.agents.talking_insert_sentence import (
    MAX_CLIPS,
    MAX_SENTENCES,
    SentenceChoiceInput,
    SentenceClip,
    TalkingInsertSentenceAgent,
)
from app.pipeline.phone_talking_insert_topics import Sentence, TopicPick, needs_retry
from app.services import insert_pick_checkpoint

log = structlog.get_logger()


def _digest(request: SentenceChoiceInput) -> str:
    spec = TalkingInsertSentenceAgent.spec
    payload = json.dumps(
        {
            "agent": f"{spec.name}@{spec.prompt_version}:{spec.model}",
            "input": request.model_dump(mode="json"),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ask(
    request: SentenceChoiceInput,
    *,
    job_id: str | None,
    request_id: str | None,
    client: ModelClient | None,
) -> dict[str, TopicPick] | None:
    """One call, as ``{clip key: TopicPick}``; ``None`` when it failed (not when unknown)."""
    ctx = RunContext(job_id=job_id, request_id=request_id) if job_id or request_id else None
    try:
        output = TalkingInsertSentenceAgent(client or default_client()).run(request, ctx=ctx)
    except ProviderOutcomeUnknownError:
        raise
    except Exception as exc:  # noqa: BLE001 - any other failure means "fall back"
        log.warning("talking_insert_sentence_failed", error_type=type(exc).__name__)
        return None
    return {
        pick.clip: TopicPick(pick.clip, pick.first, pick.last, pick.confidence)
        for pick in output.picks
    }


def pick_topic_sentences(
    clips: Mapping[str, SentenceClip],
    sentences: Sequence[Sentence],
    *,
    language: str,
    job_id: str | None = None,
    client: ModelClient | None = None,
    retry_scope: str = "",
    execution_id: str | None = None,
) -> dict[str, TopicPick] | None:
    """``{media_id: TopicPick}`` from the model, or ``None`` when the check gave nothing.

    ``clips`` maps a media id to what the clip shows and says; a clip the model did not answer
    for is absent from the result (the caller places it by keyword, then by a labelled guess).
    ``retry_scope`` is "" on a task's first attempt and names the retry otherwise (``"r1"``): it
    keys the paid call so a retried render is a new logical call; ``execution_id`` is the
    delivery's id (the Celery task id).
    """
    if not clips or not sentences or len(clips) > MAX_CLIPS or len(sentences) > MAX_SENTENCES:
        return None
    key_to_media = {f"c{i}": media_id for i, media_id in enumerate(clips, start=1)}
    try:
        request = SentenceChoiceInput(
            clips=[
                clip.model_copy(update={"key": key})
                for key, clip in zip(key_to_media, clips.values(), strict=True)
            ],
            sentences=[{"n": s.index, "text": s.text} for s in sentences],
            language=language,
        )
    except ValueError as exc:
        log.warning("talking_insert_sentence_input_invalid", error_type=type(exc).__name__)
        return None

    digest = _digest(request)
    if job_id:
        stored = insert_pick_checkpoint.load(job_id, digest)
        if stored is not None:
            return _from_rows(stored, set(clips))

    base = execution_id or job_id or ""

    def request_id(pass_no: int) -> str | None:
        if pass_no == 1 and not retry_scope:
            return None  # the ledger's own key: unchanged for a first, single attempt
        return ":".join(part for part in (base, retry_scope, f"p{pass_no}") if part)

    first = _ask(request, job_id=job_id, request_id=request_id(1), client=client)
    answered: dict[str, TopicPick] = dict(first or {})
    count = len(sentences)
    again = [key for key in key_to_media if needs_retry(answered.get(key), count)]
    if again:
        narrowed = request.model_copy(
            update={"clips": [c for c in request.clips if c.key in set(again)]}
        )
        second = _ask(narrowed, job_id=job_id, request_id=request_id(2), client=client)
        for key, pick in (second or {}).items():
            if key in again and not needs_retry(pick, count):
                answered[key] = pick
            elif key in again and key not in answered:
                answered[key] = pick
    if first is None and not answered:
        return None
    picks = {key_to_media[key]: pick for key, pick in answered.items() if key in key_to_media}
    result = {
        media_id: TopicPick(media_id, pick.first, pick.last, pick.confidence)
        for media_id, pick in picks.items()
    }
    if job_id and result:
        insert_pick_checkpoint.store(job_id, digest, _to_rows(result))
    return result


def _to_rows(picks: Mapping[str, TopicPick]) -> list[dict]:
    return [
        {"media_id": m, "first": p.first, "last": p.last, "confidence": p.confidence}
        for m, p in picks.items()
    ]


def _from_rows(rows: Sequence[Mapping], wanted: set[str]) -> dict[str, TopicPick]:
    out: dict[str, TopicPick] = {}
    for row in rows:
        media_id = row.get("media_id")
        if media_id in wanted:
            out[media_id] = TopicPick(
                media_id, row.get("first"), row.get("last"), row.get("confidence") or "medium"
            )
    return out
