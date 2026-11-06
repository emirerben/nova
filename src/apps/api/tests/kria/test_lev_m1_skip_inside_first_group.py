"""Leverkusen M1 run 2 #5: "starting with the train ride" hid the seat photo from its own skip.

Prod thread 1a2189cc (2026-10-30, turn 78855290, Fly eed794157). The prompt asked to put
"everything in the order I filmed it, starting with the train ride" and to "skip the photo of
the empty train seat". The skip matched no clip (KRI-1076), so Kria asked with the clip
picker (KRI-807), but the picker left out the seat photo IMG_1001, the train-display photo
IMG_0995 and the train video IMG_1002 (`kria_clip_exclusions_contested asks=1 pinned=0
unmatched=1`). The creator could only answer None, and the seat photo stayed in the edit.

``pinned_clips`` (KRI-761) pinned every clip of every first/last ask, written for one clip
("open on the tray clip"). "The train ride" matched three, and pinned clips are never
offered. The same rule hid the photo when the skip DID match it (the picker asked with
nothing to tap), and ``excluded_clips`` then kept it because the group "named" it.

Ways this could go wrong, written down before the code (each has a case below):

a. The incident: the picker for an unmatched skip leaves out the clips of a first/last ask
   that matched several clips, so the clip the skip means cannot be tapped.
b. The tapped seat photo still plays on the next turn, or a second question follows.
c. Leaving the seat photo out breaks the other ask: the train-display photo and the train
   video no longer open the edit, or the first/last ask is dropped.
d. KRI-1076 fixed (the resolver matches the seat photo): the skip asks anyway with the
   photo hidden, or keeps it because the train-ride group names it.
e. A first/last ask that matched ONE clip ("open on the tray clip", Merope café KRI-761)
   stops protecting it: a skip drops the opening clip, or the picker offers it.
f. A skip that names EVERY clip of a first/last group (a resolver over-match) empties the
   group silently: the opening ask has nothing left to open on.
g. Media the strategy plays (the train video's sound: montage audio) is offered or dropped.
h. A capture-time order or a look-alike skip behaves differently (KRI-761/1026 unchanged).

Runs the real clip-intent planner, resolver and runtime-v2 planner over the turn's footage;
only the model transport answers as recorded (``tests/fixtures/leverkusen_m1_run2_skip_
picker.json`` says which parts are recorded and which are reconstructed). No model is called.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._runtime import ModelInvocation
from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._skills import load_skill
from app.agents.clip_question import ClipQuestionAgent
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.clip_exclusions import exclusion_question, exclusions_to_ask, pinned_clips
from app.kria.clip_scope import excluded_clips
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_order_sequence import apply_sequence, sequence_rows
from app.services.creator_capabilities import resolve_creator_manifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASE: dict[str, Any] = json.loads((FIXTURES / "leverkusen_m1_run2_skip_picker.json").read_text())
# The KRI-623 cabin turn supplies the rest of a real Main Creator input; only its clips and
# words are swapped for this turn's (as in the KRI-807 replay).
CABIN: dict[str, Any] = json.loads((FIXTURES / "kri623_tigre_m2_montage_turn.json").read_text())
ID = {m["name"]: m["media_id"] for m in CASE["media"]}
ALL = [m["media_id"] for m in CASE["media"]]  # attachment order: the videos, then the Visuals
SEAT = ID[CASE["seat_photo"]]
DISPLAY = ID["IMG_0995"]  # the train display: the other photo of the train ride
TRAIN = ID[CASE["montage_audio"]]  # the train video, whose announcement is the kept sound
TRAIN_RIDE = [ID[name] for name in CASE["train_ride"]]
BOX = ID[CASE["one_view_photo"]]
REQUEST = CASE["creator_request"]
SKIP_ID = "exclude_empty_seat_photo"
NOT_LEFT_OUT = "I couldn't leave out"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    # CLIP_INTENTS_ENABLED is on in prod (CLAUDE.md); the exclude and picker flags default on.
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


@pytest.fixture
def no_vision(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        calls[0] += 1
        raise AssertionError("an exclusion never pays for a vision check")

    monkeypatch.setattr(ClipQuestionAgent, "run", _run)
    return calls


def _intent_clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=m["media_id"],
            kind=m["kind"],
            analysis={"understanding": {"summary": m["summary"]}},
            gcs_path=f"users/u/clips/{m['media_id']}",
            capture_time=datetime.fromisoformat(m["capture_time"]),
            file_name=m["name"],
            duration_s=m["duration_s"],
        )
        for m in CASE["media"]
    ]


_PLANNER_PROMPT = load_prompt("clip_intent_planner")[:60]
_RESOLVER_PROMPT = (
    load_skill("meaningful-moment") + "\n\n" + load_prompt("clip_request_resolver")[:60]
)


class _Provider:
    """The model provider, answering as it did on turn 78855290.

    Only the transport is a stand-in: the real clip-intent planner and resolver agents
    render their prompts, call it, and parse its raw text. ``seat_matched`` answers as the
    resolver would once KRI-1076 is fixed; ``skip`` overrides the skip's matches by name.
    """

    def __init__(
        self,
        *,
        seat_matched: bool = False,
        skip: tuple[str, ...] | None = None,
        train_ride: tuple[str, ...] | None = None,
    ) -> None:
        self.skip = skip if skip is not None else ((CASE["seat_photo"],) if seat_matched else ())
        self.train_ride = train_ride if train_ride is not None else tuple(CASE["train_ride"])
        self.calls: list[str] = []

    @staticmethod
    def _aliases(prompt: str) -> dict[str, str]:
        """Clip name -> the alias this prompt gave it (the resolver reads aliases only).

        Thirteen items are resolved in two shards (prod logged two resolver runs), so a
        prompt lists only some of them.
        """
        out: dict[str, str] = {}
        for alias, line in re.findall(r"^- (m\d{3}) \((?:video|image)\): (.*)$", prompt, re.M):
            [name] = [m["name"] for m in CASE["media"] if m["summary"][:60] in line]
            out[name] = alias
        return out

    def _matches(self, aliases: dict[str, str], names: tuple[str, ...]) -> list[dict]:
        return [
            {"media": aliases[n], "confidence": 0.95, "evidence": n} for n in names if n in aliases
        ]

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        if prompt.startswith(_PLANNER_PROMPT):
            self.calls.append("planner")
            return ModelInvocation(
                raw_text=json.dumps({"intents": CASE["planner_intents"], "question": None})
            )
        if prompt.startswith(_RESOLVER_PROMPT):
            self.calls.append("resolver")
            aliases = self._aliases(prompt)
            answers = {
                "order_train_first": self._matches(aliases, self.train_ride),
                SKIP_ID: self._matches(aliases, self.skip),
                "include_one_view_photo": self._matches(aliases, (CASE["one_view_photo"],)),
            }
            intents = [
                {"intent_id": intent_id, "assignments": matches, "needs_vision": []}
                for intent_id, matches in answers.items()
                if f"id={intent_id}" in prompt or intent_id in prompt
            ]
            return ModelInvocation(raw_text=json.dumps({"intents": intents}))
        self.calls.append("unexpected")
        raise AssertionError(f"unexpected model call: {prompt[:80]!r}")


def _provider(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> _Provider:
    provider = _Provider(**kwargs)
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    return provider


def _agent_input() -> MainCreatorInput:
    data = copy.deepcopy(CABIN["main_creator_input"])
    data["user_message"] = REQUEST
    data["creator_request"] = REQUEST
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": REQUEST}]
    data["capability_manifest"] = resolve_creator_manifest(
        item_id=str(uuid.uuid4()),
        edit_format="montage",
        media=[
            {"media_id": m["media_id"], "kind": m["kind"], "duration_s": m["duration_s"]}
            for m in CASE["media"]
        ],
        guided_capability_enabled=True,
    ).model_dump(mode="json")
    return MainCreatorInput.model_validate(data)


async def _turn(monkeypatch: pytest.MonkeyPatch, events: list | None = None) -> Any:
    """One planning turn; the Main Creator's answer is parsed, the provider is scripted."""
    agent_input = _agent_input()
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": {
                "edit_format": "montage",
                "media_scope": "all",
                "target_duration_s": 25,
                "opening_title": "Pokalnacht in Leverkusen 🔴⚫",
                "montage_audio": {"source_media_ids": [TRAIN], "preserve_source_audio": True},
                "rationale": "The night in filming order, the train announcement kept.",
            },
            "summary": "Your night at the BayArena in the order you filmed it.",
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events or []))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    manifest = agent_input.capability_manifest
    return await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(manifest.item_id),
        creator_id=uuid.uuid4(),
        user_message=REQUEST,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=_intent_clips(),
            creator_request=None,
            raw_creator_request=REQUEST,
        ),
        output=output,
        brief_selects_clips=planner._brief_selects_clips(None),
        clip_scope_texts=planner._brief_clip_scope_texts(None),
    )


