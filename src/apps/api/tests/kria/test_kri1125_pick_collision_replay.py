"""KRI-1125: one photo is never the answer for two ordered beats without a question, and a typed
correction of a clip-picker tap is heard.

Leverkusen M4 run 1 (2026-10-31, thread eeac2d3b, server 9ea7858ae). The creator tapped the
team-bus photo for "the warm-up" in the clip picker (a tile-numbering slip). Six minutes later
they corrected it in plain words: "The warm-up is the photo of the players on the pitch with the
big Leverkusen screen, not the team bus." The turn re-planned, but:

* the thread's picker answers are folded every turn and a typed message never replaces one, so
  `apply_creator_selections` settled the warm-up on the bus ("creator selected") BEFORE the
  resolver, which therefore never read the correction (the gate, matched by the resolver, did
  take its correction in the very same message);
* the bus photo then sat on two ordered beats, and nothing downstream says so: the second row is
  `contained`, the receipts skip it, the real warm-up photo falls into `apply_sequence`'s rest
  bucket and plays 9th.

Ways this can fail, each pinned below (written before the code):

 R1  The photo stays on two ordered beats and nobody is told (the incident, with its typed fix).
 R1b The same, with no typed fix in the thread: the creator must be asked about the PICKED beat,
     the contested photo left out of its candidates, nothing silently dropped.
 R2  The typed correction still loses to the stale tap: the warm-up beat must go back to the
     resolver, which reads the correction.
 R2b The resolver cannot place it either: the picker asks again, the stale bus never pre-ticked.
 R3  CONTROL: a tap nobody contradicts sticks across later typed messages (KRI-282/697/947/950),
     even one that names the beat, and the resolver never receives it.
 R4  A later typed message that does not name the beat is no correction: the tap is kept and the
     clash is asked.
 R5  Two taps on one photo: the OLDER tap is the one asked again; the newer answer stands.
 R6  CONTROL: a video is never a clash (one take can hold two moments, KRI-779/796).
 R7  CONTROL: a multi-clip group that contains the photo is no single-photo clash.

Replayed from the real M4 thread (`fixtures/kri1125_m4_warmup_pick.json`: events, the picker
question and the creator's tap, the 11 intents the t5 resolver was sent). Only the provider
boundary is replayed (the clip-intent planner's and the resolver's answers); the thread fold, the
selection match, the collision check and the picker builder are real. No network, no paid call.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.kria import planner
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services import clip_intent_planning as service
from app.services.choice_questions import tag_event
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.services.clip_selection import build_clip_question, intent_key

CASE: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri1125_m4_warmup_pick.json").read_text()
)
ALIAS = {c["alias"]: c["media_id"] for c in CASE["clips"]}
KIND = {c["media_id"]: c["kind"] for c in CASE["clips"]}
BUS = ALIAS["m009"]
GATE = ALIAS["m006"]
WARMUP_PHOTO = ALIAS["m011"]
GOAL_VIDEO = ALIAS["m001"]
WARMUP_KEY = "order:the-warm-up"
TYPED_CORRECTION = CASE["latest_user_message"]


def _thread(*, until_the_pick: bool = False, warm_up_pick: str = BUS) -> list[tuple[str, Any]]:
    """The M4 thread as the server read it at 14:30:55Z (or only up to the tap at 14:25:21Z)."""
    rows = []
    for event in CASE["thread"]:
        payload = json.loads(json.dumps(event["payload"]))
        for answer in (payload.get("clip_selection") or {}).get("answers", []):
            if answer["key"] == WARMUP_KEY:
                answer["media_ids"] = [warm_up_pick]
        rows.append(tag_event(event["role"], payload, event["event_type"], event["content"]))
        if until_the_pick and "clip_selection" in payload:
            break
    return rows


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=c["media_id"],
            kind=c["kind"],
            analysis={"subject": "x", "summary": "x"},
            gcs_path=f"users/u/{c['media_id']}",
        )
        for c in CASE["clips"]
    ]


def _matches(**extra: str | list[str]) -> dict[str, list[str]]:
    """What the three real t5 resolver shards matched, plus what a test's resolver adds."""
    merged: dict[str, Any] = {**CASE["resolver_matches"], **extra}
    return {k: [v] if isinstance(v, str) else list(v) for k, v in merged.items()}


