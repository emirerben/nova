"""KRI-993: "only about the steak" must scope the montage, not just sit in the brief.

Prod thread 4C536AFE (Don Julio lunch M2, 2026-10-27): "10-second montage, only about the
steak. Skip the clip where he cuts the cheese ... Quick cuts." The cheese-cutting clip was
settled by a tap and the friend pinned last, and the plan still hooked on 3.8 s of the
provoleta boards (M2_04) while the one other steak clip (M2_03) went unused. The draft DID
carry an `include` for "the steak being cut" (resolved to M2_02 + the steak photo), but an
`include` only forces its clips in: nothing limits the edit to them. The brief's
`select` ask ("only about the steak") is only read for clip LENGTH (`apply_clip_scope`), and
the unified planner orders the picked clips by attachment, so the Main Creator's own pick
(boards, steak, friend; its summary calls the boards "the steak being served") stands.

Replay: the recorded clip records, brief, pick and resolved intents; only the model calls
are stood in (the Main Creator parse, the runtime-v2 planner, the dispatch selection and
`plan_unified_montage` are the real code). ``exclusive`` below is the proposed marker on a
resolved `include` ("only about X"); until the fix exists those tests fail on the boards.

Failure modes the fix must keep covered (each is a case below or in the planner/resolver
tests added with the fix):

1. Every clip is off-subject: nothing may be dropped (never the last own clip, never none).
2. "Only about X" matches nothing: no clip is dropped; the creator is asked which clips show X.
3. A look-alike (a clip whose record says "food on a board") is dropped on a guess: only a
   confident match keeps a clip, only an unmatched one is dropped when the resolver is sure.
4. A clip the creator pinned ("end on my friend smiling") or the strategy places stays,
   whatever the scope says; a creator exclusion still wins over a scope match.
5. The scope drags in a clip the creator excluded by tap.
6. Turkish phrasing ("sadece biftek") scopes the same way (the planner states it; nothing
   is read from words on the server).
7. The scope leaves too little footage for the asked length (length planning must say so,
   not pad with off-subject clips).
8. Flag off / persisted strategies from before the field load unchanged.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.pipeline.unified_montage import UnifiedClip, plan_unified_montage
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.tasks import content_plan_build

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASE: dict[str, Any] = json.loads(
    (FIXTURES / "kri993_donjulio_lunch_m2_steak_only.json").read_text()
)
# The KRI-623 cabin turn supplies the recorded Main Creator envelope (capabilities, hashes).
CABIN: dict[str, Any] = json.loads((FIXTURES / "kri623_tigre_m2_montage_turn.json").read_text())

ROOT = "users/creator/creation-threads/thread/"
BY_ROLE = {m["role"]: m["media_id"] for m in CASE["media"]}
FRIEND = BY_ROLE["friend_smiling"]  # 4.7 s, "waiter and customer"
BOARDS = BY_ROLE["serving_boards"]  # 9 s, M2_04, the provoleta boards: record says "food"
KNIFE_FORK = BY_ROLE["waiter_knife_fork"]  # 3 s, M2_03, steak waiter looks up
STEAK = BY_ROLE["steak_cutting"]  # 11 s, M2_02, record says "slicing steak"
CHEESE = BY_ROLE["cheese_cutting"]  # 12 s, the clip the creator tapped to skip
ALL = [m["media_id"] for m in CASE["media"]]
DURATIONS = {m["media_id"]: m["duration_s"] for m in CASE["media"]}
PROMPT = CASE["creator_request"].split("Creative brief")[0].strip()


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)


STEAK_PHOTO = "asset-cfb745e0-513a-4ade-b2c0-8b1b34e54585"  # the creator's steak photo


def _include_steak(*, exclusive: bool, vision_match: bool = False) -> ResolvedClipIntent:
    """The `include` the draft carried for "I only want the steak being cut" (recorded).

    Recorded resolution: M2_02 (0.9) and the steak photo (0.9); the boards and the
    knife-and-fork clip matched nothing. ``vision_match`` adds M2_03, as a vision check of
    "does this clip show steak?" would (not recorded: the resolver never asked).

    ``exclusive`` is the marker the fix adds to `ClipIntent` ("only about X": the clips the
    creator wants the edit limited to). On origin/main it is an unknown key that pydantic
    ignores, so the intent is a plain `include`: membership, never a scope.
    """

    rows = [
        {"media_id": STEAK, "evidence": "waiter slices steak on the left tray", "confidence": 0.9},
        {"media_id": STEAK_PHOTO, "evidence": "a grilled steak being sliced", "confidence": 0.9},
    ]
    if vision_match:
        rows.append({"media_id": KNIFE_FORK, "evidence": "vision: steak", "confidence": 0.8})
    return ResolvedClipIntent.model_validate(
        {
            "intent_id": "include_steak_cut",
            "op": "include",
            "attribute": "the steak being cut",
            **({"exclusive": True} if exclusive else {}),
            "status": "resolved",
            "assignments": rows,
        }
    )


def _skip_cheese() -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id="skip-cheese",
        op="exclude",
        attribute="the clip where he cuts the cheese",
        status="resolved",
        assignments=[ClipAssignment(media_id=CHEESE, evidence="creator tapped", confidence=1.0)],
    )


def _friend_last() -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id="end-friend",
        op="order",
        position="last",
        attribute="my friend smiling",
        status="resolved",
        assignments=[ClipAssignment(media_id=FRIEND, evidence="creator tapped", confidence=1.0)],
    )


def _agent_input() -> MainCreatorInput:
    data = copy.deepcopy(CABIN["main_creator_input"])
    manifest = data["capability_manifest"]
    manifest["media"] = [
        {k: v for k, v in m.items() if k != "role"} for m in copy.deepcopy(CASE["media"])
    ]
    data["user_message"] = PROMPT
    data["creator_request"] = PROMPT
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": PROMPT}]
    return MainCreatorInput.model_validate(data)


async def _drafted_strategy(monkeypatch: pytest.MonkeyPatch, resolved: list) -> dict[str, Any]:
    """One planning turn over the recorded pick; the strategy the draft would apply."""
    agent_input = _agent_input()
    strategy = {
        **copy.deepcopy(CABIN["model_strategy"]),
        "opening_title": None,
        "clip_intents": [],
        "target_duration_s": 10.0,
        "creator_duration_s": 10.0,
        "pacing": "fast",
        "media_scope": "selected",
        "selected_media_ids": list(CASE["model_selected"]),
    }
    raw = {
        "action": {"kind": "propose_strategy", "strategy": strategy, "summary": "A fast montage."},
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    requested = [
        ClipIntent.model_validate(i.model_dump(include=set(ClipIntent.model_fields)))
        for i in resolved
    ]
    monkeypatch.setattr(
        planner,
        "plan_and_resolve_clip_intents",
        AsyncMock(
            return_value=PlannedIntentResolution(requested, IntentResolution(intents=resolved))
        ),
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    clips = [
        IntentClip(
            media_id=m["media_id"], kind="video", analysis=CASE["clip_analysis"][m["media_id"]]
        )
        for m in CASE["media"]
    ]
    planned = await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(agent_input.capability_manifest.item_id),
        creator_id=uuid.uuid4(),
        user_message=PROMPT,
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=clips,
            creator_request=None,
            raw_creator_request=PROMPT,
        ),
        output=output,
        brief_selects_clips=True,
        clip_scope_texts=("only about the steak",),
    )
    assert planned.plan.mode == "act", planned.plan.response
    return planned.plan.intents[0].arguments["strategy"]


def _dispatched(strategy: dict[str, Any]) -> list[str]:
    """The REAL dispatch selection over the item's clips (attachment order)."""
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[{"media_id": m, "gcs_path": ROOT + m} for m in ALL],
    )
    paths = content_plan_build._creator_selected_clip_paths(
        item, [ROOT + m for m in ALL], strategy, without_proposal=True
    )
    return [path.removeprefix(ROOT) for path in paths]


