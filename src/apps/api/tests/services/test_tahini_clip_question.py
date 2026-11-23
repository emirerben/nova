"""Tahini (job 1d062f21, 2026-11-17): a recipe chat asked "which of your clips show yogurt
spread on the plate?" with all 12 clips offered and none ticked.

What happened, from the plan item's agent runs: the turn ran three times (two auto-continues
after vision checks hit their per-run budget). The clip resolver got a byte-identical input
for the yogurt shard every time; it matched the yogurt-swirl clip (0.8) in runs 1 and 2 and
dropped it in run 3, and run 3's answer was the one the chat used. Every other described clip
was matched, so the yogurt clip was the only one no description claimed.

Failure modes (the red controls below show each test fails without its fix):

- a continued run re-asks the resolver for the same shard and a flapping model answer turns
  a found clip into a question (the incident);
- reusing a stored answer when the shard input changed (new clip, new intent, new analysis)
  gives an answer for different footage;
- the stored answer is reused outside a continued turn (first run, no turn) or with the
  checkpoint switch off;
- the "last clip left" pre-tick fires when two or more clips are unclaimed, guessing between
  look-alikes;
- "use every uploaded file" counts as claiming every clip, so nothing is ever left;
- a clip claimed by a described intent is pre-ticked for another description;
- the pre-tick is applied without the creator's tap, or it replaces a found clip or a
  near-miss suggestion the resolver did make;
- label/caption/exclude rows get a pre-tick they cannot use.

Replays the real clip analyses and the resolver's recorded answers; no model call.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverInput,
    ClipRequestResolverOutput,
)
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_resolution as R
from app.services import turn_checkpoint
from app.services.clip_intent_resolution import IntentClip, resolve_clip_intents_for_turn
from app.services.clip_selection import answered_rows

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "tahini_1d062f21_clip_resolver_runs.json"
    ).read_text()
)
YOGURT = "tahini-08-09_plate_yogurt-swirl"
TAHINI = "tahini-09-08_bowl_tahini-stir"


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=c["media_id"], kind=c["kind"], analysis={"understanding": c["understanding"]}
        )
        for c in FIXTURE["clips"]
    ]


def _intents() -> list[ClipIntent]:
    return [ClipIntent.model_validate(i) for i in FIXTURE["intents"]]


def _shard_key(shard: ClipRequestResolverInput) -> str:
    return ",".join(c.alias for c in shard.clips)


def _answers(run: int, *, yogurt_shard_run: int | None = None) -> dict[str, dict[str, Any]]:
    answers = copy.deepcopy(FIXTURE["resolver_outputs_by_run"][str(run)])
    if yogurt_shard_run is not None:
        answers["m007,m008,m009"] = copy.deepcopy(
            FIXTURE["resolver_outputs_by_run"][str(yogurt_shard_run)]["m007,m008,m009"]
        )
    return answers


def _fake_resolver(monkeypatch: pytest.MonkeyPatch, answers: dict[str, dict[str, Any]]) -> list:
    calls: list[str] = []

    def fake_run(self, input: ClipRequestResolverInput, *, ctx=None):  # noqa: A002, ANN001
        calls.append(_shard_key(input))
        return ClipRequestResolverOutput.model_validate(answers[_shard_key(input)])

    monkeypatch.setattr(ClipRequestResolverAgent, "run", fake_run)
    return calls


async def _resolve() -> R.IntentResolution:
    return await resolve_clip_intents_for_turn(
        intents=_intents(),
        creator_request="",
        clips=_clips(),
        run_context=RunContext(request_id="tahini", creator_id="u"),
    )


def _yogurt_row(result: R.IntentResolution) -> dict[str, Any]:
    assert result.clip_question is not None
    (row,) = [
        c
        for c in result.clip_question["categories"]
        if "show_yogurt_spread" in {r.get("intent_id") for r in answered_rows(c)}
    ]
    return row


# 1. The last clip no description claimed is pre-ticked -------------------------------------


async def test_control_without_the_pre_tick_prods_question_offers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red control: run 3's answers through the real resolution, pre-tick disabled, give
    production's question: the yogurt row, all 12 clips offered, none ticked."""
    _fake_resolver(monkeypatch, _answers(3))
    monkeypatch.setattr(R, "_claimed_by_described_clips", lambda *_a, **_k: frozenset())

    result = await _resolve()

    assert result.status == "needs_creator"
    row = _yogurt_row(result)
    assert len(row["candidate_media_ids"]) == 12
    assert row["suggested_media_ids"] == []