class _Replay:
    """The planner model and the resolver, replayed; everything between them is the real code."""

    def __init__(self, monkeypatch, *, matches: dict[str, list[str]], unplaced=frozenset()):
        self.matches = matches
        self.unplaced = set(unplaced)
        self.planner_runs = 0
        intents = [
            PlannedClipIntent(
                intent_id=row["intent_id"],
                op=row["op"],
                attribute=row["attribute"],
                creator_text=row.get("creator_text"),
                position=row.get("position"),
                source_quote=row["attribute"],
            )
            for row in CASE["intents"]
        ]
        agent = MagicMock()

        def run(*_a, **_k):
            self.planner_runs += 1
            return ClipIntentPlannerOutput(intents=intents)

        agent.run.side_effect = run
        monkeypatch.setattr(service, "default_client", MagicMock())
        monkeypatch.setattr(service, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
        monkeypatch.setattr(service.settings, "clip_intent_exclude_enabled", True)
        monkeypatch.setattr(planner.settings, "kria_clip_understanding_enabled", False)
        self.resolver = AsyncMock(side_effect=self._resolve)
        monkeypatch.setattr(service, "resolve_clip_intents_for_turn", self.resolver)

    async def _resolve(self, *, intents: list[ClipIntent], **_kwargs) -> IntentResolution:
        out, asked = [], []
        for intent in intents:
            if intent.intent_id in self.unplaced:
                asked.append(intent)
                continue
            assert intent.intent_id in self.matches, f"resolver was sent an unexpected {intent}"
            out.append(
                ResolvedClipIntent(
                    **intent.model_dump(),
                    status="resolved",
                    assignments=[
                        ClipAssignment(
                            media_id=ALIAS[a] if a in ALIAS else a,
                            confidence=1.0,
                            evidence="resolver",
                        )
                        for a in self.matches[intent.intent_id]
                    ],
                )
            )
        if not asked:
            return IntentResolution(intents=out)
        question = build_clip_question(
            [
                {
                    "key": intent_key(i),
                    "label": i.attribute,
                    "op": i.op,
                    "intent_id": i.intent_id,
                    "creator_text": i.creator_text,
                    "candidate_media_ids": [c["media_id"] for c in CASE["clips"]],
                    # the resolver's near-miss for the warm-up: the real photo, never the bus
                    "suggested_media_ids": [WARMUP_PHOTO],
                }
                for i in asked
            ],
            media_kinds=KIND,
        )
        text = f'I couldn\'t tell which of your clips show "{asked[0].attribute}".'
        return IntentResolution(
            intents=[
                *out,
                *(
                    ResolvedClipIntent(
                        **i.model_dump(), status="needs_creator", assignments=[], question=text
                    )
                    for i in asked
                ),
            ],
            status="needs_creator",
            question=text,
            clip_question=question,
        )


async def _turn(
    monkeypatch,
    thread: list[tuple[str, Any]],
    *,
    matches: dict[str, list[str]] | None = None,
    unplaced=frozenset(),
):
    """One chat turn through the real `_plan_turn_clip_intents` (thread fold + planning)."""
    replay = _Replay(monkeypatch, matches=matches or _matches(), unplaced=unplaced)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=thread))
    latest = next(p for r, p in reversed(thread) if r == "user")["_content"]
    inputs = SimpleNamespace(
        intent_clips=_clips(),
        raw_creator_request=CASE["creator_request"],
        creator_request=CASE["creator_request"],
        creative_copy_state=None,
    )
    planned, _events = await planner._plan_turn_clip_intents(
        MagicMock(),
        thread_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        user_message=latest,
        inputs=inputs,
        brief_request=None,
        effective_brief=None,
        wants_capture_order=False,
        describe_clips=False,
        spoken_text=(),
        strategy=None,
    )
    return planned, replay


def _holders(resolution: IntentResolution) -> dict[str, list[str]]:
    """photo -> the ordered beats (positionless `order` intents) resolved onto it alone."""
    out: dict[str, list[str]] = {}
    for intent in resolution.intents:
        if intent.op == "order" and intent.position is None and intent.status == "resolved":
            members = {a.media_id for a in intent.assignments}
            if len(members) == 1:
                out.setdefault(next(iter(members)), []).append(intent.intent_id)
    return out


def _doubled(resolution: IntentResolution) -> dict[str, list[str]]:
    return {p: beats for p, beats in _holders(resolution).items() if len(beats) > 1}


def _rows(planned) -> dict[str, dict[str, Any]]:
    question = planned.resolution.clip_question or {}
    return {c["key"]: c for c in question.get("categories", [])}


def _asks_about_the_warmup(planned) -> bool:
    return planned.resolution.needs_creator and WARMUP_KEY in _rows(planned)