def _strategy(planned: Any) -> dict[str, Any]:
    assert planned.plan.mode == "act", planned.plan.response
    return planned.plan.intents[0].arguments["strategy"]


def _played(planned: Any) -> list[str]:
    strategy = _strategy(planned)
    selected = strategy.get("selected_media_ids") or []
    keep = set(ALL) if strategy.get("media_scope") == "all" or not selected else set(selected)
    return [m for m in ALL if m in keep]


def _opening(planned: Any) -> list[str]:
    """The played items in filming order, seated by the creator's first/last asks."""
    when = {m["media_id"]: m["capture_time"] for m in CASE["media"]}
    filmed = sorted(_played(planned), key=when.__getitem__)
    rows = sequence_rows(_strategy(planned)["resolved_clip_intents"])
    return apply_sequence(filmed, rows)[:2]


def _answered(planned: Any, answer: dict[str, Any]) -> list:
    question = planned.plan.clip_question
    [category] = question["categories"]
    return [
        ("assistant", {"clip_question": question}),
        (
            "user",
            {
                "clip_selection": {
                    "question_id": question["question_id"],
                    "answers": [{"key": category["key"], **answer}],
                }
            },
        ),
    ]


# -- the turn as it ran (a, b, c, g) -------------------------------------------------------


async def test_the_seat_photo_skip_offers_the_seat_photo(
    prod_profile, monkeypatch, no_vision
) -> None:
    # a: prod offered 10 items, none of the train ride. The train video stays out (g): the
    # strategy plays its announcement, so no answer could leave it out.
    provider = _provider(monkeypatch)
    planned = await _turn(monkeypatch)
    assert provider.calls == ["planner", "resolver", "resolver"]  # two shards, as in prod
    assert (planned.plan.mode, planned.plan.turn_value) == ("respond", "question")
    [category] = planned.plan.clip_question["categories"]
    assert (category["op"], category["intent_id"]) == ("exclude", SKIP_ID)
    assert SEAT in category["candidate_media_ids"]
    assert category["candidate_media_ids"] == [m for m in ALL if m != TRAIN]
    assert set(category["candidate_media_ids"]) > set(CASE["prod_candidates"])
    assert category["suggested_media_ids"] == []  # unmatched: nothing guessed for the creator
    assert planned.plan.diagnostics["reason"] == "unmatched"
    assert no_vision == [0]