async def test_run3_answers_pre_tick_the_yogurt_clip_and_apply_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_resolver(monkeypatch, _answers(3))

    result = await _resolve()

    assert result.status == "needs_creator"  # still the creator's tap, never auto-applied
    row = _yogurt_row(result)
    assert row["suggested_media_ids"] == [YOGURT]
    yogurt = next(i for i in result.intents if i.intent_id == "show_yogurt_spread")
    assert yogurt.assignments == []


async def test_wrong_content_control_two_clips_left_tick_neither(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drop the tahini match: the yogurt and tahini clips are both unclaimed, so the yogurt
    row must not guess between them."""
    answers = _answers(3)
    for intent in answers["m007,m008,m009"]["intents"]:
        if intent["intent_id"] == "show_tahini_stirred_in":
            intent["assignments"] = []
    _fake_resolver(monkeypatch, answers)

    result = await _resolve()

    assert _yogurt_row(result)["suggested_media_ids"] == []


async def test_a_clip_another_description_claimed_is_never_pre_ticked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lemon description also claims the yogurt clip: nothing is left, nothing ticked."""
    answers = _answers(3)
    for intent in answers["m007,m008,m009"]["intents"]:
        if intent["intent_id"] == "show_lemon_grated_yogurt":
            intent["assignments"].append(
                {"media": "m008", "value": None, "evidence": "white bowl", "confidence": 0.9}
            )
    _fake_resolver(monkeypatch, answers)

    result = await _resolve()

    assert _yogurt_row(result)["suggested_media_ids"] == []


async def test_a_clip_another_open_row_found_is_never_pre_ticked() -> None:
    """Self-review: every clip but the yogurt one is claimed by resolved rows, and the lemon
    row is still open with the yogurt clip found. That clip is the lemon row's, so the yogurt
    row pre-ticks nothing (before the fix it pre-ticked the lemon row's clip)."""
    clips = _clips()
    claimed = frozenset(c.media_id for c in clips if c.media_id != YOGURT)
    yogurt = R._IntentWork(
        intent=ClipIntent(intent_id="show_yogurt_spread", op="include", attribute="yogurt")
    )
    lemon = R._IntentWork(
        intent=ClipIntent(intent_id="show_lemon", op="include", attribute="lemon grated"),
        kept=[R.ClipAssignment(media_id=YOGURT, confidence=0.9, evidence="bowl")],
    )

    alone = R._build_clip_question([yogurt], clips, claimed_elsewhere=claimed)
    both = R._build_clip_question([yogurt, lemon], clips, claimed_elsewhere=claimed)

    assert alone is not None and both is not None
    assert alone["categories"][0]["suggested_media_ids"] == [YOGURT]  # control: left alone
    rows = {r["intent_id"]: r for c in both["categories"] for r in answered_rows(c)}
    assert rows["show_yogurt_spread"]["suggested_media_ids"] == []


def test_use_every_file_claims_nothing_and_described_clips_do() -> None:
    clips = _clips()
    every = R.ResolvedClipIntent(
        intent_id="include_all_files",
        op="include",
        attribute="every uploaded file",
        assignments=[
            R.ClipAssignment(media_id=c.media_id, confidence=1.0, evidence="all") for c in clips
        ],
    )
    tahini = R.ResolvedClipIntent(
        intent_id="show_tahini",
        op="include",
        attribute="tahini stirred in",
        assignments=[R.ClipAssignment(media_id=TAHINI, confidence=0.7, evidence="stir")],
    )
    label = R.ResolvedClipIntent(
        intent_id="label_yogurt",
        op="label",
        attribute="the yogurt",
        assignments=[R.ClipAssignment(media_id=YOGURT, confidence=0.9, evidence="dip")],
    )

    assert R._claimed_by_described_clips([every], clips) == frozenset()
    assert R._claimed_by_described_clips([every, tahini, label], clips) == {TAHINI}


# 2. A continued run reuses the resolver's answer for the same shard ------------------------


class _Pipe:
    def __init__(self, store: _MemoryRedis) -> None:
        self.store, self.ops = store, []

    def hset(self, key: str, field: str, value: str) -> None:
        self.ops.append((key, field, value))

    def expire(self, key: str, _seconds: int) -> None:
        pass

    def execute(self) -> None:
        for key, field, value in self.ops:
            self.store.hashes.setdefault(key, {})[field] = value.encode()


class _MemoryRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, bytes]] = {}

    def hget(self, key: str, field: str) -> bytes | None:
        return self.hashes.get(key, {}).get(field)

    def pipeline(self) -> _Pipe:
        return _Pipe(self)


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch) -> _MemoryRedis:
    store = _MemoryRedis()
    monkeypatch.setattr(turn_checkpoint, "_redis", lambda: store)
    monkeypatch.setenv("FLY_IMAGE_REF", "registry.fly.io/nova-video:deployment-A")
    return store