def _warm_up(planned) -> ResolvedClipIntent:
    return next(i for i in planned.resolution.intents if i.intent_id == "order_warm_up")


# ── R1: one photo is never silently the answer for two ordered beats ────────────


@pytest.mark.asyncio
async def test_R1_one_photo_on_two_ordered_beats_is_never_silent(monkeypatch) -> None:
    # even a resolver that reads the typed correction and STILL lands the warm-up on the bus
    planned, _ = await _turn(monkeypatch, _thread(), matches=_matches(order_warm_up=BUS))
    doubled = _doubled(planned.resolution)
    # acceptable outcomes: no photo holds two beats, OR the creator is asked
    assert not doubled or _asks_about_the_warmup(planned), f"silent duplicate: {doubled}"
    assert _asks_about_the_warmup(planned)
    assert BUS not in _rows(planned)[WARMUP_KEY]["candidate_media_ids"]


@pytest.mark.asyncio
async def test_R1b_a_tap_that_clashes_with_another_beat_is_asked_again_without_the_photo(
    monkeypatch,
) -> None:
    """t4 (14:25:21Z): the tap itself. Nobody has corrected it yet, so the creator is asked."""
    planned, replay = await _turn(monkeypatch, _thread(until_the_pick=True))
    resolution = planned.resolution
    assert resolution.needs_creator and resolution.status == "needs_creator"
    rows = _rows(planned)
    assert list(rows) == [WARMUP_KEY], "one row: the picked beat, not the resolver's"
    offered = rows[WARMUP_KEY]["candidate_media_ids"]
    assert BUS not in offered, "the contested photo is never offered again"
    assert WARMUP_PHOTO in offered and len(offered) == len(CASE["clips"]) - 1
    assert rows[WARMUP_KEY]["suggested_media_ids"] == [], "no resolver ever read this beat"
    # nothing is quietly applied meanwhile: the beat is open, the other beat keeps its match
    assert _warm_up(planned).status == "needs_creator" and not _warm_up(planned).assignments
    bus_beat = next(i for i in resolution.intents if i.intent_id == "order_nurnberg_bus")
    assert [a.media_id for a in bus_beat.assignments] == [BUS]
    assert not _doubled(resolution)
    # both beats are named, so the creator can tell why
    assert "the warm-up" in resolution.question and "team bus" in resolution.question
    assert planned.inventory is not None, "the tap reuses the checked list (KRI-950)"
    assert replay.planner_runs == 1, "no extra model call without a typed correction"


# ── R2: a typed correction newer than the pick wins over the pick ───────────────


@pytest.mark.asyncio
async def test_R2_typed_correction_after_the_pick_replaces_it(monkeypatch) -> None:
    planned, replay = await _turn(
        monkeypatch, _thread(), matches=_matches(order_warm_up=WARMUP_PHOTO)
    )
    warm = _warm_up(planned)
    kept_bus = [a.media_id for a in warm.assignments] == [BUS]
    assert not kept_bus or _asks_about_the_warmup(planned), "bus still the warm-up, nobody asked"
    # and the strong form: the resolver read the correction and found the real photo
    assert [a.media_id for a in warm.assignments] == [WARMUP_PHOTO]
    assert warm.assignments[0].evidence == "resolver"
    assert not planned.resolution.needs_creator and not _doubled(planned.resolution)
    last = replay.resolver.await_args_list[-1].kwargs
    assert "order_warm_up" in [i.intent_id for i in last["intents"]]
    assert "players on the pitch" in last["creator_request"], "it read the typed correction"


@pytest.mark.asyncio
async def test_R2b_correction_the_resolver_cannot_place_asks_again_without_the_stale_pre_tick(
    monkeypatch,
) -> None:
    planned, replay = await _turn(monkeypatch, _thread(), unplaced={"order_warm_up"})
    assert planned.resolution.needs_creator
    row = _rows(planned)[WARMUP_KEY]
    assert BUS not in row["suggested_media_ids"], "the stale tap is never pre-ticked"
    assert row["suggested_media_ids"] == [WARMUP_PHOTO]  # the resolver's own near-miss
    assert "order_warm_up" in [i.intent_id for i in replay.resolver.await_args.kwargs["intents"]]
    assert not _doubled(planned.resolution)


# ── R3 (control, must stay green): an uncontradicted pick still sticks ──────────