async def test_the_tapped_seat_photo_leaves_and_the_train_ride_still_opens(
    prod_profile, monkeypatch, no_vision
) -> None:
    # b, c: ask -> tap the seat photo -> the next turn drafts without it, no second ask, and
    # the rest of the train ride (display photo, then the train video) still opens the edit.
    _provider(monkeypatch)
    asked = await _turn(monkeypatch)
    [category] = asked.plan.clip_question["categories"]
    assert SEAT in category["candidate_media_ids"]  # the app only lets the creator tap these
    _provider(monkeypatch)
    planned = await _turn(monkeypatch, _answered(asked, {"media_ids": [SEAT]}))
    played = _played(planned)
    assert SEAT not in played
    assert set(played) == set(ALL) - {SEAT}
    assert _opening(planned) == [DISPLAY, TRAIN]
    assert not [n for n in planned.policy_notices if NOT_LEFT_OUT in n]


async def test_none_still_keeps_every_clip(prod_profile, monkeypatch, no_vision) -> None:
    # b: the creator's None answer is final (as on 18:37:25); nothing is asked again.
    _provider(monkeypatch)
    asked = await _turn(monkeypatch)
    _provider(monkeypatch)
    planned = await _turn(monkeypatch, _answered(asked, {"none": True}))
    assert _played(planned) == ALL


# -- the same skip once the resolver matches the seat photo (d) ---------------------------


async def test_a_matched_seat_photo_leaves_without_a_question(
    prod_profile, monkeypatch, no_vision
) -> None:
    # d: one confident match inside a group placement is the narrower ask: applied, said
    # nowhere as "couldn't leave out", and the train ride still opens on what is left.
    provider = _provider(monkeypatch, seat_matched=True)
    planned = await _turn(monkeypatch)
    assert provider.calls == ["planner", "resolver", "resolver"]  # two shards, as in prod
    played = _played(planned)
    assert SEAT not in played and DISPLAY in played and TRAIN in played
    assert _opening(planned) == [DISPLAY, TRAIN]
    assert not [n for n in planned.policy_notices if NOT_LEFT_OUT in n]


async def test_a_skip_that_names_the_whole_train_ride_keeps_it_and_says_so(
    prod_profile, monkeypatch, no_vision
) -> None:
    # f: the resolver matched the skip to both train photos (the video is the kept sound):
    # the opening ask would have nothing left, so the photos stay and the draft says so.
    _provider(monkeypatch, skip=("IMG_0995", "IMG_1001"), train_ride=("IMG_0995", "IMG_1001"))
    planned = await _turn(monkeypatch)
    played = _played(planned)
    assert DISPLAY in played and SEAT in played
    assert [n for n in planned.policy_notices if NOT_LEFT_OUT in n]


