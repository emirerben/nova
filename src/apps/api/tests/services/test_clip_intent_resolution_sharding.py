"""KRI-282: a ~47-clip montage must not fail the resolver as a whole.

History: a single resolver call over 47 clips x 7-8 intents ran ~20s (the agent's
own timeout) and the turn degraded to "I couldn't reliably match that request".
#1343 sharded the clip list at 12 clips/call; that still left one shard within a
2x latency swing of the limit, a timed-out shard (outcome unknown, never blindly
re-sent) failing the WHOLE turn, and no record of WHICH shard/stage failed. These
tests pin: shard sizing by clips x intents on REAL-SIZED records (~700-1300 chars,
measured on the real Olympics thread), bounded concurrency, split-retry of an
unknown-outcome shard, fail-closed on a second failure, and redacted diagnostics.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from app.agents._runtime import (
    ProviderOutcomeUnknownError,
    RunContext,
    TerminalSchemaError,
    TransientError,
)
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
    _resolver_failure_status_and_code,
    _shard_resolver_input,
    resolve_clip_intents_for_turn,
)

pytestmark = pytest.mark.asyncio

CLIP_COUNT = 47
INTENT_COUNT = 8


def _analysis(i: int) -> dict:
    """A record shaped like the real analyzer's (mean ~700 chars, max ~1400)."""
    sport = "soccer" if i % 2 == 0 else "pizza"
    moments = [
        {
            "start_s": 1.0 + k,
            "end_s": 2.5 + k,
            "description": f"player {k} moves with the {sport} ball across the field",
        }
        for k in range(3)
    ]
    return {
        "understanding": {
            "kind": "video",
            "subject": f"group of people playing {sport}",
            "summary": (
                f"A group of young people is doing {sport} on a grassy field under a "
                "cloudy sky. The participants pass and move around while a few "
                "bystanders watch from the edge and film on their phones."
            ),
            "setting": "outdoor grassy field with trees in the background",
            "activity": f"playing {sport}",
            "people": {"speaks_to_camera": False, "count": 9, "note": "young adults in teams"},
            "speech": {
                "has_speech": i % 3 == 0,
                "to_camera": False,
                "transcript": "okay one more round who is next come on team blue " * 4
                if i % 3 == 0
                else "",
            },
            "content_type": "action",
            "audio_type": "music",
            "notable_moments": moments,
        }
    }


def _clips(n: int = CLIP_COUNT) -> list[IntentClip]:
    return [
        IntentClip(
            media_id=f"asset-{i:03d}",
            kind="video",
            analysis=_analysis(i),
            gcs_path=f"users/u/clips/{i}.mp4",
        )
        for i in range(n)
    ]


def _intents(n: int = INTENT_COUNT) -> list[ClipIntent]:
    return [ClipIntent(intent_id=f"g{k}", op="group", attribute=f"chapter {k}") for k in range(n)]


def _ctx() -> RunContext:
    return RunContext(request_id="t", creator_id="u")


def test_real_sized_records_are_realistic() -> None:
    """Guard the fixture: if the generator shrinks, the sizing tests lose meaning."""
    resolver_input, _a, _b = R._build_resolver_input(_intents(), "", _clips(12))
    per_clip = [len(json.dumps(c.record)) for c in resolver_input.clips]
    assert 500 <= sum(per_clip) / len(per_clip) <= 1400


def test_forty_seven_clips_shard_into_cell_bounded_disjoint_calls() -> None:
    resolver_input, _alias_to_media, aliases = R._build_resolver_input(_intents(), "", _clips())
    shards = _shard_resolver_input(resolver_input)

    sizes = [len(s.clips) for s in shards]
    assert len(shards) >= 6
    assert max(sizes) <= R._shard_clip_limit(INTENT_COUNT) <= R._RESOLVER_SHARD_MAX_CLIPS
    # Each call carries well under the 96-cell shard that took 7-16s in the live
    # measurement (limit was 20s).
    assert max(sizes) * INTENT_COUNT <= R._RESOLVER_SHARD_CELLS
    assert max(sizes) - min(sizes) <= 1
    flat = [c.alias for s in shards for c in s.clips]
    assert flat == aliases  # every clip exactly once, order preserved
    assert all(len(s.intents) == INTENT_COUNT for s in shards)


def test_few_intents_allow_larger_shards_but_never_over_the_clip_cap() -> None:
    assert R._shard_clip_limit(1) == R._RESOLVER_SHARD_MAX_CLIPS
    assert R._shard_clip_limit(100) == R._RESOLVER_MIN_SHARD_CLIPS


def test_small_project_keeps_a_single_call() -> None:
    limit = R._shard_clip_limit(INTENT_COUNT)
    resolver_input, _a, _b = R._build_resolver_input(_intents(), "", _clips(limit))
    assert _shard_resolver_input(resolver_input) == [resolver_input]


def test_resolver_agent_timeout_has_headroom_over_a_measured_slow_shard() -> None:
    # Live: a 96-cell shard took up to 16s (one needed a refusal retry). The
    # agent budget must be well above that and the chat deadline above 2 timeouts.
    assert ClipRequestResolverAgent.spec.timeout_s >= 30.0
    assert R._RESOLVER_DEADLINE_S >= 2 * ClipRequestResolverAgent.spec.timeout_s


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
    # KRI-282: other shards matched clips, so the stray question is dropped.
    assert intent.question is None