@pytest.mark.asyncio
async def test_R3_a_pick_nobody_contradicts_still_sticks_across_a_typed_message(
    monkeypatch,
) -> None:
    # the creator tapped the RIGHT photo (what the tester meant to do); the typed message still
    # says "the warm-up is the photo of the players on the pitch", which names the beat.
    planned, replay = await _turn(monkeypatch, _thread(warm_up_pick=WARMUP_PHOTO))
    warm = _warm_up(planned)
    assert [a.media_id for a in warm.assignments] == [WARMUP_PHOTO]
    assert warm.assignments[0].evidence == "creator selected"
    for call in replay.resolver.await_args_list:
        assert "order_warm_up" not in [i.intent_id for i in call.kwargs["intents"]]
    assert not planned.resolution.needs_creator
    assert replay.planner_runs == 1


# ── R4: a typed message that does not name the beat is no correction ────────────


@pytest.mark.asyncio
async def test_R4_a_later_message_that_does_not_name_the_beat_keeps_the_tap_and_asks(
    monkeypatch,
) -> None:
    thread = [
        *_thread(until_the_pick=True),
        tag_event("assistant", {}, "assistant_response", "I can't make this yet."),
        tag_event("user", {}, "user_message", "Make it 15 seconds."),
    ]
    planned, replay = await _turn(monkeypatch, thread)
    assert _asks_about_the_warmup(planned)
    assert BUS not in _rows(planned)[WARMUP_KEY]["candidate_media_ids"]
    for call in replay.resolver.await_args_list:
        assert "order_warm_up" not in [i.intent_id for i in call.kwargs["intents"]]
    assert replay.planner_runs == 1


# ── R5: two taps on one photo: the older tap is asked again ─────────────────────


@pytest.mark.asyncio
async def test_R5_two_taps_on_one_photo_ask_about_the_older_one(monkeypatch) -> None:
    selfie_row = {
        "op": "order",
        "key": "order:my-selfie-with-taha",
        "label": "my selfie with Taha",
        "intent_id": "order_selfie_taha",
        "candidate_media_ids": [c["media_id"] for c in CASE["clips"]],
        "suggested_media_ids": [],
    }
    thread = [
        *_thread(until_the_pick=True),
        tag_event(
            "assistant",
            {"clip_question": {"version": 1, "question_id": "q2", "categories": [selfie_row]}},
            "assistant_response",
            "I couldn't tell which of your clips show my selfie with Taha.",
        ),
        tag_event(
            "user",
            {
                "clip_selection": {
                    "question_id": "q2",
                    "answers": [{"key": selfie_row["key"], "media_ids": [BUS]}],
                    "none_keys": [],
                    "skipped": False,
                }
            },
            "user_message",
            "My selfie with Taha: clip 9.",
        ),
    ]
    planned, _ = await _turn(monkeypatch, thread)
    assert list(_rows(planned)) == [WARMUP_KEY], "the older tap (the warm-up) is asked again"
    assert BUS not in _rows(planned)[WARMUP_KEY]["candidate_media_ids"]
    selfie = next(i for i in planned.resolution.intents if i.intent_id == "order_selfie_taha")
    assert [a.media_id for a in selfie.assignments] == [BUS], "the newer answer stands"


# ── R6 / R7 (controls): not every shared clip is a clash ────────────────────────


@pytest.mark.asyncio
async def test_R6_a_video_on_two_beats_is_not_a_clash(monkeypatch) -> None:
    """One take can hold two moments (KRI-779/796): the goal video for the selfie beat too."""
    thread = _thread(warm_up_pick=WARMUP_PHOTO)
    planned, _ = await _turn(monkeypatch, thread, matches=_matches(order_selfie_taha=GOAL_VIDEO))
    holders = _holders(planned.resolution)
    assert sorted(holders[GOAL_VIDEO]) == ["order_first_goal", "order_selfie_taha"]
    assert not planned.resolution.needs_creator


@pytest.mark.asyncio
async def test_R7_a_group_that_contains_the_photo_is_not_a_single_photo_clash(monkeypatch) -> None:
    """The warm-up tap sits on the bus photo, and the bus beat is a group that contains it."""
    planned, _ = await _turn(
        monkeypatch,
        _thread(until_the_pick=True),
        matches=_matches(order_nurnberg_bus=[GATE, BUS]),
    )
    assert not planned.resolution.needs_creator
    bus = next(i for i in planned.resolution.intents if i.intent_id == "order_nurnberg_bus")
    assert [a.media_id for a in bus.assignments] == [GATE, BUS]
    assert [a.media_id for a in _warm_up(planned).assignments] == [BUS]