def _clips(media_ids: list[str]) -> list[UnifiedClip]:
    return [
        UnifiedClip(
            media_id=media_id,
            proxy_path=ROOT + media_id,
            generation="1",
            duration_s=DURATIONS[media_id],
            width=1080,
            height=1920,
            analysis=CASE["clip_analysis"][media_id],
        )
        for media_id in media_ids
    ]


def _cuts(strategy: dict[str, Any]):
    clips = _clips(_dispatched(strategy))
    plan = plan_unified_montage(clips, None, strategy=strategy, clip_intents_enabled=True)
    return plan.cuts


def test_the_recorded_records_tell_steak_from_the_rest() -> None:
    """The evidence a subject scope reads exists: this is not a missing-record bug.

    M2_02 says steak outright. The boards clip says only "food" (the provoleta is never named)
    and the knife-and-fork clip "food on a metal tray": a text match cannot place those two,
    which is why the scope is the resolver's job (records first, a vision check for the rest),
    not a keyword test on the server.
    """

    def text(media_id: str) -> str:
        return json.dumps(CASE["clip_analysis"][media_id]["understanding"]).casefold()

    assert "steak" in text(STEAK)
    assert "steak" not in text(BOARDS)
    assert "steak" not in text(KNIFE_FORK)
    assert "toast" in text(CHEESE)


async def test_replay_reproduces_the_recorded_plan(prod_profile, monkeypatch) -> None:
    """With the intents exactly as recorded the real code makes the recorded edit.

    Hook = the boards, then the steak, then the pinned friend (the cut lengths come from the
    approved length plan, which this replay does not run). This is what makes the failing
    tests below a replay of the prod bug and not a staged one.
    """
    strategy = await _drafted_strategy(
        monkeypatch, [_include_steak(exclusive=False), _skip_cheese(), _friend_last()]
    )
    cuts = _cuts(strategy)

    assert [cut.media_id for cut in cuts] == [row["clip"] for row in CASE["recorded_plan"]]


async def test_only_about_the_steak_never_hooks_on_the_cheese_boards(
    prod_profile, monkeypatch
) -> None:
    """KRI-993, as recorded: the scope limits the edit to the steak (+ the pinned friend).

    Prod: hook = the boards (3.84 s of the provoleta being served), the steak second.
    """
    strategy = await _drafted_strategy(
        monkeypatch, [_include_steak(exclusive=True), _skip_cheese(), _friend_last()]
    )
    played = [cut.media_id for cut in _cuts(strategy)]

    assert BOARDS not in played, "the provoleta boards play in an 'only about the steak' edit"
    assert CHEESE not in played
    assert played[0] == STEAK, "the hook is not the steak clip"
    assert played[-1] == FRIEND, "the pinned closing clip moved"


async def test_a_steak_clip_the_pick_left_out_plays_when_the_scope_names_it(
    prod_profile, monkeypatch
) -> None:
    """KRI-993: M2_03 (steak waiter looks up) is steak footage and went unused.

    Not recorded: with a vision check the resolver's `include` would also carry M2_03.
    """
    strategy = await _drafted_strategy(
        monkeypatch,
        [_include_steak(exclusive=True, vision_match=True), _skip_cheese(), _friend_last()],
    )
    played = [cut.media_id for cut in _cuts(strategy)]

    assert set(played) == {STEAK, KNIFE_FORK, FRIEND}
    assert played[-1] == FRIEND