def _soccer_output(input: ClipRequestResolverInput) -> ClipRequestResolverOutput:  # noqa: A002
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


async def test_forty_seven_clips_resolve_across_bounded_concurrent_shards(monkeypatch) -> None:
    calls: list[int] = []
    running = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def fake_run(self, input: ClipRequestResolverInput, *, ctx=None):  # noqa: A002, ANN001
        with lock:
            calls.append(len(input.clips))
            running["now"] += 1
            running["peak"] = max(running["peak"], running["now"])
        time.sleep(0.05)
        with lock:
            running["now"] -= 1
        return _soccer_output(input)

    monkeypatch.setattr(ClipRequestResolverAgent, "run", fake_run)

    result = await resolve_clip_intents_for_turn(
        intents=_intents(),
        creator_request="",
        clips=_clips(),
        run_context=_ctx(),
    )

    assert sum(calls) == CLIP_COUNT
    assert max(calls) <= R._shard_clip_limit(INTENT_COUNT)
    assert 1 < running["peak"] <= R._RESOLVER_MAX_CONCURRENCY
    assert result.status == "resolved"
    assert not result.needs_creator
    members = {a.media_id for a in result.intents[0].assignments}
    assert members == {f"asset-{i:03d}" for i in range(0, CLIP_COUNT, 2)}
    assert result.diagnostics is None or result.diagnostics["shards"] == len(calls)


async def test_timed_out_shard_is_split_and_retried_once(monkeypatch) -> None:
    """An unknown-outcome shard is never re-sent whole (double-charge risk); it is
    re-asked as two smaller halves and the turn still resolves."""
    seen = {"n": 0}
    sizes: list[int] = []
    lock = threading.Lock()

    def fake_run(self, input: ClipRequestResolverInput, *, ctx=None):  # noqa: A002, ANN001
        with lock:
            seen["n"] += 1
            first_of_big = seen["n"] == 2
            sizes.append(len(input.clips))
        if first_of_big:
            raise ProviderOutcomeUnknownError("gemini provider outcome unknown after 30.0s")
        return _soccer_output(input)

    monkeypatch.setattr(ClipRequestResolverAgent, "run", fake_run)

    result = await resolve_clip_intents_for_turn(
        intents=_intents(),
        creator_request="",
        clips=_clips(),
        run_context=_ctx(),
    )

    assert result.status == "resolved"
    members = {a.media_id for a in result.intents[0].assignments}
    assert members == {f"asset-{i:03d}" for i in range(0, CLIP_COUNT, 2)}
    shard_count = len(_shard_resolver_input(R._build_resolver_input(_intents(), "", _clips())[0]))
    # S first-pass calls + 2 halves for the one timed-out shard; the failed call
    # is the only one that did not answer, and its halves cover its clips.
    assert len(sizes) == shard_count + 2


async def test_second_failure_still_fails_closed_with_redacted_diagnostics(monkeypatch) -> None:
    def fake_run(self, input: ClipRequestResolverInput, *, ctx=None):  # noqa: A002, ANN001
        if input.clips[0].alias == "m001":
            raise ProviderOutcomeUnknownError("timed out")
        return ClipRequestResolverOutput(intents=[])

    monkeypatch.setattr(ClipRequestResolverAgent, "run", fake_run)

    result = await resolve_clip_intents_for_turn(
        intents=_intents(),
        creator_request="a private sentence about my family trip",
        clips=_clips(),
        run_context=_ctx(),
    )

    assert result.status == "provider_unavailable"
    assert result.error_code == "provider_outcome_unknown"
    assert all(i.status == "needs_creator" and not i.assignments for i in result.intents)
    diag = result.diagnostics
    assert diag is not None
    assert diag["stage"] == "resolver"
    assert diag["reason"] == "provider_outcome_unknown"
    assert diag["split_retries"] == 1
    assert diag["clips"] == CLIP_COUNT and diag["intents"] == INTENT_COUNT
    assert "ProviderOutcomeUnknownError" in diag["failed_error_types"]
    # Redacted: counts/codes only, never creator text.
    assert "family" not in json.dumps(diag)


async def test_single_clip_shard_is_not_split(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_run(self, input, *, ctx=None):  # noqa: A002, ANN001
        calls["n"] += 1
        raise ProviderOutcomeUnknownError("timed out")

    monkeypatch.setattr(ClipRequestResolverAgent, "run", fake_run)
    result = await resolve_clip_intents_for_turn(
        intents=_intents(2), creator_request="", clips=_clips(1), run_context=_ctx()
    )
    assert calls["n"] == 1
    assert result.error_code == "provider_outcome_unknown"
    assert result.diagnostics["split_retries"] == 0


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ProviderOutcomeUnknownError("x"), "provider_outcome_unknown"),
        (TimeoutError(), "resolver_deadline_exceeded"),
        (TransientError("busy"), "resolver_transient_exhausted"),
        (TerminalSchemaError("bad json"), "resolver_schema_error"),
        (RuntimeError("boom"), "resolver_error"),
        # An OSError in the TEXT resolver is not an unreadable clip.
        (OSError("socket"), "resolver_error"),
    ],
)
async def test_resolver_failure_reason_codes(exc, code) -> None:
    assert _resolver_failure_status_and_code(exc)[1] == code