async def _resolve_in_turn(turn: uuid.UUID, epoch: int) -> R.IntentResolution:
    from app.kria.paid_call_scope import turn_run_epoch  # noqa: PLC0415

    run_token = turn_checkpoint.turn_run_id.set(turn)
    epoch_token = turn_run_epoch.set(epoch)
    try:
        return await _resolve()
    finally:
        turn_run_epoch.reset(epoch_token)
        turn_checkpoint.turn_run_id.reset(run_token)


async def test_a_continued_run_keeps_the_match_the_first_run_made(
    monkeypatch: pytest.MonkeyPatch, memory: _MemoryRedis
) -> None:
    """Run 1 matched the yogurt clip; run 3 of the same turn gets the flapped answer from the
    model but must not ask it: the shard input is the same, so the stored answer is reused."""
    turn = uuid.uuid4()
    first_calls = _fake_resolver(monkeypatch, _answers(3, yogurt_shard_run=1))
    first = await _resolve_in_turn(turn, epoch=1)
    assert len(first_calls) == 4
    assert first.status == "resolved"

    later_calls = _fake_resolver(monkeypatch, _answers(3))
    later = await _resolve_in_turn(turn, epoch=3)

    assert later_calls == []
    assert later.status == "resolved"
    assert later.clip_question is None
    yogurt = next(i for i in later.intents if i.intent_id == "show_yogurt_spread")
    assert [a.media_id for a in yogurt.assignments] == [YOGURT]


async def test_control_with_the_checkpoint_off_the_flap_comes_back(
    monkeypatch: pytest.MonkeyPatch, memory: _MemoryRedis
) -> None:
    monkeypatch.setattr(R.settings, "kria_turn_checkpoint_enabled", False, raising=False)
    monkeypatch.setattr(turn_checkpoint.settings, "kria_turn_checkpoint_enabled", False)
    turn = uuid.uuid4()
    _fake_resolver(monkeypatch, _answers(3, yogurt_shard_run=1))
    await _resolve_in_turn(turn, epoch=1)

    later_calls = _fake_resolver(monkeypatch, _answers(3))
    later = await _resolve_in_turn(turn, epoch=3)

    assert len(later_calls) == 4
    assert later.status == "needs_creator"
    assert _yogurt_row(later)["suggested_media_ids"] == [YOGURT]  # the pre-tick still helps


async def test_a_changed_shard_is_asked_again(
    monkeypatch: pytest.MonkeyPatch, memory: _MemoryRedis
) -> None:
    """A new analysis for one clip changes its shard's input: only that shard is re-asked."""
    turn = uuid.uuid4()
    _fake_resolver(monkeypatch, _answers(3, yogurt_shard_run=1))
    await _resolve_in_turn(turn, epoch=1)

    changed = copy.deepcopy(FIXTURE)
    changed["clips"][7]["understanding"]["summary"] = "A spoon spreads thick yogurt on a plate."
    monkeypatch.setitem(FIXTURE, "clips", changed["clips"])
    later_calls = _fake_resolver(monkeypatch, _answers(3))
    await _resolve_in_turn(turn, epoch=2)

    assert later_calls == ["m007,m008,m009"]


async def test_the_first_run_never_reads_a_stored_answer(
    monkeypatch: pytest.MonkeyPatch, memory: _MemoryRedis
) -> None:
    turn = uuid.uuid4()
    _fake_resolver(monkeypatch, _answers(3, yogurt_shard_run=1))
    await _resolve_in_turn(turn, epoch=1)

    again = _fake_resolver(monkeypatch, _answers(3))
    await _resolve_in_turn(turn, epoch=1)

    assert len(again) == 4
