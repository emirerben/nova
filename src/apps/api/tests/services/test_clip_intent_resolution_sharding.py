"""KRI-282: a ~47-clip montage must not fail the resolver as a whole.

A single resolver call over 47 clips x 7-8 intents ran ~20s, i.e. at the agent's
own 20s timeout, so the chat turn degraded to "I couldn't reliably match that
request". The clip list is sharded across concurrent calls instead; these tests
pin the sharding, the merge, and that a failed shard still fails closed.
"""

from __future__ import annotations

import threading

import pytest

from app.agents._runtime import ProviderOutcomeUnknownError, RunContext
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverInput,
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
)
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_resolution as R
from app.services.clip_intent_resolution import (
    IntentClip,
    _merge_resolver_outputs,
    _shard_resolver_input,
    resolve_clip_intents_for_turn,
)

pytestmark = pytest.mark.asyncio

CLIP_COUNT = 47


def _clips(n: int = CLIP_COUNT) -> list[IntentClip]:
    return [
        IntentClip(
            media_id=f"asset-{i:03d}",
            kind="video",
            analysis={"subject": "people playing soccer" if i % 2 == 0 else "pizza on a table"},
            gcs_path=f"users/u/clips/{i}.mp4",
        )
        for i in range(n)
    ]


def _intents() -> list[ClipIntent]:
    return [ClipIntent(intent_id=f"g{k}", op="group", attribute=f"chapter {k}") for k in range(8)]


def test_forty_seven_clips_shard_into_bounded_disjoint_calls() -> None:
    resolver_input, _alias_to_media, aliases = R._build_resolver_input(_intents(), "", _clips())
    shards = _shard_resolver_input(resolver_input)

    assert len(shards) > 1
    sizes = [len(s.clips) for s in shards]
    assert max(sizes) <= R._RESOLVER_SHARD_CLIPS
    assert max(sizes) - min(sizes) <= 1
    flat = [c.alias for s in shards for c in s.clips]
    assert flat == aliases  # every clip exactly once, order preserved
    assert all(len(s.intents) == 8 for s in shards)


def test_small_project_keeps_a_single_call() -> None:
    resolver_input, _a, _b = R._build_resolver_input(
        _intents(), "", _clips(R._RESOLVER_SHARD_CLIPS)
    )
    assert _shard_resolver_input(resolver_input) == [resolver_input]


def test_merge_concatenates_membership_and_picks_caption_from_largest_shard() -> None:
    def out(aliases: list[str], caption: str | None, question: str | None = None):
        return ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="c1",
                    assignments=[ResolverAssignment(media=a, confidence=0.9) for a in aliases],
                    caption=caption,
                    question=question,
                )
            ]
        )

    merged = _merge_resolver_outputs(
        [out(["m001"], "small"), out(["m013", "m014"], "big", "which one?"), out([], None)]
    )

    (intent,) = merged.intents
    assert [a.media for a in intent.assignments] == ["m001", "m013", "m014"]
    assert intent.caption == "big"
    assert intent.question == "which one?"


async def test_forty_seven_clips_resolve_across_concurrent_shards(monkeypatch) -> None:
    calls: list[int] = []
    threads: set[int] = set()
    lock = threading.Lock()

    def fake_run(self, input: ClipRequestResolverInput, *, ctx=None):  # noqa: A002, ANN001
        with lock:
            calls.append(len(input.clips))
            threads.add(threading.get_ident())
        # Every even-numbered clip belongs to every intent.
        return ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id=i.intent_id,
                    assignments=[
                        ResolverAssignment(media=c.alias, confidence=0.95, evidence="record")
                        for c in input.clips
                        if "soccer" in str(c.record)
                    ],
                )
                for i in input.intents
            ]
        )

    monkeypatch.setattr(ClipRequestResolverAgent, "run", fake_run)

    result = await resolve_clip_intents_for_turn(
        intents=_intents(),
        creator_request="",
        clips=_clips(),
        run_context=RunContext(request_id="t", creator_id="u"),
    )

    assert sum(calls) == CLIP_COUNT
    assert max(calls) <= R._RESOLVER_SHARD_CLIPS
    assert result.status == "resolved"
    assert not result.needs_creator
    members = {a.media_id for a in result.intents[0].assignments}
    assert members == {f"asset-{i:03d}" for i in range(0, CLIP_COUNT, 2)}


async def test_one_failed_shard_still_fails_closed(monkeypatch) -> None:
    seen = {"n": 0}

    def fake_run(self, input, *, ctx=None):  # noqa: A002, ANN001
        seen["n"] += 1
        if seen["n"] == 2:
            raise ProviderOutcomeUnknownError("timed out")
        return ClipRequestResolverOutput(intents=[])

    monkeypatch.setattr(ClipRequestResolverAgent, "run", fake_run)

    result = await resolve_clip_intents_for_turn(
        intents=_intents(),
        creator_request="",
        clips=_clips(),
        run_context=RunContext(request_id="t", creator_id="u"),
    )

    assert result.status == "provider_unavailable"
    assert result.error_code == "provider_outcome_unknown"
    assert all(i.status == "needs_creator" and not i.assignments for i in result.intents)