# -- the rule, one ask at a time (e, f, g, h) ----------------------------------------------


def _assign(*names: str, confidence: float = 0.95, evidence: str = "") -> list[ClipAssignment]:
    return [ClipAssignment(media_id=ID[n], confidence=confidence, evidence=evidence) for n in names]


def _first(*names: str, evidence: str = "") -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id="order_train_first",
        op="order",
        position="first",
        attribute="the train ride",
        status="resolved",
        assignments=_assign(*names, confidence=0.9, evidence=evidence),
    )


def _skip(*names: str, confidence: float = 0.95, evidence: str = "") -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id=SKIP_ID,
        op="exclude",
        attribute="the photo of the empty train seat",
        status="resolved",
        assignments=_assign(*names, confidence=confidence, evidence=evidence),
    )


FILMED = ResolvedClipIntent(
    intent_id="order_all_filmed",
    op="order",
    order_by="capture_time",
    attribute="everything",
    status="resolved",
    assignments=[ClipAssignment(media_id=m, confidence=1.0, evidence="filming order") for m in ALL],
)
MONTAGE = CreativeStrategy.model_validate({"edit_format": "montage", "media_scope": "all"})
AUDIO = CreativeStrategy.model_validate(
    {
        "edit_format": "montage",
        "media_scope": "all",
        "montage_audio": {"source_media_ids": [TRAIN], "preserve_source_audio": True},
    }
)


def _offered(intents: list[ResolvedClipIntent], strategy: CreativeStrategy) -> list[str]:
    asked = exclusions_to_ask(intents, strategy, suspects={}, clip_count=len(ALL))
    found = exclusion_question(asked, _intent_clips())
    assert found is not None
    [category] = found[1]["categories"]
    return category["candidate_media_ids"]


def test_a_first_group_pins_none_of_its_clips() -> None:
    # a, g: only the strategy's own sound source stays out of the picker.
    intents = [FILMED, _first(*CASE["train_ride"]), _skip()]
    assert pinned_clips(intents, AUDIO) == frozenset({TRAIN})
    assert _offered(intents, AUDIO) == [m for m in ALL if m != TRAIN]


def test_a_one_clip_first_ask_is_still_a_pin() -> None:
    # e: "open on the train display" (KRI-761's tray clip): never offered, never dropped by a
    # resolver match, and a skip that matched it asks.
    intents = [_first("IMG_0995"), _skip("IMG_0995")]
    assert pinned_clips(intents, MONTAGE) == frozenset({DISPLAY})
    [asked] = exclusions_to_ask(intents, MONTAGE, suspects={}, clip_count=len(ALL))
    assert asked.blocked == (DISPLAY,)
    assert DISPLAY not in _offered(intents, MONTAGE)
    assert excluded_clips(intents, MONTAGE) == set()


def test_a_skip_inside_a_first_group_leaves_only_what_it_names() -> None:
    # d: the matched seat photo leaves; the rest of the group, and the sound source, stay.
    intents = [FILMED, _first(*CASE["train_ride"]), _skip("IMG_1001")]
    assert exclusions_to_ask(intents, AUDIO, suspects={}, clip_count=len(ALL)) == []
    assert excluded_clips(intents, AUDIO) == {SEAT}
    # g: a skip that names the sound source never drops it, inside a group or not.
    assert excluded_clips([_first(*CASE["train_ride"]), _skip("IMG_1002")], AUDIO) == set()


def test_a_skip_that_would_empty_the_group_keeps_it() -> None:
    # f: the group keeps every clip a skip would take whole; the creator's tap still wins.
    group = _first("IMG_0995", "IMG_1001")
    assert excluded_clips([group, _skip("IMG_0995", "IMG_1001")], MONTAGE) == set()
    tapped = _skip("IMG_0995", "IMG_1001", confidence=1.0, evidence="creator selected")
    assert excluded_clips([group, tapped], MONTAGE) == {DISPLAY, SEAT}


def test_look_alikes_and_filming_orders_are_unchanged() -> None:
    # h: two borderline matches still ask (now with both offered and pre-ticked); a
    # capture-time order alone never pins or keeps a clip.
    intents = [_first(*CASE["train_ride"]), _skip("IMG_0995", "IMG_1001", confidence=0.8)]
    [asked] = exclusions_to_ask(intents, AUDIO, suspects={}, clip_count=len(ALL))
    assert asked.blocked == () and asked.suggested == [DISPLAY, SEAT]
    assert {DISPLAY, SEAT} <= set(_offered(intents, AUDIO))
    assert pinned_clips([FILMED], MONTAGE) == frozenset()
    assert excluded_clips([FILMED, _skip("IMG_1001")], MONTAGE) == {SEAT}
